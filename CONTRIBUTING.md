# Contributing to RigorLoop

Thanks for considering a contribution. This document covers setup and the
quality gates; the design intent lives in `PLAN.md` and the **hard** coding
rules live in `CODING_STYLE.md` — read that one before writing code, because
its functional-core / imperative-shell rules are enforced in review.

## Setup

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv sync          # creates .venv, installs rigorloop editable + dev tools (locked)
uv run pytest    # sanity check
```

Without uv (pip ≥ 25.1):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e . --group dev
```

Optional but recommended:

```bash
uv run pre-commit install
```

## Quality gates

Everything CI runs is reachable through the `justfile` (or run the underlying
commands directly):

```bash
just lint        # ruff check + ruff format --check
just typecheck   # mypy --strict over src and tests
just test        # pytest with coverage gates: 95% core / 80% overall
just check       # all three
just build       # sdist + wheel
```

A PR must be green on all of them. Strict typing is load-bearing here: the
exhaustive-match and dev/val/test split-type guarantees are enforced by mypy,
so `# type: ignore` needs a very good justification.

## Architecture in one paragraph

`src/rigorloop/core/` is 100% pure — no I/O, no clock, no randomness that
isn't an injected seed, no subprocesses. It must be testable with plain
inputs and asserted outputs, zero mocks. `src/rigorloop/shell/` performs the
effects the core describes (claude subprocess calls, files, sandboxed script
execution) and stays as thin as possible. If your change needs a mock to test
core logic, the boundary is in the wrong place — restructure.

Two invariants are non-negotiable and covered by dedicated tests in
`tests/test_leakage.py`:

1. No validation/test example content ever enters a strategy or executor
   prompt (the agent-context channel).
2. Directives may carry the champion solution's *content* forward — never
   scores, mistakes, or per-example failures.

## Tests

- Core changes: add pure unit tests (and hypothesis property tests where the
  logic is algebraic — splitting, statistics).
- Shell changes: use the stub-CLI / fake-deps patterns in `tests/conftest.py`;
  never call the real `claude` CLI in tests.
- Anything touching the run protocol: extend the fake-agent E2E tests in
  `tests/test_e2e.py`.

## Releases (maintainer)

1. Ensure `main` is green and `CHANGELOG.md` has the new section.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. Approve the `pypi` environment deployment.
4. Verify `pip install rigorloop==X.Y.Z` in a scratch venv.
