"""Pure scoring logic: check evaluation, aggregation, and the statistics that
make the loop rigorous (Wilson intervals, bootstrap CIs, McNemar tests)."""

from __future__ import annotations

import json
import math
import random
import re

from rigorloop.core.types import (
    CandidateScore,
    Check,
    CheckOutcome,
    CheckPassRate,
    CustomPython,
    DeterministicCheck,
    Errored,
    ExactMatch,
    ExampleResult,
    Failed,
    JsonEquality,
    JudgeVerdict,
    LlmJudge,
    NamedOutcome,
    NormalizedMatch,
    NumericTolerance,
    Passed,
    RegexMatch,
)

Z_95 = 1.959963984540054


# --------------------------------------------------------------------------
# Check names and deterministic evaluation
# --------------------------------------------------------------------------


def check_name(check: Check) -> str:
    match check:
        case ExactMatch():
            return "exact_match"
        case NormalizedMatch():
            return "normalized_match"
        case JsonEquality():
            return "json_equality"
        case RegexMatch():
            return "regex_match"
        case NumericTolerance():
            return "numeric_tolerance"
        case CustomPython():
            return "custom_python"
        case LlmJudge():
            return "llm_judge"


def named_checks(checks: tuple[Check, ...]) -> tuple[tuple[str, Check], ...]:
    """Stable, unique display names: duplicates get #2, #3, … suffixes so two
    checks of the same type can't collide in per-check breakdowns."""

    def name_at(index: int, check: Check) -> str:
        base = check_name(check)
        prior = sum(1 for c in checks[:index] if check_name(c) == base)
        return base if prior == 0 else f"{base}#{prior + 1}"

    return tuple((name_at(i, c), c) for i, c in enumerate(checks))


def _normalize(text: str, check: NormalizedMatch) -> str:
    lowered = text.lower() if check.lowercase else text
    stripped = lowered.strip() if check.strip else lowered
    return re.sub(r"\s+", " ", stripped) if check.collapse_whitespace else stripped


