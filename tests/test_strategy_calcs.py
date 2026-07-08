"""Strategy core: reply parsers, directives, cadence, stopping, selection."""

from __future__ import annotations

import json

import pytest

from rigorloop.core import config_calcs, strategy_calcs
from rigorloop.core.types import (
    NOTHING,
    BudgetExhausted,
    CandidateScore,
    ChampionArtifact,
    ContinueDecision,
    DevExample,
    DirectiveSpec,
    Err,
    Example,
    ExampleResult,
    ExecutionFailed,
    ExecutionOk,
    Failed,
    LeaderboardEntry,
    NamedOutcome,
    Nothing,
    Ok,
    Passed,
    RunConfig,
    RunState,
    ScriptSolution,
    Some,
    StopRequested,
    StrategyLogEntry,
    StrategyUnresponsive,
    TargetReached,
    ValCheckpoint,
    ValidatedCandidate,
    ValidationPlateau,
)
from tests.conftest import BASE_CONFIG

pytestmark = pytest.mark.unit


def config() -> RunConfig:
    parsed = config_calcs.parse_config(BASE_CONFIG)
    assert isinstance(parsed, Ok)
    return parsed.value


def score(vector: tuple[bool, ...], aborted: bool = False) -> CandidateScore:
    from rigorloop.core.scoring_calcs import wilson_interval

    passes = sum(vector)
    low, high = wilson_interval(passes, len(vector))
    return CandidateScore(
        n=len(vector),
        passes=passes,
        pass_rate=passes / len(vector),
        ci_low=low,
        ci_high=high,
        per_check=(),
        pass_vector=vector,
        eval_aborted=aborted,
    )


def entry(candidate_id: str, vector: tuple[bool, ...], loop_index: int = 1) -> LeaderboardEntry:
    return LeaderboardEntry(candidate_id, loop_index, ScriptSolution(), "content", score(vector))


def validated(
    candidate_id: str, dev: tuple[bool, ...], val: tuple[bool, ...]
) -> ValidatedCandidate:
    return ValidatedCandidate(candidate_id, ScriptSolution(), "content", score(dev), score(val))


class TestSampleDevSubset:
    DEV = tuple(DevExample(Example(f"e{i}", f"in{i}", f"out{i}")) for i in range(20))

    def test_deterministic_per_loop(self) -> None:
        first = strategy_calcs.sample_dev_subset(self.DEV, 5, seed=1, loop_index=3)
        again = strategy_calcs.sample_dev_subset(self.DEV, 5, seed=1, loop_index=3)
        assert first == again
        assert len(first) == 5

    def test_resampled_across_loops(self) -> None:
        loop3 = strategy_calcs.sample_dev_subset(self.DEV, 5, seed=1, loop_index=3)
        loop4 = strategy_calcs.sample_dev_subset(self.DEV, 5, seed=1, loop_index=4)
        assert loop3 != loop4

    def test_small_dev_returned_whole(self) -> None:
        assert strategy_calcs.sample_dev_subset(self.DEV, 50, seed=1, loop_index=1) == self.DEV


class TestParseStrategyReply:
    def test_valid_continue(self) -> None:
        reply = json.dumps(
            {
                "action": "continue",
                "observations": "obs",
                "hypotheses": "hyp",
                "directives": [
                    {"approach_summary": "s", "instructions": "i", "base_on_champion": True}
                ],
                "request_validation": True,
            }
        )
        result = strategy_calcs.parse_strategy_reply(reply)
        assert isinstance(result, Ok)
        decision = result.value
        assert isinstance(decision, ContinueDecision)
        assert decision.directive_specs == (DirectiveSpec("s", "i", True),)
        assert decision.request_validation

    def test_valid_stop(self) -> None:
        result = strategy_calcs.parse_strategy_reply('{"action": "stop", "reason": "done"}')
        assert result == Ok(StopRequested("done"))

    def test_json_wrapped_in_fences_and_prose(self) -> None:
        inner = json.dumps(
            {"action": "continue", "directives": [{"approach_summary": "s", "instructions": "i"}]}
        )
        reply = f"Sure, here is my decision:\n```json\n{inner}\n```\nGood luck!"
        result = strategy_calcs.parse_strategy_reply(reply)
        assert isinstance(result, Ok)

    @pytest.mark.parametrize(
        "bad",
        [
            "not json at all",
            "[1, 2, 3]",
            '{"action": "dance"}',
            '{"action": "continue", "directives": []}',
            '{"action": "continue", "directives": ["not an object"]}',
            '{"action": "continue", "directives": [{"instructions": "i"}]}',
            '{"action": "continue", "directives": [{"approach_summary": "s"}]}',
            '{"action": "continue", "directives": [{"approach_summary": "s", '
            '"instructions": "i", "base_on_champion": "yes"}]}',
            '{"action": "continue", "directives": [{"approach_summary": "s", '
            '"instructions": "i"}], "request_validation": "yes"}',
        ],
    )
    def test_malformed(self, bad: str) -> None:
        assert isinstance(strategy_calcs.parse_strategy_reply(bad), Err)


