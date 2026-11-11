#!/usr/bin/env python3
"""AGSH test suite — edge cases, security, undo, scale, determinism."""
import json, os, subprocess, shutil, tempfile, time, sys

PASS, FAIL = [], []
AGSH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agsh.py")

def run_script(script, cwd):
    return subprocess.run(["python3", AGSH], input=script, capture_output=True,
                          text=True, timeout=60, cwd=cwd)

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail and not cond else ""))

def make_env():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "fixtures"))
    json.dump({"results": [
        {"id": 1, "name": "A", "status": "active", "email": "a@company.com"},
        {"id": 2, "name": "B", "status": "inactive", "email": "b@x.org"},
        {"id": 3, "name": "C", "status": "active", "email": "C@Company.COM"},
        {"id": 4, "name": "D", "status": "active", "email": "d@gmail.com"},
    ]}, open(os.path.join(d, "fixtures", "api.example.com.json"), "w"))
    return d

def parse_out(proc):
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None

d = make_env()

# --- 1. EDGE CASES ---
p = run_script("frobnicate x", d)
o = parse_out(p)
check("unknown verb -> structured error", o and "error" in o and o["error"]["step"] == "frobnicate")

p = run_script("filter status=active", d)  # filter without input
o = parse_out(p)
check("filter with no input -> structured error", o and "error" in o and o["error"]["step"] == "filter")

p = run_script('fetch https://api.example.com/users paginate=results\n| filter status=active\n| pick nosuchfield', d)
o = parse_out(p)
check("pick missing field -> empty dicts, no crash", o and o["result"] == [{}, {}, {}],
      f"got {o}")

p = run_script('write /etc/agsh_should_not_exist.json', d)
o = parse_out(p)
check("write with no data / bare path -> structured error", o and "error" in o)

p = run_script('fetch https://api.example.com/users paginate=results\n| filter status=active\n| filter email ends-with @company.com\n| pick id\n| assert count>=1\n| read nonexistent_file.json', d)
o = parse_out(p)
check("read missing file mid-pipeline -> error step read", o and "error" in o and o["error"]["step"] == "read")

p = run_script("   ", d)
check("empty script -> JSON result null, no crash", parse_out(p) is not None and parse_out(p)["result"] is None)

# --- 2. SECURITY: path traversal ---
outside = os.path.dirname(d)
for stale in ("traversal_escape.json", "escape2.json"):
    p_ = os.path.join(outside if "traversal" in stale else tempfile.gettempdir(), stale)
    if os.path.exists(p_): os.remove(p_)
p = run_script('fetch https://api.example.com/users paginate=results\n| write ../traversal_escape.json', d)
check("path traversal outside workdir BLOCKED",
      not os.path.exists(os.path.join(outside, "traversal_escape.json")),
      "file escaped the sandbox dir!")

p = run_script('fetch https://api.example.com/users paginate=results\n| write ../../escape2.json', d)
check("deep traversal blocked", not os.path.exists(os.path.join(tempfile.gettempdir(), "escape2.json")))

# --- 3. UNDO SEMANTICS ---
p = run_script('fetch https://api.example.com/users paginate=results\n| filter status=active\n| write a.json\n| checkpoint\n| write b.json\n| undo\n', d)
o = parse_out(p)
a_ok = os.path.exists(os.path.join(d, "a.json"))
b_ok = os.path.exists(os.path.join(d, "b.json"))
check("undo reverts post-checkpoint write only", a_ok and not b_ok,
      f"a.json exists={a_ok}, b.json exists={b_ok}")

p = run_script('fetch https://api.example.com/users paginate=results\n| filter status=active\n| write a.json\n| checkpoint\n| write b.json\n| undo checkpoint=0\n', d)
o = parse_out(p)
check("undo to checkpoint 0 reverts all since", not os.path.exists(os.path.join(d, "b.json")))

