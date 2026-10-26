"""Challenge B1 - Compaction.

A long multi-turn debugging session (huge test logs, big files) is compacted mid-task with the
pipeline from slide 26: observe token budget -> prune tool output -> structured checkpoint ->
reinject rules + checkpoint -> resume. After compaction the agent must still know the bug,
the failed attempt, the constraint, and the next step - and then actually finish the fix.

Run:  python b1_compact.py          (COMPACT_AT=20000 env var changes the threshold)
"""
import json
import os
import random
import re
import shutil

from llm import HERE, USAGE_LOG, chat, est_tokens, run_agent, usage_table

WS = os.path.join(HERE, "b1_workspace")
ARCHIVE = os.path.join(HERE, "b1_archive")
# Real windows are far bigger; kept low so the demo triggers compaction twice.
THRESHOLD = int(os.getenv("COMPACT_AT", 20000))
SIGNATURE = "export async function refreshToken(userId: string, oldToken: string): Promise<string>"

SYSTEM = """You are a coding agent in a TypeScript + PostgreSQL repo. Tools: list_files, read_file, edit_file, run_tests.
The repo has exactly: session.ts, auth.ts, auth.test.ts, db.ts (there are no other files).
edit_file needs the exact current text: read_file first. Do ONLY what the user asks in the current turn.
Answer in Vietnamese, concisely."""

TURNS = [
    "Dự án dùng TypeScript + PostgreSQL. Ràng buộc bắt buộc: giữ API backward compatible "
    "(không đổi chữ ký các hàm export). Hãy chạy test suite, cho tôi biết test nào fail và nguyên nhân khả dĩ. Chưa sửa gì.",
    "Tôi nghi token hết hạn quá sớm. Thử tăng TOKEN_TTL trong session.ts từ 900 lên 3600 rồi chạy lại test, báo kết quả.",
    "Nhắc lại ngắn gọn: (1) đang điều tra lỗi gì, (2) đã thử cách nào và kết quả, "
    "(3) ràng buộc bắt buộc là gì, (4) bước tiếp theo là gì?",
    "Tiếp tục task: sửa lỗi cho đúng nguyên nhân, rồi chạy test để xác nhận tất cả pass.",
]

# ---------------------------------------------------------------- fake project + tools
SESSION_TS = f"""// session.ts - refresh-token rotation backed by PostgreSQL
import {{ db }} from "./db";

export const TOKEN_TTL = 900; // seconds

{SIGNATURE} {{
  const row = await db.one("SELECT token FROM refresh_tokens WHERE user_id = $1 AND token = $2 AND revoked = false", [userId, oldToken]);
  if (!row) throw new Error("invalid refresh token");
  const next = crypto.randomUUID();
  await db.none("INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, now() + $3 * interval '1 second')", [userId, next, TOKEN_TTL]);
  await db.none("UPDATE refresh_tokens SET revoked = true WHERE token = $1", [oldToken]);
  return next;
}}
""" + "".join(f"""
export function formatSessionField{i}(value: string): string {{
  // normalizes session metadata field {i} before persisting to PostgreSQL
  const trimmed = value.trim();
  if (trimmed.length === 0) return "";
  return trimmed.slice(0, {64 + i % 32});
}}
""" for i in range(80))

AUTH_TS = """// auth.ts - HTTP handlers
import { refreshToken } from "./session";

export async function handleRefresh(req, res) {
  const { userId, refreshToken: token } = req.body;
  // mobile clients fire several refresh calls in parallel when the app resumes
  const next = await refreshToken(userId, token);
  res.json({ refreshToken: next });
}
""" + "".join(f"""
export async function handleRoute{i}(req, res) {{
  res.json({{ route: {i}, ok: true }});
}}
""" for i in range(60))

DB_TS = """// db.ts - thin PostgreSQL client (pg-promise style). `pool` is injected by the runtime; nothing else to read.
declare const pool: any;
export const db = {
  one: (sql: string, params?: unknown[]) => pool.one(sql, params),       // 1 row or null
  none: (sql: string, params?: unknown[]) => pool.none(sql, params),     // no result
  result: (sql: string, params?: unknown[]) => pool.result(sql, params), // { rowCount, rows }
  tx: <T>(fn: (t: typeof db) => Promise<T>) => pool.tx(fn),               // BEGIN ... COMMIT
};
"""

