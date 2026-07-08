# RigorLoop Packaging & Release Plan

How RigorLoop will be packaged as an open-source Python package, tested and
gated in CI via GitHub Actions, and published directly to PyPI. This document
complements `PLAN.md` (which covers the software itself) and inherits its
decisions: Python ≥ 3.12, `src/` layout, stdlib-only runtime dependencies,
console script `rigorloop`, and a `pytest` + `ruff` + `mypy --strict` quality
gate.

Verified starting facts: the repo is MIT-licensed (LICENSE, 2026, Roni
Kobrosly) and the name `rigorloop` is currently **unclaimed on PyPI**
(`pypi.org/pypi/rigorloop/json` returns 404). Claiming the name early — via a
first `0.0.1` release — is step one of the rollout (§9).

---

## 1. Distribution model at a glance

| Concern | Choice | Rationale |
|---|---|---|
| Package name | `rigorloop` | Matches repo and CLI; available on PyPI. |
| Build backend | `hatchling` | Modern, zero-config for `src/` layouts, wide adoption, plays well with dynamic versioning. |
| Versioning | Dynamic from git tags via `hatch-vcs` | One source of truth (the `vX.Y.Z` tag), no version-bump commits; the tag that triggers a release *is* the version. |
| Version scheme | SemVer, starting `0.x` | `0.x` signals pre-stable API; `1.0.0` when the config format and CLI stabilize. |
| Runtime deps | **None** (stdlib only) | Per `PLAN.md`; makes the wheel trivially installable and keeps supply-chain surface minimal. |
| Dev workflow tool | `uv` with a committed `uv.lock` | Fast, reproducible dev/CI environments; plain `pip` remains supported (§3). |
| Dev deps | PEP 735 `[dependency-groups]` in `pyproject.toml` | The standard successor to `requirements-dev.txt`; understood by `uv` and `pip ≥ 25.1`. No separate requirements files needed. |
| Publishing | PyPI **Trusted Publishing** (OIDC) via `pypa/gh-action-pypi-publish` | No long-lived API tokens in repo secrets; publish rights are bound to a specific repo + workflow + environment. |
| Artifacts | sdist + pure-Python wheel (`py3-none-any`) | No compiled code; a single universal wheel. |
| Typing | `py.typed` marker shipped in the wheel | The package is strictly typed; downstream users get full type information. |
| License metadata | SPDX `license = "MIT"` + `license-files` | Current PEP 639 style. |

## 2. `pyproject.toml` (the single packaging file)

Everything — metadata, build config, dev dependencies, and tool configuration —
lives in `pyproject.toml`. No `setup.py`, `setup.cfg`, `requirements*.txt`,
`mypy.ini`, `pytest.ini`, or `.ruff.toml`.

```toml
[build-system]
requires      = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"

[project]
name            = "rigorloop"
dynamic         = ["version"]
description     = "Statistically-sound agentic build framework: dev/val/test-split loops that produce code artifacts without overfitting."
readme          = "README.md"
license         = "MIT"
license-files   = ["LICENSE"]
authors         = [{ name = "Roni Kobrosly", email = "roni.kobrosly@gmail.com" }]
requires-python = ">=3.12"
dependencies    = []                    # stdlib-only, by design
keywords        = ["agents", "llm", "evaluation", "claude", "data-science"]
classifiers = [
  "Development Status :: 3 - Alpha",
  "Intended Audience :: Developers",
  "Intended Audience :: Science/Research",
  "Operating System :: OS Independent",
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "Programming Language :: Python :: 3.14",
  "Topic :: Software Development :: Code Generators",
  "Typing :: Typed",
]

[project.urls]
Homepage  = "https://github.com/ronikobrosly/RigorLoop"
Issues    = "https://github.com/ronikobrosly/RigorLoop/issues"
Changelog = "https://github.com/ronikobrosly/RigorLoop/blob/main/CHANGELOG.md"

[project.scripts]
rigorloop = "rigorloop.shell.cli:main"

[tool.hatch.version]
source = "vcs"                          # version = latest vX.Y.Z git tag

[tool.hatch.build.hooks.vcs]
version-file = "src/rigorloop/_version.py"   # generated; git-ignored

# ---- development dependencies (PEP 735) ----
[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-cov>=5",
  "hypothesis>=6",        # property-style tests for splitting/scoring math
  "mypy>=1.16",           # the exhaustive-match error code (§4) needs ≥1.16
  "ruff>=0.8",
  "pre-commit>=4",
]
```

Runtime version lookup uses `importlib.metadata.version("rigorloop")` (for
`rigorloop --version`), with the generated `_version.py` as the in-repo
fallback for editable installs.

## 3. Environments and "requirements files"

There are deliberately **no `requirements*.txt` files**. The complete story:

- **Runtime:** `dependencies = []`. Nothing to pin.
- **Development:** the `dev` dependency group above, resolved and locked in a
  committed **`uv.lock`**. Setup is one command: `uv sync` (creates `.venv`,
  installs the package editable plus the dev group, honoring the lock).
