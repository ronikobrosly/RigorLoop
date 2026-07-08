"""Shell I/O: JSON round-trips for persisted state, the script sandbox, and
custom-check execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from rigorloop.core.types import (
    BudgetExhausted,
    CandidateScore,
    ChampionArtifact,
    CheckPassRate,
    Directive,
    Errored,
    Example,
    ExecutionFailed,
    ExecutionOk,
    Failed,
    GuidanceSolution,
    LeaderboardEntry,
    Passed,
    RunScriptRequest,
    RunState,
    ScriptSolution,
    SkillSolution,
    Some,
    StopReason,
    StrategyLogEntry,
    StrategyRequestedStop,
    StrategyUnresponsive,
    TargetReached,
    ValCheckpoint,
    ValidatedCandidate,
    ValidationPlateau,
)
from rigorloop.shell import io_actions

pytestmark = pytest.mark.integration

SCORE = CandidateScore(
    n=4,
    passes=3,
    pass_rate=0.75,
    ci_low=0.3,
    ci_high=0.95,
    per_check=(CheckPassRate("exact_match", 3, 4),),
    pass_vector=(True, True, True, False),
    eval_aborted=False,
)


def full_state() -> RunState:
    entry = LeaderboardEntry("cand-1", 1, ScriptSolution(), "print(1)", SCORE)
    directive = Directive(
        "L1-d1", "summary", "instructions", Some(ChampionArtifact("c0", SkillSolution(), "doc"))
    )
    log_entry = StrategyLogEntry(1, "obs", "hyp", (directive,), "dev summary", Some("val"), True)
    checkpoint = ValCheckpoint(1, "cand-1", 0.75, 0.6, True)
    champion = ValidatedCandidate("cand-1", ScriptSolution(), "print(1)", SCORE, SCORE)
    return RunState(
        loops_completed=2,
        leaderboard=(entry,),
        strategy_log=(log_entry,),
        val_champion=Some(champion),
        checkpoints=(checkpoint,),
        peeks_used=1,
        last_peek_loop=Some(1),
        consecutive_fallbacks=1,
    )


class TestRoundTrips:
    def test_score(self) -> None:
        assert io_actions.score_from_json(io_actions.score_to_json(SCORE)) == SCORE

    def test_state(self) -> None:
        state = full_state()
        assert io_actions.state_from_json(io_actions.state_to_json(state)) == state

    def test_initial_state(self) -> None:
        from rigorloop.core.strategy_calcs import initial_state

        state = initial_state()
        assert io_actions.state_from_json(io_actions.state_to_json(state)) == state

    def test_example(self) -> None:
        example = Example("id-1", "in", "out")
        assert io_actions.example_from_json(io_actions.example_to_json(example)) == example

    def test_validated(self) -> None:
        champion = ValidatedCandidate("c", GuidanceSolution(), "doc", SCORE, SCORE)
        assert io_actions.validated_from_json(io_actions.validated_to_json(champion)) == champion

    @pytest.mark.parametrize(
        "reason",
        [
            BudgetExhausted(4),
            ValidationPlateau(2),
            TargetReached(0.95),
            StrategyRequestedStop("done"),
            StrategyUnresponsive(2),
        ],
    )
    def test_stop_reasons(self, reason: StopReason) -> None:
        data = io_actions.stop_reason_to_json(reason)
        assert io_actions.stop_reason_from_json(data) == reason

    def test_kind_names(self) -> None:
        for kind in (ScriptSolution(), SkillSolution(), GuidanceSolution()):
            assert io_actions.kind_from_name(io_actions.kind_to_name(kind)) == kind


class TestFilePrimitives:
    def test_write_read_json(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "file.json"
        io_actions.write_json(path, {"a": [1, 2]})
        assert io_actions.read_json(path) == {"a": [1, 2]}

    def test_append_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        io_actions.append_jsonl(path, {"n": 1})
        io_actions.append_jsonl(path, {"n": 2})
        lines = path.read_text().splitlines()
        assert len(lines) == 2

    def test_examples_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "ex.jsonl"
        io_actions.write_examples_jsonl(path, (Example("i", "a", "b"),))
        assert '"input": "a"' in path.read_text()

    def test_layout_helpers(self, tmp_path: Path) -> None:
        run = io_actions.run_dir(tmp_path, "r1")
        assert run == tmp_path / "runs" / "r1"
        assert io_actions.loop_dir(run, 3) == run / "loops" / "3"
        assert io_actions.candidate_dir(run, 3, "c") == run / "loops" / "3" / "candidates" / "c"
        assert io_actions.solution_filename(ScriptSolution()) == "solution.py"
        assert io_actions.solution_filename(SkillSolution()) == "SKILL.md"
        assert io_actions.solution_filename(GuidanceSolution()) == "GUIDANCE.md"


class TestScriptSandbox:
    def write_script(self, tmp_path: Path, body: str) -> str:
        script = tmp_path / "solution.py"
        script.write_text(body)
        return str(script)

    def test_success_sheds_one_trailing_newline(self, tmp_path: Path) -> None:
        script = self.write_script(tmp_path, "import sys\nprint(sys.stdin.read().upper())")
        result = io_actions.run_script(
            RunScriptRequest(script, "hello", 30.0), tmp_path / "scratch"
        )
        assert result == ExecutionOk("HELLO")

    def test_nonzero_exit(self, tmp_path: Path) -> None:
        script = self.write_script(tmp_path, "import sys\nsys.exit('bad input')")
        result = io_actions.run_script(RunScriptRequest(script, "x", 30.0), tmp_path / "s")
        assert isinstance(result, ExecutionFailed)
        assert "bad input" in result.detail

    def test_timeout(self, tmp_path: Path) -> None:
        script = self.write_script(tmp_path, "import time\ntime.sleep(5)")
        result = io_actions.run_script(RunScriptRequest(script, "x", 0.3), tmp_path / "s")
        assert isinstance(result, ExecutionFailed)
        assert "timed out" in result.detail

    def test_output_cap(self, tmp_path: Path) -> None:
        script = self.write_script(tmp_path, "print('x' * 300_000)")
        result = io_actions.run_script(RunScriptRequest(script, "x", 30.0), tmp_path / "s")
        assert isinstance(result, ExecutionFailed)
        assert "exceeded" in result.detail

    def test_runs_in_scratch_cwd(self, tmp_path: Path) -> None:
        script = self.write_script(tmp_path, "import os\nprint(os.getcwd())")
        scratch = tmp_path / "scratch-dir"
        result = io_actions.run_script(RunScriptRequest(script, "", 30.0), scratch)
        assert isinstance(result, ExecutionOk)
        assert result.output_text == str(scratch)


class TestCustomCheck:
    EXAMPLE = Example("e1", "the input", "expected")

    def write_check(self, tmp_path: Path, body: str) -> str:
        script = tmp_path / "check.py"
        script.write_text(body)
        return str(script)

    def test_pass_and_payload(self, tmp_path: Path) -> None:
        script = self.write_check(
            tmp_path,
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "assert payload['input'] == 'the input'\n"
            "assert payload['expected_output'] == 'expected'\n"
            "sys.exit(0 if payload['actual_output'] == 'expected' else 1)",
        )
        assert io_actions.run_custom_check(script, self.EXAMPLE, "expected") == Passed()
        outcome = io_actions.run_custom_check(script, self.EXAMPLE, "wrong")
        assert isinstance(outcome, Failed)

    def test_fail_reason_from_stderr(self, tmp_path: Path) -> None:
        script = self.write_check(tmp_path, "import sys\nsys.stderr.write('bad tone')\nsys.exit(1)")
        outcome = io_actions.run_custom_check(script, self.EXAMPLE, "x")
        assert outcome == Failed("bad tone")

    def test_error_exit_codes(self, tmp_path: Path) -> None:
        script = self.write_check(tmp_path, "import sys\nsys.exit(7)")
        outcome = io_actions.run_custom_check(script, self.EXAMPLE, "x")
        assert isinstance(outcome, Errored)

    def test_timeout(self, tmp_path: Path) -> None:
        script = self.write_check(tmp_path, "import time\ntime.sleep(5)")
        outcome = io_actions.run_custom_check(script, self.EXAMPLE, "x", timeout_s=0.3)
        assert isinstance(outcome, Errored)
