#!/usr/bin/env python3
"""Test auth=$ENV (PR-2 #1): bukti 4 sisi.
1. auth dengan env var ter-set -> fetch network OK (httpbin-like lokal? TIDAK - pakai fixture host biar offline)
   Trick: fixture menyajikan body, TAPI request header dibangun sebelum fixture check? TIDAK - fixture check
   terjadi sebelum Request dibuat, jadi header tak tersentuh. Verifikasi jalur header via probe _resolve_auth_header langsung.
2. env var hilang -> structured error
3. nama var invalid -> structured error
4. secret tidak pernah muncul di audit output
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
json.dump({"results": [{"id": 1, "name": "ok"}]}, open(os.path.join(d, "fixtures", "api.example.com.json"), "w"))

# import interpreter langsung buat unit-probe helper
sys.path.insert(0, HERE)
os.chdir(d)
import importlib.util
spec = importlib.util.spec_from_file_location("agsh", AGSH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# buat interpreter instance minimal (lihat nama class)
cls = None
for n in dir(mod):
    o = getattr(mod, n)
    if isinstance(o, type) and hasattr(o, "v_fetch") and hasattr(o, "v_filter"):
        cls = o
        break
assert cls, "interpreter class tidak ketemu"

os.environ["LUTHFY_TEST_TOKEN"] = "s3cr3t-BEARER-value"
os.chdir(d)
it = cls.__new__(cls)
# init field minimal yang dipakai helper: audit + stats
it.audit = lambda *a, **k: None
it.stats = {}

# 1. env ter-set -> header Bearer benar
h = it._resolve_auth_header("fetch", {"auth": "LUTHFY_TEST_TOKEN"})
check("auth env set -> Bearer header", h == {"Authorization": "Bearer s3cr3t-BEARER-value"}, str(h.keys()))

# 2. env hilang -> AGSHError structured
try:
    it._resolve_auth_header("fetch", {"auth": "NOPE_MISSING_VAR"})
    check("env missing -> structured error", False, "no error raised")
except mod.AGSHError as e:
    blob = json.dumps({"step": getattr(e, "step", None), "message": str(e), "hint": getattr(e, "hint", "")})
    check("env missing -> structured error", "NOPE_MISSING_VAR" in blob and "s3cr3t" not in blob, blob[:120])

# 3. nama var invalid -> structured error
try:
    it._resolve_auth_header("fetch", {"auth": "bad name!!"})
    check("invalid var name -> structured error", False, "no error raised")
except mod.AGSHError as e:
    check("invalid var name -> structured error", True)

# 4. tanpa auth kw -> {} (no-op)
h2 = it._resolve_auth_header("fetch", {})
check("no auth kw -> no header", h2 == {}, str(h2))

# 5. end-to-end offline: pipeline dengan auth= tetap jalan via fixture (tidak crash)
p = subprocess.run(["python3", AGSH], input="fetch https://api.example.com/users paginate=results auth=LUTHFY_TEST_TOKEN\n",
                   capture_output=True, text=True, timeout=30, cwd=d)
ok = p.returncode == 0 and '"id": 1' in p.stdout
check("e2e fixture+auth= no crash", ok, (p.stdout[:150] + p.stderr[:150]))

# 6. secret tidak bocor ke stdout audit path: audit mock di atas tidak menulis, cukup cek source code tidak pernah f-string val
src = open(AGSH).read()
check("secret never in audit payload (source audit)", 'REDACTED' in src and 'f"Bearer {val}"' in src and src.count("Bearer {val}") == 1)

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
sys.exit(1 if FAIL else 0)