p = run_script('fetch https://api.example.com/users paginate=results\n| write a.json\n| write a.json\n| undo\n', d)
o = parse_out(p)
check("undo restores previous content (overwrite)", o is not None and os.path.exists(os.path.join(d, "a.json")))

# --- 4. SCALE ---
json.dump({"results": [{"id": i, "status": "active", "email": f"u{i}@company.com"}
                        for i in range(10000)]},
          open(os.path.join(d, "fixtures", "api.example.com.json"), "w"))
t0 = time.time()
p = run_script('fetch https://api.example.com/users paginate=results\n| filter status=active\n| filter email ends-with @company.com\n| pick id,email\n| write big.json\n| assert count>0\n', d)
dt = time.time() - t0
o = parse_out(p)
check("10k rows pipeline completes", o and o["result"].get("written") == 10000, f"got {o}")
check("10k rows under 5s", dt < 5, f"{dt:.2f}s")

# --- 5. DETERMINISM ---
p1 = run_script('fetch https://api.example.com/users paginate=results\n| filter status=active\n| pick email\n| map lower email\n', d)
p2 = run_script('fetch https://api.example.com/users paginate=results\n| filter status=active\n| pick email\n| map lower email\n', d)
o1, o2 = parse_out(p1), parse_out(p2)
check("deterministic: same script twice -> identical result",
      o1 and o2 and json.dumps(o1["result"]) == json.dumps(o2["result"]))

# --- 6. PERMISSION MODEL: plan mode ---
p = run_script('fetch https://api.example.com/users paginate=results\n| write out.json\n| run echo hi\n| post https://api.example.com/things\n', d)
# NOTE: run_script has no flags; plan mode needs flags — run manually:
import subprocess as sp
plan_p = sp.run(["python3", AGSH, "--plan"], input='fetch https://api.example.com/users paginate=results\n| write out.json\n| run echo hi\n| post https://api.example.com/things\n',
                capture_output=True, text=True, cwd=d)
plan = json.loads(plan_p.stdout)
check("plan mode: no execution, aggregate risks", "stages" in plan and "summary" in plan
      and plan["summary"].get("exec") == 1 and plan["summary"].get("network-write") == 1,
      f"got {plan.get('summary')}")
check("plan verdict requires --allow-run + --approve",
      set(plan["approval_required"]) == {"--allow-run", "--approve"}, f"got {plan['approval_required']}")

# run gated without flag
p = run_script('run echo hi\n', d)
o = parse_out(p)
check("run blocked without --allow-run", o and "error" in o and o["error"]["step"] == "run")

# run allowed with flag, restricted env
p = sp.run(["python3", AGSH, "--allow-run"], input='run echo $HOME\n', capture_output=True, text=True, cwd=d)
o = json.loads(p.stdout)
check("run with --allow-run executes restricted (HOME=cwd)",
      o["result"]["exit_code"] == 0 and o["result"]["stdout"] == d, f"got {o['result']}")

# --- 7. HUMAN GATE: ask ---
p = run_script('ask Continue?\n', d)
o = parse_out(p)
check("ask without answer -> exit 2 + needs_human", p.returncode == 2 and o and o.get("needs_human") is True,
      f"exit={p.returncode} out={p.stdout[:100]}")
p = sp.run(["python3", AGSH, "--answer", "yes"], input='ask Continue?\n', capture_output=True, text=True, cwd=d)
o = json.loads(p.stdout)
check("ask with --answer continues pipeline", o["result"]["answer"] == "yes", f"got {o['result']}")

# --- 8. WORLD-STATE checkpoint (cross-session) ---
# session 1: create files + save world
p = run_script('fetch https://api.example.com/users paginate=results\n| filter status=active\n| write s1.json\n| checkpoint save=world.json\n', d)
o = parse_out(p)
check("checkpoint save= writes world file", o and o["result"]["world"] is not None, f"got {o}")
# simulate new session: files deleted, then resume restores them
os.remove(os.path.join(d, "s1.json"))
p = run_script('resume world.json\n', d)
o = parse_out(p)
s1_restored = os.path.exists(os.path.join(d, "s1.json"))
check("resume restores deleted files (cross-session state)", s1_restored and o and o["result"]["resumed"] == "world.json",
      f"s1 exists={s1_restored}")