- **Without uv:** `pip install -e . --group dev` (pip ≥ 25.1) — same group,
  unlocked resolution. Documented in `CONTRIBUTING.md` as the fallback path.
- **CI:** uses `uv sync --locked`, which fails if `uv.lock` is stale — so CI
  always runs the exact locked toolchain and the lock can't silently drift.

Tool-version pinning for reproducibility lives in exactly two places:
`uv.lock` (Python tools) and `.pre-commit-config.yaml` (hook revisions).
Dependabot keeps both fresh (§6).

## 4. Development tools and their configuration

All tool config in `pyproject.toml`:

| Tool | Role | Key configuration |
|---|---|---|
| **ruff** (lint) | Correctness + style lints, import sorting | `target-version = "py312"`; rule sets: `E,W,F` (pycodestyle/pyflakes), `I` (isort), `UP` (pyupgrade), `B` (bugbear), `C4` (comprehensions), `SIM` (simplify), `RUF`. Notably `T20` (no stray `print`) with a per-file ignore for `src/rigorloop/shell/**` — printing is a shell effect, forbidden only in the core. |
| **ruff format** | Formatting (Black-compatible) | Defaults; line length 100. |
| **mypy** | Strict static typing — load-bearing per `PLAN.md` (exhaustive `match`, split-type leakage guards) | `strict = true`, `warn_unreachable = true`, `enable_error_code = ["exhaustive-match", "ignore-without-code"]`; zero untyped code, including tests. |
| **pytest** | Test runner | `testpaths = ["tests"]`; markers `unit`, `integration`, `e2e`; `addopts = "--strict-markers"`. |
| **pytest-cov / coverage** | Coverage measurement | `source = ["rigorloop"]`, branch coverage on; **fail-under gates: 95% for `src/rigorloop/core/`** (pure functions have no excuse), 80% overall. Reported in CI job summary; no external coverage service initially. |
| **hypothesis** | Property-based tests | For `dataset_calcs` invariants (disjoint/exhaustive/deterministic splits) and `scoring_calcs` math (CI bounds, aggregation identities). |
| **pre-commit** | Local fast gate mirroring CI | Hooks: `ruff check --fix`, `ruff format`, `mypy` (via local hook running `uv run mypy`), plus hygiene hooks (end-of-file, trailing whitespace, TOML/YAML syntax). |

A `justfile` (or `Makefile` — pick one, `justfile` preferred) gives memorable
entry points that match CI exactly: `just lint`, `just typecheck`, `just test`,
`just check` (all three), `just build`.

## 5. Repository additions for open source

```
.
├── pyproject.toml
├── uv.lock
├── justfile
├── .pre-commit-config.yaml
├── CHANGELOG.md                  # Keep a Changelog format; updated per release
├── CONTRIBUTING.md               # setup (uv + pip paths), quality gates,
│                                 # pointer to CODING_STYLE.md as hard rules
├── SECURITY.md                   # supported versions + private reporting via
│                                 # GitHub security advisories; note that
│                                 # RigorLoop executes generated code (PLAN §7)
├── .github/
│   ├── workflows/
│   │   ├── ci.yml
│   │   └── release.yml
│   ├── dependabot.yml
│   ├── ISSUE_TEMPLATE/{bug_report.md, feature_request.md}
│   └── PULL_REQUEST_TEMPLATE.md
└── src/rigorloop/
    ├── py.typed                  # ships in the wheel
    └── _version.py               # generated by hatch-vcs; in .gitignore
```

README gets the standard badge row (CI status, PyPI version, Python versions,
license) once the first release is out.

## 6. CI workflow — `.github/workflows/ci.yml`

Triggers: every PR, and pushes to `main`. Concurrency group cancels superseded
runs on the same ref. All jobs pin actions to major versions and set
`permissions: contents: read` at the workflow level.

Jobs:

1. **lint** (ubuntu, Python 3.12): `uv sync --locked` →
   `ruff check .` → `ruff format --check .`.
2. **typecheck** (ubuntu, 3.12): `mypy src tests` (strict).
3. **test** — matrix: `python: [3.12, 3.13, 3.14]` × `os: [ubuntu-latest,
   macos-latest]` (the shell drives subprocesses, so POSIX coverage on both;
   Windows is explicitly unsupported in v1 — `claude` CLI and sandboxing
   assumptions are POSIX). Runs `pytest --cov` with coverage gates; uploads
   the coverage summary to the job summary. Uses `astral-sh/setup-uv` with
   built-in cache.
4. **build** (ubuntu, 3.12): `uv build` (sdist + wheel) → `twine check
   --strict dist/*` → install the built wheel into a fresh venv and run
   `rigorloop --version` and `python -c "import rigorloop"` — catches
   packaging mistakes (missing `py.typed`, broken entry point, bad metadata)
   on every PR, not at release time. Uploads `dist/` as a workflow artifact.
5. **ci-ok** — a single no-op job that `needs:` all of the above; the one
   required status check for branch protection (matrix-proof).

Notes:

- `actions/checkout` with `fetch-depth: 0` everywhere — `hatch-vcs` needs tag
  history to compute the version.
