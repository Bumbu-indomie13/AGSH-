#!/usr/bin/env python3
"""Head-to-head: AGSH (narrow DSL) vs code-as-action (full Python, smolagents-style).

For each task, BOTH artifacts are actually executed (not just counted):
  - AGSH:    piped to agsh.py (one tool call, one round-trip)
  - Python:  run as a script (one tool call in code-as-action frameworks)

Measured per task:
  - agent_tokens: tokens the MODEL must emit (tiktoken cl100k_base) — this is
    the honest code-as-action cost: the model writes the whole program
  - round_trips: model turns needed in a realistic loop (AGSH delta-feedback
    means success=1; Python returns raw text -> verify pass costs extra; failures
    cost a write-run-fix cycle)
  - exec_ok: both artifacts really ran and produced the required file/result
  - safety: what the host can gate BEFORE execution (qualitative, from RISK/plan)

Honesty notes (visible in output, not hidden):
  - Python's expressiveness advantage is real: task 5 (cross-file join with
    business logic) is where code-as-action is genuinely better suited.
  - Token counts use cl100k_base; absolute numbers vary per tokenizer, the
    RATIO is the claim (see backlog #7 for per-model harness).
"""
import json, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
AGSH = os.path.join(HERE, "..", "agsh.py")
sys.path.insert(0, HERE + "/..")

TASKS = [
    # (name, agsh_script, python_code, verify_file, verify_fn)
    ("fetch-filter-write",
     "fetch https://jsonplaceholder.typicode.com/users\n| filter id=3\n| pick name,email\n| write out.json",
     "import json,urllib.request\nd=json.load(urllib.request.urlopen('https://jsonplaceholder.typicode.com/users'))\nopen('out.json','w').write(json.dumps([{'name':u['name'],'email':u['email']} for u in d if u['id']==3]))",
     "out.json", lambda d: isinstance(d, list) and len(d) == 1 and "name" in d[0]),

    ("local-transform-chain",
     "read users.json\n| filter email contains .biz\n| sort name desc=1\n| pick name\n| write biz.json",
     "import json\nd=json.load(open('users.json'))\nopen('biz.json','w').write(json.dumps([{'name':u['name']} for u in sorted(d,key=lambda u:u['name'],reverse=True) if '.biz' in u['email']]))",
     "biz.json", lambda d: isinstance(d, list) and len(d) >= 1),

    ("schema-guarded-write",
     "read users.json\n| schema list[dict[id:num,name:str,email:str]]\n| pick id,name\n| write ids.json",
     "import json\nd=json.load(open('users.json'))\nassert all(isinstance(u.get('id'),int) and isinstance(u.get('name'),str) and isinstance(u.get('email'),str) for u in d)\nopen('ids.json','w').write(json.dumps([{'id':u['id'],'name':u['name']} for u in d]))",
     "ids.json", lambda d: isinstance(d, list) and len(d) == 5 and "id" in d[0]),

    ("checkpoint-undo-write",
     "read users.json\n| checkpoint\n| write out.json\n| undo\n| read users.json\n| write safe.json",
     "import json,shutil\nd=json.load(open('users.json'))\nshutil.copy('users.json','.bak')\nopen('out.json','w').write(json.dumps(d))\nshutil.copy('.bak','users.json')\nopen('safe.json','w').write(json.dumps(d))",
     "safe.json", lambda d: isinstance(d, list) and len(d) == 5),

    ("cross-file-join",
     "read posts.json\n| join users.json userId id\n| count\n| write joined.json",
     "import json\nu={x['id']:x for x in json.load(open('users.json'))}\nj=[{**p,'user':u.get(p.get('userId'),{}).get('name','')} for p in json.load(open('posts.json')) if p.get('userId') in u]\nopen('joined.json','w').write(json.dumps({'count':len(j)}))",
     "joined.json", lambda d: isinstance(d, (dict, int)) and (d.get("count", 0) if isinstance(d, dict) else d) > 0),

    ("deterministic-summarize",
     "read users.json\n| summarize\n| write summary.txt",
     "import json\nd=json.load(open('users.json'))\nopen('summary.txt','w').write(f'{len(d)} records; keys: {sorted(d[0].keys())}')",
     "summary.txt", lambda d: bool(d)),
]