# --- 9. POST approval + REAL API undo (local HTTP server) ---
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
hits = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
        hits.append(("POST", self.path))
        self.send_response(201); self.end_headers(); self.wfile.write(b'{"id": 42}')
    def do_DELETE(self):
        hits.append(("DELETE", self.path))
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]

# post without approval -> blocked
p = run_script(f'post http://127.0.0.1:{port}/things\n', d)
o = parse_out(p)
check("post blocked without --approve", o and "error" in o and "approval" in o["error"]["message"])

# post with approval, then real DELETE undo
p = sp.run(["python3", AGSH, "--approve"], input=f'post http://127.0.0.1:{port}/things undo_method=DELETE undo_url=http://127.0.0.1:{port}/things/42\n| undo\n', capture_output=True, text=True, cwd=d)
o = json.loads(p.stdout)
methods = [m for m, _ in hits]
check("post with --approve executes", any(m == "POST" for m in methods), f"hits={hits}")
check("undo performs REAL DELETE against server", any(m == "DELETE" for m in methods)
      and "HTTP 204" in json.dumps(o["result"]), f"hits={hits} result={o['result']}")
srv.shutdown()

# --- 10. REVIEWER BLIND-SPOTS (negative tests) ---
# append must be sandboxed like read/write
outside = os.path.dirname(d)
esc = os.path.join(outside, "APPEND_ESCAPE.txt")
if os.path.exists(esc): os.remove(esc)
p = run_script('append /tmp/APPEND_ESCAPE_TXT_PLACEHOLDER\n', d)  # absolute → likely blocked
p = run_script(f'append {esc}\n', d)
o = parse_out(p)
check("append outside workdir BLOCKED (reviewer bug #1)",
      o and "error" in o and not os.path.exists(esc), f"out={o} exists={os.path.exists(esc)}")

# resume must not be an arbitrary write primitive
wf = os.path.join(d, "evil_world.json")
json.dump({"data": 1, "files": {"/tmp/RESUME_ESCAPE.txt": "pwned"}, "log_len": 0},
          open(wf, "w"))
esc2 = "/tmp/RESUME_ESCAPE.txt"
if os.path.exists(esc2): os.remove(esc2)
p = run_script('resume evil_world.json\n', d)
o = parse_out(p)
check("resume with absolute path in world file BLOCKED (reviewer bug #2)",
      o and "error" in o and not os.path.exists(esc2), f"out={o} exists={os.path.exists(esc2)}")

# inline approve=1 must NOT bypass --approve (reviewer bug #3)
p = sp.run(["python3", AGSH], input=f'post http://127.0.0.1:{port}/things approve=1\n',
           capture_output=True, text=True, cwd=d)
o = parse_out(p)
check("post with inline approve=1 still BLOCKED (reviewer bug #3)",
      o and "error" in o and "approval" in o["error"]["message"], f"out={o}")

# no tracebacks cross the boundary (reviewer bug #4)
p = run_script('read a=b.json\n', d)
o = parse_out(p)
check("malformed token -> structured error, NOT traceback (reviewer bug #4)",
      o and "error" in o and "Traceback" not in p.stdout + p.stderr, f"stdout={p.stdout[:120]} stderr={p.stderr[:80]}")

# quoted value with spaces (reviewer bonus) — fixture at this point is the 10k dataset
p = run_script('fetch https://api.example.com/users paginate=results\n| filter email="u1@company.com"\n', d)
o = parse_out(p)
check("filter quoted value works", o and isinstance(o["result"], list) and len(o["result"]) == 1,
      f"got {o}")

# --- cleanup report ---
print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
for name, det in FAIL:
    print("FAILED:", name, "|", det)
shutil.rmtree(d, ignore_errors=True)
sys.exit(1 if FAIL else 0)