class TestParseExecutorReply:
    def test_happy_path(self) -> None:
        result = strategy_calcs.parse_executor_reply("intro\n```solution\nprint(1)\n```\n")
        assert result == Ok("print(1)")

    def test_solution_may_contain_nested_fences(self) -> None:
        body = "# Skill\n\n```python\nprint(1)\n```\n\ndone"
        result = strategy_calcs.parse_executor_reply(f"```solution\n{body}\n```")
        assert result == Ok(body)

    @pytest.mark.parametrize(
        "bad",
        [
            "no fenced block here",
            "```solution\na\n```\n```solution\nb\n```",
            "```solution\nnever closed",
            "```solution\ncontent\n```\ntrailing prose breaks the contract",
            "```solution\n   \n```",
        ],
    )
    def test_contract_violations(self, bad: str) -> None:
        assert isinstance(strategy_calcs.parse_executor_reply(bad), Err)


class TestParseJudgeReply:
    def test_valid(self) -> None:
        result = strategy_calcs.parse_judge_reply('{"pass": true, "reason": "good"}')
        assert isinstance(result, Ok)
        assert result.value.passed

    def test_accepts_passed_alias(self) -> None:
        result = strategy_calcs.parse_judge_reply('{"passed": false}')
        assert isinstance(result, Ok)
        assert not result.value.passed

    def test_malformed(self) -> None:
        assert isinstance(strategy_calcs.parse_judge_reply('{"pass": "yes"}'), Err)
        assert isinstance(strategy_calcs.parse_judge_reply("garbage"), Err)


class TestDirectives:
    CHAMPION = ChampionArtifact("cand-1", ScriptSolution(), "the champion content")

    def test_champion_attached_only_when_requested(self) -> None:
        specs = (
            DirectiveSpec("refine", "improve it", True),
            DirectiveSpec("explore", "try fresh", False),
        )
        directives = strategy_calcs.build_directives(specs, Some(self.CHAMPION), 3, 4)
        assert directives[0].base == Some(self.CHAMPION)
        assert directives[1].base == Nothing()
        assert [d.directive_id for d in directives] == ["L3-d1", "L3-d2"]

    def test_capped_at_max_directives(self) -> None:
        specs = tuple(DirectiveSpec(f"s{i}", f"i{i}", False) for i in range(6))
        directives = strategy_calcs.build_directives(specs, NOTHING, 1, 2)
        assert len(directives) == 2

    def test_fallback_decision(self) -> None:
        with_champion = strategy_calcs.fallback_decision(True)
        assert with_champion.directive_specs[0].base_on_champion
        without = strategy_calcs.fallback_decision(False)
        assert not without.directive_specs[0].base_on_champion
        assert len(without.directive_specs) == 1


class TestLeaderboard:
    def test_fold_ranks_by_pass_rate_with_stable_ties(self) -> None:
        a = entry("a", (True, True, False, False), loop_index=1)
        b = entry("b", (True, True, True, False), loop_index=2)
        c = entry("c", (True, True, False, False), loop_index=3)
        board = strategy_calcs.fold_leaderboard((a,), (b, c))
        assert [e.candidate_id for e in board] == ["b", "a", "c"]
        best = strategy_calcs.dev_best(board)
        assert isinstance(best, Some)
        assert best.value.candidate_id == "b"
        assert strategy_calcs.dev_best(()) == Nothing()

    def test_beats_previous_best_is_ci_band_gated(self) -> None:
        weak = entry("weak", (False,) * 10)
        slightly = entry("slightly", (True, True, *([False] * 8)))
        much = entry("much", (True,) * 10)
        assert strategy_calcs.beats_previous_best(NOTHING, weak)
        assert not strategy_calcs.beats_previous_best(Some(weak), slightly)
        assert strategy_calcs.beats_previous_best(Some(weak), much)

    def test_render_lines_mark_noise(self) -> None:
        best = entry("best", (True,) * 12)
        rival = entry("rival", (True,) * 11 + (False,))
        lines = strategy_calcs.render_leaderboard_lines(
            strategy_calcs.fold_leaderboard((), (best, rival))
        )
        assert "best" in lines[0] and "not statistically distinguishable" not in lines[0]
        assert "not statistically distinguishable" in lines[1]
        assert strategy_calcs.render_leaderboard_lines(()) == ()

    def test_render_marks_aborted(self) -> None:
        aborted = LeaderboardEntry(
            "x", 1, ScriptSolution(), "c", score((True, False), aborted=True)
        )
        lines = strategy_calcs.render_leaderboard_lines((aborted,))
        assert "aborted" in lines[0]


