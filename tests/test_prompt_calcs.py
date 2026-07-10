"""Prompt builders: content assertions for both channels and the guarantee
that directives carry champion content only."""

from __future__ import annotations

import pytest

from rigorloop.core import config_calcs, prompt_calcs, strategy_calcs
from rigorloop.core.types import (
    NOTHING,
    ChampionArtifact,
    CustomPython,
    DevExample,
    Directive,
    ExactMatch,
    Example,
    ExecutorRole,
    GuidanceSolution,
    JsonEquality,
    LlmJudge,
    NormalizedMatch,
    Nothing,
    NumericTolerance,
    Ok,
    RegexMatch,
    ScriptSolution,
    SkillSolution,
    SolutionKind,
    Some,
    StrategyRole,
)
from tests.conftest import BASE_CONFIG

pytestmark = pytest.mark.unit

DEV = tuple(DevExample(Example(f"e{i}", f"dev input {i}", f"dev output {i}")) for i in range(3))
CHAMPION = ChampionArtifact("cand-7", ScriptSolution(), "CHAMPION SCRIPT CONTENT")
DIRECTIVE = Directive("L2-d1", "refine the parser", "handle commas", Some(CHAMPION))


class TestRenderDirective:
    def test_embeds_champion_content_only(self) -> None:
        rendered = prompt_calcs.render_directive(DIRECTIVE)
        assert "refine the parser" in rendered
        assert "handle commas" in rendered
        assert "CHAMPION SCRIPT CONTENT" in rendered
        # Content only: no scores, no failure text, no history.
        for forbidden in ("%", "pass rate", "score", "failed", "mistake", "loop "):
            assert forbidden not in rendered.lower(), forbidden

    def test_without_base(self) -> None:
        bare = Directive("L1-d1", "explore", "try it", NOTHING)
        rendered = prompt_calcs.render_directive(bare)
        assert "current-solution" not in rendered


class TestExecutorPrompt:
    SCRIPT_KIND: SolutionKind = ScriptSolution()

    def build(self, kind: SolutionKind = SCRIPT_KIND) -> str:
        prompt = prompt_calcs.build_executor_prompt(
            "TASK DESCRIPTION", kind, DIRECTIVE, (ExactMatch(),), DEV
        )
        assert prompt.role == ExecutorRole()
        return prompt.text

    def test_contains_the_essentials_and_nothing_historical(self) -> None:
        text = self.build()
        assert "TASK DESCRIPTION" in text
        assert "handle commas" in text
        assert "CHAMPION SCRIPT CONTENT" in text
        assert "dev input 1" in text and "dev output 1" in text
        assert "exact_match" in text
        assert "```solution" in text
        assert "leaderboard" not in text.lower()
        assert "prior" not in text.lower()

    def test_kind_specific_contracts(self) -> None:
        assert "stdin" in self.build(ScriptSolution())
        assert "skill markdown" in self.build(SkillSolution())
        assert "AGENTS.md" in self.build(GuidanceSolution())


