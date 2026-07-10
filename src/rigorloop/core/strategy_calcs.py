"""Pure strategy logic: context assembly, agent-reply parsing, validation
cohorts and cadence, stopping rules, champion selection, and the dev
leaderboard.

The search base (the artifact future generations refine) is the validation
champion once any candidate has been validated; the raw dev leaderboard is a
diagnostic, not a selection rule."""

from __future__ import annotations

import json
import random
import re

from rigorloop.core.scoring_calcs import example_passed, significantly_better
from rigorloop.core.types import (
    NOTHING,
    BudgetExhausted,
    CandidateScore,
    ChampionArtifact,
    ContinueDecision,
    DevExample,
    Directive,
    DirectiveSpec,
    Err,
    ExampleResult,
    ExecutionFailed,
    ExecutionOk,
    FailureSample,
    JudgeVerdict,
    LeaderboardEntry,
    MalformedReply,
    Nothing,
    Ok,
    Option,
    Passed,
    Result,
    RunConfig,
    RunState,
    Some,
    StopReason,
    StopRequested,
    StrategyContext,
    StrategyDecision,
    StrategyLogEntry,
    StrategyUnresponsive,
    TargetReached,
    ValCheckpoint,
    ValidatedCandidate,
    ValidationPlateau,
)

ALPHA = 0.05
MAX_FALLBACKS = 2
_LEADERBOARD_TOP = 8
_FAILURE_SAMPLE_LIMIT = 3
_GAP_WARN = 0.15


def initial_state() -> RunState:
    return RunState(
        loops_completed=0,
        leaderboard=(),
        strategy_log=(),
        val_champion=NOTHING,
        checkpoints=(),
        peeks_used=0,
        last_peek_loop=NOTHING,
        consecutive_fallbacks=0,
    )


# --------------------------------------------------------------------------
# Deterministic dev-subset sampling
# --------------------------------------------------------------------------


def sample_dev_subset(
    dev: tuple[DevExample, ...], k: int, seed: int, loop_index: int
) -> tuple[DevExample, ...]:
    """Resampled every loop, deterministically from the injected seed and the
    loop index, so score movement isn't over-attributed to directives."""
    if k >= len(dev):
        return dev
    rng = random.Random(seed * 1_000_003 + loop_index)  # pure function of its inputs
    return tuple(rng.sample(dev, k=k))


# --------------------------------------------------------------------------
# Agent-reply parsing (strategy JSON, executor fenced block, judge JSON)
# --------------------------------------------------------------------------


def _json_candidates(text: str) -> tuple[str, ...]:
    fenced = tuple(re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL))
    start, end = text.find("{"), text.rfind("}")
    braced = (text[start : end + 1],) if 0 <= start < end else ()
    return (text.strip(), *fenced, *braced)


def _extract_json_object(text: str) -> Result[dict[str, object], MalformedReply]:
    def try_load(chunk: str) -> dict[str, object] | None:
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    loaded = next((o for o in map(try_load, _json_candidates(text)) if o is not None), None)
    return Ok(loaded) if loaded is not None else Err(MalformedReply("no JSON object found"))


def _parse_directive_spec(index: int, raw: object) -> Result[DirectiveSpec, MalformedReply]:
    if not isinstance(raw, dict):
        return Err(MalformedReply(f"directives[{index}] is not an object"))
    summary = raw.get("approach_summary")
    instructions = raw.get("instructions")
    base = raw.get("base_on_champion", False)
    if not isinstance(summary, str) or not summary.strip():
        return Err(MalformedReply(f"directives[{index}].approach_summary missing or empty"))
    if not isinstance(instructions, str) or not instructions.strip():
        return Err(MalformedReply(f"directives[{index}].instructions missing or empty"))
    if not isinstance(base, bool):
        return Err(MalformedReply(f"directives[{index}].base_on_champion must be a boolean"))
    return Ok(DirectiveSpec(summary.strip(), instructions.strip(), base))


