# AGSH Grammar Specification (v0.1)

## Pipeline structure

```
script     := stage (NEWLINE "|" stage)*
stage      := verb subject*
verb       := [a-z]+
subject    := token (WS token)*
token      := bare | quoted | kv
kv         := key "=" value          # named option (paginate=, checkpoint=)
bare       := [^\s"'|]+
quoted     := '"' [^"]* '"' | "'" [^']* "'"
```

- Data flows left-to-right as JSON values (list, dict, str, num, null).
- Continuation lines start with `|`; empty stages are ignored.
- Comments start with `#`.

## Verbs

### fetch
`fetch URL [paginate=FIELD] [auth=ENVVAR] [record=CACHE.jsonl] [replay=CACHE.jsonl] [retries=N]`
- URL must match `https?://`.
- **SSRF guard**: hosts resolving to private/metadata networks (RFC1918, loopback, link-local `169.254.x`, ULA) are **blocked by default** — agents must not be reachable to internal services via prompt injection. Bypass only with the host-level `--allow-private-net` flag.
- Offline mode: if `fixtures/<host>.json` exists, it is served as the complete dataset (no pagination) instead of the network. **The data source is always disclosed**: `stats.fetch_sources` reports `fixture:<host>` vs `network:<host>` vs `replay:<host>` for every fetch — fixture shadowing can never be silent.
- `auth=ENVVAR` — send `Authorization: Bearer *** of $ENVVAR>`; the env var must exist (structured error otherwise) and its value is never logged (audit shows the variable name + `REDACTED`). Applies to `post` too.
- `header=NAME:VALUE` — **repeatable** for multi-header APIs (e.g. `header=Accept:application/xml header=X-API-Key:$MY_KEY`). VALUE may be a literal or `$ENVVAR` (resolved at runtime, audit records only the var name + `REDACTED`); header names are validated `[A-Za-z0-9_-]+` (CR/LF injection blocked). Applies to `fetch` and `post`.
- `--llm-cache FILE` — sqlite cache of LLM answers keyed by hash(model+messages+params): identical re-runs are served with **zero network** (`llm_cache_hits` in stats) — deterministic offline runs.
- `--llm-budget N` (or env `AGSH_LLM_BUDGET`) — hard-fail with a structured error once a run exceeds N LLM calls (llm/summarize).
- `record=CACHE.jsonl` — after a successful fetch (network or fixture), upsert `{url, body}` into a JSONL cache keyed by URL.
- `replay=CACHE.jsonl` — serve the body from cache when the URL is present: zero network, bypasses SSRF/DNS checks by design (nothing leaves the box), still disclosed via `fetch_sources: replay:<host>`. Corrupt cache lines are skipped. Cache miss falls through to normal fetch.
- `retries=N` (0..3) — validated **before** any DNS/SSRF work (fail-fast); a failed request retries with linear backoff (0.2s × attempt), then raises a structured error including the attempt count.
- `paginate=FIELD`: repeatedly appends `FIELD` arrays across `?page=N` until empty (max 100 pages). A failing page after ≥1 success — or hitting the 100-page cap — raises a structured error; results are never silently truncated.

### post
`post URL [undo_method=DELETE|POST] [undo_url=URL]` — performs a real POST with the pipeline data as JSON body. **Requires `--approve`** (host CLI flag only — a script can never approve itself; inline `approve=1` is rejected). With `undo_method`/`undo_url`, `undo` issues a real HTTP call (e.g. DELETE the created resource). Feedback includes HTTP status + response.

### read / write / append
`read FILE` · `write FILE` · `append FILE`
- **Sandboxed**: resolved paths must stay inside the working directory (realpath check).
- `write` on a list/dict emits JSON; other data is written as text.
- Feedback: `{"written": N, "path": ..., "diff_vs_previous": ±k}`.
- `append` snapshots prior content for undo; appends one JSON line per list item.