AUTH_TEST_TS = """// auth.test.ts (excerpt)
test("concurrent refresh issues exactly one new token", async () => { /* 2 parallel refreshes, same token */ });
test("old token rejected after rotation", async () => { /* reuse old token -> expect 401 */ });
"""

FAILS = [
    ["FAIL auth.test.ts > refresh > concurrent refresh issues exactly one new token",
     "  AssertionError: expected 1 active refresh token for user u_42, got 2",
     "    at auth.test.ts:88:31",
     "  note: 2 parallel POST /auth/refresh with the same token, TOKEN_TTL={ttl}"],
    ["FAIL auth.test.ts > refresh > old token rejected after rotation",
     "  AssertionError: expected 401 for reused refresh token, got 200",
     "    at auth.test.ts:112:9"],
    ["FAIL session.test.ts > rotation > concurrent rotation keeps single session row",
     "  AssertionError: expected 1 row in refresh_tokens(user_id=u_42, revoked=false), got 2",
     "    at session.test.ts:57:14"],
]
COMPAT_FAIL = ["FAIL api.compat.test.ts > refreshToken(userId, oldToken) signature unchanged",
               "  AssertionError: exported signature of refreshToken changed (breaks backward compatibility)",
               "    at api.compat.test.ts:12:5"]
SUITES = ["auth", "session", "users", "orders", "billing", "search", "profile", "admin", "notify", "audit"]
EDITS = []        # (compactions_so_far, old, new) - to detect a repeated failed attempt
# Harness guard on what each turn may edit: "none" (turns 1, 3), "ttl" (turn 2: only the TOKEN_TTL line), "any".
# Weaker models (nemotron) fixed the bug in turn 1 despite "don't fix yet", and in turn 2 followed the
# checkpoint's next_action instead of the user's request - both broke the scenario.
EDIT_SCOPE = ["any"]
TTL_LINE = re.compile(r"[^\n]*TOKEN_TTL\s*=\s*\d+[^\n]*")
TURN_SCOPE = {1: "none", 2: "ttl", 3: "none", 4: "any"}
COMPACTIONS = []  # (est_before, est_after)


def _path(name):
    return os.path.join(WS, os.path.basename(name))  # flat workspace; basename blocks ../ escapes


def list_files():
    return "\n".join(f"{n} ({sum(1 for _ in open(_path(n), encoding='utf-8'))} lines)" for n in sorted(os.listdir(WS)))


def read_file(path):
    if not os.path.exists(_path(path)):
        return f"ERROR: {path} does not exist. The repo has ONLY these files: {', '.join(sorted(os.listdir(WS)))}"
    with open(_path(path), encoding="utf-8") as f:
        return f.read()


def edit_file(path, old, new):
    if EDIT_SCOPE[0] == "none":
        return "ERROR: the user said not to change anything in this turn. Only report findings."
    if EDIT_SCOPE[0] == "ttl" and not (TTL_LINE.fullmatch(old.strip()) and TTL_LINE.fullmatch(new.strip())):
        return ("ERROR: this turn only asks to change the TOKEN_TTL line. Do not change other code; "
                "report the test result to the user.")
    src = read_file(path)
    if src.count(old) != 1:
        return f"ERROR: `old` must occur exactly once in {path} (found {src.count(old)}). read_file and copy the exact text."
    with open(_path(path), "w", encoding="utf-8") as f:
        f.write(src.replace(old, new))
    EDITS.append((len(COMPACTIONS), old, new))
    return f"OK: edited {path}"


def run_tests():
    src = read_file("session.ts")
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)  # a comment does not fix a race condition
    # accepted fixes: compare-and-swap revoke (conditional UPDATE + rowCount/RETURNING check),
    # row/advisory lock, or in-process mutex/in-flight map
    cas = (re.search(r"set\s+revoked\s*=\s*true[^\"'`]*and\s+revoked\s*=\s*false", code, re.I)
           and re.search(r"rowCount|returning", code, re.I))
    fixed = cas or re.search(r"for\s+update|pg_advisory\w*lock|mutex|in.?flight|single.?flight", code, re.I)
    fails = [] if fixed else [[l.format(ttl=ttl(src) or "?") for l in b] for b in FAILS]
    if SIGNATURE not in src:
        fails.append(COMPAT_FAIL)
    rng, lines, slots = random.Random(7), [], {120: 0, 260: 1, 377: 2, 430: 3}
    for i in range(480):
        suite = SUITES[i % len(SUITES)]
        lines.append(f"PASS {suite}.test.ts > case_{i:03d} ({rng.randint(1, 40)}ms)")
        lines.append(f"  [db] pool conn={rng.randint(1, 20)} query={rng.randint(1, 9)}ms rows={rng.randint(0, 50)}")
        if i % 3 == 0:
            lines.append(f"  [http] GET /v1/{suite}/{rng.randint(1, 999)} 200 {rng.randint(2, 90)}ms")
        if i in slots and slots[i] < len(fails):
            lines += fails[slots[i]]
    lines.append(f"Tests: {len(fails)} failed, 480 passed, {480 + len(fails)} total")
    return "\n".join(lines)


