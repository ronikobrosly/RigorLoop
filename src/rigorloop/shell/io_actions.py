"""Shell I/O: the run directory, artifact persistence and reload for resume,
and sandboxed execution of candidate scripts and custom checks.

The JSON (de)serialization helpers here are pure data conversions; the
functions that touch the filesystem or subprocesses are the effects."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from rigorloop.core.types import (
    NOTHING,
    BudgetExhausted,
    CandidateScore,
    ChampionArtifact,
    CheckOutcome,
    CheckPassRate,
    DevExample,
    Directive,
    Errored,
    Example,
    ExecutionFailed,
    ExecutionOk,
    ExecutionResult,
    Failed,
    FailureSample,
    GuidanceSolution,
    JsonValue,
    LeaderboardEntry,
    Nothing,
    Option,
    Passed,
    RunScriptRequest,
    RunState,
    ScriptSolution,
    SkillSolution,
    SolutionKind,
    Some,
    SplitManifest,
    StopReason,
    StrategyLogEntry,
    StrategyRequestedStop,
    StrategyUnresponsive,
    TargetReached,
    ValCheckpoint,
    ValidatedCandidate,
    ValidationPlateau,
)

SCRIPT_TIMEOUT_S = 60.0
OUTPUT_CAP_CHARS = 262_144
_STDERR_PREVIEW = 300


# --------------------------------------------------------------------------
# Filesystem primitives
# --------------------------------------------------------------------------


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, data: JsonValue) -> None:
    write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, data: JsonValue) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True) + "\n")


def read_json(path: Path) -> JsonValue:
    loaded: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def write_examples_jsonl(path: Path, examples: tuple[Example, ...]) -> None:
    write_text(
        path,
        "\n".join(json.dumps(example_to_json(e), sort_keys=True) for e in examples) + "\n",
    )


# --------------------------------------------------------------------------
# Run directory layout
# --------------------------------------------------------------------------


def run_dir(base: Path, run_id: str) -> Path:
    return base / "runs" / run_id


def loop_dir(run: Path, loop_index: int) -> Path:
    return run / "loops" / str(loop_index)


def candidate_dir(run: Path, loop_index: int, candidate_id: str) -> Path:
    return loop_dir(run, loop_index) / "candidates" / candidate_id


def solution_filename(kind: SolutionKind) -> str:
    match kind:
        case ScriptSolution():
            return "solution.py"
        case SkillSolution():
            return "SKILL.md"
        case GuidanceSolution():
            return "GUIDANCE.md"


# --------------------------------------------------------------------------
# Pure JSON conversions (kind, examples, scores, state)
# --------------------------------------------------------------------------


def kind_to_name(kind: SolutionKind) -> str:
    match kind:
        case ScriptSolution():
            return "script"
        case SkillSolution():
            return "skill"
        case GuidanceSolution():
            return "guidance"


def kind_from_name(name: str) -> SolutionKind:
    match name:
        case "skill":
            return SkillSolution()
        case "guidance":
            return GuidanceSolution()
        case _:
            return ScriptSolution()


def example_to_json(example: Example) -> dict[str, JsonValue]:
    return {
        "id": example.example_id,
        "input": example.input_text,
        "expected_output": example.expected_output,
    }


def example_from_json(data: dict[str, JsonValue]) -> Example:
    return Example(str(data["id"]), str(data["input"]), str(data["expected_output"]))


def manifest_to_json(manifest: SplitManifest) -> dict[str, JsonValue]:
    def digests(split: str) -> list[JsonValue]:
        entries: tuple[object, ...] = getattr(manifest, split)
        return [
            {"id": d.example_id, "hash": d.content_hash}  # type: ignore[attr-defined]
            for d in entries
        ]

    return {
        "seed": manifest.seed,
        "ratios": [manifest.ratios.dev, manifest.ratios.val, manifest.ratios.test],
        "dev": digests("dev"),
        "val": digests("val"),
        "test": digests("test"),
        "eval_model": manifest.eval_model,
        "cli_version": manifest.cli_version,
    }


def score_to_json(score: CandidateScore) -> dict[str, JsonValue]:
    return {
        "n": score.n,
        "passes": score.passes,
        "pass_rate": score.pass_rate,
        "ci_low": score.ci_low,
        "ci_high": score.ci_high,
        "per_check": [
            {"check": c.check_name, "passes": c.passes, "n": c.n} for c in score.per_check
        ],
        "pass_vector": list(score.pass_vector),
        "eval_aborted": score.eval_aborted,
    }


def score_from_json(data: dict[str, JsonValue]) -> CandidateScore:
    per_check_raw = data["per_check"]
    per_check = (
        tuple(
            CheckPassRate(str(c["check"]), int(str(c["passes"])), int(str(c["n"])))
            for c in per_check_raw
            if isinstance(c, dict)
        )
        if isinstance(per_check_raw, list)
        else ()
    )
    vector_raw = data["pass_vector"]
    vector = tuple(bool(v) for v in vector_raw) if isinstance(vector_raw, list) else ()
    return CandidateScore(
        n=int(str(data["n"])),
        passes=int(str(data["passes"])),
        pass_rate=float(str(data["pass_rate"])),
        ci_low=float(str(data["ci_low"])),
        ci_high=float(str(data["ci_high"])),
        per_check=per_check,
        pass_vector=vector,
        eval_aborted=bool(data["eval_aborted"]),
    )


def entry_to_json(entry: LeaderboardEntry) -> dict[str, JsonValue]:
    return {
        "candidate_id": entry.candidate_id,
        "loop_index": entry.loop_index,
        "kind": kind_to_name(entry.kind),
        "content": entry.content,
        "score": score_to_json(entry.score),
        "based_on_champion": entry.based_on_champion,
    }


def _entry_from_json(data: dict[str, JsonValue]) -> LeaderboardEntry:
    score = data["score"]
    return LeaderboardEntry(
        candidate_id=str(data["candidate_id"]),
        loop_index=int(str(data["loop_index"])),
        kind=kind_from_name(str(data["kind"])),
        content=str(data["content"]),
        score=score_from_json(score if isinstance(score, dict) else {}),
        # Absent in state persisted before validation cohorts existed.
        based_on_champion=bool(data.get("based_on_champion", False)),
    )


def _directive_to_json(directive: Directive) -> dict[str, JsonValue]:
    base: JsonValue
    match directive.base:
        case Some(artifact):
            base = {
                "candidate_id": artifact.candidate_id,
                "kind": kind_to_name(artifact.kind),
                "content": artifact.content,
            }
        case Nothing():
            base = None
    return {
        "directive_id": directive.directive_id,
        "approach_summary": directive.approach_summary,
        "instructions": directive.instructions,
        "base": base,
    }


def _directive_from_json(data: dict[str, JsonValue]) -> Directive:
    raw_base = data["base"]
    base: Option[ChampionArtifact] = (
        Some(
            ChampionArtifact(
                str(raw_base["candidate_id"]),
                kind_from_name(str(raw_base["kind"])),
                str(raw_base["content"]),
            )
        )
        if isinstance(raw_base, dict)
        else NOTHING
    )
    return Directive(
        directive_id=str(data["directive_id"]),
        approach_summary=str(data["approach_summary"]),
        instructions=str(data["instructions"]),
        base=base,
    )


def log_entry_to_json(entry: StrategyLogEntry) -> dict[str, JsonValue]:
    val_summary: JsonValue
    match entry.val_summary:
        case Some(summary):
            val_summary = summary
        case Nothing():
            val_summary = None
    return {
        "loop_index": entry.loop_index,
        "observations": entry.observations,
        "hypotheses": entry.hypotheses,
        "directives": [_directive_to_json(d) for d in entry.directives],
        "dev_summary": entry.dev_summary,
        "val_summary": val_summary,
        "fallback": entry.fallback,
    }


def log_entry_from_json(data: dict[str, JsonValue]) -> StrategyLogEntry:
    raw_directives = data["directives"]
    directives = tuple(
        _directive_from_json(d)
        for d in (raw_directives if isinstance(raw_directives, list) else [])
        if isinstance(d, dict)
    )
    raw_val = data["val_summary"]
    return StrategyLogEntry(
        loop_index=int(str(data["loop_index"])),
        observations=str(data["observations"]),
        hypotheses=str(data["hypotheses"]),
        directives=directives,
        dev_summary=str(data["dev_summary"]),
        val_summary=Some(raw_val) if isinstance(raw_val, str) else NOTHING,
        fallback=bool(data["fallback"]),
    )


def failure_sample_to_json(sample: FailureSample) -> dict[str, JsonValue]:
    return {
        "example": example_to_json(sample.dev_example.example),
        "actual_output": sample.actual_output,
        "failed_checks": list(sample.failed_checks),
    }


def failure_sample_from_json(data: dict[str, JsonValue]) -> FailureSample:
    raw_example = data["example"]
    raw_checks = data["failed_checks"]
    return FailureSample(
        dev_example=DevExample(
            example_from_json(raw_example if isinstance(raw_example, dict) else {})
        ),
        actual_output=str(data["actual_output"]),
        failed_checks=(tuple(str(c) for c in raw_checks) if isinstance(raw_checks, list) else ()),
    )


def write_failure_samples(path: Path, samples: tuple[FailureSample, ...]) -> None:
    """Persist a candidate's dev failure samples keyed by its candidate dir,
    so champion diagnostics survive non-improving loops and resume."""
    write_json(path, [failure_sample_to_json(s) for s in samples])


def read_failure_samples(path: Path) -> tuple[FailureSample, ...]:
    """Missing or unreadable files read as no samples: diagnostics are
    best-effort and must never block a run or a resume."""
    if not path.is_file():
        return ()
    try:
        loaded = read_json(path)
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(failure_sample_from_json(d) for d in loaded if isinstance(d, dict))


def validated_to_json(validated: ValidatedCandidate) -> dict[str, JsonValue]:
    return {
        "candidate_id": validated.candidate_id,
        "kind": kind_to_name(validated.kind),
        "content": validated.content,
        "dev_score": score_to_json(validated.dev_score),
        "val_score": score_to_json(validated.val_score),
    }


def validated_from_json(data: dict[str, JsonValue]) -> ValidatedCandidate:
    dev_score = data["dev_score"]
    val_score = data["val_score"]
    return ValidatedCandidate(
        candidate_id=str(data["candidate_id"]),
        kind=kind_from_name(str(data["kind"])),
        content=str(data["content"]),
        dev_score=score_from_json(dev_score if isinstance(dev_score, dict) else {}),
        val_score=score_from_json(val_score if isinstance(val_score, dict) else {}),
    )


def _checkpoint_to_json(checkpoint: ValCheckpoint) -> dict[str, JsonValue]:
    return {
        "loop_index": checkpoint.loop_index,
        "candidate_id": checkpoint.candidate_id,
        "dev_pass_rate": checkpoint.dev_pass_rate,
        "val_pass_rate": checkpoint.val_pass_rate,
        "displaced_champion": checkpoint.displaced_champion,
    }


def _checkpoint_from_json(data: dict[str, JsonValue]) -> ValCheckpoint:
    return ValCheckpoint(
        loop_index=int(str(data["loop_index"])),
        candidate_id=str(data["candidate_id"]),
        dev_pass_rate=float(str(data["dev_pass_rate"])),
        val_pass_rate=float(str(data["val_pass_rate"])),
        displaced_champion=bool(data["displaced_champion"]),
    )


def state_to_json(state: RunState) -> dict[str, JsonValue]:
    val_champion: JsonValue
    match state.val_champion:
        case Some(champion):
            val_champion = validated_to_json(champion)
        case Nothing():
            val_champion = None
    last_peek: JsonValue
    match state.last_peek_loop:
        case Some(loop_index):
            last_peek = loop_index
        case Nothing():
            last_peek = None
    return {
        "loops_completed": state.loops_completed,
        "leaderboard": [entry_to_json(e) for e in state.leaderboard],
        "strategy_log": [log_entry_to_json(e) for e in state.strategy_log],
        "val_champion": val_champion,
        "checkpoints": [_checkpoint_to_json(c) for c in state.checkpoints],
        "peeks_used": state.peeks_used,
        "last_peek_loop": last_peek,
        "consecutive_fallbacks": state.consecutive_fallbacks,
    }


def state_from_json(data: dict[str, JsonValue]) -> RunState:
    raw_leaderboard = data["leaderboard"]
    raw_log = data["strategy_log"]
    raw_checkpoints = data["checkpoints"]
    raw_champion = data["val_champion"]
    raw_peek = data["last_peek_loop"]
    return RunState(
        loops_completed=int(str(data["loops_completed"])),
        leaderboard=tuple(
            _entry_from_json(e)
            for e in (raw_leaderboard if isinstance(raw_leaderboard, list) else [])
            if isinstance(e, dict)
        ),
        strategy_log=tuple(
            log_entry_from_json(e)
            for e in (raw_log if isinstance(raw_log, list) else [])
            if isinstance(e, dict)
        ),
        val_champion=(
            Some(validated_from_json(raw_champion)) if isinstance(raw_champion, dict) else NOTHING
        ),
        checkpoints=tuple(
            _checkpoint_from_json(c)
            for c in (raw_checkpoints if isinstance(raw_checkpoints, list) else [])
            if isinstance(c, dict)
        ),
        peeks_used=int(str(data["peeks_used"])),
        last_peek_loop=Some(int(raw_peek)) if isinstance(raw_peek, int) else NOTHING,
        consecutive_fallbacks=int(str(data["consecutive_fallbacks"])),
    )


def stop_reason_to_json(reason: StopReason) -> dict[str, JsonValue]:
    match reason:
        case BudgetExhausted(max_loops):
            return {"type": "budget_exhausted", "max_loops": max_loops}
        case ValidationPlateau(checkpoints):
            return {"type": "validation_plateau", "checkpoints": checkpoints}
        case TargetReached(pass_rate):
            return {"type": "target_reached", "pass_rate": pass_rate}
        case StrategyRequestedStop(why):
            return {"type": "strategy_requested_stop", "reason": why}
        case StrategyUnresponsive(fallbacks):
            return {"type": "strategy_unresponsive", "fallbacks": fallbacks}


def stop_reason_from_json(data: dict[str, JsonValue]) -> StopReason:
    match data.get("type"):
        case "validation_plateau":
            return ValidationPlateau(int(str(data["checkpoints"])))
        case "target_reached":
            return TargetReached(float(str(data["pass_rate"])))
        case "strategy_requested_stop":
            return StrategyRequestedStop(str(data["reason"]))
        case "strategy_unresponsive":
            return StrategyUnresponsive(int(str(data["fallbacks"])))
        case _:
            return BudgetExhausted(int(str(data.get("max_loops", 0))))


# --------------------------------------------------------------------------
# Sandboxed execution of untrusted generated code.
# NOT a security boundary (documented): mitigations are a hard timeout, an
# output cap, no stdin inheritance, and a scratch working directory.
# --------------------------------------------------------------------------


def run_script(request: RunScriptRequest, scratch_dir: Path) -> ExecutionResult:
    scratch_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [sys.executable, request.script_path],
            input=request.stdin_text,
            capture_output=True,
            text=True,
            timeout=request.timeout_s,
            cwd=scratch_dir,
        )
    except subprocess.TimeoutExpired:
        return ExecutionFailed(f"timed out after {request.timeout_s}s")
    except OSError as exc:
        return ExecutionFailed(f"could not run script: {exc}")
    if proc.returncode != 0:
        return ExecutionFailed(f"exit {proc.returncode}: {proc.stderr.strip()[:_STDERR_PREVIEW]}")
    if len(proc.stdout) > OUTPUT_CAP_CHARS:
        return ExecutionFailed(f"output exceeded {OUTPUT_CAP_CHARS} characters")
    # Scripts conventionally end stdout with one newline; the expected outputs
    # are single-line JSONL strings, so exactly one trailing newline is shed.
    return ExecutionOk(proc.stdout.removesuffix("\n"))


def run_custom_check(
    script_path: str, example: Example, actual_output: str, timeout_s: float = SCRIPT_TIMEOUT_S
) -> CheckOutcome:
    """User-supplied checker: JSON on stdin, exit 0 = pass, exit 1 = fail
    (stderr as the reason), anything else = error."""
    payload = json.dumps(
        {
            "input": example.input_text,
            "expected_output": example.expected_output,
            "actual_output": actual_output,
        }
    )
    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return Errored(f"custom check timed out after {timeout_s}s")
    except OSError as exc:
        return Errored(f"could not run custom check: {exc}")
    match proc.returncode:
        case 0:
            return Passed()
        case 1:
            return Failed(proc.stderr.strip()[:_STDERR_PREVIEW] or "custom check failed")
        case code:
            return Errored(f"custom check exit {code}: {proc.stderr.strip()[:_STDERR_PREVIEW]}")