def parse_strategy_reply(text: str) -> Result[StrategyDecision, MalformedReply]:
    obj = _extract_json_object(text)
    if isinstance(obj, Err):
        return obj
    data = obj.value
    action = data.get("action")
    if action == "stop":
        reason = data.get("reason", data.get("stop_reason", ""))
        return Ok(StopRequested(reason if isinstance(reason, str) else ""))
    if action != "continue":
        return Err(MalformedReply(f"action must be 'continue' or 'stop', got {action!r}"))
    raw_directives = data.get("directives")
    if not isinstance(raw_directives, list) or not raw_directives:
        return Err(MalformedReply("continue decision needs a non-empty 'directives' array"))
    parsed = [_parse_directive_spec(i, d) for i, d in enumerate(raw_directives)]
    failures = [p for p in parsed if isinstance(p, Err)]
    if failures:
        return failures[0]
    observations = data.get("observations", "")
    hypotheses = data.get("hypotheses", "")
    request_validation = data.get("request_validation", False)
    if not isinstance(request_validation, bool):
        return Err(MalformedReply("'request_validation' must be a boolean"))
    return Ok(
        ContinueDecision(
            observations=observations if isinstance(observations, str) else "",
            hypotheses=hypotheses if isinstance(hypotheses, str) else "",
            directive_specs=tuple(p.value for p in parsed if isinstance(p, Ok)),
            request_validation=request_validation,
        )
    )


def parse_executor_reply(text: str) -> Result[str, MalformedReply]:
    """The output contract: exactly one fenced block tagged `solution`."""
    markers = text.count("```solution")
    if markers == 0:
        return Err(MalformedReply("no ```solution fenced block found"))
    if markers > 1:
        return Err(MalformedReply(f"expected exactly one ```solution block, found {markers}"))
    # The block must close with the LAST fence in the reply, so solution
    # content may itself contain fenced code blocks.
    match = re.search(r"```solution[^\n]*\n(.*)\n```\s*\Z", text, re.DOTALL)
    if match is None:
        return Err(MalformedReply("```solution block is not terminated at the end of the reply"))
    content = match.group(1)
    if not content.strip():
        return Err(MalformedReply("```solution block is empty"))
    return Ok(content)


def parse_judge_reply(text: str) -> Result[JudgeVerdict, MalformedReply]:
    obj = _extract_json_object(text)
    if isinstance(obj, Err):
        return obj
    verdict = obj.value.get("pass", obj.value.get("passed"))
    if not isinstance(verdict, bool):
        return Err(MalformedReply("judge reply needs a boolean 'pass' field"))
    reason = obj.value.get("reason", "")
    return Ok(JudgeVerdict(verdict, reason if isinstance(reason, str) else ""))


# --------------------------------------------------------------------------
# Directives and the fallback path
# --------------------------------------------------------------------------


def build_directives(
    specs: tuple[DirectiveSpec, ...],
    champion: Option[ChampionArtifact],
    loop_index: int,
    max_directives: int,
) -> tuple[Directive, ...]:
    """Attach the champion artifact where requested. The artifact is the one
    sanctioned carry-forward channel: solution content only."""

    def build(index: int, spec: DirectiveSpec) -> Directive:
        base: Option[ChampionArtifact] = champion if spec.base_on_champion else NOTHING
        return Directive(
            directive_id=f"L{loop_index}-d{index}",
            approach_summary=spec.approach_summary,
            instructions=spec.instructions,
            base=base,
        )

    return tuple(build(i, spec) for i, spec in enumerate(specs[:max_directives], start=1))


def fallback_decision(has_champion: bool) -> ContinueDecision:
    """Substituted when the strategy agent's reply cannot be parsed twice."""
    spec = (
        DirectiveSpec(
            approach_summary="Refine the current champion solution",
            instructions=(
                "Carefully review the provided current best solution and produce an "
                "improved version: fix edge cases, tighten output formatting, and "
                "keep whatever already works."
            ),
            base_on_champion=True,
        )
        if has_champion
        else DirectiveSpec(
            approach_summary="Produce a solid first solution",
            instructions=(
                "Read the task description and the example inputs and outputs "
                "closely, then produce a careful, straightforward solution."
            ),
            base_on_champion=False,
        )
    )
    return ContinueDecision(
        observations="(fallback: strategy reply was malformed)",
        hypotheses="",
        directive_specs=(spec,),
        request_validation=False,
    )


# --------------------------------------------------------------------------
# Leaderboard
# --------------------------------------------------------------------------


def fold_leaderboard(
    leaderboard: tuple[LeaderboardEntry, ...], new_entries: tuple[LeaderboardEntry, ...]
) -> tuple[LeaderboardEntry, ...]:
    """Ranked by pass rate (stable: earlier candidates win ties)."""
    merged = (*leaderboard, *new_entries)
    return tuple(sorted(merged, key=lambda e: (-e.score.pass_rate, e.loop_index, e.candidate_id)))


def dev_best(leaderboard: tuple[LeaderboardEntry, ...]) -> Option[LeaderboardEntry]:
    return Some(leaderboard[0]) if leaderboard else NOTHING


