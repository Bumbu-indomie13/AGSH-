# AGSH vs other agent DSLs — landscape comparison (backlog #9 follow-up)

Where AGSH sits among the agent-DSL crowd, what we lose to each, and what we
changed after evaluating. Sources listed so the reader can check us.

## The landscape (three different categories)

"Agent DSL" means at least three different things. Comparing across
categories honestly matters more than a single scoreboard.

### 1. Orchestration DSLs (declare *agent workflows*)
- **Dhenara Agent DSL (DAD)** — Python-package DSL for multi-agent workflows,
  type-safe, observability-focused (github.com/dhenara/dhenara-agent)
- **AgenticPipeline DSL (Daunis, Dec 2025)** — IR-driven, multi-backend,
  branching/parallel composition in <50 lines
- **Salesforce AgentScript** — indentation DSL for Agentforce: topics, typed
  variables, guardrails (arXiv 2611.13183, execution-graded 100-task benchmark)
- **RubyLLM Agent DSL** — message-role blocks (`system`/`user`/`assistant`)
  for LLM prompt flows

### 2. Declarative config DSLs (declare *agent definitions*)
- **AgentForge YAML DSL** (arXiv 2601.13383) — YAML agent specs; measured
  8–15% fewer tokens than LangChain, smolagents listed as lightweight rival
- **ADL/MICA**, Nanosyntax — YAML/logic agent schemas

### 3. Pipeline/data DSLs (declare *what the agent does to data*) ← **AGSH here**
- AGSH: one tool call executes a whole data pipeline; plan-first gate; audit;
  structured errors. The model is the *author* of the program, at runtime,
  per task — none of the above category-1/2 systems are designed for that.

## Feature matrix (honest)

| capability | AGSH | DAD/AgenticPipeline | AgentForge YAML | code-as-action |
|---|---|---|---|---|
| author = the LLM at runtime | **yes** | no (developer) | no (developer) | yes |
| token cost per task | **~2.8x lower than Python** (measured) | n/a (not per-task) | low for config | baseline |
| round-trips per data task | **1** | n/a | n/a | 2+ |
| branching / conditionals | `when` (stage-skip; added after this eval) | **full** (parallel, fan-out) | partial | **full** |
| iteration / loops | ✗ (by design — `run` escape hatch) | **full** (forEach) | partial | **full** |
| security gate before exec | **plan-first + RISK classes** | partial (guardrails in AgentScript) | basic | ✗ (review generated code) |
| audit trail | **structural JSONL, redacted** | observability features | logging | framework-dependent |
| multi-backend portability | one stdlib interpreter | **yes (IR, 3 languages)** | Python | any Python host |
| ecosystem/familiarity | ✗ (must learn) | Python-native | YAML-native | **universal** |

## What we lost, and what we changed because of it

**Lost: control flow.** Every orchestration DSL has branching; AGSH had
none — a pipeline whose filter returns empty still ran its `write`, or died
on `assert`. **Fixed**: added `when COND` (same grammar as `assert`:
`count OP N` | `type T`). False condition skips the remaining stages and
lines (each skipped stage audited, `stats.skipped_stages` reported), a later
`when` re-evaluates and can re-enable. Pure, +5 tests, 132 checks green.
This closes the most-used primitive we lacked while staying far from
Turing-complete.

**Deliberately not added: loops and parallelism.** Evaluated and declined —
iteration inside the DSL is the on-ramp to kitchen-sink (the CONTRIBUTING
scope rule exists for exactly this). The agent loop *is* the iteration;
each AGSH call is now cheap enough that N calls is fine. Revisit only if
real usage shows an agent burning round-trips on pure data loops.

**Lost: portability and familiarity.** DAD compiles to a JSON IR across
Java/Python/Go; YAML DSLs need no learning. AGSH is one interpreter and a
grammar to learn. Mitigation already shipped: the MCP server makes adoption
a config line, and AGENT-GUIDE is written for the model, not the human.
This remains our weakest flank; we say so in POSITIONING.md.

## Where to check our claims
- Token/round-trip numbers: `bench/bench_vs_code.py` (executes both sides)
- Cross-model parse compliance: `bench/bench_roundtrip.py`
- All numbers reproduced from the repo at the HEAD that carries this file.
