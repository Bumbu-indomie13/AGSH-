# AGSH — Panduan Integrasi untuk AI Agent

**Versi:** v0.2 | **Repo:** https://github.com/Bumbu-indomie13/AGSH-
**Untuk:** AI agent (Hermes, Claude, Cursor, dll) yang ingin memakai AGSH sebagai tool orkestrasi.

---

## 1. Apa itu AGSH

AGSH (Agent Shell) adalah pipe-style DSL yang dieksekusi interpreter deterministik di LUAR LLM. Agent menulis 3-6 stage pendek, runtime menjalankan semuanya dalam SATU eksekusi, dan mengembalikan JSON terstruktur + delta-feedback.

**Kapan pakai AGSH vs Python:**
- PAKAI AGSH: fetch API + filter/transform + simpan file, pagination, operasi file berulang, pipeline multi-langkah yang butuh verifikasi/undo.
- PAKAI Python: logika kompleks, perhitungan, parsing tidak standar (AGSH punya escape hatch `run`).

**Manfaat terukur:** 1 round-trip LLM menggantikan 5+ tool call; ~69% lebih hemat token (benchmark.py); error selalu structured (tanpa traceback); mutasi bisa di-undo.

## 2. Cara memanggil

Interpreter di mesin target: `/workspace/agsh/agsh.py` (stdlib-only, tidak perlu install apa pun).

```bash
printf '<pipeline AGSH>' | python3 /workspace/agsh/agsh.py [flags]
```

**Flag host (wajib untuk aksi berisiko — script TIDAK bisa meng-approve dirinya):**
- `--plan` — analisis tanpa eksekusi: risiko per stage + verdict. SELALU jalankan ini dulu untuk pipeline baru.
- `--allow-run` — buka verb `run` (shell, restricted env).
- `--approve` — buka verb `post` (network write).
- `--answer "TEXT"` — jawab gate `ask` (exit code 2).
- `--allow-private-net` — bypass SSRF guard (jarang perlu).

**Aturan loop yang benar:**
1. Tulis pipeline → jalankan `--plan`
2. Jika verdict `BLOCKED: escapes sandbox` → perbaiki path, JANGAN coba bypass
3. Jika `needs approval` → tampilkan ke user, tunggu persetujuan, baru eksekusi dengan flag
4. Jika sukses → baca `result` + `stats` (delta-feedback)

## 3. Verb reference (16)

```
fetch URL [paginate=FIELD]     # HTTP GET JSON; auto-pagination; SSRF guard aktif
post URL [undo_method=DELETE] [undo_url=URL]   # butuh --approve; body = pipeline data
read FILE | write FILE | append FILE    # path-sandboxed ke working dir
filter FIELD=VALUE             # string equality
filter email ends-with X       # suffix match (quoted value utk spasi: filter name="John Smith")
pick f1,f2                     # project fields (missing = {})
map lower|upper|strip FIELD    # transformasi string
map NEWNAME=OLDNAME            # rename field
search TEXT                    # grep working dir
run CMD                        # BUTUH --allow-run; restricted: env minimal, timeout 15s
guard file exists PATH         # precondition
assert count >N               # postcondition (juga < >= <= ==); gagal = hint perbaikan
checkpoint [save=FILE]         # rollback point; save= = world-state lintas sesi
undo [checkpoint=N]            # revert mutasi sejak checkpoint (file byte-per-byte, API = real HTTP)
resume FILE                    # restore world-state di sesi baru
ask QUESTION                   # human gate: exit 2 + {"needs_human": true}
```

**Parsing contract:** value yang mengandung `=`, `>`, `<`, `|` WAJIB di-quote: `filter x="a>b"`. Inline pipe boleh: `read f.json|pick n` = `read f.json | pick n`.

## 4. Format output (kontrak untuk konsumen LLM)

Sukses:
```json
{"result": {"written": 4, "path": "targets.json", "diff_vs_previous": null},
 "stats": {"round_trips": 1, "lines": 1, "verbs": 5,
           "fetch_sources": ["fixture:api.example.com"]}}
```

Gagal (exit 1) — SELALU JSON, tidak pernah traceback:
```json
{"error": {"step": "assert", "message": "count 0 not > 0",
           "hint": "filter returned 0 — check field names exist", "got": 0}}
```

**Cara menangani error:** baca `hint` → perbaiki script → jalankan ulang. `got` = nilai aktual yang dilihat runtime. Jika `message` diawali `internal:` — laporkan sebagai bug interpreter, jangan di-retry.

**Exit codes:** 0 sukses | 1 error | 2 needs_human (baca `question`, tampilkan ke user).

## 5. Contoh siap pakai

Pipeline dasar:
```
fetch https://api.example.com/users paginate=results
| filter status=active
| filter email ends-with @company.com
| write targets.json
| assert count>0
```

Transformasi + rename:
```
fetch https://api.example.com/users paginate=results
| map lower email
| pick email
| map corporate_email=email
| write emails.json
```

Eksplorasi aman dengan rollback:
```
guard file exists users.json
| checkpoint
| read users.json
| filter status=active
| write active.json
| undo
```

## 6. Mode offline / testing

Buat file `fixtures/<host>.json` di working dir → `fetch` ke host itu menyajikan data fixture (dataset utuh, tanpa network). Sumber data SELALU terungkap di `stats.fetch_sources` (`fixture:<host>` vs `network:<host>`) — periksa ini sebelum mempercayai data.

## 7. Batasan yang harus agent ketahui

- Sandbox path: semua file ops TERBATAS ke working dir — path absolut/luar ditolak. Simpan hanya di cwd.
- SSRF: host yang resolve ke IP private/metadata diblok default.
- `undo` hanya merevert mutasi SETELAH checkpoint terakhir — urutan checkpoint penting.
- API undo butuh `undo_method`+`undo_url` eksplisit; tanpa itu hanya tercatat sebagai intent.
- `map` terbatas (lower/upper/strip/rename) — transformasi kompleks lewat `run` (butuh approval).
- Pagination cap 100 halaman; gagal/cap = structured error (tidak pernah truncate diam-diam).

## 8. Integrasi sebagai tool (pola wrapper)

Pola yang direkomendasikan untuk agent runtime — satu tool call per pipeline:

```
agsh_execute(script: str, plan_first: bool = true, flags: list[str] = [])
  -> jika plan_first: jalankan --plan dulu; jika verdict needs approval,
     kembalikan plan ke user; jika disetujui, eksekusi dengan flags.
```

Di Hermes: panggil via terminal/execute_code dengan printf pipe (lihat §2). Untuk distribusi luas, bungkus sebagai MCP server (`agsh --mcp`) — lihat roadmap repo.

## 9. Checklist sebelum dipercaya pada tugas nyata

1. `--plan` dijalankan dan verdict dipahami (bukan sekadar dilewati)
2. Approval disetujui USER, bukan dijawab oleh agent sendiri
3. Working dir bersih/dedikasi — semua file ops terkunci di situ
4. `checkpoint` dipasang sebelum mutasi yang mungkin di-undo
5. `stats.fetch_sources` diperiksa — jangan percaya data fixture bila mengira network
6. Test suite lulus di mesin target: `python3 tests.py` (42/42)

---
AGSH v0.2 | MIT | 42 test, CI GitHub Actions | Repo: github.com/Bumbu-indomie13/AGSH-
