"""Offline checks for the demo logic (no API calls). Run: python test_offline.py"""
import os
import tempfile

import b1_compact as b1
import b2_codemode as b2

# keep the real run's b1_workspace/ and b1_archive/ (checkpoints) intact
TMP = tempfile.mkdtemp()
b1.WS, b1.ARCHIVE = os.path.join(TMP, "ws"), os.path.join(TMP, "archive")

# B1: fake test suite reacts to the right fix only
b1.setup()
out = b1.run_tests()
assert out.endswith("Tests: 3 failed, 480 passed, 483 total"), out[-80:]
b1.edit_file("session.ts", "TOKEN_TTL = 900", "TOKEN_TTL = 3600")
assert "3 failed" in b1.run_tests().splitlines()[-1]                      # TTL is not the fix
b1.setup()
b1.edit_file("session.ts", "WHERE token = $1\"", "WHERE token = $1 AND revoked = false RETURNING token\"")
assert b1.run_tests().splitlines()[-1].startswith("Tests: 0 failed")       # atomic conditional revoke is a fix
b1.setup()
b1.edit_file("session.ts", "  const row", "  await db.none('SELECT pg_advisory_xact_lock(1)');\n  const row")
assert b1.run_tests().splitlines()[-1].startswith("Tests: 0 failed")       # locking is
b1.edit_file("session.ts", "oldToken: string)", "token: string)")
assert "1 failed" in b1.run_tests().splitlines()[-1]                      # API break is caught
assert b1.ttl("export const TOKEN_TTL = 3600;") == 3600 and b1.ttl("no ttl") is None
b1.setup()
b1.EDIT_SCOPE[0] = "none"
assert b1.edit_file("session.ts", "TOKEN_TTL = 900", "TOKEN_TTL = 1").startswith("ERROR")   # turn 1 is read-only
b1.EDIT_SCOPE[0] = "ttl"                                                                   # turn 2: TTL line only
assert b1.edit_file("session.ts", "  const row", "  await lock();\n  const row").startswith("ERROR")
assert b1.edit_file("session.ts", "export const TOKEN_TTL = 900; // seconds", "export const TOKEN_TTL = 3600; // seconds") == "OK: edited session.ts"
b1.EDIT_SCOPE[0] = "any"
b1.setup()
b1.edit_file("session.ts", "// seconds", "// seconds  TODO lock /* mutex */")
assert "3 failed" in b1.run_tests().splitlines()[-1]                      # a comment is not a fix
# the agent's real fix from run 5: compare-and-swap revoke in db.tx, with and without its comment
OLD_BODY = b1.SESSION_TS[b1.SESSION_TS.index("  const row"): b1.SESSION_TS.index("  return next;")]
CAS = """  const next = crypto.randomUUID();
  await db.tx(async (t) => {
    // Concurrent callers serialize on the row lock; the loser sees revoked = true -> rowCount 0.
    const res = await t.result("UPDATE refresh_tokens SET revoked = true WHERE user_id = $1 AND token = $2 AND revoked = false", [userId, oldToken]);
    if (!res || res.rowCount !== 1) throw new Error("invalid refresh token");
    await t.none("INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, now() + $3 * interval '1 second')", [userId, next, TOKEN_TTL]);
  });
"""
for body in (CAS, "\n".join(l for l in CAS.splitlines() if "//" not in l) + "\n"):
    b1.setup()
    b1.edit_file("session.ts", OLD_BODY, body)
    assert b1.run_tests().splitlines()[-1].startswith("Tests: 0 failed"), body

# B1: maybe_compact (chat mocked) - forced compact keeps only system+checkpoint and the current turn
CP = {k: "x" for k in b1.CHECKPOINT_KEYS}
real_chat = b1.chat
b1.chat = lambda *a, **k: {"content": "ok " + b1.json.dumps(CP)}
msgs = [{"role": "system", "content": b1.SYSTEM}, {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "a1"}, {"role": "user", "content": "turn 2"}]
b1.maybe_compact(msgs)                                       # below threshold: untouched
assert len(msgs) == 4
b1.maybe_compact(msgs, force=True)
assert [m["role"] for m in msgs] == ["system", "user"] and "CHECKPOINT" in msgs[0]["content"]
assert msgs[1]["content"] == "turn 2"
b1.chat = lambda *a, **k: {"content": b1.json.dumps({"goal": "g", "next_action": "n"})}  # missing 5 keys
msgs = [{"role": "system", "content": b1.SYSTEM}, {"role": "user", "content": "t1"}, {"role": "user", "content": "t2"}]
b1.maybe_compact(msgs, force=True)
assert len(msgs) == 3                                        # invalid checkpoint: history kept
b1.chat = real_chat

# B1: prune keeps every FAIL line and archives the raw log
b1.setup()
msgs = [{"role": "tool", "name": "run_tests", "content": b1.run_tests()},
        {"role": "user", "content": "turn 2"},
        {"role": "tool", "name": "read_file", "content": b1.SESSION_TS},
        {"role": "tool", "name": "run_tests", "content": b1.run_tests()}]
raw_len = len(msgs[0]["content"])
b1.prune(msgs, current_turn=1)
assert len(msgs[0]["content"]) < raw_len // 20 and msgs[0]["content"].count("FAIL") == 3, msgs[0]["content"]
assert msgs[2]["content"] == b1.SESSION_TS            # file read in the current turn is kept
assert msgs[3]["content"].count("FAIL") == 3 and len(msgs[3]["content"]) < 2000   # current-turn log is cut

# B2: ground truth, scoring, and run_code returning only stdout
b2.make_logs()
truth = b2.ground_truth()
assert len(truth) == 5 and truth[0][1] > truth[4][1]
assert b2.score("\n".join(f"| {e} | {n} |" for e, n in truth), truth) == 5
assert b2.score(f"| {truth[0][0]} | {truth[0][1] + 1} |", truth) == 0
code = ("import glob,collections\nc=collections.Counter(l.split()[3] for f in glob.glob('*.log') "
        "for l in open(f) if l.split()[4]=='500')\nprint(c.most_common(5))")
assert b2.run_code(code).strip() == str(truth), b2.run_code(code)
os.environ["OPENROUTER_API_KEY_TEST"] = "secret"
assert b2.run_code("import os; print(sorted(k for k in os.environ if 'KEY' in k))").strip() == "[]"
print("offline checks OK, prune ratio:", raw_len, "->", len(msgs[0]["content"]), "chars")
