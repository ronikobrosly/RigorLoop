"""First-class leakage tests.

The guarantee: no *agent-context* prompt (strategy or executor) produced during
a full simulated run ever contains validation or test example content. The
evaluation channel (running a candidate or a judge on one example) is exempt
by design; its outputs return to the harness as scores only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rigorloop.core import strategy_calcs
from rigorloop.core.prompt_calcs import render_directive
from rigorloop.core.types import (
    NOTHING,
    AgentTextRequest,
    ChampionArtifact,
    DirectiveSpec,
    ScriptSolution,
    Some,
)
from rigorloop.shell.cli import execute_run
from tests.conftest import Recorder, make_project, scripted_agent
from tests.test_e2e import TestJudgeCheck, TestSkillKind

pytestmark = pytest.mark.e2e


def agent_context_prompts(requests: list[AgentTextRequest]) -> list[str]:
    """Agent-context prompts are exactly the strategy/executor calls; both
    channels flow through the same runner, so classify by their preambles."""
    return [
        r.user_prompt
        for r in requests
        if "You are the strategy agent" in r.user_prompt
        or "You are an executor agent" in r.user_prompt
    ]


def holdout_fragments(project_dir: Path) -> list[str]:
    fragments: list[str] = []
    for split_name in ("val", "test"):
        path = project_dir / "runs" / "run-test" / "splits" / f"{split_name}.jsonl"
        for line in path.read_text().splitlines():
            record = json.loads(line)
            fragments.append(record["input"])
            fragments.append(record["expected_output"])
    return fragments


def assert_no_holdout_leakage(project_dir: Path, recorder: Recorder) -> None:
    context_prompts = agent_context_prompts(recorder.agent_requests)
    assert context_prompts, "expected strategy/executor prompts to have been built"
    fragments = holdout_fragments(project_dir)
    assert fragments
    for prompt in context_prompts:
        for fragment in fragments:
            assert fragment not in prompt, (
                f"holdout content leaked into an agent-context prompt: {fragment!r}"
            )


class TestNoValTestLeakage:
    def test_script_run_with_validation_peeks(self, tmp_path: Path) -> None:
        recorder = Recorder(agent_handler=scripted_agent)
        project = make_project(tmp_path)
        assert execute_run(project, tmp_path, recorder.deps(), NOTHING) == 0
        assert_no_holdout_leakage(tmp_path, recorder)

    def test_skill_run_evaluation_channel_is_exempt_but_contained(self, tmp_path: Path) -> None:
        recorder = Recorder(agent_handler=TestSkillKind.skill_agent)
        project = make_project(tmp_path, TestSkillKind.CONFIG)
        assert execute_run(project, tmp_path, recorder.deps(), NOTHING) == 0
        assert_no_holdout_leakage(tmp_path, recorder)
        # Sanity check on the sanctioned channel: evaluation prompts DO carry
        # holdout inputs (that is their job), proving the scan has teeth.
        eval_prompts = [
            r.user_prompt for r in recorder.agent_requests if isinstance(r.system_prompt, Some)
        ]
        fragments = holdout_fragments(tmp_path)
        assert any(any(f in p for f in fragments) for p in eval_prompts)

    def test_judge_run(self, tmp_path: Path) -> None:
        recorder = Recorder(agent_handler=TestJudgeCheck.judging_agent)
        project = make_project(tmp_path, TestJudgeCheck.CONFIG)
        assert execute_run(project, tmp_path, recorder.deps(), NOTHING) == 0
        assert_no_holdout_leakage(tmp_path, recorder)


class TestDirectiveCarryForward:
    def test_directive_embeds_champion_content_and_nothing_else(self) -> None:
        champion = ChampionArtifact("cand-1", ScriptSolution(), "def solve(): ...")
        directives = strategy_calcs.build_directives(
            (DirectiveSpec("refine", "make it handle unicode", True),),
            Some(champion),
            loop_index=5,
            max_directives=4,
        )
        rendered = render_directive(directives[0])
        assert "def solve(): ..." in rendered
        # No scores, mistakes, or per-example failures can ride along: the
        # ChampionArtifact type carries content only, and the rendering adds
        # only the directive's own text.
        stripped = (
            rendered.replace("def solve(): ...", "")
            .replace("refine", "")
            .replace("make it handle unicode", "")
        )
        for forbidden in ("%", "pass", "fail", "score", "mistake", "leaderboard"):
            assert forbidden not in stripped.lower(), forbidden

    def test_champion_artifact_type_carries_content_only(self) -> None:
        fields = {f.name for f in ChampionArtifact.__dataclass_fields__.values()}
        assert fields == {"candidate_id", "kind", "content"}


class TestTypeLevelGuards:
    def test_agent_context_builders_reject_val_examples_by_type(self) -> None:
        """The static guarantee: build_executor_prompt accepts DevExample only.
        (mypy --strict enforces this at type-check time; here we assert the
        annotation so a refactor can't silently widen it.)"""
        import inspect

        from rigorloop.core import prompt_calcs

        signature = inspect.signature(prompt_calcs.build_executor_prompt)
        assert "DevExample" in str(signature.parameters["dev_sample"].annotation)
        context_sig = inspect.signature(strategy_calcs.failure_samples)
        assert "DevExample" in str(context_sig.parameters["dev"].annotation)

    def test_prompt_channels_are_distinct_types(self) -> None:
        from rigorloop.core.types import AgentContextPrompt, EvalPrompt

        assert not issubclass(AgentContextPrompt, EvalPrompt)
        assert not issubclass(EvalPrompt, AgentContextPrompt)
