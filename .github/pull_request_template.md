Thanks for the PR! Please confirm:

## Checklist (required for verb/flag changes)
- [ ] `v_<name>` implementation in `agsh.py`, signature `(self, data, subject, kw)`
- [ ] `RISK` entry with honest risk class
- [ ] `SPEC.md` section (grammar + semantics + security notes)
- [ ] `AGENT-GUIDE.md` cheat-sheet line
- [ ] Tests (happy path + negative/abuse case)
- [ ] `CHANGELOG.md` entry
- [ ] CI green

## Scope
- Does this reduce agent round-trips or close a security hole?
- Could it be expressed with existing verbs + the `run` escape hatch?

## What changed
<!-- 1-3 sentences -->