class TestFailureSamples:
    def test_collects_failing_dev_examples_only(self) -> None:
        dev = tuple(DevExample(Example(f"e{i}", f"in{i}", f"out{i}")) for i in range(4))
        results = (
            ExampleResult("e0", ExecutionOk("wrong"), (NamedOutcome("c", Failed("no")),)),
            ExampleResult("e1", ExecutionOk("out1"), (NamedOutcome("c", Passed()),)),
            ExampleResult("e2", ExecutionFailed("crash"), (NamedOutcome("c", Failed("no")),)),
        )
        samples = strategy_calcs.failure_samples(dev, results)
        assert [s.dev_example.example.example_id for s in samples] == ["e0", "e2"]
        assert samples[0].actual_output == "wrong"
        assert "crash" in samples[1].actual_output
        assert samples[0].failed_checks == ("c",)

    def test_limit(self) -> None:
        dev = tuple(DevExample(Example(f"e{i}", f"in{i}", f"out{i}")) for i in range(10))
        results = tuple(
            ExampleResult(f"e{i}", ExecutionOk("bad"), (NamedOutcome("c", Failed("r")),))
            for i in range(10)
        )
        assert len(strategy_calcs.failure_samples(dev, results, limit=3)) == 3


def state_with(
    loops: int,
    leaderboard: tuple[LeaderboardEntry, ...] = (),
    checkpoints: tuple[ValCheckpoint, ...] = (),
    peeks: int = 0,
    last_peek: int | None = None,
    val_champion: ValidatedCandidate | None = None,
    fallbacks: int = 0,
) -> RunState:
    return RunState(
        loops_completed=loops,
        leaderboard=leaderboard,
        strategy_log=(),
        val_champion=Some(val_champion) if val_champion else NOTHING,
        checkpoints=checkpoints,
        peeks_used=peeks,
        last_peek_loop=Some(last_peek) if last_peek is not None else NOTHING,
        consecutive_fallbacks=fallbacks,
    )


class TestShouldValidate:
    def test_no_candidates_no_peek(self) -> None:
        assert not strategy_calcs.should_validate(state_with(1), config(), False, False)

    def test_scheduled_cadence(self) -> None:
        board = (entry("a", (True, False)),)
        # BASE_CONFIG has val_every = 1: every loop is a scheduled checkpoint.
        assert strategy_calcs.should_validate(state_with(1, board), config(), False, False)

    def test_budget_cap(self) -> None:
        board = (entry("a", (True, False)),)
        state = state_with(1, board, peeks=10)
        assert not strategy_calcs.should_validate(state, config(), True, True)

    def test_already_validated_candidate_is_skipped(self) -> None:
        board = (entry("a", (True, False)),)
        checkpoint = ValCheckpoint(1, "a", 0.5, 0.5, True)
        state = state_with(2, board, checkpoints=(checkpoint,), peeks=1, last_peek=1)
        assert not strategy_calcs.should_validate(state, config(), True, True)

    def test_triggered_peek_respects_min_gap(self) -> None:
        text = BASE_CONFIG.replace("val_every  = 1", "val_every  = 10").replace(
            "min_loops_between_peeks = 1", "min_loops_between_peeks = 3"
        )
        parsed = config_calcs.parse_config(text)
        assert isinstance(parsed, Ok)
        gapped = parsed.value
        board = (entry("b", (True, True)),)
        checkpoint = ValCheckpoint(1, "a", 0.5, 0.5, True)
        soon = state_with(2, board, checkpoints=(checkpoint,), peeks=1, last_peek=1)
        assert not strategy_calcs.should_validate(soon, gapped, True, True)
        later = state_with(4, board, checkpoints=(checkpoint,), peeks=1, last_peek=1)
        assert strategy_calcs.should_validate(later, gapped, True, True)
        # Without a trigger, nothing is scheduled until loop 10.
        assert not strategy_calcs.should_validate(later, gapped, False, False)


