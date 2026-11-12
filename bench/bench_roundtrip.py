#!/usr/bin/env python3
"""AGSH round-trip harness (patch batch 3, putra/vm-0-86).
For each (task, model): ask the model to write an AGSH pipeline, then EXECUTE
the emitted line with agsh.py and record parse/execute outcome + token usage.
Tasks are offline (read/filter/sort/count/summarize/let/take — no fetch).
Error-recovery tasks give the model a broken pipeline + its structured error.
Results append to results.jsonl (raw model output + verdict)."""
import json, os, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AGSH = os.path.join(HERE, "..", "agsh.py")
KEY = os.environ.get("NINEROUTER_API_KEY", "")
ROUTER = "http://127.0.0.1:20128/v1/chat/completions"

GREEN = [
    {"id": "g1", "kind": "greenfield",
     "ask": "Pipeline AGSH: baca file events.jsonl, filter items dengan status=failed, ambil 5 pertama (head), lalu hitung jumlahnya (count). File: events.jsonl"},
    {"id": "g2", "kind": "greenfield",
     "ask": "Pipeline AGSH: baca users.jsonl, urutkan berdasar field score descending, ambil 10 pertama, tulis ke top.jsonl (write)."},
    {"id": "g3", "kind": "greenfield",
     "ask": "Pipeline AGSH: baca log.jsonl, kelompokkan per field level (group), hitung tiap kelompok (count), lalu ringkas hasilnya dengan summarize."},
    {"id": "g4", "kind": "greenfield",
     "ask": "Pipeline AGSH: baca data.jsonl, simpan ke state bernama src (let), filter price>100, ambil (take) state src kembali, hitung (count)."},
    {"id": "g5", "kind": "greenfield",
     "ask": "Pipeline AGSH: baca words.jsonl, regex hanya baris yang field word cocok ^a, uniq hasilnya, tulis ke uniq-a.jsonl."},
    {"id": "g6", "kind": "greenfield",
     "ask": "Pipeline AGSH: baca metrics.jsonl, ambil field value tiap item (map), head 3, join jadi satu string koma (join)."},
]
RECOVERY = [
    {"id": "r1", "kind": "recovery",
     "broken": "read events.jsonl | tace 3",
     "error": '{"error":{"step":"tace","message":"unknown verb \'tace\'","hint":"did you mean: head?","got":null}}',
     "expect_fix": "head"},
    {"id": "r2", "kind": "recovery",
     "broken": "read users.jsonl | lines | sort score | head 10 | wriite top.jsonl",
     "error": '{"error":{"step":"wriite","message":"unknown verb \'wriite\'","hint":"did you mean: write?","got":null}}',
     "expect_fix": "write"},
    {"id": "r3", "kind": "recovery",
     "broken": "read log.jsonl | lines | filter status=fild | count",
     "error": '{"error":{"step":"filter","message":"filter FIELD=VALUE: field \'status\' missing in 3 of 6 items","got":"fild"}}',
     "expect_fix": ""},
    {"id": "r4", "kind": "recovery",
     "broken": "read data.jsonl | lines | let src = | take src | count",
     "error": '{"error":{"step":"let","message":"variable name must be [A-Za-z_]word","got":""}}',
     "expect_fix": ""},
]
TASKS = GREEN + RECOVERY

SYS = ("You write pipelines in AGSH, a shell DSL. One line, stages separated by '|'. "
       "Verbs: read FILE (returns raw text for .jsonl), lines (split text into a JSON list; "
       "ALWAYS use 'read FILE.jsonl | lines' before filter/sort/head on jsonl), "
       "write FILE, append, filter FIELD=VALUE, filter FIELD>NUM, filter FIELD contains TEXT, "
       "pick F1,F2, map lower|upper|strip FIELD, map NEW=OLD, search TEXT, run CMD, ask Q, "
       "checkpoint, guard, undo, resume FILE, assert TYPE, sort FIELD, group FIELD, count, "
       "head N, tail N, uniq, regex PATTERN, join SEP, llm \"instruction\", summarize, lines, "
       "let NAME, take NAME, get PATH. "
       "Example: read events.jsonl | lines | filter status=failed | count. "
       "Answer with ONLY the pipeline line, no explanation, no backticks.")

