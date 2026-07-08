"""Report rendering and the kind-aware budget estimate."""

from __future__ import annotations

import pytest

from rigorloop.core import config_calcs, report_calcs
from rigorloop.core.types import (
    NOTHING,
    BudgetExhausted,
    CandidateScore,
    DuplicateWarning,
    GuidanceSolution,
    Ok,
    PowerWarning,
    RunConfig,
    RunState,
    ScriptSolution,
    SkillSolution,
    StrategyRequestedStop,
    StrategyUnresponsive,
    TargetReached,
    ValidatedCandidate,
    ValidationPlateau,
)
from tests.conftest import BASE_CONFIG

pytestmark = pytest.mark.unit


def config(text: str = BASE_CONFIG) -> RunConfig:
    parsed = config_calcs.parse_config(text)
    assert isinstance(parsed, Ok)
    return parsed.value


def score(passes: int, n: int) -> CandidateScore:
    return CandidateScore(n, passes, passes / n, 0.1, 0.9, (), (True,) * passes, False)


class TestBudgetEstimate:
    def test_script_kind_costs_no_eval_calls(self) -> None:
        budget = report_calcs.estimate_budget(config(), n_dev=18, n_val=6, n_test=6)
        # BASE_CONFIG: 4 loops x 2 executors.
        assert budget.strategy_calls == 4
        assert budget.executor_calls == 8
        assert budget.solution_eval_calls == 0
        assert budget.judge_calls == 0
        assert budget.total_calls == 12

    def test_skill_kind_pays_per_example(self) -> None:
        text = BASE_CONFIG.replace('solution_kind    = "script"', 'solution_kind    = "skill"')
        budget = report_calcs.estimate_budget(config(text), n_dev=18, n_val=6, n_test=6)
        # candidates(8) * dev(18) + peeks(10) * val(6) + test(6) = 210 evaluated examples
        assert budget.solution_eval_calls == 210
        assert budget.total_calls == 4 + 8 + 210

    def test_judge_checks_multiply(self) -> None:
        text = BASE_CONFIG + '\n[[checks]]\ntype = "llm_judge"\nrubric = "r"\nn_samples = 3\n'
        budget = report_calcs.estimate_budget(config(text), n_dev=18, n_val=6, n_test=6)
        assert budget.judge_calls == 210 * 3
        assert budget.solution_eval_calls == 0


class TestCheckSummary:
    def test_renders_everything(self) -> None:
        budget = report_calcs.estimate_budget(config(), 18, 6, 6)
        summary = report_calcs.render_check_summary(
            n_total=30,
            n_dev=18,
            n_val=6,
            n_test=6,
            duplicates=(DuplicateWarning("dup input", 3),),
            warnings=(PowerWarning("test", 6, 0.4, "test set warning message"),),
            budget=budget,
            model="claude-test",
        )
        assert "dev 18 / validation 6 / test 6" in summary
        assert "dup input" in summary
        assert "test set warning message" in summary
        assert "claude-test" in summary
        assert "No tokens were spent" in summary

    def test_empty_sections(self) -> None:
        budget = report_calcs.estimate_budget(config(), 18, 6, 6)
        summary = report_calcs.render_check_summary(30, 18, 6, 6, (), (), budget, "m")
        assert "(none)" in summary


class TestStopReasonLabels:
    def test_every_variant(self) -> None:
        labels = [
            report_calcs.stop_reason_label(reason)
            for reason in (
                BudgetExhausted(12),
                ValidationPlateau(2),
                TargetReached(0.97),
                StrategyRequestedStop("saturated"),
                StrategyUnresponsive(2),
            )
        ]
        assert all(labels)
        assert "12" in labels[0]
        assert "97.0%" in labels[2]
        assert "saturated" in labels[3]


class TestRenderReport:
    def make_winner(self) -> ValidatedCandidate:
        return ValidatedCandidate(
            "cand-L2-d1", ScriptSolution(), "content", score(16, 18), score(5, 6)
        )

    def test_report_content(self) -> None:
        state = RunState(4, (), (), NOTHING, (), 3, NOTHING, 0)
        report = report_calcs.render_report(
            run_id="run-1",
            kind=ScriptSolution(),
            eval_model="claude-test",
            cli_version="cli-9",
            stop_reason=BudgetExhausted(4),
            winner=self.make_winner(),
            test_score=score(4, 6),
            state=state,
            agent_calls_made=42,
        )
        assert "run-1" in report
        assert "cand-L2-d1" in report
        assert "Selection-bias caveat" in report
        assert "Validation peeks used: 3" in report
        assert "Agent calls made: 42" in report
        assert "burns the holdout" in report
        # Script kind: no stochastic-evaluator caveat.
        assert "Stochastic evaluator caveat" not in report

    def test_guidance_kind_report(self) -> None:
        state = RunState(1, (), (), NOTHING, (), 1, NOTHING, 0)
        winner = ValidatedCandidate("c", GuidanceSolution(), "content", score(16, 18), score(5, 6))
        report = report_calcs.render_report(
            "r",
            GuidanceSolution(),
            "m",
            "c1",
            ValidationPlateau(2),
            winner,
            score(6, 6),
            state,
            7,
        )
        assert "**guidance**" in report
        assert "Stochastic evaluator caveat" in report

    def test_stochastic_caveat_for_skill_kind(self) -> None:
        state = RunState(1, (), (), NOTHING, (), 1, NOTHING, 0)
        winner = ValidatedCandidate("c", SkillSolution(), "content", score(16, 18), score(5, 6))
        report = report_calcs.render_report(
            "r",
            SkillSolution(),
            "model-m",
            "cli-1",
            TargetReached(0.9),
            winner,
            score(6, 6),
            state,
            7,
        )
        assert "Stochastic evaluator caveat" in report
        assert "model-m" in report