def _truncate(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evaluate_deterministic_check(
    check: DeterministicCheck, expected: str, actual: str
) -> CheckOutcome:
    match check:
        case ExactMatch():
            return (
                Passed()
                if actual == expected
                else Failed(f"expected {_truncate(expected)!r}, got {_truncate(actual)!r}")
            )
        case NormalizedMatch():
            return (
                Passed()
                if _normalize(actual, check) == _normalize(expected, check)
                else Failed(
                    f"normalized mismatch: expected {_truncate(expected)!r}, "
                    f"got {_truncate(actual)!r}"
                )
            )
        case JsonEquality():
            try:
                expected_obj = json.loads(expected)
            except json.JSONDecodeError as exc:
                return Errored(f"expected output is not valid JSON: {exc}")
            try:
                actual_obj = json.loads(actual)
            except json.JSONDecodeError as exc:
                return Failed(f"output is not valid JSON: {exc}")
            return Passed() if actual_obj == expected_obj else Failed("JSON values differ")
        case RegexMatch():
            try:
                pattern = re.compile(check.pattern, re.DOTALL)
            except re.error as exc:
                return Errored(f"invalid pattern {check.pattern!r}: {exc}")
            return (
                Passed()
                if pattern.search(actual) is not None
                else Failed(f"pattern {check.pattern!r} not found in output")
            )
        case NumericTolerance():
            try:
                expected_num = float(expected.strip())
            except ValueError:
                return Errored(f"expected output {_truncate(expected)!r} is not numeric")
            try:
                actual_num = float(actual.strip())
            except ValueError:
                return Failed(f"output {_truncate(actual)!r} is not numeric")
            return (
                Passed()
                if math.isclose(actual_num, expected_num, rel_tol=check.rtol, abs_tol=check.atol)
                else Failed(f"{actual_num} not within tolerance of {expected_num}")
            )


def judge_outcome(check: LlmJudge, verdicts: tuple[JudgeVerdict, ...], errors: int) -> CheckOutcome:
    """Aggregate n judge samples into one outcome (majority vote against the
    configured threshold). `errors` counts judge calls that failed outright."""
    if not verdicts:
        return Errored(f"all {errors} judge calls failed")
    pass_fraction = sum(1 for v in verdicts if v.passed) / len(verdicts)
    if pass_fraction >= check.pass_threshold:
        return Passed()
    failing = [v.reason for v in verdicts if not v.passed]
    return Failed(
        f"judge pass fraction {pass_fraction:.2f} < {check.pass_threshold:.2f}: "
        + _truncate("; ".join(failing))
    )


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def wilson_interval(passes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion; honest at small n."""
    if n == 0:
        return (0.0, 1.0)
    p_hat = passes / n
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


def bootstrap_ci(
    values: tuple[float, ...], seed: int, n_resamples: int = 2000, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile bootstrap CI for a mean. Resample indices derive from the
    injected seed, so results are reproducible."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)  # pure function of the seed parameter
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_resamples))
    lo_index = int((alpha / 2) * n_resamples)
    hi_index = min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))
    return (means[lo_index], means[hi_index])


def mcnemar_p(vector_a: tuple[bool, ...], vector_b: tuple[bool, ...]) -> float:
    """Exact two-sided McNemar test on paired pass/fail vectors.

    Returns the p-value for the null that both candidates have the same
    per-example pass probability. Vectors must be aligned to the same example
    order; unequal lengths are treated as maximally uninformative (p=1)."""
    if len(vector_a) != len(vector_b) or not vector_a:
        return 1.0
    a_only = sum(1 for a, b in zip(vector_a, vector_b, strict=True) if a and not b)
    b_only = sum(1 for a, b in zip(vector_a, vector_b, strict=True) if b and not a)
    discordant = a_only + b_only
    if discordant == 0:
        return 1.0
    k = min(a_only, b_only)
    # Exact binomial: 2 * P(X <= k) under X ~ Bin(discordant, 0.5), capped at 1.
    tail = sum(math.comb(discordant, i) for i in range(k + 1)) / (1 << discordant)
    return min(1.0, 2 * tail)


def significantly_better(
    challenger: tuple[bool, ...], incumbent: tuple[bool, ...], alpha: float = 0.05
) -> bool:
    """True iff the challenger beats the incumbent beyond the paired noise band."""
    challenger_wins = sum(challenger) > sum(incumbent)
    return challenger_wins and mcnemar_p(challenger, incumbent) < alpha


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def example_passed(result: ExampleResult) -> bool:
    """The headline metric is conjunctive: every configured check must pass."""
    return bool(result.outcomes) and all(isinstance(o.outcome, Passed) for o in result.outcomes)


def _per_check_rates(
    results: tuple[ExampleResult, ...], names: tuple[str, ...]
) -> tuple[CheckPassRate, ...]:
    def rate(name: str) -> CheckPassRate:
        outcomes = [o for r in results for o in r.outcomes if o.check_name == name]
        return CheckPassRate(
            check_name=name,
            passes=sum(1 for o in outcomes if isinstance(o.outcome, Passed)),
            n=len(outcomes),
        )

    return tuple(rate(name) for name in names)


def score_candidate(
    results: tuple[ExampleResult, ...],
    example_order: tuple[str, ...],
    check_names: tuple[str, ...],
    eval_aborted: bool,
) -> CandidateScore:
    """Fold per-example results into a CandidateScore.

    `example_order` is the id order of the FULL evaluation set; examples with
    no result (short-circuited evaluation) count as failures so pass vectors
    stay aligned for paired tests."""
    by_id = {r.example_id: r for r in results}
    pass_vector = tuple(ex_id in by_id and example_passed(by_id[ex_id]) for ex_id in example_order)
    n = len(example_order)
    passes = sum(pass_vector)
    ci_low, ci_high = wilson_interval(passes, n)
    return CandidateScore(
        n=n,
        passes=passes,
        pass_rate=passes / n if n else 0.0,
        ci_low=ci_low,
        ci_high=ci_high,
        per_check=_per_check_rates(results, check_names),
        pass_vector=pass_vector,
        eval_aborted=eval_aborted,
    )


def execution_failure_outcomes(
    check_names: tuple[str, ...], detail: str
) -> tuple[NamedOutcome, ...]:
    """When a candidate fails to produce output, every check is Errored."""
    return tuple(NamedOutcome(name, Errored(detail)) for name in check_names)
