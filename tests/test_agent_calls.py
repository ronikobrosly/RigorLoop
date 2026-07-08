"""Shell integration: the claude subprocess wrapper against a stub executable
that mimics `claude -p --output-format json`."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from rigorloop.core.types import (
    NOTHING,
    AgentTextRequest,
    CallFailed,
    CallTimeout,
    EnvelopeError,
    Err,
    Ok,
    Some,
)
from rigorloop.shell import agent_calls

pytestmark = pytest.mark.integration


def make_stub(tmp_path: Path, body: str) -> str:
    """A tiny python-backed `claude` stand-in; `body` decides its behavior."""
    stub = tmp_path / "claude-stub"
    stub.write_text(f"#!/usr/bin/env python3\nimport json, sys, time\n{body}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return str(stub)


def request(timeout_s: float = 10.0) -> AgentTextRequest:
    return AgentTextRequest("the prompt", NOTHING, "model-x", timeout_s)


class TestBuildArgs:
    def test_headless_no_tools_json(self) -> None:
        args = agent_calls.build_claude_args(request(), "claude")
        assert args == [
            "claude",
            "-p",
            "--tools",
            "",
            "--output-format",
            "json",
            "--model",
            "model-x",
        ]

    def test_system_prompt_appended(self) -> None:
        with_system = AgentTextRequest("p", Some("SYSTEM DOC"), "m", 1.0)
        args = agent_calls.build_claude_args(with_system, "claude")
        assert args[-2:] == ["--append-system-prompt", "SYSTEM DOC"]


class TestParseEnvelope:
    def test_success(self) -> None:
        assert agent_calls.parse_envelope(json.dumps({"result": "hello"})) == Ok("hello")

    def test_is_error_flag(self) -> None:
        parsed = agent_calls.parse_envelope(json.dumps({"is_error": True, "result": "boom"}))
        assert parsed == Err(CallFailed("boom"))

    def test_garbage(self) -> None:
        parsed = agent_calls.parse_envelope("not json")
        assert isinstance(parsed, Err)
        assert isinstance(parsed.error, EnvelopeError)
        not_object = agent_calls.parse_envelope('["a"]')
        assert isinstance(not_object, Err)
        no_result = agent_calls.parse_envelope('{"other": 1}')
        assert isinstance(no_result, Err)


class TestRunAgentOnce:
    def test_success_echoes_prompt(self, tmp_path: Path) -> None:
        stub = make_stub(
            tmp_path,
            "print(json.dumps({'result': 'echo: ' + sys.stdin.read()}))",
        )
        result = agent_calls.run_agent_once(request(), stub)
        assert result == Ok("echo: the prompt")

    def test_nonzero_exit(self, tmp_path: Path) -> None:
        stub = make_stub(tmp_path, "sys.stderr.write('kaput'); sys.exit(3)")
        result = agent_calls.run_agent_once(request(), stub)
        assert isinstance(result, Err)
        assert isinstance(result.error, CallFailed)
        assert "kaput" in result.error.detail

    def test_timeout(self, tmp_path: Path) -> None:
        stub = make_stub(tmp_path, "time.sleep(5)")
        result = agent_calls.run_agent_once(request(timeout_s=0.3), stub)
        assert result == Err(CallTimeout(0.3))

    def test_missing_executable(self) -> None:
        result = agent_calls.run_agent_once(request(), "/nonexistent/claude")
        assert isinstance(result, Err)
        assert isinstance(result.error, CallFailed)


class TestMakeRunner:
    def test_counts_calls_and_retries_failures_once(self, tmp_path: Path) -> None:
        # Fails on the first invocation, succeeds after (state via a marker file).
        marker = tmp_path / "called-once"
        stub = make_stub(
            tmp_path,
            "import os\n"
            f"m = {str(marker)!r}\n"
            "if not os.path.exists(m):\n"
            "    open(m, 'w').close(); sys.exit(1)\n"
            "print(json.dumps({'result': 'recovered'}))",
        )
        runner, calls_made = agent_calls.make_runner(stub)
        assert runner(request()) == Ok("recovered")
        assert calls_made() == 2

    def test_success_costs_one_call(self, tmp_path: Path) -> None:
        stub = make_stub(tmp_path, "print(json.dumps({'result': 'ok'}))")
        runner, calls_made = agent_calls.make_runner(stub)
        assert runner(request()) == Ok("ok")
        assert calls_made() == 1


class TestRunConcurrently:
    def test_preserves_order(self, tmp_path: Path) -> None:
        stub = make_stub(tmp_path, "print(json.dumps({'result': sys.stdin.read().upper()}))")
        runner, _ = agent_calls.make_runner(stub)
        requests = tuple(AgentTextRequest(f"req {i}", NOTHING, "m", 10.0) for i in range(5))
        results = agent_calls.run_concurrently(requests, runner, max_workers=3)
        assert [r.value for r in results if isinstance(r, Ok)] == [f"REQ {i}" for i in range(5)]

    def test_empty(self) -> None:
        runner, _ = agent_calls.make_runner("claude")
        assert agent_calls.run_concurrently((), runner, 4) == ()