def search_base(state: RunState) -> Option[LeaderboardEntry]:
    """The primary exploitation base for the next generation: the validation
    champion once any candidate has been validated, the dev leader before
    that. Validation evidence — not raw dev rank — steers the search."""
    match state.val_champion:
        case Some(champion):
            found = next(
                (e for e in state.leaderboard if e.candidate_id == champion.candidate_id), None
            )
            return Some(found) if found is not None else dev_best(state.leaderboard)
        case Nothing():
            return dev_best(state.leaderboard)


def champion_artifact(entry: LeaderboardEntry) -> ChampionArtifact:
    return ChampionArtifact(entry.candidate_id, entry.kind, entry.content)


def beats_previous_best(
    previous_best: Option[LeaderboardEntry], challenger: LeaderboardEntry
) -> bool:
    """CI-band-gated: a raw uptick is not an improvement."""
    match previous_best:
        case Nothing():
            return True
        case Some(best):
            return significantly_better(challenger.score.pass_vector, best.score.pass_vector, ALPHA)


def render_leaderboard_lines(
    leaderboard: tuple[LeaderboardEntry, ...], top: int = _LEADERBOARD_TOP
) -> tuple[str, ...]:
    """Aggregate scores only; differences within noise of the best are marked
    so the strategy agent doesn't chase them."""
    if not leaderboard:
        return ()
    best = leaderboard[0]

    def line(rank: int, e: LeaderboardEntry) -> str:
        score = e.score
        within_noise = e is not best and not significantly_better(
            best.score.pass_vector, score.pass_vector, ALPHA
        )
        flags = (" — not statistically distinguishable from best" if within_noise else "") + (
            " — evaluation aborted early; missing examples count as failures"
            if score.eval_aborted
            else ""
        )
        return (
            f"{rank}. {e.candidate_id} (loop {e.loop_index}): "
            f"{score.pass_rate:.1%} [{score.ci_low:.1%}, {score.ci_high:.1%}] "
            f"on n={score.n}{flags}"
        )

    return tuple(line(i, e) for i, e in enumerate(leaderboard[:top], start=1))


# --------------------------------------------------------------------------
# Failure patterns (dev-only, for the strategy context)
# --------------------------------------------------------------------------


def failure_samples(
    dev: tuple[DevExample, ...],
    results: tuple[ExampleResult, ...],
    limit: int = _FAILURE_SAMPLE_LIMIT,
) -> tuple[FailureSample, ...]:
    by_id = {d.example.example_id: d for d in dev}

    def sample(result: ExampleResult) -> FailureSample:
        match result.execution:
            case ExecutionOk(output_text):
                actual = output_text
            case ExecutionFailed(detail):
                actual = f"(no output: {detail})"
        return FailureSample(
            dev_example=by_id[result.example_id],
            actual_output=actual,
            failed_checks=tuple(
                o.check_name for o in result.outcomes if not isinstance(o.outcome, Passed)
            ),
        )

    failing = tuple(r for r in results if not example_passed(r) and r.example_id in by_id)
    return tuple(sample(r) for r in failing[:limit])


# --------------------------------------------------------------------------
# Validation cohorts, cadence, and champion selection
# --------------------------------------------------------------------------


def validation_cohort(state: RunState, config: RunConfig) -> tuple[LeaderboardEntry, ...]:
    """The candidates a checkpoint will evaluate, precommitted before any of
    that checkpoint's validation outcomes are seen: the top unvalidated
    candidates by dev score, with the last slot reserved for the best
    unvalidated candidate NOT built on the champion (approach diversity).
    Capped by the remaining peek budget — every evaluation costs one peek."""
    already_validated = {c.candidate_id for c in state.checkpoints}
    unvalidated = tuple(e for e in state.leaderboard if e.candidate_id not in already_validated)
    size = min(config.validation.cohort_size, config.validation.max_peeks - state.peeks_used)
    if size <= 0 or not unvalidated:
        return ()
    top = unvalidated[:size]
    diverse = next((e for e in unvalidated if not e.based_on_champion), None)
    if diverse is None or diverse in top or size < 2:
        return top
    return (*top[:-1], diverse)