class TestStrategyPrompt:
    def test_first_loop_prompt(self) -> None:
        parsed = config_calcs.parse_config(BASE_CONFIG)
        assert isinstance(parsed, Ok)
        context = strategy_calcs.assemble_strategy_context(
            "TASK TEXT", parsed.value, strategy_calcs.initial_state(), (), ("exact_match",)
        )
        prompt = prompt_calcs.build_strategy_prompt(context)
        assert prompt.role == StrategyRole()
        assert "TASK TEXT" in prompt.text
        assert "first loop" in prompt.text
        assert '"action": "continue"' in prompt.text
        assert "Loops completed: 0 of 4" in prompt.text
        assert "resampled every loop" in prompt.text

    def test_gap_warning_is_rendered(self) -> None:
        from rigorloop.core.scoring_calcs import wilson_interval
        from rigorloop.core.types import (
            CandidateScore,
            LeaderboardEntry,
            RunState,
            ValCheckpoint,
            ValidatedCandidate,
        )

        parsed = config_calcs.parse_config(BASE_CONFIG)
        assert isinstance(parsed, Ok)

        def make_score(vector: tuple[bool, ...]) -> CandidateScore:
            low, high = wilson_interval(sum(vector), len(vector))
            return CandidateScore(
                len(vector), sum(vector), sum(vector) / len(vector), low, high, (), vector, False
            )

        dev_score = make_score((True, True, True, False))  # 75% on dev
        val_score = make_score((True, False, False, False))  # 25% on validation
        board_entry = LeaderboardEntry("best", 1, ScriptSolution(), "content", dev_score)
        champion = ValidatedCandidate("best", ScriptSolution(), "content", dev_score, val_score)
        state = RunState(
            1,
            (board_entry,),
            (),
            Some(champion),
            (ValCheckpoint(1, "best", 0.75, 0.25, True),),
            1,
            Some(1),
            0,
        )
        context = strategy_calcs.assemble_strategy_context(
            "T", parsed.value, state, (), ("exact_match",)
        )
        prompt = prompt_calcs.build_strategy_prompt(context)
        assert "overfitting" in prompt.text
        assert "champion by validation score" in prompt.text

    def test_champion_and_dev_leader_sections_are_present(self) -> None:
        parsed = config_calcs.parse_config(BASE_CONFIG)
        assert isinstance(parsed, Ok)
        context = strategy_calcs.assemble_strategy_context(
            "T", parsed.value, strategy_calcs.initial_state(), (), ("exact_match",)
        )
        prompt = prompt_calcs.build_strategy_prompt(context)
        assert "# Primary artifact to refine — current champion" in prompt.text
        assert "# Diagnostic dev leader" in prompt.text
        assert "every candidate evaluation costs one peek" in prompt.text

    def test_reformat_note_appended(self) -> None:
        parsed = config_calcs.parse_config(BASE_CONFIG)
        assert isinstance(parsed, Ok)
        context = strategy_calcs.assemble_strategy_context(
            "T", parsed.value, strategy_calcs.initial_state(), (), ()
        )
        prompt = prompt_calcs.build_strategy_prompt(context)
        redone = prompt_calcs.reformat_context_prompt(prompt, "bad json")
        assert redone.text.startswith(prompt.text)
        assert "bad json" in redone.text
        assert redone.role == prompt.role


class TestEvalPrompts:
    EXAMPLE = Example("e1", "the raw input", "the gold output")

    def test_solution_eval_prompt(self) -> None:
        prompt = prompt_calcs.build_solution_eval_prompt("SKILL DOC BODY", self.EXAMPLE)
        assert prompt.user_prompt == "the raw input"
        system = prompt.system_prompt
        assert isinstance(system, Some)
        assert "SKILL DOC BODY" in system.value
        assert "output only" in system.value

    def test_judge_prompt(self) -> None:
        prompt = prompt_calcs.build_judge_prompt("THE RUBRIC", self.EXAMPLE, "candidate out")
        assert prompt.system_prompt == Nothing()
        assert "THE RUBRIC" in prompt.user_prompt
        assert "the raw input" in prompt.user_prompt
        assert "the gold output" in prompt.user_prompt
        assert "candidate out" in prompt.user_prompt
        assert '"pass"' in prompt.user_prompt

    def test_reformat_eval_prompt(self) -> None:
        prompt = prompt_calcs.build_judge_prompt("R", self.EXAMPLE, "out")
        redone = prompt_calcs.reformat_eval_prompt(prompt, "not json")
        assert redone.user_prompt.startswith(prompt.user_prompt)
        assert "not json" in redone.user_prompt
        assert redone.system_prompt == prompt.system_prompt


class TestDescribeCheck:
    def test_every_check_kind_renders(self) -> None:
        descriptions = [
            prompt_calcs.describe_check(check)
            for check in (
                ExactMatch(),
                NormalizedMatch(True, True, True),
                JsonEquality(),
                RegexMatch(r"\d+"),
                NumericTolerance(0.1, 0.2),
                CustomPython("checker.py"),
                LlmJudge("rubric text", 3, 0.67),
            )
        ]
        assert all(descriptions)
        assert "rubric text" in descriptions[-1]
        assert "checker.py" in descriptions[-2]

    def test_kind_labels(self) -> None:
        assert "script" in prompt_calcs.kind_label(ScriptSolution())
        assert "skill" in prompt_calcs.kind_label(SkillSolution())
        assert "guidance" in prompt_calcs.kind_label(GuidanceSolution())
