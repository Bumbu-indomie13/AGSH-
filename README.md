# AGSH — Agent Shell

![CI](https://github.com/Bumbu-indomie13/AGSH-/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/badge/license-MIT-blue)
![Tests](https://img.shields.io/badge/tests-127%20passing-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-informational)
![Dependencies](https://img.shields.io/badge/dependencies-zero-9cf)

**A pipe-style DSL for AI agents: token-efficient orchestration that executes outside the LLM.**

Instead of writing Python for every data-plumbing step (and burning tokens + round-trips doing it), an agent writes a few short AGSH stages that a deterministic interpreter runs in **one** execution — returning structured JSON, delta-feedback, and undo-able mutations.

```
fetch https://api.example.com/users paginate=results
| filter status=active
| filter email ends-with @company.com
| write targets.json
| assert count>0
```

> **1 LLM round-trip. ~69% fewer tokens than Python. Zero dependencies.**

---

## Why AGSH

| Without AGSH | With AGSH |
|---|---|
| 5+ tool calls, each re-sending context | **1 tool call** = 1 whole pipeline |
| ~297 tokens per equivalent Python script | **~93 tokens** (measured, [benchmark.py](benchmark.py)) |
| Model re-reads files to verify writes | **Delta-feedback**: `written: N, diff_vs_previous: ±k` |
| Tracebacks the model must debug | **Structured errors**: `{step, message, hint, got}` |
| Mutations are irreversible | **Undo + checkpoints**, byte-exact, incl. real API DELETE |
| Security = hoping the model behaves | **Runtime-enforced**: sandbox, SSRF guard, no self-approve |

Errors are JSON, never tracebacks — built for machine recovery:

```json
{"error": {"step": "assert", "message": "count 0 not > 0",
           "hint": "filter returned 0 — check field names exist", "got": 0}}
```

## Quick start

```bash
printf 'fetch https://api.example.com/users paginate=results
| filter status=active
| write targets.json
| assert count>0
' | python3 agsh.py
```

```json
{"result": {"written": 4, "path": "targets.json", "diff_vs_previous": null},
 "stats": {"round_trips": 1, "verbs": 4, "fetch_sources": ["network:api.example.com"]}}
```

Analyze before executing (informed permission):

```bash
printf '<pipeline>' | python3 agsh.py --plan
```

```json
{"stages": [{"verb": "fetch", "risk": "network-read"}, ...],
 "verdict": "needs approval: --approve"}
```

## Verbs (30)

| Category | Verbs |
|---|---|
| **Data in/out** | `fetch` · `post` · `read` · `write` · `append` · `lines` |
| **Transform** | `filter` (`F=V`, `F>N`, `F contains X`, `F == V`, ends-with) · `pick` · `map` · `regex` · `get a.b.c` · `let` / `take` |
| **Compose** | `sort` · `limit` · `head` · `tail` · `uniq` · `group` · `count` · `join` · `summarize` |
| **Verify** | `guard file exists` · `assert count>N` · `assert type list` · `schema SPEC` (path-precise shape contracts) |
| **Mutate safely** | `checkpoint` · `undo` · `resume` — file writes flock-locked, API undo = real HTTP DELETE |
| **Coordinate** | `lease NAME` — cross-run advisory lease, stale-lease stealing, host `--wait` |
| **Escape hatch** | `run CMD` (gated, restricted env, argv allowlist) · `search` |
| **Human/LLM** | `ask` (exit 2 human gate) · `llm "PROMPT"` (gated `--allow-llm`) |

Full grammar: [SPEC.md](SPEC.md) · Agent integration guide: [AGENT-GUIDE.md](AGENT-GUIDE.md) · Examples: [examples/](examples/)

## Security model

All enforced by the **runtime**, not by model obedience:

- **Plan first** — `--plan` classifies risk per stage and names required flags; approval happens only at the host, a script can never approve itself
- **Path sandbox** — every file op stays inside the working dir (realpath + symlink-safe)
- **SSRF guard + DNS pinning** — private/metadata IPs blocked by default; re-resolve must match the guard-time pin
- **Payload validation** — `post` checks required keys & size *before* any network call
- **Rate limits** — `--max-fetch` caps fetches per run
- **Audit log** — `--audit PATH` (or `AGSH_AUDIT` env): append-only JSONL of every stage outcome
- **No-shell `run`** — `run CMD allow=a,b` or host policy `--allow-run=a,b`: argv-only, metacharacters rejected
- **File locking** — advisory flock on every mutation; concurrent agents get a structured error
- **Bounded retries** — `retries=0..3`, validated before any DNS work

## Testing

```bash
python3 tests.py              # 70 checks: sandbox escapes, SSRF, DNS pin, undo, scale, determinism...
for t in test_*.py; do python3 $t.py; done   # +46 feature checks + unittest suite (auth-env, record/replay,
                                             # plan-redact, audit-all, relational filter, llm verbs)
```

CI runs all 7 files on every push, including a live HTTP undo test and an OpenAI-compatible LLM mock.

## Token benchmark

```bash
pip install tiktoken && python3 benchmark.py
```

3 equivalent task pairs, tiktoken `cl100k_base`: **Python 297 → AGSH 93 tokens (~69%)**. Task 3 bonus: AGSH has `undo`; the Python equivalent cannot roll back.

## Related work

| Project | Idea | What AGSH adds |
|---|---|---|
| [smolagents](https://github.com/huggingface/smolagents) | Code-as-action (30% fewer steps) | Narrow sub-language for the 80% orchestration — fewer tokens, deterministic, no arbitrary-code risk by default |
| [MAPL](https://github.com/orielhaim/MAPL) | Non-Turing-complete one-call programs | Runs today on stock models; measured numbers; verified LLM round-trip (3/3, no fine-tuning) |
| [AgentScript](https://github.com/vinodhalaharvi/agentscript) | S-expression AI pipelines | 30-verb surface with shell/SQL/lodash semantics + runtime feedback primitives |
| [AgentDSL](https://github.com/halidrauf/AgentDSL) | Token-efficient message format | Saves tokens in *execution* — delta-feedback, undo, permission gates |
| [StepArgsDSL](https://github.com/sika7/StepArgsDSL) | KV IR for tool calls | Executes, verifies (guard/assert), and rolls back |

**The combination AGSH offers — compact programs whose runtime returns delta-feedback, structured recovery hints, and undoable mutations, behind an informed-permission plan — is not present in any of the above.**

## Using it with an agent

Wrap the interpreter as one tool (`agsh_execute(script, flags, plan_first=True)`) and give the agent [AGENT-GUIDE.md](AGENT-GUIDE.md) — it covers the plan→approve→execute loop, the flag rules ("the model never adds approval flags itself"), and the verb cheat-sheet. A working example lives in the guide (§10).

## Status

~500 lines, stdlib-only Python, interpreter + 7 test files, all security backlogs closed. See [CHANGELOG.md](CHANGELOG.md) for the full history.

MIT — contributions welcome. Open a PR or send a patch series; `git am` merges preserve your authorship.
