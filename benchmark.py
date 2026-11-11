#!/usr/bin/env python3
"""Reproducible token benchmark: equivalent (Python vs AGSH) task pairs.

Usage: .venv/bin/python benchmark.py   (needs tiktoken)
Outputs per-task token counts and the total reduction — the numbers cited
in README come from running exactly this script.
"""
import csv, sys

try:
    import tiktoken
except ImportError:
    sys.exit("pip install tiktoken first (e.g. python3 -m pip install tiktoken)")

enc = tiktoken.get_encoding("cl100k_base")

PAIRS = [
    ("fetch-filter-write-assert",
     # Python equivalent of examples/basic-pipeline.agsh
     '''import requests, json
users = []
page = 1
while True:
    r = requests.get("https://api.example.com/users", params={"page": page})
    data = r.json()
    if not data["results"]: break
    users += data["results"]
    page += 1
targets = [u for u in users if u["status"] == "active" and u["email"].endswith("@company.com")]
with open("targets.json", "w") as f:
    json.dump(targets, f)
assert len(targets) > 0
''',
     'fetch https://api.example.com/users paginate=results\n'
     '| filter status=active\n'
     '| filter email ends-with @company.com\n'
     '| write targets.json\n'
     '| assert count>0\n'),
    ("normalize-rename-write",
     '''import requests, json
emails = []
page = 1
while True:
    r = requests.get("https://api.example.com/users", params={"page": page})
    data = r.json()
    if not data["results"]: break
    emails += [u["email"].lower() for u in data["results"]]
    page += 1
out = [{"corporate_email": e} for e in emails]
with open("emails.json", "w") as f:
    json.dump(out, f)
''',
     'fetch https://api.example.com/users paginate=results\n'
     '| map lower email\n'
     '| pick email\n'
     '| map corporate_email=email\n'
     '| write emails.json\n'),
    ("guard-read-filter-write-undo",
     '''import json, os
assert os.path.exists("users.json")
users = json.load(open("users.json"))
active = [u for u in users if u["status"] == "active"]
with open("active.json", "w") as f:
    json.dump(active, f)
# (Python has no built-in undo — this script cannot roll back)
''',
     'guard file exists users.json\n'
     '| read users.json\n'
     '| filter status=active\n'
     '| write active.json\n'
     '| undo\n'),
]

rows, tp_total, ta_total = [], 0, 0
for name, py, agsh in PAIRS:
    tp, ta = len(enc.encode(py)), len(enc.encode(agsh))
    tp_total += tp; ta_total += ta
    rows.append({"task": name, "python_tokens": tp, "agsh_tokens": ta,
                 "reduction_pct": round(100 * (1 - ta / tp))})

w = csv.DictWriter(sys.stdout, fieldnames=rows[0].keys())
w.writeheader()
w.writerows(rows)
print(f"# TOTAL: python {tp_total} tokens, agsh {ta_total} tokens, "
      f"reduction {round(100 * (1 - ta_total / tp_total))}%")
