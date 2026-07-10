# Changelog

All notable changes to this project are documented in this file, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[SemVer](https://semver.org/) (`0.x`: minor = features/breaking, patch = fixes).

## [v0.2.0] - 2026-07-10

### Changed

- **Validation now steers the search, not just final selection.** Previously
  the raw dev leaderboard chose both the artifact each loop refined and the
  only candidate ever validated, so an overfit dev leader could monopolize the
  run while a better generalizer was never measured on validation:
  - The base artifact for refinement (`base_on_champion`) and the strategy
    prompt's primary "champion" are now the **validation champion** once any
    candidate has been validated (the dev leader before that). A diverging dev
    leader is still shown, as a clearly labeled diagnostic line with an
    overfit warning — aggregate scores only, never its content.
  - Each validation checkpoint evaluates a **precommitted cohort** (new
    `validation.cohort_size` knob, default 2): the top unvalidated candidates
    by dev score, with the last slot reserved for the best unvalidated
    candidate not built on the champion (approach diversity). `max_peeks` now
    budgets individual candidate evaluations, and an already-validated dev
    leader no longer stalls validation of newer candidates.
  - Within the McNemar noise band, champion selection tie-breaks on the
    validation pass rate instead of the dev pass rate (dev is the one metric
    under direct selection pressure). Significant wins still gate the
    plateau/patience rule, which now counts checkpoint loops rather than
    individual cohort evaluations.
  - `target_pass_rate` early stopping requires the validation score's Wilson
    lower confidence bound to clear the target, not the raw point estimate.
  - Per-candidate dev failure samples are persisted
    (`failure_samples.json` in each candidate directory) and the champion's
    are reloaded every loop, so the strategy agent keeps concrete
    counterexamples across non-improving loops and `--resume`.
  - The report's selection-bias caveat now states that validation both steered
    the search and selected the winner (the untouched test set remains the
    honest number).

## [v0.1.1] - 2026-07-9

Documentation and repository-hygiene only; no changes to the `rigorloop`
package or its behavior.

### Added

- `AGENTS.md`: an orientation guide for coding agents, describing the package,
  the functional-core / imperative-shell architecture (pointing at
  `CODING_STYLE.md` as the binding rules), and a full annotated folder tree.

### Changed

- `CONTRIBUTING.md`: expanded the maintainer *Releases* section to document the
  tag-driven flow — `hatch-vcs` derives the version from the `vX.Y.Z` tag (no
  file to bump), the `[Unreleased]` → `[X.Y.Z]` changelog promotion, OIDC
  Trusted Publishing, prerelease/TestPyPI rehearsal, and the "publish a tag
  exactly once, fix forward" rule.

### Removed

- Internal planning docs `PLAN.md`, `PACKAGING_PLAN.md`, and `CLAUDE.md` removed
  from the repository (superseded by the shipped implementation and the
  README/CONTRIBUTING docs).

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
