# Contributing to AGSH

Thanks for contributing! AGSH is a deliberately **narrow** DSL — every addition
is reviewed against scope, or it becomes a kitchen-sink. Read this first.

## Workflow

1. **Fork & branch** — branch from `main`, one feature per branch/PR.
2. **Small PRs win** — target < ~300 changed lines. Big proposals: open an
   Issue first so we can agree on the design before you write code.
3. **Every verb/flag change needs the full set** (PRs missing any item are
   asked to complete it — this checklist *is* the review):
   - [ ] Implementation as `v_<name>` in `agsh.py` (signature `(self, data, subject, kw)`)
   - [ ] Entry in `RISK` with an honest risk class (`pure` / `mutating` / `network` / `coordination` / `dangerous`)
   - [ ] Section in `SPEC.md` (grammar + semantics + security notes)
   - [ ] Line in `AGENT-GUIDE.md` verb cheat-sheet
   - [ ] Tests in `tests.py` (happy path + at least one negative/abuse case)
   - [ ] `CHANGELOG.md` entry
4. **CI must be green.** We do not merge red.

## Scope rules — what belongs in AGSH

**In scope** — things that reduce agent round-trips or close a security hole:
- verbs that move/filter/verify JSON data between stages
- host-level gates, audit, provenance disclosure, structured errors with hints
- coordination primitives between runs

**Out of scope — will be declined** (the kitchen-sink guard):
- general-purpose programming (loops, functions, classes → use the `run` escape hatch or plain Python)
- verb-specific one-off hacks for a single API (use `header=`/`map=`/fixtures instead)
- shell conveniences that widen the attack surface (`run` stays argv-allowlisted)
- features that cannot be expressed as a `RISK` class + audit record

Rule of thumb: **if a verb cannot say clearly what it risks, it does not go in.**

## Attribution

Contributions are merged with authorship preserved (`git am -3`, bundles, or
`--author` + `Co-authored-by` for manual applies). Your name stays in
`git log` and `CHANGELOG.md`. Do not submit others' work as your own.

## Security reports

Do **not** open a public Issue for exploitable holes. Use GitHub Security
Advisories (Security tab → Report a vulnerability) or contact the owner
directly. Fixes land before disclosure.