def should_validate(
    state: RunState,
    config: RunConfig,
    strategy_requested: bool,
    new_best_significant: bool,
) -> bool:
    """Peeks are budgeted per candidate evaluation; triggered peeks respect a
    minimum gap so early easy wins can't front-load the budget. A checkpoint
    runs only when its precommitted cohort has at least one candidate."""
    if not validation_cohort(state, config):
        return False
    scheduled = state.loops_completed % config.validation.val_every == 0
    match state.last_peek_loop:
        case Nothing():
            gap_ok = True
        case Some(last):
            gap_ok = state.loops_completed - last >= config.validation.min_loops_between_peeks
    triggered = (strategy_requested or new_best_significant) and gap_ok
    return scheduled or triggered


def select_val_champion(
    incumbent: Option[ValidatedCandidate], challenger: ValidatedCandidate
) -> tuple[ValidatedCandidate, bool]:
    """Noise-aware selection on validation evidence only: (winner, improvement).

    A challenger counts as an improvement (for the plateau rule) only beyond
    the paired-test noise band; within the band a higher raw validation rate
    takes the title without counting as improvement. Dev scores never break
    the tie — dev is the metric under direct selection pressure, and letting
    it decide here would let an overfit dev leader displace a better
    generalizer."""
    match incumbent:
        case Nothing():
            return challenger, True
        case Some(current):
            if significantly_better(
                challenger.val_score.pass_vector, current.val_score.pass_vector, ALPHA
            ):
                return challenger, True
            if significantly_better(
                current.val_score.pass_vector, challenger.val_score.pass_vector, ALPHA
            ):
                return current, False
            tie_break = challenger.val_score.pass_rate > current.val_score.pass_rate
            return (challenger, False) if tie_break else (current, False)


# --------------------------------------------------------------------------
# Stopping rules
# --------------------------------------------------------------------------


def stopping_decision(state: RunState, config: RunConfig) -> Option[StopReason]:
    """Checked after each loop's bookkeeping. Strategy-requested stops and
    unresponsiveness are handled where they arise; this covers the rest."""
    if state.consecutive_fallbacks >= MAX_FALLBACKS:
        return Some(StrategyUnresponsive(state.consecutive_fallbacks))
    match config.validation.target_pass_rate, state.val_champion:
        case Some(target), Some(champion):
            # Gate on the lower confidence bound, not the point estimate: a
            # lucky draw on a small validation set must not end the run.
            if champion.val_score.ci_low >= target:
                return Some(TargetReached(champion.val_score.pass_rate))
        case _, _:
            pass
    patience = config.validation.patience
    # A checkpoint evaluates a whole cohort, so the plateau rule counts
    # checkpoint LOOPS; a loop improved if any cohort member displaced the
    # champion beyond the noise band.
    checkpoint_loops = tuple(sorted({c.loop_index for c in state.checkpoints}))
    recent = checkpoint_loops[-patience:]
    improved = tuple(
        any(c.displaced_champion for c in state.checkpoints if c.loop_index == loop)
        for loop in recent
    )
    if len(recent) == patience and not any(improved):
        return Some(ValidationPlateau(patience))
    if state.loops_completed >= config.loop.max_loops:
        return Some(BudgetExhausted(config.loop.max_loops))
    return NOTHING


# --------------------------------------------------------------------------
# Strategy log and context assembly
# --------------------------------------------------------------------------


def dev_summary_line(n_scored: int, n_malformed: int, best: Option[LeaderboardEntry]) -> str:
    best_text = ""
    match best:
        case Some(entry):
            best_text = (
                f"; loop best {entry.candidate_id} at {entry.score.pass_rate:.1%} "
                f"[{entry.score.ci_low:.1%}, {entry.score.ci_high:.1%}]"
            )
        case Nothing():
            best_text = ""
    malformed_text = f"; {n_malformed} malformed candidate(s)" if n_malformed else ""
    return f"{n_scored} candidate(s) scored{malformed_text}{best_text}"


def val_summary_line(checkpoint: ValCheckpoint) -> str:
    gap = checkpoint.dev_pass_rate - checkpoint.val_pass_rate
    displaced = "new champion" if checkpoint.displaced_champion else "champion unchanged"
    return (
        f"validated {checkpoint.candidate_id}: {checkpoint.val_pass_rate:.1%} on validation "
        f"(dev {checkpoint.dev_pass_rate:.1%}, gap {gap:+.1%}); {displaced}"
    )


def compact_log_line(entry: StrategyLogEntry) -> str:
    approaches = "; ".join(d.approach_summary for d in entry.directives)
    val_text = ""
    match entry.val_summary:
        case Some(summary):
            val_text = f" | {summary}"
        case Nothing():
            val_text = ""
    return f"loop {entry.loop_index}: [{approaches}] | {entry.dev_summary}{val_text}"