def chat(model, prompt, max_tokens=250):
    body = json.dumps({"model": model, "stream": False, "max_tokens": max_tokens, "temperature": 0,
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(ROUTER, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read().decode())
    msg = resp["choices"][0]["message"]
    content = msg.get("content") or ""
    if (msg.get("reasoning_content") or "") and not content.strip():
        content = msg["reasoning_content"]
    u = resp.get("usage", {})
    return content.strip(), u

def extract_pipeline(text):
    # strip fences, take the last non-empty line that does not start with prose markers
    t = text.strip()
    if t.startswith("```"):
        import re
        t = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", t).rstrip("`").strip()
    for line in reversed([l.strip() for l in t.splitlines() if l.strip()]):
        if line.startswith(("#", "//", "Note", "Catatan", "**")):
            continue
        return line
    return t.splitlines()[-1].strip() if t.splitlines() else t

def run_agsh(line, cwd):
    if any(v in line for v in ("fetch", "post ", "run ", "llm ")):
        return {"skipped": True, "why": "network/tool verb excluded in offline suite"}
    try:
        p = subprocess.run([sys.executable, AGSH], input=line, capture_output=True,
                           text=True, timeout=30, cwd=cwd)
        out = json.loads(p.stdout.strip()) if p.stdout.strip() else {}
        return {"error": out.get("error"), "has_result": "result" in out}
    except Exception as e:
        return {"error": {"message": f"harness: {e}"}, "has_result": False}

def main():
    models = sys.argv[1].split(",") if len(sys.argv) > 1 else ["dolphin-fleet", "lokall", "utama"]
    fixtures = os.path.join(HERE, "fixtures")
    os.makedirs(fixtures, exist_ok=True)
    os.makedirs(os.path.join(fixtures, "events.jsonl"), exist_ok=True) if False else None
    # fixtures: agsh fixtures dir expects <host>.json for fetch; for read, plain files in cwd
    for name, rows in {
        "events.jsonl": [{"id": i, "status": "failed" if i % 2 else "ok"} for i in range(1, 9)],
        "users.jsonl": [{"id": i, "score": (i * 37) % 100} for i in range(1, 16)],
        "log.jsonl": [{"id": i, "level": ["info", "warn", "error"][i % 3]} for i in range(1, 7)],
        "data.jsonl": [{"id": i, "price": i * 25} for i in range(1, 9)],
        "words.jsonl": [{"word": w} for w in ["apple", "bee", "avocado", "cat", "apricot", "dog"]],
        "metrics.jsonl": [{"value": v} for v in [3, 9, 1, 7]],
    }.items():
        with open(os.path.join(HERE, name), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    out = open(os.path.join(HERE, "results.jsonl"), "a")
    for model in models:
        for t in TASKS:
            if t["kind"] == "greenfield":
                prompt = f"Tulis satu baris pipeline AGSH untuk: {t['ask']}\nJawab hanya barisnya."
            else:
                prompt = (f"Pipeline AGSH ini gagal:\n{t['broken']}\n\nDengan error:\n{t['error']}\n\n"
                          f"Perbaiki pipeline-nya. Jawab hanya baris yang sudah diperbaiki.")
            rec = {"model": model, "task": t["id"], "kind": t["kind"], "ts": time.time()}
            try:
                text, usage = chat(model, prompt)
                rec["tokens"] = {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")}
                rec["raw"] = text[:400]
                line = extract_pipeline(text)
                rec["emitted"] = line
                rec["looks_like_agsh"] = bool(line) and "|" in line and line.split()[0] in (
                    "read", "write", "append", "filter", "pick", "map", "search", "run", "ask",
                    "checkpoint", "guard", "undo", "resume", "assert", "sort", "group", "count",
                    "head", "tail", "uniq", "regex", "join", "llm", "summarize", "lines", "let", "take", "get")
                verdict = run_agsh(line, HERE)
                rec.update(verdict)
                rec["executed_ok"] = (not verdict.get("skipped")) and verdict.get("has_result") and not verdict.get("error")
                if t["kind"] == "recovery" and t["expect_fix"]:
                    rec["fix_present"] = t["expect_fix"] in line
            except Exception as e:
                rec["api_error"] = str(e)[:200]
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            print(json.dumps({k: rec.get(k) for k in ("model", "task", "emitted", "executed_ok", "api_error")}, ensure_ascii=False))
    out.close()
    print("DONE")

if __name__ == "__main__":
    main()
