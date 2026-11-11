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
`fetch URL [paginate=FIELD]`
- URL must match `https?://`.
- Offline mode: if `fixtures/<host>.json` exists, it is served as the complete dataset (no pagination) instead of the network.
- `paginate=FIELD`: repeatedly appends `FIELD` arrays across `?page=N` until empty (max 100 pages). A failing page after ≥1 success raises a structured error — results are never silently truncated.

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
- `filter email ends-with SUFFIX` — suffix match on `email` field.
- Input must be a list, else structured error.

### pick
`pick f1[,f2,...]` — project fields; missing fields yield `{}` (no crash).

### map
- `map lower|upper|strip FIELD`
- `map NEWNAME=OLDNAME` — rename (multiple `=` in the token: last segment wins as OLDNAME).

### search
`search TEXT` — returns file paths whose content contains TEXT.

### run
`run CMD [timeout=N]` — shell escape hatch. **Gated**: refused unless the host passes `--allow-run`. When allowed, executes restricted: minimal env (`PATH=/usr/bin:/bin`, `HOME`=working dir), hard timeout (default 15s), stdout/stderr truncated to 2000 chars.

### ask
`ask QUESTION` — human-in-the-loop gate. Without `--answer`: prints `{"needs_human": true, "question": ...}` and exits with code **2**. With `--answer TEXT`: injects the answer and continues.

## CLI flags (informed permission model)

```
agsh.py [--plan] [--allow-run] [--approve] [--answer TEXT]
```

- `--plan` — analyze without executing: per-stage risk classification (pure / local-read / local-write / network-read / network-write / exec / human / mutating) plus an aggregate verdict listing which approval flags are required.
- `--allow-run` — unlock the `run` verb (restricted execution).
- `--approve` — unlock `post` (network writes).
- `--answer TEXT` — supply a human decision to resume past an `ask` gate.

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

## Parsing contract

- Values containing `=`, `>`, `<`, or `|` **must be quoted**: `filter x="a>b"`.
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
