"""End-to-end runs with fake agents: a full multi-loop protocol (strategy
turns, executor fan-out, dev scoring, validation checkpoints, finalization)
executing in-process with deterministic assertions on the artifacts."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from rigorloop.core.strategy_calcs import initial_state
from rigorloop.core.types import (
    NOTHING,
    AgentCallError,
    AgentTextRequest,
    CallFailed,
    Err,
    Ok,
    Result,
    Some,
)
from rigorloop.shell import io_actions
from rigorloop.shell.cli import execute_run
from tests.conftest import (
    BASE_CONFIG,
    Recorder,
    loops_completed_in,
    make_project,
    scripted_agent,
    solution_block,
    strategy_reply,
)

pytestmark = pytest.mark.e2e


def run_project(tmp_path: Path, recorder: Recorder, config_text: str = BASE_CONFIG) -> int:
    project = make_project(tmp_path, config_text)
    return execute_run(project, tmp_path, recorder.deps(), NOTHING)


class TestFullScriptRun:
    def test_run_finalizes_with_the_strong_candidate(
        self, tmp_path: Path, recorder: Recorder
    ) -> None:
        exit_code = run_project(tmp_path, recorder)
        assert exit_code == 0

        run_path = tmp_path / "runs" / "run-test"
        report = (run_path / "final" / "report.md").read_text()
        results = json.loads((run_path / "final" / "test_results.json").read_text())
        assert results["winner"]["candidate_id"] == "cand-L2-d1"
        assert results["test_score"]["pass_rate"] == 1.0
        assert "loop budget exhausted" in report
        assert (run_path / "final" / "solution.py").read_text().startswith("# MODE=UPPER")
        assert (run_path / "manifest.json").is_file()
        assert (run_path / "splits" / "test.jsonl").is_file()
        assert (run_path / "leaderboard.json").is_file()

    def test_test_set_is_evaluated_exactly_once(self, tmp_path: Path, recorder: Recorder) -> None:
        run_project(tmp_path, recorder)
        run_path = tmp_path / "runs" / "run-test"
        test_lines = (run_path / "splits" / "test.jsonl").read_text().splitlines()
        test_inputs = {json.loads(line)["input"] for line in test_lines}
        executions = Counter(recorder.script_inputs)
        for test_input in test_inputs:
            assert executions[test_input] == 1, "test example must be read exactly once"

    def test_val_set_only_touched_at_checkpoints(self, tmp_path: Path, recorder: Recorder) -> None:
        run_project(tmp_path, recorder)
        run_path = tmp_path / "runs" / "run-test"
        state = json.loads((run_path / "state.json").read_text())
        val_lines = (run_path / "splits" / "val.jsonl").read_text().splitlines()
        val_inputs = {json.loads(line)["input"] for line in val_lines}
        executions = Counter(recorder.script_inputs)
        peeks = state["peeks_used"]
        assert peeks >= 1
        for val_input in val_inputs:
            assert executions[val_input] <= peeks

    def test_strategy_log_and_loop_artifacts(self, tmp_path: Path, recorder: Recorder) -> None:
        run_project(tmp_path, recorder)
        run_path = tmp_path / "runs" / "run-test"
        log_lines = (run_path / "strategy_log.jsonl").read_text().splitlines()
        assert len(log_lines) == 4  # max_loops
        loop1 = run_path / "loops" / "1"
        assert (loop1 / "strategy_prompt.md").is_file()
        assert (loop1 / "strategy_reply.md").is_file()
        assert (loop1 / "candidates" / "cand-L1-d1" / "solution.py").is_file()
        assert (loop1 / "candidates" / "cand-L1-d1" / "scores.json").is_file()
        assert (loop1 / "candidates" / "cand-L1-d1" / "outputs.jsonl").is_file()


class TestStrategyStop:
    def test_strategy_requested_stop(self, tmp_path: Path) -> None:
        def stopping_agent(request: AgentTextRequest) -> Result[str, AgentCallError]:
            prompt = request.user_prompt
            if "You are the strategy agent" in prompt:
                if loops_completed_in(prompt) >= 2:
                    return Ok('{"action": "stop", "reason": "no more ideas"}')
                return Ok(strategy_reply(("Build MODE=UPPER", False)))
            return scripted_agent(request)

        recorder = Recorder(agent_handler=stopping_agent)
        assert run_project(tmp_path, recorder) == 0
        results = json.loads(
            (tmp_path / "runs" / "run-test" / "final" / "test_results.json").read_text()
        )
        assert results["stop_reason"]["type"] == "strategy_requested_stop"
        assert results["stop_reason"]["reason"] == "no more ideas"


class TestMalformedRepliesAreSurvivable:
    def test_malformed_strategy_reply_uses_fallback_then_recovers(self, tmp_path: Path) -> None:
        def flaky_strategy(request: AgentTextRequest) -> Result[str, AgentCallError]:
            prompt = request.user_prompt
            if "You are the strategy agent" in prompt:
                if loops_completed_in(prompt) == 0:
                    return Ok("utter garbage, not json")
                return Ok(strategy_reply(("Refine MODE=UPPER", True)))
            return scripted_agent(request)

        recorder = Recorder(agent_handler=flaky_strategy)
        assert run_project(tmp_path, recorder) == 0
        log_lines = [
            json.loads(line)
            for line in (tmp_path / "runs" / "run-test" / "strategy_log.jsonl")
            .read_text()
            .splitlines()
        ]
        assert log_lines[0]["fallback"] is True
        assert log_lines[1]["fallback"] is False

    def test_unresponsive_strategy_stops_the_run(self, tmp_path: Path) -> None:
        def broken_strategy(request: AgentTextRequest) -> Result[str, AgentCallError]:
            if "You are the strategy agent" in request.user_prompt:
                return Ok("never json")
            return scripted_agent(request)

        recorder = Recorder(agent_handler=broken_strategy)
        exit_code = run_project(tmp_path, recorder)
        # Fallback directives still produce candidates, so the run finalizes.
        assert exit_code == 0
        results = json.loads(
            (tmp_path / "runs" / "run-test" / "final" / "test_results.json").read_text()
        )
        assert results["stop_reason"]["type"] == "strategy_unresponsive"

    def test_all_executors_malformed_yields_no_result(self, tmp_path: Path) -> None:
        def no_solutions(request: AgentTextRequest) -> Result[str, AgentCallError]:
            prompt = request.user_prompt
            if "You are the strategy agent" in prompt:
                return Ok(strategy_reply(("Build MODE=UPPER", False)))
            if "You are an executor agent" in prompt:
                return Ok("I decline to answer in the required format.")
            return Err(CallFailed("unexpected"))

        recorder = Recorder(agent_handler=no_solutions)
        assert run_project(tmp_path, recorder) == 2
        assert not (tmp_path / "runs" / "run-test" / "final" / "report.md").exists()

    def test_executor_reformat_retry_recovers(self, tmp_path: Path) -> None:
        seen_retries: list[bool] = []

        def flaky_executor(request: AgentTextRequest) -> Result[str, AgentCallError]:
            prompt = request.user_prompt
            if "You are the strategy agent" in prompt:
                return Ok(strategy_reply(("Build MODE=UPPER", False)))
            if "You are an executor agent" in prompt:
                if "previous reply was rejected" in prompt:
                    seen_retries.append(True)
                    return Ok(solution_block("# MODE=UPPER\npass"))
                return Ok("no fenced block, oops")
            return Err(CallFailed("unexpected"))

        recorder = Recorder(agent_handler=flaky_executor)
        assert run_project(tmp_path, recorder) == 0
        assert seen_retries


class TestResume:
    def test_interrupted_run_resumes_and_finishes(self, tmp_path: Path) -> None:
        calls = {"n": 0}

        def dies_in_loop_two(request: AgentTextRequest) -> Result[str, AgentCallError]:
            if "You are the strategy agent" in request.user_prompt:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("simulated crash mid-run")
            return scripted_agent(request)

        project = make_project(tmp_path)
        crashing = Recorder(agent_handler=dies_in_loop_two)
        with pytest.raises(RuntimeError):
            execute_run(project, tmp_path, crashing.deps(), NOTHING)

        state = json.loads((tmp_path / "runs" / "run-test" / "state.json").read_text())
        assert state["loops_completed"] == 1

        healthy = Recorder(agent_handler=scripted_agent)
        assert execute_run(project, tmp_path, healthy.deps(), Some("run-test")) == 0
        final_state = json.loads((tmp_path / "runs" / "run-test" / "state.json").read_text())
        assert final_state["loops_completed"] == 4
        assert (tmp_path / "runs" / "run-test" / "final" / "report.md").is_file()

    def test_resume_refuses_dataset_drift(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        recorder = Recorder(agent_handler=scripted_agent)
        run_path = tmp_path / "runs" / "run-test"

        # Fabricate an unfinished run whose manifest disagrees with the dataset.
        io_actions.write_json(run_path / "manifest.json", {"manifest": {"seed": 999}})
        io_actions.write_json(run_path / "state.json", io_actions.state_to_json(initial_state()))
        exit_code = execute_run(project, tmp_path, recorder.deps(), Some("run-test"))
        assert exit_code == 1
        assert any("reshuffle" in line for line in recorder.echoes)

    def test_resume_of_unknown_or_finalized_run(self, tmp_path: Path, recorder: Recorder) -> None:
        project = make_project(tmp_path)
        assert execute_run(project, tmp_path, recorder.deps(), Some("missing")) == 1

        finished = Recorder(agent_handler=scripted_agent)
        assert execute_run(project, tmp_path, finished.deps(), NOTHING) == 0
        again = Recorder(agent_handler=scripted_agent)
        assert execute_run(project, tmp_path, again.deps(), Some("run-test")) == 1
        assert any("already finalized" in line for line in again.echoes)


class TestSkillKind:
    CONFIG = BASE_CONFIG.replace('solution_kind    = "script"', 'solution_kind    = "skill"')

    @staticmethod
    def skill_agent(request: AgentTextRequest) -> Result[str, AgentCallError]:
        prompt = request.user_prompt
        if isinstance(request.system_prompt, Some):
            # Evaluation channel: an agent following the skill document.
            if "UPPERCASE" in request.system_prompt.value:
                return Ok(prompt.upper())
            return Ok(prompt.lower())
        if "You are the strategy agent" in prompt:
            if loops_completed_in(prompt) == 0:
                return Ok(strategy_reply(("Write a skill that says lowercase", False)))
            return Ok(strategy_reply(("Write a skill that says UPPERCASE", True)))
        if "You are an executor agent" in prompt:
            word = "UPPERCASE" if "UPPERCASE" in prompt else "lowercase"
            return Ok(solution_block(f"# Skill\n\nAlways {word} the input and reply with it."))
        return Err(CallFailed("unexpected"))

    def test_skill_run_finalizes(self, tmp_path: Path) -> None:
        recorder = Recorder(agent_handler=self.skill_agent)
        assert run_project(tmp_path, recorder, self.CONFIG) == 0
        run_path = tmp_path / "runs" / "run-test"
        results = json.loads((run_path / "final" / "test_results.json").read_text())
        assert results["test_score"]["pass_rate"] == 1.0
        assert (run_path / "final" / "SKILL.md").read_text().startswith("# Skill")
        report = (run_path / "final" / "report.md").read_text()
        assert "Stochastic evaluator caveat" in report


class TestJudgeCheck:
    CONFIG = (
        BASE_CONFIG + '\n[[checks]]\ntype = "llm_judge"\nrubric = "matches gold"\nn_samples = 3\n'
    )

    @staticmethod
    def judging_agent(request: AgentTextRequest) -> Result[str, AgentCallError]:
        prompt = request.user_prompt
        if "You are grading one candidate output" in prompt:
            expected = prompt.split("# Expected output (gold standard)\n")[1].split(
                "\n\n# Candidate output"
            )[0]
            actual = prompt.split("# Candidate output\n")[1].split("\n\nReply with")[0]
            verdict = expected.strip() == actual.strip()
            return Ok(json.dumps({"pass": verdict, "reason": "checked"}))
        return scripted_agent(request)

    def test_judge_votes_are_aggregated(self, tmp_path: Path) -> None:
        recorder = Recorder(agent_handler=self.judging_agent)
        assert run_project(tmp_path, recorder, self.CONFIG) == 0
        results = json.loads(
            (tmp_path / "runs" / "run-test" / "final" / "test_results.json").read_text()
        )
        per_check = {c["check"]: c for c in results["test_score"]["per_check"]}
        assert set(per_check) == {"exact_match", "llm_judge"}
        assert results["test_score"]["pass_rate"] == 1.0
        judge_calls = [r for r in recorder.agent_requests if "You are grading" in r.user_prompt]
        assert len(judge_calls) % 3 == 0 and judge_calls
