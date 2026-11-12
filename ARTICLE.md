# Why your AI agent keeps re-sending 40 tokens of context to do 4 tokens of work

*— and the pipe language we built to stop it*

## The problem nobody prices in

Every tool call an agent makes costs more than the call. It costs a full
round-trip: the model re-reads the conversation, decides the next step, emits
a tool invocation, waits, reads the result, re-decides. A "simple" task —
fetch some JSON, filter it, write a file — becomes 5–8 round-trips, and each
one re-ships the whole growing context.

Agents don't lack programming languages. They lack a **layer**: a way to say
"do this whole data pipeline" in one shot, with the runtime reporting back
*meaning* instead of raw text.

## The idea: a pipe DSL written *by* models, *for* models

AGSH (Agent Shell) is a one-line-per-stage pipe language:

```
fetch https://api.example.com/users | filter active=true | pick name,email | write users.json
```

One tool call. One round-trip. The result comes back as structured JSON with
a delta — not a wall of text the model must re-read.

It sounds naive. It works, and the numbers hold: on comparable tasks,
AGSH scripts cost ~30% of the equivalent Python the model would otherwise
write — and, more importantly, eliminate the round-trips entirely.

## The part people skip: trust

An agent that can `fetch` and `write` is one prompt injection away from a bad
day. So AGSH treats security as a first-class language feature:

- **Plan-first contract** — `--plan` shows every stage with a risk class;
  mutating or network verbs need explicit human approval. The model *cannot*
  self-approve; the gate lives on the host, not in the script.
- **Audit everything** — append-only JSONL of every stage, secrets redacted.
- **Layered safeguards** — path sandbox for file operations and SSRF/DNS-rebinding
  guards; `run` is host-allowlisted and argv-only, but is not an OS sandbox. File
  locks and rate limits add further controls.
- **Structured errors with hints** — failures say *where* and *what to try*,
  including schema mismatches down to `list[0].field`.

## Proof it generalizes

Five external contributors reviewed and patched AGSH in three batches — new
verbs, LLM integration, an MCP server, a cross-model round-trip harness. The
harness's first finding is the interesting one: a small 3B model wrote valid
AGSH **10/10** while a larger model leaked reasoning into its output and
passed only 10/20. Constraint helps small models. That's the thesis in one
data point.

## Try it

- Repo: https://github.com/Bumbu-indomie13/AGSH- (MIT, zero dependencies, Python ≥3.10)
- MCP server included: point any MCP client at `mcp/agsh_mcp.py`
- 127 tests green in CI; the plan-first contract is enforced end-to-end

The future of agent tooling isn't a bigger framework. It's a smaller language
with harder edges.
