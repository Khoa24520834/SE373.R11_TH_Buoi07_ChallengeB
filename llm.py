"""Minimal OpenRouter client + native tool-calling agent loop, shared by the B1/B2 demos."""
import json
import os
import re
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # live output even when piped to tee
HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://openrouter.ai/api/v1/chat/completions"
# Models with tool calling, tried in order (comma separated). minimax-m3.1-flash:free was retired on
# 2026-10-22; the paid `minimax/minimax-m3.1-flash` still works if MODEL is set to it.
MODELS = os.getenv("MODEL", "nvidia/nemotron-3.5-lightning:free").split(",")
USAGE_LOG = []  # one record per LLM request: label, model, prompt/completion tokens
DEAD = set()    # models the API reported as retired/unknown: skipped for the rest of the run
GONE = re.compile(r"retired|no endpoints found|not a valid model|model not found", re.I)


def _key():
    if os.getenv("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("Missing OPENROUTER_API_KEY (env var or .env)")


CALIB = [1.0]  # real prompt_tokens / chars-4 estimate, from the previous request


def _chars4(messages):
    return len(json.dumps(messages, ensure_ascii=False)) // 4


def est_tokens(messages):
    # chars/4 undercounts logs (digits, symbols) and ignores tool schemas: scale it by the ratio
    # real usage.prompt_tokens / estimate observed on the previous request
    return int(_chars4(messages) * CALIB[0])


def chat(messages, tools=None, label="", tool_choice=None):
    err = None
    for attempt in range(6):
        alive = [m for m in MODELS if m not in DEAD]
        if not alive:
            raise RuntimeError(f"No usable model left (all retired/unknown): {MODELS}. Set MODEL=...")
        model = alive[min(attempt // 2, len(alive) - 1)]  # 2 tries per model, then fall back
        body = {"model": model, "messages": messages, "temperature": 0.2}
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        try:
            data = requests.post(URL, headers={"Authorization": f"Bearer {_key()}"}, json=body, timeout=300).json()
        except (requests.RequestException, ValueError) as e:
            data, err = {}, e
        if data.get("choices"):
            u = data.get("usage") or {}
            rec = {"label": label, "model": model, "prompt": u.get("prompt_tokens", 0),
                   "completion": u.get("completion_tokens", 0),
                   "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)}
            USAGE_LOG.append(rec)
            if rec["prompt"] and tools:  # calibrate on agent requests (same shape as the budget check)
                CALIB[0] = min(3.0, max(0.5, rec["prompt"] / max(_chars4(messages), 1)))
            print(f"   [llm] {label:<11} {model.split('/')[-1]:<28} input={rec['prompt']:>7,}  output={rec['completion']:>5,}")
            return data["choices"][0]["message"]
        err = data.get("error", err)
        if isinstance(err, dict) and "per-day" in str(err.get("message", "")):
            reset = ((err.get("metadata") or {}).get("headers") or {}).get("X-RateLimit-Reset")
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(reset) / 1000)) if reset else "tomorrow"
            raise RuntimeError(f"Daily free-model quota used up (resets {when}); retrying now cannot help")
        if isinstance(err, dict) and GONE.search(str(err.get("message", ""))):
            DEAD.add(model)  # retrying a retired model only wastes time: drop it at once, no sleep
            print(f"   [llm] {model} unavailable, dropped for this run: {str(err.get('message'))[:120]}")
            continue
        print(f"   [llm] retry {attempt + 1}: {str(err)[:160]}")
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"LLM failed after retries: {err}")


def run_agent(messages, tools, impls, max_steps=10, before_call=None, label="agent"):
    """Native tool-calling loop. Mutates `messages`; returns the final assistant text.
    `before_call(messages)` runs before every LLM request (the compaction hook in B1).
    The last step disables tools so the agent must answer with what it already has."""
    for step in range(max_steps):
        if before_call:
            before_call(messages)
        msg = chat(messages, tools, label, tool_choice="none" if step == max_steps - 1 else None)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
        if not calls:
            return msg.get("content") or ""
        for c in calls:
            name = c["function"]["name"]
            args = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(args)
                out = str(impls[name](**args))
            except Exception as e:  # bad args / tool error goes back to the model, not a crash
                out = f"ERROR: {type(e).__name__}: {e}"
            shown = json.dumps(args, ensure_ascii=False).replace("\\n", " ")
            print(f"   [tool] {name}({shown[:90]}{'...' if len(shown) > 90 else ''}) -> {len(out):,} chars")
            if len(out) <= 400:
                print("          " + out.strip().replace("\n", "\n          "))
            messages.append({"role": "tool", "tool_call_id": c["id"], "name": name, "content": out})
    return "(max_steps reached)"


def usage_table(records):
    print(f"   {'#':>2}  {'label':<11} {'input tokens':>12} {'output':>7}")
    for i, r in enumerate(records, 1):
        print(f"   {i:>2}  {r['label']:<11} {r['prompt']:>12,} {r['completion']:>7,}")
    print(f"   total input = {sum(r['prompt'] for r in records):,} | peak input = {max((r['prompt'] for r in records), default=0):,}")
