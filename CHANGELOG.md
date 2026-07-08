# Changelog

All notable changes to this project are documented in this file, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[SemVer](https://semver.org/) (`0.x`: minor = features/breaking, patch = fixes).

## [Unreleased]

## [0.1.0] - 2026-07-08

### Added

- Initial implementation of the full RigorLoop protocol:
  - `rigorloop init | check | run | report` CLI.
  - Deterministic dev/validation/test splitting with content-hash manifests,
    exact-duplicate collapsing, and statistical power warnings.
  - Strategy agent / executor agent loop over the `claude` CLI (headless,
    tool-less), with strict output contracts, reformat retries, and a
    fallback path for malformed strategy replies.
  - Checks: exact/normalized/JSON/regex/numeric matching, custom Python
    checkers, and n-sample majority-vote LLM judges.
  - Statistics: Wilson intervals, seeded bootstrap CIs, exact McNemar paired
    tests; CI-band-gated champion selection, budgeted validation peeks, and
    a one-shot final test evaluation with a selection-bias caveat in the
    report.
  - Solution kinds: executable Python scripts, agent skills (SKILL.md), and
    guidance markdown (AGENTS.md/CLAUDE.md style).
  - Resumable run directories (`runs/<run_id>/`) with split-drift protection.
  - Type-level and test-level leakage guards keeping holdout data out of all
    agent-context prompts.