TOOLS = [{"type": "function", "function": f} for f in [
    {"name": "list_files", "description": "List files in the repo", "parameters": {"type": "object", "properties": {}}},
    {"name": "read_file", "description": "Read a whole file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "edit_file", "description": "Replace exact text `old` (must occur once) with `new`",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}},
    {"name": "run_tests", "description": "Run the full test suite (verbose output)", "parameters": {"type": "object", "properties": {}}},
]]
IMPLS = {"list_files": list_files, "read_file": read_file, "edit_file": edit_file, "run_tests": run_tests}

# ---------------------------------------------------------------- compaction pipeline (slide 26)
KEY_LINE = re.compile(r"^(FAIL|\s+(AssertionError|at |note:)|Tests:)")
CHECKPOINT_PROMPT = """You compact a coding agent's working context. The old messages will be DELETED; the agent continues
only from your checkpoint. Return ONLY a JSON object with keys:
goal (the overall task across all turns), current_state, constraints (list: standing project rules the user set, verbatim;
NOT one-turn instructions such as "don't fix yet"), findings (list), failed_attempts (list: what was tried + result),
files (list), next_action (the next concrete technical step toward fixing the bug - never a one-turn
instruction such as "report results" or "wait for the user").
Be specific: test names, values, file names. Do not invent facts."""
CHECKPOINT_KEYS = ("goal", "current_state", "constraints", "findings", "failed_attempts", "files", "next_action")
ARCHIVED = [0]  # raw tool outputs written to b1_archive/ (checkpoints are numbered separately)


def prune(messages, current_turn):
    """TRUNCATE tool outputs to key lines (raw archived on disk). Files read in the current turn are KEPT
    so the agent can still edit them; only test logs are cut there."""
    for i, m in enumerate(messages):
        if m["role"] == "tool" and len(m["content"]) > 2000 and (i < current_turn or m["name"] == "run_tests"):
            ARCHIVED[0] += 1
            path = os.path.join(ARCHIVE, f"{ARCHIVED[0]:03d}_{m['name']}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(m["content"])
            raw = m["content"].splitlines()
            key = [l for l in raw if KEY_LINE.match(l)] if m["name"] == "run_tests" else raw[:15]
            m["content"] = f"[pruned {len(raw)} lines to key lines; re-run the tool for full output]\n" + "\n".join(key)
            print(f"   >>> pruned {m['name']}: {len(raw):,} lines -> {len(key)} (raw saved to {os.path.relpath(path, HERE)})")


def render(messages):
    out = []
    for m in messages:
        for c in m.get("tool_calls", []):
            out.append(f"[assistant -> {c['function']['name']}] {c['function'].get('arguments', '')[:400]}")
        if m.get("content"):
            out.append(f"[{m['role']}{'/' + m['name'] if m.get('name') else ''}] {m['content']}")
    return "\n".join(out)


def maybe_compact(messages, force=False):
    before = est_tokens(messages)
    if before < THRESHOLD and not force:
        return
    print(f"\n   >>> COMPACT{' (forced)' if force else ''}: context ~{before:,} tokens, threshold {THRESHOLD:,}")
    turn = max(i for i, m in enumerate(messages) if m["role"] == "user")
    prune(messages, turn)                                                 # 1. prune / truncate
    for _ in range(3):                                                    # 2. structured checkpoint
        txt = chat([{"role": "system", "content": CHECKPOINT_PROMPT},
                    # framed as data: weaker models otherwise continue the transcript as the agent
                    {"role": "user", "content": "TRANSCRIPT TO COMPACT (do not continue it, do not call tools):\n<<<\n"
                     + render(messages) + "\n>>>\nNow return ONLY the JSON checkpoint object."}],
                   label="checkpoint").get("content") or ""
        try:
            cp = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
            if all(k in cp for k in CHECKPOINT_KEYS) and cp["goal"] and cp["next_action"]:
                break
        except ValueError:
            pass
        print(f"   >>> checkpoint invalid ({txt[:80]!r}), retrying")
    else:
        # dropping history without a valid checkpoint would lose the task state: keep the pruned messages
        print(f"   >>> no valid checkpoint; kept pruned history (~{est_tokens(messages):,} tokens)\n")
        return
    # 3. KEEP current turn (pruned), DROP older turns, reinject rules + checkpoint
    tail = messages[turn:]
    messages[:] = [{"role": "system", "content": SYSTEM + "\n\n## CHECKPOINT (older messages were compacted away)\n"
                    + json.dumps(cp, ensure_ascii=False, indent=2)}] + tail
    after = est_tokens(messages)
    COMPACTIONS.append((before, after))
    with open(os.path.join(ARCHIVE, f"checkpoint_{len(COMPACTIONS)}.json"), "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False, indent=2)
    print(f"   >>> checkpoint injected into context:\n{json.dumps(cp, ensure_ascii=False, indent=2)}")
    print(f"   >>> context ~{before:,} -> ~{after:,} tokens; kept {len(tail)} messages of the current turn\n")


def ttl(src):
    m = re.search(r"TOKEN_TTL\s*=\s*(\d+)", src)
    return int(m[1]) if m else None


def setup():
    ARCHIVED[0] = 0
    for d in (WS, ARCHIVE):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)  # OneDrive may keep the folder locked
    for name, src in {"session.ts": SESSION_TS, "auth.ts": AUTH_TS, "auth.test.ts": AUTH_TEST_TS, "db.ts": DB_TS}.items():
        with open(_path(name), "w", encoding="utf-8") as f:
            f.write(src)


def main():
    setup()
    messages, answers = [{"role": "system", "content": SYSTEM}], []
    for i, text in enumerate(TURNS, 1):
        print(f"\n{'=' * 90}\nUSER turn {i}: {text}")
        messages.append({"role": "user", "content": text})
        EDIT_SCOPE[0] = TURN_SCOPE[i]
        if i == 3:
            # compact ALL of turn 2 away: the recall question must be answered from the checkpoint alone
            maybe_compact(messages, force=True)
            compactions_before_recall = len(COMPACTIONS)
            recall_context = [m["role"] for m in messages]
        answers.append(run_agent(messages, TOOLS, IMPLS, max_steps=10, before_call=maybe_compact, label=f"turn{i}"))
        print(f"--- AGENT: {answers[-1]}")

    recall = answers[2].lower()
    next_step = recall[max(recall.rfind("bước tiếp"), recall.rfind("(4)"), recall.rfind("4."), 0):]  # only part (4)
    final = run_tests().splitlines()[-1]
    checks = [
        ("recall turn sees only system+checkpoint and the question",
         compactions_before_recall >= 1 and recall_context == ["system", "user"]),
        ("recall: bug = concurrent refresh / race", any(k in recall for k in ("concurrent", "race", "đồng thời", "song song", "lock", "khóa"))),
        ("recall: failed attempt = TOKEN_TTL increase", "ttl" in recall),
        ("recall: constraint = backward compatible", any(k in recall for k in ("backward", "tương thích", "chữ ký", "signature"))),
        ("recall: next step = lock / transaction / atomic revoke",
         bool(re.search(r"lock|khóa|mutex|transaction|giao dịch|\btx\b|atomic|nguyên tử|for update|returning", next_step))),
        ("did not repeat the TTL attempt (only 1 edit raising TOKEN_TTL)",
         sum((ttl(new) or 0) > 900 and ttl(new) != ttl(old) for _, old, new in EDITS) <= 1),
        (f"task finished: {final}", final.startswith("Tests: 0 failed")),
    ]
    with open(os.path.join(ARCHIVE, "edits.json"), "w", encoding="utf-8") as f:
        json.dump([{"compactions_before": n, "old": o, "new": w} for n, o, w in EDITS], f, ensure_ascii=False, indent=2)
    print(f"\n{'=' * 90}\nRESULTS")
    usage_table(USAGE_LOG)
    for b, a in COMPACTIONS:
        print(f"   compaction: ~{b:,} -> ~{a:,} tokens")
    for name, ok in checks:
        print(f"   [{'PASS' if ok else 'FAIL'}] {name}")


if __name__ == "__main__":
    main()