def _score_span(score: CandidateScore) -> str:
    return f"{score.pass_rate:.1%} [{score.ci_low:.1%}, {score.ci_high:.1%}] on n={score.n}"


def _champion_line(entry: LeaderboardEntry, val_champion: Option[ValidatedCandidate]) -> str:
    match val_champion:
        case Some(champion) if champion.candidate_id == entry.candidate_id:
            return (
                f"{entry.candidate_id}: dev {_score_span(champion.dev_score)}; "
                f"validation {_score_span(champion.val_score)} — champion by validation score"
            )
        case Some(_) | Nothing():
            return (
                f"{entry.candidate_id}: dev {_score_span(entry.score)} — dev leader "
                "(no validation checkpoint yet)"
            )


def _latest_val_rate(checkpoints: tuple[ValCheckpoint, ...], candidate_id: str) -> Option[float]:
    rates = tuple(c.val_pass_rate for c in checkpoints if c.candidate_id == candidate_id)
    return Some(rates[-1]) if rates else NOTHING


def _dev_leader_line(best: LeaderboardEntry, state: RunState) -> str:
    """Diagnostic line for a dev leader that is NOT the champion: aggregate
    scores only, never its content."""
    base_text = f"{best.candidate_id} (loop {best.loop_index}): dev {_score_span(best.score)}"
    match _latest_val_rate(state.checkpoints, best.candidate_id):
        case Some(val_rate):
            gap = best.score.pass_rate - val_rate
            warning = (
                " — WARNING: large dev-val gap; likely overfit to the dev set. "
                "Do not chase its dev score."
                if gap > _GAP_WARN
                else ""
            )
            return f"{base_text}; validation {val_rate:.1%}{warning}"
        case Nothing():
            return f"{base_text}; not yet validated"


def assemble_strategy_context(
    task_description: str,
    config: RunConfig,
    state: RunState,
    samples: tuple[FailureSample, ...],
    check_names: tuple[str, ...],
) -> StrategyContext:
    """Full detail for the most recent loops, compact lines beyond that, the
    dev leaderboard with CIs, the champion's content and dev failure patterns,
    a diagnostic line for a diverging dev leader, and aggregate validation
    scores. Nothing else."""
    detail = config.loop.strategy_full_detail_loops
    recent = state.strategy_log[-detail:]
    compacted = tuple(compact_log_line(e) for e in state.strategy_log[:-detail])

    base = search_base(state)
    champion: Option[ChampionArtifact] = NOTHING
    champion_line: Option[str] = NOTHING
    match base:
        case Some(entry):
            champion = Some(champion_artifact(entry))
            champion_line = Some(_champion_line(entry, state.val_champion))
        case Nothing():
            pass

    dev_leader_line: Option[str] = NOTHING
    match base, dev_best(state.leaderboard):
        case Some(base_entry), Some(best_entry) if (
            base_entry.candidate_id != best_entry.candidate_id
        ):
            dev_leader_line = Some(_dev_leader_line(best_entry, state))
        case _, _:
            pass

    val_lines = tuple(val_summary_line(c) for c in state.checkpoints)
    gap_warning: Option[str] = NOTHING
    match state.val_champion:
        case Some(val_champion):
            gap = val_champion.dev_score.pass_rate - val_champion.val_score.pass_rate
            if gap > _GAP_WARN:
                gap_warning = Some(
                    f"WARNING: the champion's dev-val gap is {gap:+.1%} — the loop may be "
                    "overfitting to the dev set. Prefer simpler, more general approaches."
                )
        case Nothing():
            pass

    return StrategyContext(
        task_description=task_description,
        solution_kind=config.task.solution_kind,
        loops_completed=state.loops_completed,
        max_loops=config.loop.max_loops,
        executors_per_loop=config.loop.executors_per_loop,
        check_names=check_names,
        recent_log=recent,
        compacted_log=compacted,
        leaderboard_lines=render_leaderboard_lines(state.leaderboard),
        failure_samples=samples,
        champion=champion,
        champion_line=champion_line,
        dev_leader_line=dev_leader_line,
        val_lines=val_lines,
        dev_val_gap_warning=gap_warning,
        peeks_used=state.peeks_used,
        max_peeks=config.validation.max_peeks,
        cohort_size=config.validation.cohort_size,
        dev_subset_note=(
            "Executor agents see a dev-example subset that is resampled every loop; "
            "some loop-to-loop score movement is sample luck, and differences marked "
            "as within noise should not be chased."
        ),
    )