### filter
- `filter FIELD=VALUE` — string equality on dict field.
- `filter FIELD contains TEXT` — substring match on dict field (subject-form; e6a32b1).
- `filter FIELD>NUM` / `>=` / `<` / `<=` — numeric compare; non-numeric/missing values simply don't match (consistent with `=`).
- `filter email ends-with SUFFIX` — suffix match on `email` field.
- Form order: relational → contains → FIELD=VALUE → ends-with; a field literally named e.g. `contains` cannot shadow named forms (kw parse order).
- Legacy fork syntax `contains=STR in=FIELD` is NOT supported upstream — subject-form is the source of truth (e6a32b1, authored Luthfy).
- Input must be a list, else structured error.

### pick
`pick f1[,f2,...]` — project fields; missing fields yield `{}` (no crash).

### map
- `map lower|upper|strip FIELD`
- `map NEWNAME=OLDNAME` — rename (multiple `=` in the token: last segment wins as OLDNAME).

### search
`search TEXT` — returns file paths whose content contains TEXT.

### run
`run CMD [timeout=N]` — argv-only command execution. **Gated**: the host must pass a non-empty `--allow-run=cmd1,cmd2` list; bare `--allow-run` is insufficient. `argv[0]` must match a host-listed command, shell metacharacters are rejected, and an optional in-script `allow=...` list can only narrow the host list. The process gets a minimal environment and a hard timeout (default 15s); output is truncated to 2000 chars. This is **not an OS sandbox**: allowed programs run with AGSH's filesystem and network permissions.

### ask
`ask QUESTION` — human-in-the-loop gate. Without `--answer`: prints `{"needs_human": true, "question": ...}` and exits with code **2**. With `--answer TEXT`: injects the answer and continues.

## CLI flags (informed permission model)

```
agsh.py [--plan] [--allow-run=COMMANDS] [--approve] [--answer TEXT] [--allow-private-net]
        [--allow-llm] [--llm-base URL] [--llm-key KEY] [--llm-model NAME] [--llm-max-input N]
```

- `--plan` — analyze without executing: per-stage risk classification (pure / local-read / local-write / network-read / network-write / exec / human / mutating / network-llm) plus an aggregate verdict listing which approval flags are required.
- `--allow-run=COMMANDS` — allow only listed `argv[0]` commands for `run`; bare `--allow-run` does not authorize execution. This is not OS-level isolation.
- `--approve` — unlock `post` (network writes).
- `--answer TEXT` — supply a human decision to resume past an `ask` gate.
- `--allow-private-net` — unlock `fetch` to private/metadata networks (SSRF guard bypass; use only when intentional).
- `--allow-llm` — unlock the `llm` verb. Endpoint ONLY from host flags/env (`--llm-base` atau `AGSH_LLM_BASE`), script tidak bisa mengaturnya sendiri. Key hanya via host (`--llm-key` / `AGSH_LLM_KEY`), tidak pernah lewat script.

The intended loop: agent runs `--plan`, shows the risk summary to the user, gets a decision, then re-runs with the required flag.

### guard
`guard file exists PATH` — precondition; fails with a recovery hint.

### assert
`assert count OP N` where OP ∈ `> < >= <= ==`. On failure: `{step, message, hint, got}` with a hint when count is 0.

### checkpoint / undo / resume
- `checkpoint` — marks a rollback point in the mutation log.
- `checkpoint save=FILE` — additionally writes a **world-state** file: pipeline data + full snapshot of every tracked file. This is the cross-session state format.
- `undo` — reverts writes since the last checkpoint (restores previous content or deletes created files); `undo checkpoint=N` targets checkpoint N. API calls recorded with `undo_method`/`undo_url` are reverted with a real HTTP call; others log intent.
- `resume FILE` — restores a world-state in a **new session** (recreates/deletes tracked files, restores pipeline data).

### let / take — named state
`let NAME` — bind pipeline data saat ini ke variabel, **data diteruskan** (pass-through, pipeline tetap mengalir). `take NAME` — ganti pipeline data dengan isi variabel. Multi-branch dalam satu eksekusi:
```
read data.json | let users | filter status=active | let active
| read cfg.json | get meta.total
| take active | write active.json
```

