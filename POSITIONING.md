# AGSH vs code-as-action (smolagents-style): the honest positioning

Backlog #9. This document exists because the claim "narrow DSL beats full
Python for agent data tasks" is repeated a lot and proven rarely. Below is
what we can actually demonstrate, what Python genuinely wins at, and where
the boundary sits.

## The measured claim

`bench/bench_vs_code.py` runs the **same six tasks** through both artifacts
for real (not estimated): a one-tool-call AGSH pipeline vs a one-tool-call
Python program (the code-as-action pattern: the model writes and executes
code). Measured with tiktoken `cl100k_base`, tiktoken fallback disclosed:

| task | AGSH tok | Python tok | ratio | round-trips A / P |
|---|---|---|---|---|
| fetch-filter-write | 23 | 59 | 2.57x | 1 / 2 |
| local-transform-chain | 26 | 55 | 2.12x | 1 / 2 |
| schema-guarded-write | 25 | 73 | 2.92x | 1 / 2 |
| checkpoint-undo-write | 24 | 56 | 2.33x | 1 / 2 |
| cross-file-join | 18 | 79 | 4.39x | 1 / 2 |
| deterministic-summarize | 11 | 37 | 3.36x | 1 / 2 |
| **TOTAL** | **127** | **359** | **2.83x** | **6 / 12** |

Reading:
- **Token ratio ~2.8x** in AGSH's favor — the model emits a declarative
  pipeline instead of a whole program with imports, plumbing, and error
  handling it will get wrong anyway.
- **Round-trips: the bigger win.** AGSH's delta-feedback is structured and
  trustworthy ("written: 1, path: …", audit records, `out=N`), so the agent
  does not need a verification re-read. Python's `print` output is opaque
  text; a careful agent loop reads back to confirm — 2 turns per task.
- Combine them: for these six tasks an agent spends **~127 emitted tokens
  and 6 turns** with AGSH vs **~359 tokens and 12 turns** with code-as-action.

## The structural claims (not measurable by tokens)

1. **The gate is scannable.** A 5-line AGSH plan lists every network and
   mutating stage with a risk class *before* anything runs. Reviewing 40
   lines of generated Python for hidden `rm`, `eval`, or data exfiltration
   is a different (and unsolved) problem. Code-as-action gives the model a
   Turing-complete attack surface; AGSH gives the host a manifest.
2. **Audit is structural.** Every stage lands in an append-only JSONL with
   secrets redacted. In code-as-action this exists only if the framework
   builds it, for code the model wrote ad hoc.
3. **Constraint helps small models.** Our round-trip harness (backlog #6/#7,
   `bench/bench_roundtrip.py`) found a 3B model produced parse-valid AGSH
   10/10 while a larger model leaked reasoning into output (10/20). A narrow
   grammar with structured errors is *easier* to comply with than a full
   language — this matters as agents move to smaller local models.
4. **Determinism.** Same script + same inputs = same behavior (replay cache,
   fixture disclosure). Generated Python has the same property in principle,
   but nothing enforces the discipline.

## What Python genuinely wins (we lose, on purpose)

- **Expressiveness.** Cross-file joins with business logic, statistical
  transforms, anything non-tabular: code is the right tool. AGSH's answer is
  the `run` escape hatch — at which point you are writing Python anyway, just
  with a gate and audit around it. We consider that the correct composition,
  not a defeat.
- **Familiarity and ecosystem.** Every developer and every model already
  knows Python. AGSH must be learned from AGENT-GUIDE.md. Adoption cost is
  our real competitor, not smolagents.
- **No DSL lock-in.** Code-as-action artifacts are portable across frameworks;
  AGSH pipelines depend on our interpreter (mitigated by it being one
  ~600-line stdlib-only file).

## Fair-comparison rules we hold ourselves to

1. Both artifacts **execute**; nothing is estimated.
2. Round-trip counts come from a stated loop policy, printed with the results.
3. Token counter is disclosed (cl100k_base here; per-model counting is
   backlog #7 territory).
4. Tasks where Python is clearly better belong in the benchmark too. If AGSH
   only benchmarks its own sweet spots, the number is marketing, not data.

## One-line position

> Code-as-action optimizes for the developer reviewing the code. AGSH
> optimizes for the agent paying in tokens and round-trips — and for the
> human who has to trust what the agent did. The data says both costs drop
> ~2.8x/2x; the structural argument (scannable plan, structural audit,
> small-model compliance) is the part tokens can't price.
