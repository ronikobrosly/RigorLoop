"""argparse entry point and the orchestration loop.

The loop is a dumb driver: at each step it hands current state to a core
function and performs the decision it gets back. The core sequences nothing."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from rigorloop import __version__
from rigorloop.core import (
    config_calcs,
    dataset_calcs,
    prompt_calcs,
    report_calcs,
    scoring_calcs,
    strategy_calcs,
)
from rigorloop.core.types import (
    NOTHING,
    AgentConfig,
    AgentContextPrompt,
    AgentTextRequest,
    BadRatios,
    Candidate,
    CandidateScore,
    Check,
    CheckOutcome,
    ConfigParseError,
    ContinueDecision,
    CustomPython,
    DuplicateWarning,
    Err,
    EvalPrompt,
    Example,
    ExampleParseError,
    ExampleResult,
    ExecutionFailed,
    ExecutionOk,
    ExecutionResult,
    FailureSample,
    GuidanceSolution,
    InvalidJsonLine,
    InvalidValue,
    JudgeVerdict,
    LeaderboardEntry,
    LlmJudge,
    MissingField,
    MissingKey,
    NamedOutcome,
    NotAnObject,
    Nothing,
    Ok,
    Option,
    Result,
    RunConfig,
    RunScriptRequest,
    RunState,
    ScriptSolution,
    SkillSolution,
    Some,
    Split,
    StopReason,
    StopRequested,
    StrategyDecision,
    StrategyLogEntry,
    StrategyRequestedStop,
    TomlSyntax,
    TooFewExamples,
    ValCheckpoint,
    ValidatedCandidate,
)
from rigorloop.shell import agent_calls, io_actions

_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_NO_RESULT = 2


@dataclass(frozen=True, slots=True)
class ShellDeps:
    """Injected effects: tests swap in fakes, main() wires the real ones."""

    agent_runner: agent_calls.AgentRunner
    calls_made: Callable[[], int]
    script_runner: Callable[[RunScriptRequest, Path], ExecutionResult]
    custom_check_runner: Callable[[str, Example, str], CheckOutcome]
    make_run_id: Callable[[], str]
    cli_version: Callable[[], str]
    echo: Callable[[str], None]


# --------------------------------------------------------------------------
# Channel conversion: prompts → transport requests
# --------------------------------------------------------------------------


def _request_from_context(prompt: AgentContextPrompt, agents: AgentConfig) -> AgentTextRequest:
    return AgentTextRequest(prompt.text, NOTHING, agents.model, agents.timeout_s)


def _request_from_eval(prompt: EvalPrompt, agents: AgentConfig) -> AgentTextRequest:
    return AgentTextRequest(
        prompt.user_prompt, prompt.system_prompt, agents.model, agents.timeout_s
    )


# --------------------------------------------------------------------------
# Candidate evaluation (the evaluation prompt channel)
# --------------------------------------------------------------------------


def _judge_outcome(
    check: LlmJudge, example: Example, actual: str, config: RunConfig, deps: ShellDeps
) -> CheckOutcome:
    prompt = prompt_calcs.build_judge_prompt(check.rubric, example, actual)

    def one_verdict(_: int) -> Result[JudgeVerdict, str]:
        reply = deps.agent_runner(_request_from_eval(prompt, config.agents))
        match reply:
            case Err(error):
                return Err(str(error))
            case Ok(text):
                parsed = strategy_calcs.parse_judge_reply(text)
                match parsed:
                    case Ok(verdict):
                        return Ok(verdict)
                    case Err(malformed):
                        retry = deps.agent_runner(
                            _request_from_eval(
                                prompt_calcs.reformat_eval_prompt(prompt, malformed.detail),
                                config.agents,
                            )
                        )
                        match retry:
                            case Ok(retry_text):
                                reparsed = strategy_calcs.parse_judge_reply(retry_text)
                                match reparsed:
                                    case Ok(verdict):
                                        return Ok(verdict)
                                    case Err(again):
                                        return Err(again.detail)
                            case Err(error):
                                return Err(str(error))

    votes = tuple(one_verdict(i) for i in range(check.n_samples))
    verdicts = tuple(v.value for v in votes if isinstance(v, Ok))
    errors = sum(1 for v in votes if isinstance(v, Err))
    return scoring_calcs.judge_outcome(check, verdicts, errors)


def _produce_output(
    candidate: Candidate,
    example: Example,
    script_path: Option[Path],
    config: RunConfig,
    deps: ShellDeps,
    scratch_dir: Path,
) -> ExecutionResult:
    match candidate.kind:
        case ScriptSolution():
            match script_path:
                case Some(path):
                    return deps.script_runner(
                        RunScriptRequest(
                            str(path), example.input_text, io_actions.SCRIPT_TIMEOUT_S
                        ),
                        scratch_dir,
                    )
                case Nothing():
                    return ExecutionFailed("script was never materialized (internal error)")
        case SkillSolution() | GuidanceSolution():
            prompt = prompt_calcs.build_solution_eval_prompt(candidate.content, example)
            reply = deps.agent_runner(_request_from_eval(prompt, config.agents))
            match reply:
                case Ok(text):
                    return ExecutionOk(text.strip())
                case Err(error):
                    return ExecutionFailed(str(error))


def _check_outcomes(
    checks: tuple[tuple[str, Check], ...],
    example: Example,
    actual: str,
    config: RunConfig,
    deps: ShellDeps,
) -> tuple[NamedOutcome, ...]:
    def outcome(name: str, check: Check) -> NamedOutcome:
        match check:
            case LlmJudge():
                return NamedOutcome(name, _judge_outcome(check, example, actual, config, deps))
            case CustomPython(script_path):
                return NamedOutcome(name, deps.custom_check_runner(script_path, example, actual))
            case _:
                return NamedOutcome(
                    name,
                    scoring_calcs.evaluate_deterministic_check(
                        check, example.expected_output, actual
                    ),
                )

    return tuple(outcome(name, check) for name, check in checks)


def _evaluate_candidate(
    candidate: Candidate,
    examples: tuple[Example, ...],
    config: RunConfig,
    deps: ShellDeps,
    work_dir: Path,
) -> tuple[tuple[ExampleResult, ...], bool]:
    """Evaluate one candidate on every example (sorted by id), short-circuiting
    after max_consecutive_eval_failures consecutive execution failures."""
    checks = scoring_calcs.named_checks(config.checks)
    check_names = tuple(name for name, _ in checks)
    ordered = tuple(sorted(examples, key=lambda e: e.example_id))

    script_path: Option[Path] = NOTHING
    if isinstance(candidate.kind, ScriptSolution):
        path = work_dir / io_actions.solution_filename(candidate.kind)
        io_actions.write_text(path, candidate.content)
        script_path = Some(path)
    scratch_dir = work_dir / "scratch"

    # Local mutable accumulation: this is the shell's evaluation drive loop.
    results: list[ExampleResult] = []
    consecutive_failures = 0
    aborted = False
    for example in ordered:
        if consecutive_failures >= config.loop.max_consecutive_eval_failures:
            aborted = True
            break
        execution = _produce_output(candidate, example, script_path, config, deps, scratch_dir)
        match execution:
            case ExecutionFailed(detail):
                consecutive_failures += 1
                outcomes = scoring_calcs.execution_failure_outcomes(check_names, detail)
            case ExecutionOk(actual):
                consecutive_failures = 0
                outcomes = _check_outcomes(checks, example, actual, config, deps)
        results.append(ExampleResult(example.example_id, execution, outcomes))
    return tuple(results), aborted


def _score(
    results: tuple[ExampleResult, ...],
    examples: tuple[Example, ...],
    checks: tuple[Check, ...],
    aborted: bool,
) -> CandidateScore:
    order = tuple(sorted(e.example_id for e in examples))
    names = tuple(name for name, _ in scoring_calcs.named_checks(checks))
    return scoring_calcs.score_candidate(results, order, names, aborted)


# --------------------------------------------------------------------------
# Loading a project (config + task + examples)
# --------------------------------------------------------------------------


def _format_config_error(error: ConfigParseError) -> str:
    match error:
        case TomlSyntax(detail):
            return f"rigorloop.toml is not valid TOML: {detail}"
        case MissingKey(key):
            return f"rigorloop.toml is missing required key: {key}"
        case InvalidValue(key, detail):
            return f"rigorloop.toml has an invalid value for {key}: {detail}"


def _format_example_error(error: ExampleParseError) -> str:
    match error:
        case InvalidJsonLine(line, detail):
            return f"examples file line {line} is not valid JSON: {detail}"
        case NotAnObject(line):
            return f"examples file line {line} is not a JSON object"
        case MissingField(line, field):
            return f"examples file line {line} is missing the {field!r} field"


@dataclass(frozen=True, slots=True)
class LoadedProject:
    config: RunConfig
    config_text: str
    task_description: str
    examples: tuple[Example, ...]
    duplicates: tuple[DuplicateWarning, ...]


def _load_project(
    project_dir: Path, config_file: str, echo: Callable[[str], None]
) -> LoadedProject | None:
    config_path = project_dir / config_file
    if not config_path.is_file():
        echo(f"error: {config_path} not found (run `rigorloop init` to scaffold a project)")
        return None
    config_text = config_path.read_text(encoding="utf-8")
    parsed = config_calcs.parse_config(config_text)
    match parsed:
        case Err(config_error):
            echo(f"error: {_format_config_error(config_error)}")
            return None
        case Ok(config):
            pass
    task_path = project_dir / config.task.description_file
    if not task_path.is_file():
        echo(f"error: task description file {task_path} not found")
        return None
    examples_path = project_dir / config.task.examples_file
    if not examples_path.is_file():
        echo(f"error: examples file {examples_path} not found")
        return None
    examples_result = dataset_calcs.parse_examples(examples_path.read_text(encoding="utf-8"))
    match examples_result:
        case Err(example_error):
            echo(f"error: {_format_example_error(example_error)}")
            return None
        case Ok(raw_examples):
            pass
    unique, duplicates = dataset_calcs.dedupe_examples(raw_examples)
    return LoadedProject(
        config=config,
        config_text=config_text,
        task_description=task_path.read_text(encoding="utf-8"),
        examples=unique,
        duplicates=duplicates,
    )


def _split_or_report(project: LoadedProject, echo: Callable[[str], None]) -> Split | None:
    result = dataset_calcs.split_examples(
        project.examples, project.config.split.ratios, project.config.split.seed
    )
    match result:
        case Ok(split):
            return split
        case Err(error):
            match error:
                case TooFewExamples(n_total, minimum):
                    echo(f"error: {n_total} unique examples is too few (minimum {minimum})")
                case BadRatios(detail):
                    echo(f"error: bad split ratios: {detail}")
            return None


# --------------------------------------------------------------------------
# The strategy turn
# --------------------------------------------------------------------------


def _strategy_decision(
    context_prompt: AgentContextPrompt,
    config: RunConfig,
    deps: ShellDeps,
) -> tuple[Option[StrategyDecision], str]:
    """Returns (decision, raw_reply_text); Nothing means both the reply and the
    one sanctioned reformat retry were unusable and the fallback applies."""
    reply = deps.agent_runner(_request_from_context(context_prompt, config.agents))
    match reply:
        case Ok(text):
            parsed = strategy_calcs.parse_strategy_reply(text)
            match parsed:
                case Ok(decision):
                    return Some(decision), text
                case Err(malformed):
                    retry_prompt = prompt_calcs.reformat_context_prompt(
                        context_prompt, malformed.detail
                    )
                    retry = deps.agent_runner(_request_from_context(retry_prompt, config.agents))
                    match retry:
                        case Ok(retry_text):
                            reparsed = strategy_calcs.parse_strategy_reply(retry_text)
                            match reparsed:
                                case Ok(decision):
                                    return Some(decision), retry_text
                                case Err(_):
                                    return NOTHING, retry_text
                        case Err(error):
                            return NOTHING, f"(transport error: {error})"
        case Err(error):
            return NOTHING, f"(transport error: {error})"


# --------------------------------------------------------------------------
# The run protocol
# --------------------------------------------------------------------------


def execute_run(
    project: LoadedProject,
    project_dir: Path,
    deps: ShellDeps,
    resume_run_id: Option[str],
) -> int:
    config = project.config
    split = _split_or_report(project, deps.echo)
    if split is None:
        return _EXIT_ERROR

    for warning in dataset_calcs.power_warnings(split):
        deps.echo(f"power warning: {warning.message}")
    for duplicate in project.duplicates:
        deps.echo(
            f"duplicate inputs collapsed: {duplicate.input_preview!r} "
            f"appears {duplicate.occurrences} times"
        )

    cli_version = deps.cli_version()
    manifest = dataset_calcs.build_manifest(
        split, config.split.ratios, config.split.seed, config.agents.model, cli_version
    )

    match resume_run_id:
        case Some(existing_id):
            run_id = existing_id
            run_path = io_actions.run_dir(project_dir, run_id)
            state_path = run_path / "state.json"
            manifest_path = run_path / "manifest.json"
            if not state_path.is_file() or not manifest_path.is_file():
                deps.echo(f"error: run {run_id} has no resumable state under {run_path}")
                return _EXIT_ERROR
            if (run_path / "final" / "report.md").is_file():
                deps.echo(f"error: run {run_id} is already finalized")
                return _EXIT_ERROR
            stored_manifest = io_actions.read_json(manifest_path)
            if not isinstance(stored_manifest, dict) or stored_manifest.get(
                "manifest"
            ) != io_actions.manifest_to_json(manifest):
                deps.echo(
                    "error: the examples file or split config changed since this run "
                    "started; resuming would reshuffle examples across splits. Aborting."
                )
                return _EXIT_ERROR
            stored_state = io_actions.read_json(state_path)
            if not isinstance(stored_state, dict):
                deps.echo("error: state.json is corrupt")
                return _EXIT_ERROR
            state = io_actions.state_from_json(stored_state)
            deps.echo(f"resuming run {run_id} at loop {state.loops_completed + 1}")
        case Nothing():
            run_id = deps.make_run_id()
            run_path = io_actions.run_dir(project_dir, run_id)
            io_actions.write_json(
                run_path / "manifest.json",
                {
                    "manifest": io_actions.manifest_to_json(manifest),
                    "config_snapshot": project.config_text,
                },
            )
            for name, examples in (
                ("dev", tuple(d.example for d in split.dev)),
                ("val", tuple(v.example for v in split.val)),
                ("test", tuple(t.example for t in split.test)),
            ):
                io_actions.write_examples_jsonl(run_path / "splits" / f"{name}.jsonl", examples)
            state = strategy_calcs.initial_state()
            deps.echo(
                f"run {run_id}: dev {len(split.dev)} / val {len(split.val)} / "
                f"test {len(split.test)} examples"
            )

    dev_examples = tuple(d.example for d in split.dev)
    val_examples = tuple(v.example for v in split.val)
    check_pairs = scoring_calcs.named_checks(config.checks)
    check_names = tuple(name for name, _ in check_pairs)

    # In-memory only: failing examples of the champion candidate from the most
    # recent loop, for the strategy context. Empty after a resume.
    last_samples: tuple[FailureSample, ...] = ()

    stop_reason: StopReason | None = None
    while stop_reason is None:
        pre_stop = strategy_calcs.stopping_decision(state, config)
        match pre_stop:
            case Some(reason):
                stop_reason = reason
                continue
            case Nothing():
                pass

        loop_index = state.loops_completed + 1
        loop_path = io_actions.loop_dir(run_path, loop_index)
        deps.echo(f"loop {loop_index}: strategy turn")

        context = strategy_calcs.assemble_strategy_context(
            project.task_description, config, state, last_samples, check_names
        )
        strategy_prompt = prompt_calcs.build_strategy_prompt(context)
        io_actions.write_text(loop_path / "strategy_prompt.md", strategy_prompt.text)
        maybe_decision, raw_reply = _strategy_decision(strategy_prompt, config, deps)
        io_actions.write_text(loop_path / "strategy_reply.md", raw_reply)
        match maybe_decision:
            case Some(parsed_decision):
                decision: StrategyDecision = parsed_decision
                fallback_used = False
            case Nothing():
                best_now = strategy_calcs.dev_best(state.leaderboard)
                decision = strategy_calcs.fallback_decision(isinstance(best_now, Some))
                fallback_used = True
                deps.echo(f"loop {loop_index}: strategy reply malformed; using fallback directive")

        consecutive_fallbacks = state.consecutive_fallbacks + 1 if fallback_used else 0
        if consecutive_fallbacks >= strategy_calcs.MAX_FALLBACKS:
            state = replace(
                state,
                loops_completed=loop_index,
                consecutive_fallbacks=consecutive_fallbacks,
            )
            io_actions.write_json(run_path / "state.json", io_actions.state_to_json(state))
            continue

        match decision:
            case StopRequested(reason_text):
                stop_reason = StrategyRequestedStop(reason_text)
                stop_entry = StrategyLogEntry(
                    loop_index=loop_index,
                    observations="",
                    hypotheses="",
                    directives=(),
                    dev_summary="(strategy requested stop before executing)",
                    val_summary=NOTHING,
                    fallback=False,
                )
                state = replace(
                    state,
                    strategy_log=(*state.strategy_log, stop_entry),
                    consecutive_fallbacks=0,
                )
                io_actions.append_jsonl(
                    run_path / "strategy_log.jsonl", io_actions.log_entry_to_json(stop_entry)
                )
                io_actions.write_json(run_path / "state.json", io_actions.state_to_json(state))
                continue
            case ContinueDecision():
                pass

        previous_best = strategy_calcs.dev_best(state.leaderboard)
        champion = (
            Some(strategy_calcs.champion_artifact(previous_best.value))
            if isinstance(previous_best, Some)
            else NOTHING
        )
        directives = strategy_calcs.build_directives(
            decision.directive_specs, champion, loop_index, config.loop.executors_per_loop
        )
        dev_sample = strategy_calcs.sample_dev_subset(
            split.dev, config.loop.dev_examples_in_prompt, config.split.seed, loop_index
        )

        deps.echo(f"loop {loop_index}: running {len(directives)} executor(s)")
        executor_prompts = tuple(
            prompt_calcs.build_executor_prompt(
                project.task_description, config.task.solution_kind, d, config.checks, dev_sample
            )
            for d in directives
        )
        replies = agent_calls.run_concurrently(
            tuple(_request_from_context(p, config.agents) for p in executor_prompts),
            deps.agent_runner,
            config.loop.executors_per_loop,
        )

        candidates: list[Candidate] = []
        n_malformed = 0
        for directive, ex_prompt, reply in zip(directives, executor_prompts, replies, strict=True):
            candidate_id = f"cand-{directive.directive_id}"
            content: Result[str, object]
            match reply:
                case Ok(text):
                    content = strategy_calcs.parse_executor_reply(text)
                    match content:
                        case Err(malformed_reply):
                            retry = deps.agent_runner(
                                _request_from_context(
                                    prompt_calcs.reformat_context_prompt(
                                        ex_prompt, malformed_reply.detail
                                    ),
                                    config.agents,
                                )
                            )
                            match retry:
                                case Ok(retry_text):
                                    content = strategy_calcs.parse_executor_reply(retry_text)
                                case Err(error):
                                    content = Err(error)
                        case Ok(_):
                            pass
                case Err(error):
                    content = Err(error)
            match content:
                case Ok(solution_text):
                    candidates.append(
                        Candidate(
                            candidate_id=candidate_id,
                            loop_index=loop_index,
                            kind=config.task.solution_kind,
                            content=solution_text,
                            directive_id=directive.directive_id,
                        )
                    )
                case Err(failure):
                    n_malformed += 1
                    io_actions.write_text(
                        io_actions.candidate_dir(run_path, loop_index, candidate_id)
                        / "malformed.txt",
                        str(failure),
                    )

        new_entries: list[LeaderboardEntry] = []
        loop_results: dict[str, tuple[ExampleResult, ...]] = {}
        for candidate in candidates:
            deps.echo(f"loop {loop_index}: evaluating {candidate.candidate_id} on dev")
            cand_path = io_actions.candidate_dir(run_path, loop_index, candidate.candidate_id)
            results, aborted = _evaluate_candidate(candidate, dev_examples, config, deps, cand_path)
            score = _score(results, dev_examples, config.checks, aborted)
            io_actions.write_text(
                cand_path / io_actions.solution_filename(candidate.kind), candidate.content
            )
            io_actions.write_json(cand_path / "scores.json", io_actions.score_to_json(score))
            for result in results:
                io_actions.append_jsonl(
                    cand_path / "outputs.jsonl",
                    {
                        "example_id": result.example_id,
                        "output": (
                            result.execution.output_text
                            if isinstance(result.execution, ExecutionOk)
                            else None
                        ),
                        "error": (
                            result.execution.detail
                            if isinstance(result.execution, ExecutionFailed)
                            else None
                        ),
                        "outcomes": [
                            {"check": o.check_name, "outcome": type(o.outcome).__name__}
                            for o in result.outcomes
                        ],
                    },
                )
            loop_results[candidate.candidate_id] = results
            new_entries.append(
                LeaderboardEntry(
                    candidate_id=candidate.candidate_id,
                    loop_index=loop_index,
                    kind=candidate.kind,
                    content=candidate.content,
                    score=score,
                )
            )

        new_best_significant = any(
            strategy_calcs.beats_previous_best(previous_best, entry) for entry in new_entries
        )
        leaderboard = strategy_calcs.fold_leaderboard(state.leaderboard, tuple(new_entries))
        state = replace(
            state,
            loops_completed=loop_index,
            leaderboard=leaderboard,
            consecutive_fallbacks=consecutive_fallbacks,
        )

        best_after = strategy_calcs.dev_best(leaderboard)
        match best_after:
            case Some(best_entry) if best_entry.candidate_id in loop_results:
                last_samples = strategy_calcs.failure_samples(
                    split.dev, loop_results[best_entry.candidate_id]
                )
            case _:
                last_samples = ()

        val_summary: Option[str] = NOTHING
        if strategy_calcs.should_validate(
            state, config, decision.request_validation, new_best_significant
        ):
            match strategy_calcs.dev_best(state.leaderboard):
                case Some(entry):
                    deps.echo(f"loop {loop_index}: validation peek at {entry.candidate_id}")
                    val_candidate = Candidate(
                        entry.candidate_id, loop_index, entry.kind, entry.content, "validation"
                    )
                    val_path = loop_path / "validation" / entry.candidate_id
                    val_results, val_aborted = _evaluate_candidate(
                        val_candidate, val_examples, config, deps, val_path
                    )
                    val_score = _score(val_results, val_examples, config.checks, val_aborted)
                    challenger = ValidatedCandidate(
                        candidate_id=entry.candidate_id,
                        kind=entry.kind,
                        content=entry.content,
                        dev_score=entry.score,
                        val_score=val_score,
                    )
                    winner, improved = strategy_calcs.select_val_champion(
                        state.val_champion, challenger
                    )
                    checkpoint = ValCheckpoint(
                        loop_index=loop_index,
                        candidate_id=entry.candidate_id,
                        dev_pass_rate=entry.score.pass_rate,
                        val_pass_rate=val_score.pass_rate,
                        displaced_champion=improved,
                    )
                    state = replace(
                        state,
                        val_champion=Some(winner),
                        checkpoints=(*state.checkpoints, checkpoint),
                        peeks_used=state.peeks_used + 1,
                        last_peek_loop=Some(loop_index),
                    )
                    val_summary = Some(strategy_calcs.val_summary_line(checkpoint))
                case Nothing():
                    pass

        entry_log = StrategyLogEntry(
            loop_index=loop_index,
            observations=decision.observations,
            hypotheses=decision.hypotheses,
            directives=directives,
            dev_summary=strategy_calcs.dev_summary_line(
                len(new_entries),
                n_malformed,
                strategy_calcs.dev_best(
                    tuple(sorted(new_entries, key=lambda e: (-e.score.pass_rate, e.candidate_id)))
                ),
            ),
            val_summary=val_summary,
            fallback=fallback_used,
        )
        state = replace(state, strategy_log=(*state.strategy_log, entry_log))

        io_actions.append_jsonl(
            run_path / "strategy_log.jsonl", io_actions.log_entry_to_json(entry_log)
        )
        io_actions.write_json(
            run_path / "leaderboard.json",
            [io_actions.entry_to_json(e) for e in state.leaderboard],
        )
        io_actions.write_json(run_path / "state.json", io_actions.state_to_json(state))

    return _finalize(project, split, run_path, run_id, state, stop_reason, deps)


def _finalize(
    project: LoadedProject,
    split: Split,
    run_path: Path,
    run_id: str,
    state: RunState,
    stop_reason: StopReason,
    deps: ShellDeps,
) -> int:
    config = project.config
    deps.echo(f"stopping: {report_calcs.stop_reason_label(stop_reason)}")
    if not state.leaderboard:
        deps.echo("no scored candidates were produced; nothing to finalize")
        return _EXIT_NO_RESULT

    val_examples = tuple(v.example for v in split.val)
    test_examples = tuple(t.example for t in split.test)

    winner: ValidatedCandidate
    match state.val_champion:
        case Some(champion):
            winner = champion
        case Nothing():
            # No checkpoint ever ran (e.g. a very short run): selection on
            # validation is mandatory, so run one final peek now.
            entry = state.leaderboard[0]
            deps.echo(f"final validation peek at {entry.candidate_id}")
            candidate = Candidate(
                entry.candidate_id,
                state.loops_completed,
                entry.kind,
                entry.content,
                "final-validation",
            )
            results, aborted = _evaluate_candidate(
                candidate, val_examples, config, deps, run_path / "final" / "validation"
            )
            winner = ValidatedCandidate(
                candidate_id=entry.candidate_id,
                kind=entry.kind,
                content=entry.content,
                dev_score=entry.score,
                val_score=_score(results, val_examples, config.checks, aborted),
            )
            state = replace(state, val_champion=Some(winner), peeks_used=state.peeks_used + 1)

    # The only time test examples are ever read after splitting: a pure
    # harness computation, run exactly once. No agent sees test results.
    deps.echo(f"evaluating winner {winner.candidate_id} on the test set (once)")
    test_candidate = Candidate(
        winner.candidate_id, state.loops_completed, winner.kind, winner.content, "final-test"
    )
    test_results, test_aborted = _evaluate_candidate(
        test_candidate, test_examples, config, deps, run_path / "final" / "test"
    )
    test_score = _score(test_results, test_examples, config.checks, test_aborted)

    manifest_data = io_actions.read_json(run_path / "manifest.json")
    manifest_inner = manifest_data.get("manifest") if isinstance(manifest_data, dict) else None
    cli_version = (
        str(manifest_inner.get("cli_version", "unknown"))
        if isinstance(manifest_inner, dict)
        else "unknown"
    )

    report = report_calcs.render_report(
        run_id=run_id,
        kind=winner.kind,
        eval_model=config.agents.model,
        cli_version=cli_version,
        stop_reason=stop_reason,
        winner=winner,
        test_score=test_score,
        state=state,
        agent_calls_made=deps.calls_made(),
    )
    io_actions.write_text(
        run_path / "final" / io_actions.solution_filename(winner.kind), winner.content
    )
    io_actions.write_json(
        run_path / "final" / "test_results.json",
        {
            "run_id": run_id,
            "stop_reason": io_actions.stop_reason_to_json(stop_reason),
            "winner": io_actions.validated_to_json(winner),
            "test_score": io_actions.score_to_json(test_score),
            "agent_calls_made": deps.calls_made(),
        },
    )
    io_actions.write_text(run_path / "final" / "report.md", report)
    io_actions.write_json(run_path / "state.json", io_actions.state_to_json(state))

    deps.echo(f"final artifact: {run_path / 'final' / io_actions.solution_filename(winner.kind)}")
    deps.echo(f"report: {run_path / 'final' / 'report.md'}")
    deps.echo(
        f"test pass rate: {test_score.pass_rate:.1%} "
        f"[{test_score.ci_low:.1%}, {test_score.ci_high:.1%}]"
    )
    return _EXIT_OK


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _cmd_init(project_dir: Path, echo: Callable[[str], None]) -> int:
    targets = {
        "rigorloop.toml": _SAMPLE_CONFIG,
        "task.md": _SAMPLE_TASK,
        "examples.jsonl": _toy_examples_jsonl(),
    }
    existing = [name for name in targets if (project_dir / name).exists()]
    if existing:
        echo(f"error: refusing to overwrite existing file(s): {', '.join(existing)}")
        return _EXIT_ERROR
    project_dir.mkdir(parents=True, exist_ok=True)
    for name, content in targets.items():
        io_actions.write_text(project_dir / name, content)
        echo(f"wrote {project_dir / name}")
    echo("next: edit task.md and examples.jsonl, then run `rigorloop check`")
    return _EXIT_OK


def _cmd_check(project_dir: Path, config_file: str, echo: Callable[[str], None]) -> int:
    project = _load_project(project_dir, config_file, echo)
    if project is None:
        return _EXIT_ERROR
    split = _split_or_report(project, echo)
    if split is None:
        return _EXIT_ERROR
    budget = report_calcs.estimate_budget(
        project.config, len(split.dev), len(split.val), len(split.test)
    )
    echo(
        report_calcs.render_check_summary(
            n_total=len(project.examples),
            n_dev=len(split.dev),
            n_val=len(split.val),
            n_test=len(split.test),
            duplicates=project.duplicates,
            warnings=dataset_calcs.power_warnings(split),
            budget=budget,
            model=project.config.agents.model,
        )
    )
    return _EXIT_OK


def _cmd_report(project_dir: Path, run_id: str, echo: Callable[[str], None]) -> int:
    run_path = io_actions.run_dir(project_dir, run_id)
    results_path = run_path / "final" / "test_results.json"
    state_path = run_path / "state.json"
    manifest_path = run_path / "manifest.json"
    if not results_path.is_file() or not state_path.is_file():
        echo(f"error: run {run_id} has no finalized results under {run_path}")
        return _EXIT_ERROR
    results = io_actions.read_json(results_path)
    stored_state = io_actions.read_json(state_path)
    manifest = io_actions.read_json(manifest_path) if manifest_path.is_file() else {}
    if not isinstance(results, dict) or not isinstance(stored_state, dict):
        echo("error: persisted artifacts are corrupt")
        return _EXIT_ERROR
    winner_raw = results["winner"]
    stop_raw = results["stop_reason"]
    test_raw = results["test_score"]
    if not (
        isinstance(winner_raw, dict) and isinstance(stop_raw, dict) and isinstance(test_raw, dict)
    ):
        echo("error: persisted artifacts are corrupt")
        return _EXIT_ERROR
    winner = io_actions.validated_from_json(winner_raw)
    manifest_inner = manifest.get("manifest") if isinstance(manifest, dict) else None
    eval_model = (
        str(manifest_inner.get("eval_model", "unknown"))
        if isinstance(manifest_inner, dict)
        else "unknown"
    )
    cli_version = (
        str(manifest_inner.get("cli_version", "unknown"))
        if isinstance(manifest_inner, dict)
        else "unknown"
    )
    calls_raw = results.get("agent_calls_made", 0)
    report = report_calcs.render_report(
        run_id=run_id,
        kind=winner.kind,
        eval_model=eval_model,
        cli_version=cli_version,
        stop_reason=io_actions.stop_reason_from_json(stop_raw),
        winner=winner,
        test_score=io_actions.score_from_json(test_raw),
        state=io_actions.state_from_json(stored_state),
        agent_calls_made=int(calls_raw) if isinstance(calls_raw, int) else 0,
    )
    io_actions.write_text(run_path / "final" / "report.md", report)
    echo(report)
    return _EXIT_OK


def _real_cli_version(claude_cmd: str) -> str:
    try:
        proc = subprocess.run([claude_cmd, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.strip() or "unknown"


def _real_deps(claude_cmd: str) -> ShellDeps:
    runner, calls_made = agent_calls.make_runner(claude_cmd)
    return ShellDeps(
        agent_runner=runner,
        calls_made=calls_made,
        script_runner=io_actions.run_script,
        custom_check_runner=io_actions.run_custom_check,
        make_run_id=lambda: time.strftime("%Y%m%d-%H%M%S"),
        cli_version=lambda: _real_cli_version(claude_cmd),
        echo=print,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rigorloop",
        description=(
            "Statistically-sound agentic build loops: dev/val/test splits, a strategy "
            "agent, executor agents, and a one-shot final test evaluation."
        ),
    )
    parser.add_argument("--version", action="version", version=f"rigorloop {__version__}")
    parser.add_argument("--dir", default=".", help="project directory (default: current directory)")
    parser.add_argument(
        "--config", default="rigorloop.toml", help="config file name inside the project directory"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="scaffold rigorloop.toml, task.md and examples.jsonl")
    subparsers.add_parser("check", help="parse everything, print split sizes and budget; no tokens")
    run_parser = subparsers.add_parser("run", help="execute the loop protocol")
    run_parser.add_argument("--resume", metavar="RUN_ID", default=None)
    report_parser = subparsers.add_parser("report", help="re-render a run's report.md")
    report_parser.add_argument("run_id")

    args = parser.parse_args(argv)
    project_dir = Path(args.dir).resolve()
    claude_cmd = os.environ.get("RIGORLOOP_CLAUDE_CMD", "claude")

    match args.command:
        case "init":
            return _cmd_init(project_dir, print)
        case "check":
            return _cmd_check(project_dir, args.config, print)
        case "run":
            project = _load_project(project_dir, args.config, print)
            if project is None:
                return _EXIT_ERROR
            resume: Option[str] = Some(args.resume) if args.resume else NOTHING
            return execute_run(project, project_dir, _real_deps(claude_cmd), resume)
        case "report":
            return _cmd_report(project_dir, args.run_id, print)
        case _:
            # argparse enforces the subcommand set; this is unreachable.
            parser.error(f"unknown command {args.command!r}")


# --------------------------------------------------------------------------
# `init` scaffolding content
# --------------------------------------------------------------------------

_SAMPLE_CONFIG = """\
# RigorLoop project configuration. Run `rigorloop check` to validate.

