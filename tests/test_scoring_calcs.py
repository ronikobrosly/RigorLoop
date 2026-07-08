"""Scoring core: check evaluators, aggregation, and the statistics validated
against hand-computed known values."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rigorloop.core import scoring_calcs
from rigorloop.core.types import (
    CheckPassRate,
    CustomPython,
    Errored,
    ExactMatch,
    ExampleResult,
    ExecutionFailed,
    ExecutionOk,
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

pytestmark = pytest.mark.unit


class TestDeterministicChecks:
    def test_exact_match(self) -> None:
        assert scoring_calcs.evaluate_deterministic_check(ExactMatch(), "abc", "abc") == Passed()
        outcome = scoring_calcs.evaluate_deterministic_check(ExactMatch(), "abc", "abd")
        assert isinstance(outcome, Failed)

    def test_normalized_match(self) -> None:
        check = NormalizedMatch(lowercase=True, strip=True, collapse_whitespace=True)
        assert (
            scoring_calcs.evaluate_deterministic_check(check, "Hello  World", " hello world\n")
            == Passed()
        )
        no_lower = NormalizedMatch(lowercase=False, strip=True, collapse_whitespace=True)
        assert isinstance(
            scoring_calcs.evaluate_deterministic_check(no_lower, "Hello", "hello"), Failed
        )

    def test_json_equality(self) -> None:
        check = JsonEquality()
        assert (
            scoring_calcs.evaluate_deterministic_check(check, '{"a": 1, "b": 2}', '{"b":2,"a":1}')
            == Passed()
        )
        assert isinstance(
            scoring_calcs.evaluate_deterministic_check(check, '{"a": 1}', '{"a": 2}'), Failed
        )
        assert isinstance(
            scoring_calcs.evaluate_deterministic_check(check, '{"a": 1}', "not json"), Failed
        )
        # A non-JSON *expected* output is a configuration error, not a failure.
        assert isinstance(
            scoring_calcs.evaluate_deterministic_check(check, "not json", "{}"), Errored
        )

    def test_regex_match(self) -> None:
        assert (
            scoring_calcs.evaluate_deterministic_check(RegexMatch(r"\d{3}"), "", "abc 123")
            == Passed()
        )
        assert isinstance(
            scoring_calcs.evaluate_deterministic_check(RegexMatch(r"\d{3}"), "", "abc"), Failed
        )
        assert isinstance(
            scoring_calcs.evaluate_deterministic_check(RegexMatch("("), "", "x"), Errored
        )

    def test_numeric_tolerance(self) -> None:
        check = NumericTolerance(atol=0.01, rtol=0.0)
        assert scoring_calcs.evaluate_deterministic_check(check, "1.0", "1.005") == Passed()
        assert isinstance(scoring_calcs.evaluate_deterministic_check(check, "1.0", "1.02"), Failed)
        assert isinstance(scoring_calcs.evaluate_deterministic_check(check, "1.0", "abc"), Failed)
        assert isinstance(scoring_calcs.evaluate_deterministic_check(check, "abc", "1.0"), Errored)


class TestJudgeAggregation:
    CHECK = LlmJudge(rubric="r", n_samples=3, pass_threshold=0.5)

    def test_majority_passes(self) -> None:
        verdicts = (JudgeVerdict(True, ""), JudgeVerdict(True, ""), JudgeVerdict(False, "meh"))
        assert scoring_calcs.judge_outcome(self.CHECK, verdicts, 0) == Passed()

    def test_majority_fails(self) -> None:
        verdicts = (JudgeVerdict(False, "a"), JudgeVerdict(False, "b"), JudgeVerdict(True, ""))
        outcome = scoring_calcs.judge_outcome(self.CHECK, verdicts, 0)
        assert isinstance(outcome, Failed)

    def test_all_errors(self) -> None:
        outcome = scoring_calcs.judge_outcome(self.CHECK, (), 3)
        assert isinstance(outcome, Errored)

    def test_threshold_boundary_is_inclusive(self) -> None:
        strict = LlmJudge(rubric="r", n_samples=2, pass_threshold=0.5)
        verdicts = (JudgeVerdict(True, ""), JudgeVerdict(False, ""))
        assert scoring_calcs.judge_outcome(strict, verdicts, 0) == Passed()


class TestWilson:
    def test_known_values(self) -> None:
        # Hand-computed with z = 1.959964: 8/10 -> (0.4901, 0.9433)
        low, high = scoring_calcs.wilson_interval(8, 10)
        assert low == pytest.approx(0.4901, abs=1e-3)
        assert high == pytest.approx(0.9433, abs=1e-3)

    def test_extremes(self) -> None:
        assert scoring_calcs.wilson_interval(0, 0) == (0.0, 1.0)
        low, high = scoring_calcs.wilson_interval(0, 20)
        assert low == 0.0
        assert 0 < high < 0.2
        low, high = scoring_calcs.wilson_interval(20, 20)
        assert high == 1.0
        assert 0.8 < low < 1.0

    @given(st.integers(min_value=0, max_value=50), st.integers(min_value=1, max_value=50))
    def test_property_contains_point_estimate(self, passes: int, extra: int) -> None:
        n = passes + extra
        low, high = scoring_calcs.wilson_interval(passes, n)
        epsilon = 1e-12  # float noise at the p=0 / p=1 boundaries
        assert 0.0 <= low <= passes / n + epsilon
        assert passes / n - epsilon <= high <= 1.0


class TestBootstrap:
    def test_deterministic_for_seed(self) -> None:
        values = tuple(float(v) for v in (1, 2, 3, 4, 5, 6, 7, 8))
        assert scoring_calcs.bootstrap_ci(values, seed=42) == scoring_calcs.bootstrap_ci(
            values, seed=42
        )
        assert scoring_calcs.bootstrap_ci(values, seed=42) != scoring_calcs.bootstrap_ci(
            values, seed=43
        )

    def test_brackets_the_mean(self) -> None:
        values = tuple(float(v) for v in (2, 4, 4, 4, 5, 5, 7, 9))
        low, high = scoring_calcs.bootstrap_ci(values, seed=7)
        mean = sum(values) / len(values)
        assert low <= mean <= high
        assert min(values) <= low and high <= max(values)

    def test_empty_and_constant(self) -> None:
        assert scoring_calcs.bootstrap_ci((), seed=1) == (0.0, 0.0)
        assert scoring_calcs.bootstrap_ci((3.0, 3.0, 3.0), seed=1) == (3.0, 3.0)


class TestMcNemar:
    def test_known_value(self) -> None:
        # 6 discordant pairs, 1 in the minority: p = 2 * (C(6,0)+C(6,1)) / 64 = 0.21875
        a = (True, True, True, True, True, False, False)
        b = (False, False, False, False, False, True, False)
        # a_only = 5, b_only = 1 -> discordant 6, k = 1
        assert scoring_calcs.mcnemar_p(a, b) == pytest.approx(0.21875)

    def test_all_discordant_one_sided_dominance(self) -> None:
        a = (True,) * 6
        b = (False,) * 6
        # k = 0: p = 2 * C(6,0)/64 = 0.03125
        assert scoring_calcs.mcnemar_p(a, b) == pytest.approx(0.03125)

    def test_no_discordance_and_degenerate_inputs(self) -> None:
        same = (True, False, True)
        assert scoring_calcs.mcnemar_p(same, same) == 1.0
        assert scoring_calcs.mcnemar_p((), ()) == 1.0
        assert scoring_calcs.mcnemar_p((True,), (True, False)) == 1.0

    def test_symmetry(self) -> None:
        a = (True, True, False, True, False, True, True, False)
        b = (False, True, True, True, False, False, True, True)
        assert scoring_calcs.mcnemar_p(a, b) == scoring_calcs.mcnemar_p(b, a)

    def test_significantly_better(self) -> None:
        strong = (True,) * 8
        weak = (False,) * 8
        assert scoring_calcs.significantly_better(strong, weak)
        assert not scoring_calcs.significantly_better(weak, strong)
        # Within noise: 2 discordant pairs can never be significant.
        close = (True, True, True, False, False, False)
        closer = (True, True, True, True, False, False)
        assert not scoring_calcs.significantly_better(closer, close)


def _result(example_id: str, passed: bool) -> ExampleResult:
    outcome = Passed() if passed else Failed("nope")
    return ExampleResult(example_id, ExecutionOk("out"), (NamedOutcome("exact_match", outcome),))


class TestScoreCandidate:
    ORDER = ("e1", "e2", "e3", "e4")
    NAMES = ("exact_match",)

    def test_conjunctive_and_aligned(self) -> None:
        results = (
            _result("e1", True),
            _result("e2", False),
            _result("e3", True),
            _result("e4", True),
        )
        score = scoring_calcs.score_candidate(results, self.ORDER, self.NAMES, False)
        assert score.passes == 3
        assert score.pass_rate == 0.75
        assert score.pass_vector == (True, False, True, True)
        assert score.per_check == (CheckPassRate("exact_match", 3, 4),)
        assert score.ci_low < 0.75 < score.ci_high

    def test_missing_results_count_as_failures(self) -> None:
        score = scoring_calcs.score_candidate((_result("e1", True),), self.ORDER, self.NAMES, True)
        assert score.n == 4
        assert score.passes == 1
        assert score.pass_vector == (True, False, False, False)
        assert score.eval_aborted

    def test_all_checks_must_pass(self) -> None:
        mixed = ExampleResult(
            "e1",
            ExecutionOk("out"),
            (NamedOutcome("a", Passed()), NamedOutcome("b", Failed("no"))),
        )
        assert not scoring_calcs.example_passed(mixed)
        errored = ExampleResult("e1", ExecutionFailed("x"), (NamedOutcome("a", Errored("x")),))
        assert not scoring_calcs.example_passed(errored)
        assert not scoring_calcs.example_passed(ExampleResult("e1", ExecutionOk("out"), ()))


class TestNamedChecks:
    def test_unique_names_for_duplicates(self) -> None:
        checks = (
            ExactMatch(),
            LlmJudge("a", 3, 0.5),
            LlmJudge("b", 3, 0.5),
        )
        names = tuple(name for name, _ in scoring_calcs.named_checks(checks))
        assert names == ("exact_match", "llm_judge", "llm_judge#2")

    def test_check_name_covers_every_variant(self) -> None:
        assert scoring_calcs.check_name(ExactMatch()) == "exact_match"
        assert scoring_calcs.check_name(NormalizedMatch(True, True, True)) == "normalized_match"
        assert scoring_calcs.check_name(JsonEquality()) == "json_equality"
        assert scoring_calcs.check_name(RegexMatch("x")) == "regex_match"
        assert scoring_calcs.check_name(NumericTolerance(0, 0)) == "numeric_tolerance"
        assert scoring_calcs.check_name(CustomPython("p")) == "custom_python"
        assert scoring_calcs.check_name(LlmJudge("r", 1, 0.5)) == "llm_judge"


def test_execution_failure_outcomes() -> None:
    outcomes = scoring_calcs.execution_failure_outcomes(("a", "b"), "timeout")
    assert all(isinstance(o.outcome, Errored) for o in outcomes)
    assert [o.check_name for o in outcomes] == ["a", "b"]
