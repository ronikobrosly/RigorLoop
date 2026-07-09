# AGENTS.md

Orientation for coding agents working in this repository. Read this first, then
read **[`CODING_STYLE.md`](CODING_STYLE.md)** before writing or modifying any
code — its rules are hard constraints, not preferences.

## What this package is

**RigorLoop** is a statistically-sound agentic build framework. You give it a
task description, a pile of gold-standard input/output examples, and a set of
checks; it runs agentic loops (a strategy agent directing concurrent executor
agents) that iteratively build a solution and evaluate it on a strict
**dev / validation / test** split, so the final score is trustworthy. The
produced artifact is portable: an executable Python script, an agent skill
(`SKILL.md`), or a guidance file (`AGENTS.md`/`CLAUDE.md`).

- Pure Python, **stdlib-only** by design (no runtime dependencies), Python ≥ 3.12.
- Ships a single CLI: `rigorloop` (`init` / `check` / `run` / `report`).
- Invokes agents headless and tool-less via the `claude` CLI (`claude -p`).
- User-facing docs: [`README.md`](README.md). Contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Architecture: functional core / imperative shell

This is the single most important thing to understand, and it is enforced by
[`CODING_STYLE.md`](CODING_STYLE.md):

- **`src/rigorloop/core/`** — the functional core. 100% pure: no I/O, no
  mutation, no time, no randomness, no network, no environment access. It
  *decides*; it returns values and plans of effects. Testable with plain inputs
  and zero mocks. Core coverage is held to a higher bar (≥95%).
- **`src/rigorloop/shell/`** — the thin imperative shell. Performs all effects
  (filesystem, subprocess, the `claude` CLI) and hands plain data to the core.
  Keep it small.

The dev/val/test split is encoded in the type system (`DevExample`,
`ValExample`, `TestExample`) so leaking holdout data into an agent-context
prompt is a *type error*, not a runtime bug. Don't defeat this.

## Working in this repo

- Dev commands live in the [`justfile`](justfile), each mirroring a CI job:
  `just lint`, `just typecheck`, `just test`, `just check` (all three), `just fmt`.
- Tooling: `uv` for env/build, `ruff` (lint + format), `mypy --strict`, `pytest`.
  `T20` (print) is banned in the core and allowed in the shell.
- Every source module has a sibling test in `tests/` (e.g. `scoring_calcs.py` →
  `test_scoring_calcs.py`); `test_leakage.py` guards the split-type invariant and
  `test_e2e.py` runs full loops against fake agents.

## Folder tree

```
RigorLoop/
├── AGENTS.md                     # this file
├── CODING_STYLE.md               # MANDATORY coding rules (read before editing)
├── CONTRIBUTING.md               # contributor workflow
├── README.md                     # user-facing overview & docs
├── CHANGELOG.md
├── SECURITY.md                   # runs generated code locally — read this
├── LICENSE                       # MIT
├── justfile                      # dev entry points (mirror CI jobs)
├── pyproject.toml                # package metadata + tool config
├── uv.lock
├── .pre-commit-config.yaml
├── .gitignore
│
├── src/
│   └── rigorloop/
│       ├── __init__.py           # package version resolution
│       ├── py.typed              # PEP 561 typing marker
│       ├── core/                 # FUNCTIONAL CORE — pure, effect-free
│       │   ├── __init__.py
│       │   ├── types.py          # domain model: algebraic data types (products + sums)
│       │   ├── config_calcs.py   # parse rigorloop.toml → typed RunConfig
│       │   ├── dataset_calcs.py  # parse examples, dedup, split, manifests, power warnings
│       │   ├── prompt_calcs.py   # prompt builders (agent-context vs. evaluation channels)
│       │   ├── scoring_calcs.py  # checks, aggregation, Wilson/bootstrap CIs, McNemar
│       │   ├── strategy_calcs.py # validation cadence, stopping rules, champion selection
│       │   └── report_calcs.py   # render report, check summary, budget estimate
│       └── shell/                # IMPERATIVE SHELL — effects at the edges
│           ├── __init__.py
│           ├── cli.py            # argparse entry point + orchestration driver
│           ├── agent_calls.py    # claude CLI subprocess wrapper, retries, concurrency
│           └── io_actions.py     # run dir, artifact persist/reload, sandboxed exec
│
├── tests/                        # one test module per source module + leakage/e2e
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_config_calcs.py
│   ├── test_dataset_calcs.py
│   ├── test_prompt_calcs.py
│   ├── test_scoring_calcs.py
│   ├── test_strategy_calcs.py
│   ├── test_report_calcs.py
│   ├── test_agent_calls.py
│   ├── test_io_actions.py
│   ├── test_cli.py
│   ├── test_leakage.py           # asserts split types prevent holdout leakage
│   └── test_e2e.py               # full runs against fake agents
│
├── examples/
│   └── contact-cards/            # toy project; exactly what `rigorloop init` scaffolds
│       ├── rigorloop.toml
│       ├── task.md
│       └── examples.jsonl
│
├── scripts/
│   └── live-smoke.sh             # live smoke test against the real claude CLI
│
├── .github/                      # CI/release workflows, issue/PR templates, dependabot
│   ├── workflows/
│   │   ├── ci.yml
│   │   └── release.yml
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md
│   │   └── feature_request.md
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── dependabot.yml
│
└── .claude/
    └── settings.json             # Claude Code project settings & safety hooks
```
