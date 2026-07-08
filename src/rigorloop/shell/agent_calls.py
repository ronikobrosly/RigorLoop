"""Shell wrapper around the claude CLI: subprocess calls, JSON envelope
parsing, transport retries, and concurrency. All CLI flags live in one
function so flag drift is absorbed in one place."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from rigorloop.core.types import (
    AgentCallError,
    AgentTextRequest,
    CallFailed,
    CallTimeout,
    EnvelopeError,
    Err,
    Nothing,
    Ok,
    Result,
    Some,
)

AgentRunner = Callable[[AgentTextRequest], Result[str, AgentCallError]]

_STDERR_PREVIEW = 500


def build_claude_args(request: AgentTextRequest, claude_cmd: str) -> list[str]:
    """The single place that knows the claude CLI's flags: headless, no tools,
    JSON envelope output. The prompt itself travels via stdin."""
    args = [
        claude_cmd,
        "-p",
        "--tools",
        "",
        "--output-format",
        "json",
        "--model",
        request.model,
    ]
    match request.system_prompt:
        case Some(system):
            return [*args, "--append-system-prompt", system]
        case Nothing():
            return args


def parse_envelope(stdout: str) -> Result[str, AgentCallError]:
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return Err(EnvelopeError(f"non-JSON envelope: {exc}"))
    if not isinstance(obj, dict):
        return Err(EnvelopeError("envelope is not a JSON object"))
    result = obj.get("result")
    if obj.get("is_error"):
        detail = result if isinstance(result, str) else "agent envelope flagged is_error"
        return Err(CallFailed(detail))
    if not isinstance(result, str):
        return Err(EnvelopeError("envelope has no string 'result' field"))
    return Ok(result)


def run_agent_once(request: AgentTextRequest, claude_cmd: str) -> Result[str, AgentCallError]:
    try:
        proc = subprocess.run(
            build_claude_args(request, claude_cmd),
            input=request.user_prompt,
            capture_output=True,
            text=True,
            timeout=request.timeout_s,
        )
    except subprocess.TimeoutExpired:
        return Err(CallTimeout(request.timeout_s))
    except OSError as exc:
        return Err(CallFailed(f"could not launch {claude_cmd!r}: {exc}"))
    if proc.returncode != 0:
        return Err(CallFailed(f"exit {proc.returncode}: {proc.stderr.strip()[:_STDERR_PREVIEW]}"))
    return parse_envelope(proc.stdout)


def make_runner(claude_cmd: str) -> tuple[AgentRunner, Callable[[], int]]:
    """A counting runner with one transport retry. The counter is shared
    mutable shell state, guarded by a lock (effects live at the shell)."""
    lock = threading.Lock()
    calls = [0]

    def bump() -> None:
        with lock:
            calls[0] += 1

    def run(request: AgentTextRequest) -> Result[str, AgentCallError]:
        bump()
        first = run_agent_once(request, claude_cmd)
        match first:
            case Ok(_):
                return first
            case Err(CallTimeout()):
                return first  # a timeout retried would double the stall
            case Err(_):
                bump()
                return run_agent_once(request, claude_cmd)

    def calls_made() -> int:
        with lock:
            return calls[0]

    return run, calls_made


def run_concurrently(
    requests: tuple[AgentTextRequest, ...], runner: AgentRunner, max_workers: int
) -> tuple[Result[str, AgentCallError], ...]:
    if not requests:
        return ()
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        return tuple(pool.map(runner, requests))