### get — nested path access
`get a.b.c` — dict keys dan list indexes (mis. `get users.0.name`). Key/index tidak ada → structured error dengan hint (keys tersedia / panjang list).

### assert (perluasan)
`assert type list|dict|str|num` — kontrak tipe antar-stage; mismatch → structured error dengan hint bentuk output.

- `run CMD` — gated by host `--allow-run=a,b`; the command runs WITHOUT a shell and `argv[0]` must be host-allowed. In-script `allow=a,b` is optional and can only narrow the host list. Shell metacharacters (`; & | < >` and related operators) are rejected. A minimal environment and timeout reduce exposure but do not sandbox the program from the host user's filesystem or network permissions.

## DNS-rebinding defense

`fetch` (network mode) pins the IPs seen at guard time and re-resolves immediately before the request: a mismatch raises `DNS rebinding suspected` — a host that rebinds to a private IP between check and request never gets contacted.

## POST payload validation

`post URL keys=id,name [max_bytes=N]` — validation happens BEFORE any network call: payload must be a dict or list-of-dicts, every item must contain the `keys=` fields (error lists the missing keys and the keys present), and the JSON body is capped at `max_bytes` (default 1 MB). Payload-free posts send `{}` (backward compatible).

## Rate limiting

`fetch` stages are capped at `--max-fetch N` per run (default 20). Exceeding the cap raises a structured error with the count — nothing is silently sent. Host wrapper decides the budget.

## Audit log

`--audit PATH` (or `AGSH_AUDIT` env; the flag wins) — append-only JSONL. One record per stage outcome (and per network resolve):
`{"ts": "<iso8601>", "verb": "...", "risk": "...", "subject": "...", "outcome": "ok"|"error", "cwd": "...", ...extra}`.
Recommended for any shared/production use; the host wrapper should always pass `--audit`.

## File locking

All file mutations (`write`, `append`, `resume` restores) take an advisory `flock` on `<path>.agshlock`. A second process mutating the same file gets a structured `file is locked by another process` error (retry hint included). Stale `.agshlock` files are safe to delete.

### lease — cross-run coordination (backlog #2)
`lease NAME [ttl=SECONDS]` — acquires a flock-backed advisory lease (file `.<name>.lease`, PID + timestamp inside). A second run with the same lease name gets a structured error until the holder's run exits. Stale leases (holder PID dead, age > ttl, default 300s) are stolen automatically. Host `--wait N` retries acquisition for N seconds. Leases are released when the run ends (success or error). Pass-through verb.

### schema — inter-stage shape contract (backlog #4)
`schema SPEC` — declares the expected shape of the incoming pipeline data. SPEC grammar:
`base` | `list[spec]` | `dict[base]` | `dict{k:spec,...}` where base ∈ `str|num|bool|list|dict|any`.
Examples: `schema list[dict]` · `schema list[dict[id:num,name:str]]` · `schema dict[users:list,meta:dict[total:num]]`.
Mismatch → structured error carrying the **exact path** of the first failure (e.g. `type mismatch at [0].n: expected num, got str`) or `missing key at <root>: k`. Pure; `--plan` shows it as a pure stage.

## Parsing contract

- Values containing `=`, `>`, `<`, or `|` **must be quoted**: `filter x="a>b"`.
- `;` is a stage separator equivalent to `|` (multi-stage on one line); `;` inside quotes is safe.
- A token is treated as `key=value` when it contains `=` before any operator sequence (`>=`, `<=`, `==`) — so `assert count>=1` parses as an assertion, not a kwarg.
- Quoted strings are opaque: a `|` inside quotes never splits a stage.
- Inline pipes work with or without spaces: `read f.json|pick n` ≡ `read f.json | pick n`.

## Errors

All failures exit 1 with:

```json
{"error": {"step": "<verb>", "message": "...", "hint": "...?", "got": ...}, "stats": {...}}
```

No tracebacks cross the boundary — consumers are LLMs.

## Exit codes

`0` success · `1` structured error