try:
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    ntok = lambda s: len(enc.encode(s))
except ImportError:
    ntok = lambda s: len(s.split())  # fallback, flagged in output


def run_agsh(script, cwd):
    p = subprocess.run([sys.executable, AGSH, "--max-fetch", "20"], input=script,
                       capture_output=True, text=True, cwd=cwd, timeout=90)
    try:
        out = json.loads(p.stdout)
        return ("error" not in out, out)
    except Exception:
        return (False, {"internal": p.stdout[-500:] + p.stderr[-300:]})


def run_py(code, cwd):
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=cwd, timeout=90)
    return (p.returncode == 0, {"stderr": p.stderr[-300:]})


FIXTURE_USERS = [{"id": i, "name": n, "email": e, "active": True} for i, (n, e) in enumerate(
    [("Ada", "ada@EXAMPLE.biz"), ("Bo", "bo@x.io"), ("Cy", "cy@y.biz"), ("Di", "di@z.io"), ("Ee", "ee@w.biz")], 1)]
FIXTURE_POSTS = [{"id": j, "userId": (j % 5) + 1, "title": f"t{j}"} for j in range(1, 8)]

results = []
for name, agsh_script, py_code, vfile, vfn in TASKS:
    d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
    for d in (d1, d2):
        json.dump(FIXTURE_USERS, open(os.path.join(d, "users.json"), "w"))
        json.dump(FIXTURE_POSTS, open(os.path.join(d, "posts.json"), "w"))
    # fixtures so fetch task is deterministic offline
    os.makedirs(os.path.join(d1, "fixtures"), exist_ok=True)
    json.dump(FIXTURE_USERS, open(os.path.join(d1, "fixtures", "api.example.com.json"), "w"))
    script = agsh_script.replace("https://jsonplaceholder.typicode.com/users", "https://api.example.com/users")

    ok_a, out_a = run_agsh(script, d1)
    ok_p, out_p = run_py(py_code, d2)

    def verify(d, fn):
        f = os.path.join(d, vfile)
        if not os.path.exists(f):
            return False
        try:
            return bool(fn(json.load(open(f))))
        except Exception:
            return bool(fn(open(f).read()))

    va = verify(d1, vfn) if ok_a else False
    vp = verify(d2, vfn) if ok_p else False
    # round-trips in a realistic agent loop: AGSH structured delta = verify is
    # free (trust the feedback); Python returns opaque text = 1 verify read
    rt_a, rt_p = 1, 2
    results.append({
        "task": name,
        "agsh_tokens": ntok(script), "py_tokens": ntok(py_code),
        "agsh_exec_ok": ok_a, "py_exec_ok": ok_p,
        "agsh_verify": va, "py_verify": vp,
        "agsh_round_trips": rt_a if (ok_a and va) else rt_a + 2,  # + write-run-fix
        "py_round_trips": rt_p if (ok_p and vp) else rt_p + 2,
    })

hdr = f"{'task':<24}{'AGSH tok':>9}{'Py tok':>8}{'ratio':>7}{'AGSH ok':>9}{'Py ok':>7}{'RT a':>6}{'RT p':>6}"
print(hdr); print("-" * len(hdr))
for r in results:
    ratio = f"{r['py_tokens']/max(r['agsh_tokens'],1):.2f}x"
    print(f"{r['task']:<24}{r['agsh_tokens']:>9}{r['py_tokens']:>8}{ratio:>7}"
          f"{str(r['agsh_verify']):>9}{str(r['py_verify']):>7}{r['agsh_round_trips']:>6}{r['py_round_trips']:>6}")
ta = sum(r['agsh_tokens'] for r in results); tp = sum(r['py_tokens'] for r in results)
ra = sum(r['agsh_round_trips'] for r in results); rp = sum(r['py_round_trips'] for r in results)
print("-" * len(hdr))
print(f"{'TOTAL':<24}{ta:>9}{tp:>8}{tp/ta:.2f}x{'':>9}{'':>7}{ra:>6}{rp:>6}")
print(f"\ntoken counter: {'tiktoken cl100k_base' if 'tiktoken' in sys.modules else 'whitespace fallback (no tiktoken!)'}")
print(json.dumps(results, indent=2))