- No `claude` CLI in CI: unit/integration/E2E tests use the stub CLI and fake
  agent function from `PLAN.md` §11. Live smoke tests against real `claude`
  stay local/manual.

## 7. Release workflow — `.github/workflows/release.yml`

Trigger: push of a tag matching `v[0-9]+.[0-9]+.[0-9]+*` (e.g. `v0.3.0`).

```
jobs:
  build:            # rebuild from the tag; never reuse CI artifacts
    - uv build
    - twine check --strict dist/*
    - upload dist/ as artifact

  publish-pypi:
    needs: build
    environment: pypi                # GitHub environment, protection rules on
    permissions:
      id-token: write                # OIDC for Trusted Publishing — no token
    steps:
      - download dist/ artifact
      - uses: pypa/gh-action-pypi-publish@release/v1

  github-release:
    needs: publish-pypi
    permissions:
      contents: write
    steps:
      - create GitHub Release from the tag with auto-generated notes
        plus the matching CHANGELOG.md section; attach dist/*
```

Security posture:

- **Trusted Publishing** (OIDC): on PyPI, register the publisher as repo
  `ronikobrosly/RigorLoop`, workflow `release.yml`, environment `pypi`. No
  `PYPI_API_TOKEN` secret ever exists.
- The `pypi` GitHub environment carries protection rules (required reviewer:
  the maintainer) so a pushed tag alone can't publish without a human click —
  cheap insurance for a solo-maintainer project.
- Publish job contains *only* download-and-publish; it never runs the test
  suite or arbitrary project code with the OIDC token available.
- Tags are pushed by the maintainer; branch protection on `main` requires the
  `ci-ok` check, so anything tagged has passed CI.

Optional (recommended) pre-flight: a `publish-testpypi` job that runs on
prerelease tags (`v*rc*`) against `test.pypi.org` with its own trusted
publisher + `testpypi` environment, verifying the full pipeline before the
first real release.

## 8. Release process (maintainer runbook)

1. Ensure `main` is green and `CHANGELOG.md` has an entry for the new version
   (a small `ci.yml` step warns on PRs that touch `src/` without touching
   `CHANGELOG.md` — advisory, not blocking).
2. `git tag v0.3.0 && git push origin v0.3.0`.
3. Approve the `pypi` environment deployment when prompted.
4. Workflow publishes to PyPI and cuts the GitHub Release.
5. Verify: `pip install rigorloop==0.3.0` in a scratch venv; `rigorloop
   --version`.

Version semantics while `0.x`: minor bump = features or breaking changes
(config format, CLI, artifact layout — call breakage out in the changelog);
patch bump = fixes only. `1.0.0` when `rigorloop.toml`, the CLI surface, and
the run-directory format are declared stable.

## 9. Rollout milestones

Ordered; the first three can happen immediately after `PLAN.md` Phase 1
(scaffolding) lands, and are prerequisites for everything after.

1. **Packaging skeleton** — `pyproject.toml` as specified, `py.typed`,
   `uv.lock`, `justfile`, `.pre-commit-config.yaml`, tool configs.
   *Accept:* `uv sync && just check && uv build && twine check dist/*` all
   pass locally; wheel installs and `rigorloop --version` works.
2. **CI live** — `ci.yml` + Dependabot config; branch protection on `main`
   requiring `ci-ok`.
   *Accept:* a deliberately-failing PR is blocked; a clean PR is green across
   the full matrix.
3. **Name claim** — configure the PyPI trusted publisher, then tag `v0.0.1`
   (packaging skeleton, honest "pre-alpha" description) to claim `rigorloop`
   and prove the release pipeline end-to-end.
   *Accept:* `pip install rigorloop` works from PyPI.
4. **OSS hygiene** — `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, issue/PR
   templates, README badges.
5. **First real release** — tag `v0.1.0` once `PLAN.md` Phase 7 (full loop
   with validation/finalization) is done.
6. **Post-1.0 niceties** (deferred): docs site (mkdocs-material) on GitHub
   Pages, Sigstore attestations for releases, Windows support decision,
   conda-forge feedstock if demand appears.

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Name squatting before first release | Milestone 3 claims `rigorloop` with a `v0.0.1` release as soon as CI is green. |
| Tag-based publishing fires accidentally | `pypi` environment requires manual approval; strict tag pattern; publishing rights bound to one workflow via OIDC. |
| `hatch-vcs` version wrong in CI/builds | `fetch-depth: 0` on all checkouts; the build job's wheel-install smoke test asserts `rigorloop --version` matches the expected pattern. |
| Lock/tool drift breaking new-contributor setup | `uv sync --locked` in CI fails on stale lock; Dependabot PRs keep `uv.lock`, GitHub Actions, and pre-commit hooks current (weekly, grouped). |
| Matrix cost creep | 3 Python × 2 OS test matrix only; lint/typecheck/build run once on 3.12; concurrency cancellation on superseded pushes. |
| Supply-chain exposure | Zero runtime deps; dev deps locked; actions pinned; `permissions` minimal per job; no secrets anywhere in the pipeline (OIDC only). |
