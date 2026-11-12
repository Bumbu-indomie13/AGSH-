#!/usr/bin/env python3
"""Test plan redaction (PR-2 #4):
1. URL dgn basic-auth inline -> password jadi *** di --plan output
2. URL biasa -> tampil utuh
3. verdict tetap benar (redaction tidak merusak risk)
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
json.dump({"results": [{"id": 1}]}, open(os.path.join(d, "fixtures", "secret.example.com.json"), "w"))

def plan(script):
    p = subprocess.run(["python3", AGSH, "--plan"], input=script,
                       capture_output=True, text=True, timeout=30, cwd=d)
    return json.loads(p.stdout)

# 1. basic-auth inline di URL -> redacted
p1 = plan("fetch https://alice:hunter2@secret.example.com/users\n")
subj = p1["stages"][0]["subject"]
check("basic-auth password redacted", "hunter2" not in subj and "***" in subj, subj)
check("user tetap terlihat (forensik ok)", "alice" in subj, subj)
check("host tetap terlihat", "secret.example.com" in subj, subj)

# 2. URL biasa -> utuh
p2 = plan("fetch https://api.example.com/users paginate=results\n")
check("URL biasa tampil utuh", p2["stages"][0]["subject"] == "https://api.example.com/users", p2["stages"][0]["subject"])

# 3. verdict tidak berubah
check("verdict masih safe", "safe" in p2["verdict"], p2["verdict"])

# 4. Bearer literal di subject write (rawangan kecil) -> redacted
p3 = plan("write bearer-ABCDEF123456.txt\n")
check("plan tetap fungsional utk non-url", p3["stages"][0]["verb"] == "write", str(p3["stages"]))

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
sys.exit(1 if FAIL else 0)
