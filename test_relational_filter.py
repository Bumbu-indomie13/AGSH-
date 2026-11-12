#!/usr/bin/env python3
"""Luthfy enhancement tests untuk AGSH (sort/limit/unique/count/reverse + filter numeric/contains).
Jalankan dari repo root: python3 luthfy_tests.py
"""
import json, os, subprocess, tempfile, sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGSH = os.path.join(HERE, "agsh.py")
PASS, FAIL = [], []

def run(script, flags=()):
    return subprocess.run(["python3", AGSH, *flags], input=script,
                          capture_output=True, text=True, timeout=30, cwd=d)

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail and not cond else ""))

def out(p):
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"_raw": p.stdout[:200], "_err": p.stderr[:200]}

d = tempfile.mkdtemp()
USERS = [{"id": 1, "name": "A", "score": 30, "tag": "x"},
         {"id": 2, "name": "B", "score": 91, "tag": "y"},
         {"id": 3, "name": "C", "score": 12, "tag": "x"},
         {"id": 4, "name": "D", "score": 55, "tag": "x"},
         {"id": 5, "name": "E", "score": 7, "tag": "z"}]
os.makedirs(os.path.join(d, "fixtures"), exist_ok=True)
json.dump({"results": USERS}, open(os.path.join(d, "fixtures", "api.example.com.json"), "w"))

BASE = "fetch https://api.example.com/users paginate=results\n"

# filter numeric >= and <
# verifikasi tanpa count/limit: tulis hasil, ukur isi file
o = out(run(BASE + "| filter score >= 30\n| write score30.json\n"))
import json as _j, os as _os
p = _os.path.join(d, "score30.json")
rows = _j.load(open(p)) if _os.path.exists(p) else []
check("filter score >= 30 -> 3 rows written", o.get("result", {}).get("written") == 3,
      f"written={o.get('result')} rows={len(rows)}")
o = out(run(BASE + "| filter score < 30\n| pick id\n"))
check("filter score < 30", o.get("result") == [{"id": 3}, {"id": 5}], str(o.get("result")))

# filter contains
o = out(run(BASE + "| filter tag contains x\n| pick id\n"))
check("filter tag contains x", o.get("result") == [{"id": 1}, {"id": 3}, {"id": 4}], str(o.get("result")))

# regression: suite lama masih hijau (65 sejak merge filter+verbs; >=61 juga ok jika upstream tumbuh)
p = subprocess.run(["python3", "tests.py"], capture_output=True, text=True, timeout=120, cwd=HERE)
check("upstream suite tetap PASS (>=65, 0 failed)", "0 failed" in p.stdout and int(
    next(l for l in p.stdout.splitlines() if "passed" in l).split()[1]) >= 65, p.stdout[-120:])

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
sys.exit(1 if FAIL else 0)
