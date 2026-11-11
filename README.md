# AGSH — Agent Shell

A pipe-style DSL designed for AI agents: **token-efficient orchestration that executes outside the LLM.**

Instead of writing Python for every data-plumbing step (and burning tokens + round-trips doing it), an agent writes 3–6 short AGSH stages that a deterministic interpreter runs in **one** execution — returning structured JSON feedback, delta diffs, and undo-able mutations.

Measured with tiktoken `cl100k_base` across 3 equivalent task pairs ([benchmark.py](benchmark.py)): Python **297 tokens** vs AGSH **93 tokens** (~69% reduction), plus 1 LLM round-trip instead of 5+.

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
python3 tests.py   # 54 checks: edge cases, path sandbox (incl. append & resume escapes),
                   # SSRF guard, run allowlist, audit log, file locking,
                   # no-self-approve gate, no-traceback contract, SSRF guard (metadata/RFC1918
                   # blocked by default), fixture-source disclosure, undo semantics, 10k-row scale,
                   # determinism, permission gates, human gating, world-state resume,
                   # real API undo (verified against a live HTTP server)
```

See [SPEC.md](SPEC.md) for the full grammar and [examples/](examples/).

## Related work

| Project | Idea | What AGSH adds |
|---|---|---|
| [smolagents](https://github.com/huggingface/smolagents) (HF) | Agents write actions as Python code (30% fewer steps than JSON tool-calls) | AGSH is a *narrow sub-language* for the 80% orchestration: fewer tokens than full Python, deterministic interpreter, no arbitrary-code risk by default |
| [MAPL](https://github.com/orielhaim/MAPL) | Compact non-Turing-complete program emitted in one call, deterministic runtime | MAPL is a theoretical proposal (special-token encoding); AGSH runs today on stock models, with measured numbers (125→37 tokens) and a verified LLM round-trip (3/3, no fine-tuning) |
| [AgentScript](https://github.com/vinodhalaharvi/agentscript) | S-expression pipeline DSL chaining AI + 100+ commands | AGSH keeps a 16-verb surface optimized for LLM transfer learning (shell/SQL/lodash semantics) and adds runtime feedback primitives |
| [AgentDSL](https://github.com/halidrauf/AgentDSL) | Token-efficient delimiter format for agent-to-agent payloads | AgentDSL saves tokens in *messages*; AGSH saves tokens in *execution* — plus delta-feedback, undo, and permission gates |
| [StepArgsDSL](https://github.com/sika7/StepArgsDSL) | Key-value intermediate representation for tool calls | StepArgsDSL stops at parsing; AGSH executes, verifies (guard/assert), and rolls back |

The combination AGSH offers — **compact programs whose runtime returns delta-feedback, structured recovery hints, and undoable mutations, behind an informed-permission plan** — is not present in any of the above.

## Token benchmark

Reproduce the README claim yourself:

```bash
python3 -m pip install tiktoken
python3 benchmark.py
```

Outputs per-task token counts (CSV) for 3 equivalent task pairs. Measured: Python **297 tokens** vs AGSH **93 tokens** (~69% reduction). Task 3 bonus: AGSH has `undo`; the Python equivalent cannot roll back.

## Status

Prototype (~470 lines, stdlib-only Python). All five "unsolved layers" now have first-class answers: cross-session world-state (`checkpoint save=` / `resume`), real API-call undo (`post ... undo_method=DELETE undo_url=...` verified against a live server), informed permission model (`--plan` risk aggregation + `--allow-run`/`--approve` flags), gated+restricted `run` with optional argv allowlist (`run CMD allow=sort,wc` — no shell, metacharacters rejected), `ask` human-gating (exit code 2), an append-only audit log (`--audit PATH`, JSONL with ts/verb/risk/outcome), advisory file locking (flock on every mutation, structured error on contention), named pipeline state (`let`/`take`), nested access (`get a.b.c`), and inter-stage type contracts (`assert type list`). Not yet: DNS-rebinding pinning, per-verb rate limiting.

## Token benchmark

Reproduce the README claim yourself:

```bash
python3 -m pip install tiktoken
python3 benchmark.py
```

Outputs per-task token counts (CSV) for 3 equivalent task pairs. Measured: Python **231 tokens** vs AGSH **81 tokens** across the 3 tasks (~65