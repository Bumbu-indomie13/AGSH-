#!/usr/bin/env python3
"""Test record/replay + retries (PR-2 #2) — offline via fixture + network-side simulation.
Fixture-based fetch never hits network, so record/replay test uses the fixture path too:
- record= writes cache entry keyed by URL (fixture body)
- replay= serves from cache; fetch_sources shows 'replay:...' instead of 'fixture:...'
- retries=0..3 accepted; invalid -> structured error; replay of missing URL falls back to fixture/network
"""
import json, os, subprocess, tempfile, sys

HERE = "/home/agentuser/agsh"
AGSH = os.path.join(HERE, "agsh.py")
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail and not cond else ""))

d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "fixtures"), exist_ok=True)
json.dump({"results": [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]},
          open(os.path.join(d, "fixtures", "api.example.com.json"), "w"))

def run(script, extra=()):
    return subprocess.run(["python3", AGSH, *extra], input=script,
                          capture_output=True, text=True, timeout=30, cwd=d)

def out(p):
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"_raw": p.stdout[:200], "_err": p.stderr[:200]}

# 1. record: fetch + record=cache.jsonl -> cache file ada isinya
cache = os.path.join(d, "cache.jsonl")
p = out(run("fetch https://api.example.com/users paginate=results record=cache.jsonl\n| write step1.json\n"))
check("record: fetch+write OK", p.get("result", {}).get("written") == 2, str(p)[:150])
recs = [json.loads(l) for l in open(cache)] if os.path.exists(cache) else []
check("record: cache JSONL terisi", len(recs) == 1 and recs[0].get("url") == "https://api.example.com/users" and recs[0].get("body"), str(recs)[:120])

# 2. replay: hapus fixture -> fetch replay= masih balikin data dari cache (tanpa network)
os.remove(os.path.join(d, "fixtures", "api.example.com.json"))
p = out(run("fetch https://api.example.com/users replay=cache.jsonl\n"))
check("replay: serve dari cache", p.get("result") == [{"results": [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]}] or p.get("result") == {"results": [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]}, str(p)[:150])
check("replay: fetch_sources ditandai", p.get("stats", {}).get("fetch_sources") == ["replay:api.example.com"], str(p.get("stats", {}).get("fetch_sources")))

# 3. replay miss (URL lain, tanpa fixture, tanpa network) -> structured error, bukan crash
p = out(run("fetch https://other.example.com/x replay=cache.jsonl\n"))
err = p.get("error", {})
check("replay miss -> structured error (bukan crash)", bool(err) and p.get("result") is None, str(p)[:150])

# 4. retries: invalid value -> structured error
p = out(run("fetch https://api.example.com/users retries=abc\n"))
check("retries=abc -> structured error", bool(p.get("error")), str(p)[:100])
p = out(run("fetch https://api.example.com/users retries=9\n"))
check("retries=9 (>cap) -> structured error", bool(p.get("error")) and "0..3" in json.dumps(p.get("error", {})), str(p)[:120])

# 5. retries valid + fixture sudah dihapus: fetch fixture-less URL gagal structured (network blocked ke example.com resolusi? unreachable di sandbox)
p = out(run("fetch https://api.example.com/users retries=2\n"))
# di sandbox fixture hilang -> coba network -> gagal DNS/private -> error structured dgn attempt count
err = json.dumps(p.get("error", {}))
check("retries=2 network fail -> structured error", bool(p.get("error")), str(p)[:150])

# 6. corrupt cache line: replay tetap jalan (skip line rusak)
with open(cache, "a") as f:
    f.write("NOT-JSON-GARBAGE\n")
p = out(run("fetch https://api.example.com/users replay=cache.jsonl\n"))
check("corrupt cache line di-skip", p.get("result") == {"results": [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]}, str(p)[:120])

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
sys.exit(1 if FAIL else 0)