class TestSelectValChampion:
    def test_first_champion_counts_as_improvement(self) -> None:
        challenger = validated("a", (True,), (True,))
        winner, improved = strategy_calcs.select_val_champion(NOTHING, challenger)
        assert winner == challenger and improved

    def test_significant_challenger_displaces(self) -> None:
        incumbent = validated("old", (True,) * 8, (False,) * 8)
        challenger = validated("new", (True,) * 8, (True,) * 8)
        winner, improved = strategy_calcs.select_val_champion(Some(incumbent), challenger)
        assert winner == challenger and improved

    def test_significantly_worse_challenger_rejected(self) -> None:
        incumbent = validated("old", (True,) * 8, (True,) * 8)
        challenger = validated("new", (True,) * 8, (False,) * 8)
        winner, improved = strategy_calcs.select_val_champion(Some(incumbent), challenger)
        assert winner == incumbent and not improved

    def test_within_band_tie_broken_by_dev(self) -> None:
        incumbent = validated("old", (True, False, False, False), (True, True, False, False))
        challenger = validated("new", (True, True, False, False), (True, True, True, False))
        winner, improved = strategy_calcs.select_val_champion(Some(incumbent), challenger)
        assert winner == challenger and not improved

    def test_within_band_without_dev_edge_keeps_incumbent(self) -> None:
        incumbent = validated("old", (True, True, False, False), (True, True, False, False))
        challenger = validated("new", (True, True, False, False), (True, True, True, False))
        winner, improved = strategy_calcs.select_val_champion(Some(incumbent), challenger)
        assert winner == incumbent and not improved


class TestStoppingDecision:
    def test_budget(self) -> None:
        decision = strategy_calcs.stopping_decision(state_with(4), config())
        assert decision == Some(BudgetExhausted(4))
        assert strategy_calcs.stopping_decision(state_with(3), config()) == Nothing()

    def test_target_reached(self) -> None:
        text = BASE_CONFIG + "\n[validation.extra]\n"
        parsed = config_calcs.parse_config(
            text.replace("patience   = 3", "patience   = 3\ntarget_pass_rate = 0.9")
        )
        assert isinstance(parsed, Ok)
        champion = validated("a", (True,) * 4, (True,) * 4)
        state = state_with(1, val_champion=champion)
        decision = strategy_calcs.stopping_decision(state, parsed.value)
        assert decision == Some(TargetReached(1.0))

    def test_plateau_counts_only_non_improving_checkpoints(self) -> None:
        flat = tuple(ValCheckpoint(i, f"c{i}", 0.5, 0.5, False) for i in range(1, 4))
        decision = strategy_calcs.stopping_decision(state_with(2, checkpoints=flat), config())
        assert decision == Some(ValidationPlateau(3))
        improving = (*flat[:2], ValCheckpoint(3, "c3", 0.6, 0.6, True))
        assert (
            strategy_calcs.stopping_decision(state_with(2, checkpoints=improving), config())
            == Nothing()
        )

    def test_unresponsive_strategy(self) -> None:
        decision = strategy_calcs.stopping_decision(state_with(1, fallbacks=2), config())
        assert decision == Some(StrategyUnresponsive(2))


class TestContextAssembly:
    def test_windowed_compaction_and_champion(self) -> None:
        board = (entry("best", (True, True, True, False)),)
        log = tuple(
            StrategyLogEntry(
                loop_index=i,
                observations=f"obs{i}",
                hypotheses="",
                directives=(),
                dev_summary=f"summary {i}",
                val_summary=NOTHING,
                fallback=False,
            )
            for i in range(1, 8)
        )
        state = RunState(7, board, log, NOTHING, (), 0, NOTHING, 0)
        context = strategy_calcs.assemble_strategy_context(
            "task text", config(), state, (), ("exact_match",)
        )
        # BASE_CONFIG default: full detail for the last 4 loops.
        assert [e.loop_index for e in context.recent_log] == [4, 5, 6, 7]
        assert len(context.compacted_log) == 3
        assert "loop 1" in context.compacted_log[0]
        champion = context.champion
        assert isinstance(champion, Some)
        assert champion.value.candidate_id == "best"
        assert isinstance(context.champion_dev_line, Some)

    def test_gap_warning_appears_when_gap_is_wide(self) -> None:
        board = (entry("best", (True, True, True, False)),)
        wide = ValCheckpoint(
            1, "best", dev_pass_rate=0.9, val_pass_rate=0.5, displaced_champion=True
        )
        state = RunState(1, board, (), NOTHING, (wide,), 1, Some(1), 0)
        context = strategy_calcs.assemble_strategy_context(
            "task", config(), state, (), ("exact_match",)
        )
        assert isinstance(context.dev_val_gap_warning, Some)
        assert "overfitting" in context.dev_val_gap_warning.value

    def test_summary_lines(self) -> None:
        line = strategy_calcs.dev_summary_line(3, 1, Some(entry("a", (True, False))))
        assert "3 candidate(s)" in line and "1 malformed" in line and "50.0%" in line
        assert strategy_calcs.dev_summary_line(0, 0, NOTHING) == "0 candidate(s) scored"
        checkpoint = ValCheckpoint(2, "a", 0.8, 0.6, True)
        val_line = strategy_calcs.val_summary_line(checkpoint)
        assert "60.0%" in val_line and "new champion" in val_line and "+20.0%" in val_line
