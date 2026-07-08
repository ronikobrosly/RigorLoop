---
name: Bug report
about: Something misbehaved
labels: bug
---

## What happened

<!-- What did you run, what did you expect, what happened instead? -->

## Reproduction

- `rigorloop --version`:
- OS:
- `rigorloop.toml` (redact anything sensitive):
- Command and full output:

## Run artifacts

If the failure happened mid-run, the contents of `runs/<run_id>/` (especially
`state.json` and the relevant `loops/<n>/` directory) are the most useful
thing you can attach — minus anything confidential in your examples.