[task]
description_file = "task.md"
solution_kind    = "script"          # script | skill | guidance
examples_file    = "examples.jsonl"

[split]
ratios = [0.6, 0.2, 0.2]             # dev / validation / test
seed   = 17

[loop]
max_loops              = 12
executors_per_loop     = 4
dev_examples_in_prompt = 30

[validation]
val_every  = 3
max_peeks  = 10
patience   = 2
target_pass_rate = 0.95

[agents]
model      = "claude-sonnet-5"
timeout_s  = 300

[[checks]]
type = "json_equality"
"""

_SAMPLE_TASK = """\
# Task

Convert a short plain-text contact card into a JSON object.

Each input is a few lines of text describing one person. Produce a single-line
JSON object with exactly these keys:

- "name": the person's full name
- "email": their email address
- "city": the city they live in

Output the JSON object only — no code fences, no commentary.
"""


def _toy_examples_jsonl() -> str:
    people = (
        ("Ada Lovelace", "ada@calc.org", "London"),
        ("Grace Hopper", "grace@navy.mil", "Arlington"),
        ("Alan Turing", "alan@bletchley.uk", "Wilmslow"),
        ("Katherine Johnson", "kj@nasa.gov", "Hampton"),
        ("Edsger Dijkstra", "ewd@utexas.edu", "Austin"),
        ("Barbara Liskov", "liskov@mit.edu", "Cambridge"),
        ("Donald Knuth", "don@stanford.edu", "Stanford"),
        ("Margaret Hamilton", "mh@draper.com", "Boston"),
        ("John McCarthy", "jmc@stanford.edu", "Palo Alto"),
        ("Frances Allen", "fran@ibm.com", "Yorktown"),
        ("Tony Hoare", "car@oxford.uk", "Oxford"),
        ("Radia Perlman", "radia@dec.com", "Boston"),
        ("Dennis Ritchie", "dmr@bell-labs.com", "Murray Hill"),
        ("Adele Goldberg", "adele@parc.com", "Palo Alto"),
        ("Ken Thompson", "ken@bell-labs.com", "Murray Hill"),
        ("Jean Bartik", "jean@eniac.org", "Philadelphia"),
        ("Niklaus Wirth", "wirth@ethz.ch", "Zurich"),
        ("Mary Shaw", "shaw@cmu.edu", "Pittsburgh"),
        ("Leslie Lamport", "ll@microsoft.com", "New York"),
        ("Shafi Goldwasser", "shafi@mit.edu", "Cambridge"),
        ("Vint Cerf", "vint@google.com", "McLean"),
        ("Lynn Conway", "lynn@umich.edu", "Ann Arbor"),
        ("Tim Berners-Lee", "timbl@w3.org", "Boston"),
        ("Anita Borg", "anita@borg.org", "Palo Alto"),
    )
    templates = (
        "Name: {name}\nEmail: {email}\nCity: {city}",
        "{name} lives in {city}. Reach them at {email}.",
        "Contact card — {name} <{email}> based in {city}",
    )
    lines = [
        json.dumps(
            {
                "input": templates[i % len(templates)].format(name=name, email=email, city=city),
                "expected_output": json.dumps(
                    {"name": name, "email": email, "city": city}, sort_keys=True
                ),
            },
            sort_keys=True,
        )
        for i, (name, email, city) in enumerate(people)
    ]
    return "\n".join(lines) + "\n"
