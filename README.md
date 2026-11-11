# AGSH — Agent Shell

A pipe-style DSL designed for AI agents: **token-efficient orchestration that executes outside the LLM.**

Instead of writing Python for every data-plumbing step (and burning tokens + round-trips doing it), an agent writes 3–6 short AGSH stages that a deterministic interpreter runs in **one** execution — returning structured JSON feedback, delta diffs, and undo-able mutations.

Measured with tiktoken `cl100k_base`: equivalent Python **125 tokens** vs AGSH **37 tokens** (~70% reduction), plus 1 LLM round-trip instead of 5+.

## Quick start

```bash
printf 'fetch https://api.example.com/users paginate=results\n| filter status=active\n| filter email ends-with @company.com\n| write targets.json\n| assert count>0\n' | python3 agsh.py
```

Output (JSON only):

```json
{"result": {"written": 4, "path": "targets.json", "diff_vs_previous": null},
 "stats": {"round_trips": 1, "lines": 0, "verbs": 5}}
```

Errors are structured, not tracebacks — built for machine recovery:

```json
{"error": {"step": "assert", "message": "count 0 not > 0",
           "hint": "filter returned 0 — check field names exist", "got": 0}}
```

## Verbs

| Verb | Form | Notes |
|---|---|---|
| `fetch` | `fetch URL paginate=FIELD` | auto-pagination, JSON; offline fixtures supported |
| `post` | `post URL` | logs undo intent |
| `read` / `write` / `append` | `read FILE` | path-sandboxed to working dir |
| `filter` | `filter FIELD=VALUE` · `filter email ends-with X` | |
| `pick` | `pick f1,f2` | project fields |
| `map` | `map lower\|upper\|strip FIELD` · `map NEW=OLD` | |
| `search` | `search TEXT` | grep working dir |
| `run` | `run CMD` | escape hatch (unsandboxed — use deliberately) |
| `guard` | `guard file exists PATH` | precondition |
| `assert` | `assert count > N` | postcondition with recovery hints |
| `checkpoint` / `undo` | `checkpoint` · `undo [checkpoint=N]` | revert mutations |

## Design principles

1. **Verb-first, typed** — declarative stages, JSON end-to-end
2. **Delta-feedback** — `write` reports `written: N, diff_vs_previous: ±k`; the model never re-reads to verify
3. **One round-trip** — a whole pipeline executes in a single tool call
4. **Undoable mutations** — file writes snapshot previous content; `undo` rolls back since a checkpoint
5. **Escape hatch** — complex logic stays in Python; AGSH covers the 80% orchestration

## Testing

```bash
python3 tests.py   # 25 checks: edge cases, path sandbox, undo semantics, 10k-row scale,
                   # determinism, plan/permission gates, human gating, world-state resume,
                   # real API undo (verified against a live HTTP server)
```

See [SPEC.md](SPEC.md) for the full grammar and [examples/](examples/).

## Status

Prototype (~450 lines, stdlib-only Python). All five "unsolved layers" now have first-class answers: cross-session world-state (`checkpoint save=` / `resume`), real API-call undo (`post ... undo_method=DELETE undo_url=...` verified against a live server), informed permission model (`--plan` risk aggregation + `--allow-run`/`--approve` flags), gated+restricted `run`, and `ask` human-gating (exit code 2). Not yet: multi-user audit log, per-verb rate limiting.
