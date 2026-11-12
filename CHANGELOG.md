# Changelog

All notable changes to AGSH. Dates are 2026 (project started Nov 2026).

## [0.7] — 2026-11-12

### Added
- **COMPARISON.md** (backlog #9 eval): landscape comparison vs orchestration DSLs (Dhenara DAD, AgenticPipeline, AgentScript, AgentForge YAML) across 3 categories + honest feature matrix
- `when COND` conditional stage-skip verb (gap found in DSL evaluation): same grammar as `assert`, false condition skips remaining stages/lines with per-stage audit + `stats.skipped_stages`, later `when` re-enables; pure, +5 tests
- **POSITIONING.md** (backlog #9): honest head-to-head vs code-as-action — `bench/bench_vs_code.py` executes the same 6 tasks both ways for real: **AGSH 127 tok vs Python 359 tok (2.83x), 6 vs 12 round-trips, 6/6 exec+verify both sides**; includes what Python genuinely wins and fair-comparison rules
- **Official MCP server** `mcp/agsh_mcp.py` (backlog #5): zero-dependency stdio JSON-RPC server exposing `agsh_execute` with the plan-first approval contract baked in; live-tested (initialize/tools/list/tools/call, pure plan vs gated `run`)
- **Governance pack** (backlog #8): CONTRIBUTING.md (PR workflow, kitchen-sink scope rules, verb checklist, attribution, security-report policy), CODEOWNERS, PR template
- ARTICLE.md — adoption essay ("Why your AI agent keeps re-sending 40 tokens…")
- `lease NAME [ttl=S]` cross-run coordination verb (backlog #2): flock-backed advisory lease, structured contention error, stale-lease stealing (dead PID past ttl), host `--wait N` retry window; released at run end (success or error)
- `schema SPEC` inter-stage shape contracts (backlog #4): recursive spec grammar (`list[dict[id:num,name:str]]`), errors carry the exact path of the first mismatch
- unittest suite in CI (8th suite); `header=NAME:VALUE` documented on fetch/post

### Fixed
- `limit` verb was documented but never implemented — now alias of `head N` (caught in real-use sanity probe)

## [0.5] — 2026-11-12

### Added
- `;` as a stage separator (equivalent to `|`); quoted `;` is safe (review 3 #13)
- `AGSH_AUDIT` env var enables the audit log without `--audit` (review 3 #6)
- Host-level run policy `--allow-run=cmd1,cmd2`: restricts `run` to plain-argv commands even without in-script `allow=` (review 3 #8)
- `filter FIELD == VALUE` equality form; parse_stage operator-shadowing reorder (DeryClaw)
- `llm` / `summarize` / `lines` verbs (Dery): in-pipeline OpenAI-compatible LLM calls, gated `--allow-llm` + host-only endpoint config; deterministic head+tail compression
- `auth=ENVVAR` on fetch/post: Bearer token from env, secret REDACTED in audit (Putra)
- `record=C.jsonl` / `replay=C.jsonl` fetch cache: deterministic offline replays, corrupt lines skipped (Putra)
- `retries=0..3` on fetch, fail-fast validated, linear backoff (Putra)
- `--plan` subject redaction: basic-auth passwords & Bearer literals scrubbed (Putra)
- `pyproject.toml` (ramudera13)
- CHANGELOG.md; README rewritten (review 3 #10)

### Changed
- All-stage audit is single-point: intercept in `execute_line` (with `out=N` row counts) is the only audit source for pure verbs — duplicates eliminated
- CI runs all 7 test files; 114 checks green

### Security
- SSRF/DNS-rebinding guard extended to `post` and `undo_url` via `_pin_host` (ramudera13)

## [0.4] — 2026-11-12

### Added
- Relational filter forms: `filter FIELD>N` (also `>= < <=`) and `filter FIELD contains TEXT` (Luthfy)
- 9 composition verbs: `sort`, `limit`, `head`, `tail`, `uniq`, `group`, `count`, `regex`, `join` (Alfayn/DeryClaw)
- Real-use hardening: `let` is pass-through (bind + forward data); default User-Agent + `ua=` kwarg on fetch/post

## [0.3] — 2026-11-11

### Added
- `run CMD allow=a,b`: no-shell argv allowlist mode, metacharacters rejected (backlog #1)
- `--audit PATH`: append-only JSONL audit log — every stage outcome + network resolve (backlog #2)
- Advisory flock file locking on all mutations (backlog #3)
- DNS-rebinding pin: re-resolve must match guard-time pin before any request (backlog #4)
- `post` payload validation before network: `keys=` + `max_bytes=` (backlog #5)
- `--max-fetch N` rate limit, default 20/run (backlog #6)
- Semantic layer: `let`/`take` named state, `get a.b.c` nested access, `assert type`
- AGENT-GUIDE.md: tool-wrap pattern (§10) and verb-authoring checklist (§11)

## [0.2] — 2026-11-11

### Added
- Permission model: `--plan` risk aggregation, `--allow-run` / `--approve` host flags, no self-approve
- Real API undo: `post ... undo_method=DELETE undo_url=...` (verified against a live HTTP server)
- `checkpoint save=` world-state / `resume` across sessions; `ask` human gate (exit 2)
- Structured errors with `{step, message, hint, got}` — no tracebacks cross the boundary
- Delta-feedback on `write` (`written`, `diff_vs_previous`)
- Offline fixtures mode with source disclosure (`stats.fetch_sources`)
- Related-work section; benchmark.py (tiktoken cl100k_base)

### Security (external review round 1 — Luthfy)
- Fixed sandbox escape in `append` (missing `_check_path`)
- Fixed `resume` arbitrary-file-write (paths validated, stored relative)
- Removed inline `approve=1`; approval only via host flags
- Catch-all exception boundary — no tracebacks

## [0.1] — 2026-11-11

- Initial prototype: 16-verb pipe DSL, deterministic interpreter, token benchmark (125→37), LLM round-trip verified (3/3 without fine-tuning)
