#!/usr/bin/env python3
"""LLM round-trip test: ask a model to write AGSH for a task, execute it verbatim.
Key is read from ~/.hermes/config.yaml, never printed."""
import json, re, subprocess, sys, urllib.request, yaml

cfg = yaml.safe_load(open("/root/.hermes/config.yaml"))
mcfg = cfg.get("model", {})
key = mcfg.get("api_key") or mcfg.get("custom", {}).get("api_key")
base = mcfg.get("base_url", "https://token.lightvela.ai/v1")
model = mcfg.get("custom_model") or mcfg.get("model_name") or "auto"

# pull key from any nested spot if not found
def find_key(d):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == "api_key" and isinstance(v, str) and len(v) > 10:
                return v
            r = find_key(v)
            if r: return r
    elif isinstance(d, list):
        for v in d:
            r = find_key(v)
            if r: return r
    return None
key = key or find_key(cfg)
if not key:
    sys.exit("no api key found (not printed)")

SPEC = """AGSH — agent shell. Pipe-style DSL, one verb per stage, data flows as JSON.
Verbs: fetch <url> paginate=<field> | post <url> | read <file> | write <file> |
append <file> | filter <field>=<value> | filter email ends-with <suffix> |
pick <field,field,...> | map lower|upper|strip <field> | map newname=<oldname> |
search <text> | run <shell-cmd> | guard file exists <path> | checkpoint | undo |
assert count>|<|>=|<=|== <n>
Continuation lines start with '|'. Output is JSON only."""

def chat(task):
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SPEC},
            {"role": "user", "content": task},
        ],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]

TASKS = [
    ("T1", "Ambil semua user dari https://api.example.com/users (paginasi di field 'results'), "
           "filter status=active dan email berakhiran @company.com, simpan ke targets.json, "
           "pastikan jumlahnya > 0."),
    ("T2", "Dari https://api.example.com/users (paginate=results): normalisasi email jadi lowercase, "
           "ambil field email saja, rename jadi corporate_email, simpan emails.json."),
    ("T3", "Baca users.json, filter status=active, simpan ke active.json, tambahkan guard bahwa "
           "file users.json ada dulu."),
]

# prepare input file for T3
with open("fixtures/api.example.com.json") as f:
    json.dump(json.load(f)["results"], open("users.json", "w"))

for tid, task in TASKS:
    print(f"=== {tid} ===")
    try:
        out = chat(task)
    except Exception as e:
        print(f"LLM call failed: {e}"); continue
    print("--- model output ---")
    print(out.strip())
    # execute verbatim
    proc = subprocess.run(["python3", "agsh.py"], input=out, capture_output=True,
                          text=True, timeout=60)
    print("--- execution ---")
    print(proc.stdout.strip()[:600] or proc.stderr.strip()[:300])
    if tid == "T3":  # prepare input file
        with open("fixtures/api.example.com.json") as f:
            users = json.load(f)["results"]
        json.dump(users, open("users.json", "w"))
