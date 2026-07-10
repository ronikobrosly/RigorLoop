"""Shared test scaffolding: a toy uppercase task, fake shell dependencies, and
a scripted fake agent that plays strategy + executors deterministically."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from rigorloop.core import config_calcs
from rigorloop.core.types import (
    AgentCallError,
    AgentTextRequest,
    CallFailed,
    Err,
    Example,
    ExecutionFailed,
    ExecutionOk,
    ExecutionResult,
    Ok,
    Passed,
    Result,
    RunScriptRequest,
)
from rigorloop.shell.cli import LoadedProject, ShellDeps

TOY_N = 30

BASE_CONFIG = """\
[task]
description_file = "task.md"
solution_kind    = "script"
examples_file    = "examples.jsonl"

[split]
ratios = [0.6, 0.2, 0.2]
seed   = 17

[loop]
max_loops              = 4
executors_per_loop     = 2
dev_examples_in_prompt = 5

[validation]
val_every  = 1
max_peeks  = 10
min_loops_between_peeks = 1
patience   = 3

[agents]
model      = "claude-test"
timeout_s  = 30

[[checks]]
type = "exact_match"
"""


def toy_examples_jsonl(n: int = TOY_N) -> str:
    lines = [
        json.dumps(
            {"input": f"item {i:02d} alpha bravo", "expected_output": f"ITEM {i:02d} ALPHA BRAVO"}
        )
        for i in range(n)
    ]
    return "\n".join(lines) + "\n"


def toy_examples(n: int = TOY_N) -> tuple[Example, ...]:
    from rigorloop.core.dataset_calcs import parse_examples

    parsed = parse_examples(toy_examples_jsonl(n))
    assert isinstance(parsed, Ok)
    return parsed.value


def make_project(tmp_path: Path, config_text: str = BASE_CONFIG, n: int = TOY_N) -> LoadedProject:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "rigorloop.toml").write_text(config_text)
    (tmp_path / "task.md").write_text("# Task\nUppercase the input text exactly.\n")
    (tmp_path / "examples.jsonl").write_text(toy_examples_jsonl(n))
    parsed = config_calcs.parse_config(config_text)
    assert isinstance(parsed, Ok)
    return LoadedProject(
        config=parsed.value,
        config_text=config_text,
        task_description="# Task\nUppercase the input text exactly.\n",
        examples=toy_examples(n),
        duplicates=(),
    )


# --------------------------------------------------------------------------
# Fake shell dependencies
# --------------------------------------------------------------------------


def fake_script_runner(request: RunScriptRequest, scratch_dir: Path) -> ExecutionResult:
    """Interprets marker comments instead of spawning subprocesses, so E2E runs
    stay in-process. The real runner is covered in test_io_actions.

    `# FAIL_ON:<input>` lines make an otherwise-correct script emit garbage for
    those exact inputs — the deterministic stand-in for a candidate that does
    well on some examples and badly on others."""
    content = Path(request.script_path).read_text(encoding="utf-8")
    fail_on = {
        line.removeprefix("# FAIL_ON:")
        for line in content.splitlines()
        if line.startswith("# FAIL_ON:")
    }
    if request.stdin_text in fail_on:
        return ExecutionOk("WRONG OUTPUT")
    if "MODE=UPPER" in content:
        return ExecutionOk(request.stdin_text.upper())
    if "MODE=LOWER" in content:
        return ExecutionOk(request.stdin_text.lower())
    if "MODE=CRASH" in content:
        return ExecutionFailed("deliberate crash")
    return ExecutionFailed("unknown fake script mode")


@dataclass
class Recorder:
    """Mutable test double capturing every agent request and script execution."""

    agent_handler: Callable[[AgentTextRequest], Result[str, AgentCallError]]
    agent_requests: list[AgentTextRequest] = field(default_factory=list)
    script_inputs: list[str] = field(default_factory=list)
    echoes: list[str] = field(default_factory=list)

    def run_agent(self, request: AgentTextRequest) -> Result[str, AgentCallError]:
        self.agent_requests.append(request)
        return self.agent_handler(request)

    def run_script(self, request: RunScriptRequest, scratch_dir: Path) -> ExecutionResult:
        self.script_inputs.append(request.stdin_text)
        return fake_script_runner(request, scratch_dir)

    def deps(self) -> ShellDeps:
        return ShellDeps(
            agent_runner=self.run_agent,
            calls_made=lambda: len(self.agent_requests),
            script_runner=self.run_script,
            custom_check_runner=lambda _path, _example, _actual: Passed(),
            make_run_id=lambda: "run-test",
            cli_version=lambda: "claude-stub 0.0",
            echo=self.echoes.append,
        )


# --------------------------------------------------------------------------
# The scripted fake agent
# --------------------------------------------------------------------------


def loops_completed_in(prompt: str) -> int:
    match = re.search(r"Loops completed: (\d+) of", prompt)
    assert match is not None
    return int(match.group(1))


def strategy_reply(*directives: tuple[str, bool], validate: bool = False) -> str:
    return json.dumps(
        {
            "action": "continue",
            "observations": "fake observations",
            "hypotheses": "fake hypotheses",
            "directives": [
                {
                    "approach_summary": f"approach {i}",
                    "instructions": instructions,
                    "base_on_champion": base,
                }
                for i, (instructions, base) in enumerate(directives, start=1)
            ],
            "request_validation": validate,
        }
    )


def solution_block(body: str) -> str:
    return f"Here is my candidate.\n\n```solution\n{body}\n```\n"


def scripted_agent(request: AgentTextRequest) -> Result[str, AgentCallError]:
    """Loop 1 directs a weak (lowercasing) script; later loops refine to the
    correct uppercasing script."""
    prompt = request.user_prompt
    if "You are the strategy agent" in prompt:
        if loops_completed_in(prompt) == 0:
            return Ok(strategy_reply(("Build the script with MODE=LOWER", False)))
        return Ok(strategy_reply(("Refine using MODE=UPPER", True)))
    if "You are an executor agent" in prompt:
        mode = "MODE=UPPER" if "MODE=UPPER" in prompt else "MODE=LOWER"
        return Ok(solution_block(f"# {mode}\nimport sys\nprint(sys.stdin.read())"))
    return Err(CallFailed(f"unexpected prompt: {prompt[:80]}"))


@pytest.fixture
def recorder() -> Recorder:
    return Recorder(agent_handler=scripted_agent)
