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

The git tag **is** the version: `hatch-vcs` derives it from the latest
`vX.Y.Z` tag, so there is no version number to bump in any file. The only file
you edit for a release is `CHANGELOG.md`; pushing the tag does the rest.

1. Land the change the normal way — branch, PR, `just check` green, add an
   entry under `## [Unreleased]` in `CHANGELOG.md`, merge to `main`.
2. On `main` (`git checkout main && git pull`), promote the changelog: rename
   `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a fresh empty `[Unreleased]`
   above it, commit, and push. The tag will point at this commit, so `main`
   must be green first (branch protection requires `ci-ok`).
3. Tag and push — this triggers `release.yml`:
   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```
4. Approve the `pypi` environment deployment when prompted (the publish job is
   gated on manual approval). The workflow then rebuilds from the tag,
   publishes to PyPI via OIDC Trusted Publishing (no token), and cuts a GitHub
   Release with generated notes and the built artifacts.
5. Verify `pip install rigorloop==X.Y.Z` in a scratch venv, then
   `rigorloop --version`.

Version choice while `0.x`: **patch** (`v0.1.1`) = fixes only; **minor**
(`v0.2.0`) = features or breaking changes (call breakage out in the changelog);
`v1.0.0` when `rigorloop.toml`, the CLI, and the run-directory format are
declared stable.

A version publishes to PyPI exactly once — never re-push or edit a tag. If a
release fails partway, fix forward with a new patch tag rather than reusing the
old one. To rehearse the full pipeline first, push a prerelease tag
(e.g. `v0.2.0rc1`) against TestPyPI.
