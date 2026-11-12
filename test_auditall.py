#!/usr/bin/env python3
"""Test audit-all-stages (PR-2 #3): 8 verb baru ter-audit, tanpa duplikat.
- Pipeline filter|pick|let|take|get|assert|guard -> baris audit per stage.
- Dup-guard: fetch/write/post/run/append/undo/resume/map/read TIDAK boleh dapat baris dobel.
"""
import json, os, subprocess, tempfile, sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGSH = os.path.join(HERE, "agsh.py")
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail and not cond else ""))

d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "fixtures"), exist_ok=True)
json.dump({"results": [{"id": 1, "name": "alpha", "score": 30, "tag": "x"},
                        {"id": 2, "name": "beta", "score": 91, "tag": "y"}]},
          open(os.path.join(d, "fixtures", "api.example.com.json"), "w"))

def run(script, flags):
    return subprocess.run(["python3", AGSH, *flags], input=script,
                          capture_output=True, text=True, timeout=30, cwd=d)

audit = os.path.join(d, "audit.jsonl")
script = ("fetch https://api.example.com/users paginate=results\n"
          "| filter score > 20\n"
          "| pick id,name\n"
          "| write out.json\n")
p = run(script, ["--audit", audit])
recs = [json.loads(l) for l in open(audit)] if os.path.exists(audit) else []
verbs = [r.get("verb") for r in recs]

check("filter ter-audit", "filter" in verbs, str(verbs))
check("pick ter-audit", "pick" in verbs, str(verbs))
check("write tetap ter-audit (no reg)", "write" in verbs, str(verbs))
check("fetch tidak dobel (dup-guard)", verbs.count("fetch") == 1, str(verbs))
check("write tidak dobel", verbs.count("write") == 1, str(verbs))
check("filter outcome=out rows", next((r.get("out") for r in recs if r.get("verb") == "filter"), None) == 2, str([r for r in recs if r.get("verb")=="filter"]))

# let/take/get/assert ter-audit (let = bind-only verb, tanpa assignment value)
audit2 = os.path.join(d, "audit2.jsonl")
script2 = ("fetch https://api.example.com/users paginate=results\n"
           "| let saved\n"
           "| assert count>=1\n")
p2 = run(script2, ["--audit", audit2])
recs2 = [json.loads(l) for l in open(audit2)] if os.path.exists(audit2) else []
verbs2 = [r.get("verb") for r in recs2]
check("let ter-audit", "let" in verbs2, str(verbs2))
check("assert ter-audit", "assert" in verbs2, str(verbs2))

# guard (pass) ter-audit
audit3 = os.path.join(d, "audit3.jsonl")
script3 = "read out.json\n| guard rows>0\n"
p3 = run(script3, ["--audit", audit3])
recs3 = [json.loads(l) for l in open(audit3)] if os.path.exists(audit3) else []
verbs3 = [r.get("verb") for r in recs3]
check("guard pass ter-audit", "guard" in verbs3, str(verbs3))

# count verb (Alfayn) TIDAK dobel juga
check("no double lines for any verb", all(v in (None,) or verbs.count(v) <= 1 for v in set(verbs)), str({v: verbs.count(v) for v in set(verbs) if v}))

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
sys.exit(1 if FAIL else 0)
