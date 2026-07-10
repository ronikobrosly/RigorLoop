"""Pure rendering of user-facing artifacts: the final report, the pre-run
check summary, and the kind-aware agent-call budget estimate."""

from __future__ import annotations

from rigorloop.core.strategy_calcs import compact_log_line
from rigorloop.core.types import (
    BudgetEstimate,
    BudgetExhausted,
    CandidateScore,
    Check,
    DuplicateWarning,
    GuidanceSolution,
    LlmJudge,
    PowerWarning,
    RunConfig,
    RunState,
    ScriptSolution,
    SkillSolution,
    SolutionKind,
    StopReason,
    StrategyRequestedStop,
    StrategyUnresponsive,
    TargetReached,
    ValidatedCandidate,
    ValidationPlateau,
)


def stop_reason_label(reason: StopReason) -> str:
    match reason:
        case BudgetExhausted(max_loops):
            return f"loop budget exhausted ({max_loops} loops)"
        case ValidationPlateau(checkpoints):
            return (
                f"validation plateau: {checkpoints} consecutive checkpoints without "
                "improvement beyond the CI band"
            )
        case TargetReached(pass_rate):
            return (
                f"target pass rate reached ({pass_rate:.1%} on validation; "
                "lower confidence bound cleared the target)"
            )
        case StrategyRequestedStop(why):
            return f"strategy agent requested stop: {why}"
        case StrategyUnresponsive(fallbacks):
            return f"strategy agent unresponsive ({fallbacks} consecutive malformed replies)"


def _kind_name(kind: SolutionKind) -> str:
    match kind:
        case ScriptSolution():
            return "script"
        case SkillSolution():
            return "skill"
        case GuidanceSolution():
            return "guidance"


def _is_stochastic_kind(kind: SolutionKind) -> bool:
    match kind:
        case ScriptSolution():
            return False
        case SkillSolution() | GuidanceSolution():
            return True


def _per_example_eval_calls(kind: SolutionKind) -> int:
    """Skill/guidance evaluation costs one claude call per example; scripts run
    locally for free."""
    return 1 if _is_stochastic_kind(kind) else 0


def _judge_samples_per_example(checks: tuple[Check, ...]) -> int:
    return sum(c.n_samples for c in checks if isinstance(c, LlmJudge))


def estimate_budget(config: RunConfig, n_dev: int, n_val: int, n_test: int) -> BudgetEstimate:
    """Upper bound on claude calls for a full run (retries excluded). For
    skill/guidance kinds, candidate evaluation is the dominant cost."""
    loops = config.loop.max_loops
    candidates = loops * config.loop.executors_per_loop
    per_example = _per_example_eval_calls(config.task.solution_kind)
    judge_per_example = _judge_samples_per_example(config.checks)
    evaluated_examples = candidates * n_dev + config.validation.max_peeks * n_val + n_test
    return BudgetEstimate(
        strategy_calls=loops,
        executor_calls=candidates,
        solution_eval_calls=evaluated_examples * per_example,
        judge_calls=evaluated_examples * judge_per_example,
        total_calls=loops + candidates + evaluated_examples * (per_example + judge_per_example),
    )


def render_check_summary(
    n_total: int,
    n_dev: int,
    n_val: int,
    n_test: int,
    duplicates: tuple[DuplicateWarning, ...],
    warnings: tuple[PowerWarning, ...],
    budget: BudgetEstimate,
    model: str,
) -> str:
    dup_lines = (
        "\n".join(
            f"  - input {d.input_preview!r} appears {d.occurrences} times (collapsed to one)"
            for d in duplicates
        )
        if duplicates
        else "  (none)"
    )
    warn_lines = "\n".join(f"  - {w.message}" for w in warnings) if warnings else "  (none)"
    return (
        "RigorLoop pre-run check\n"
        "=======================\n"
        f"Examples: {n_total} unique → dev {n_dev} / validation {n_val} / test {n_test}\n"
        f"Exact duplicates collapsed:\n{dup_lines}\n"
        f"Statistical power warnings:\n{warn_lines}\n"
        "\n"
        f"Agent-call budget estimate (upper bound, model {model}):\n"
        f"  strategy calls:              {budget.strategy_calls}\n"
        f"  executor calls:              {budget.executor_calls}\n"
        f"  solution evaluation calls:   {budget.solution_eval_calls}\n"
        f"  judge calls:                 {budget.judge_calls}\n"
        f"  total:                       {budget.total_calls}\n"
        "\n"
        "No tokens were spent by this command."
    )


def _score_line(name: str, score: CandidateScore) -> str:
    aborted = " (evaluation aborted early)" if score.eval_aborted else ""
    return (
        f"| {name} | {score.pass_rate:.1%} | [{score.ci_low:.1%}, {score.ci_high:.1%}] "
        f"| {score.passes}/{score.n} |{aborted}"
    )


def render_report(
    run_id: str,
    kind: SolutionKind,
    eval_model: str,
    cli_version: str,
    stop_reason: StopReason,
    winner: ValidatedCandidate,
    test_score: CandidateScore,
    state: RunState,
    agent_calls_made: int,
) -> str:
    per_check = (
        "\n".join(
            f"| {c.check_name} | {c.passes}/{c.n} ({(c.passes / c.n if c.n else 0):.1%}) |"
            for c in test_score.per_check
        )
        or "| (none) | |"
    )
    history = "\n".join(f"- {compact_log_line(e)}" for e in state.strategy_log) or "(no loops)"
    dev_rate = winner.dev_score.pass_rate
    val_rate = winner.val_score.pass_rate
    test_rate = test_score.pass_rate
    stochastic_note = (
        "\n> **Stochastic evaluator caveat:** this artifact kind is evaluated by a "
        f"model (`{eval_model}`, claude CLI {cli_version}). Scores are conditional on "
        "that pinned evaluator and understate total uncertainty (one sample per "
        "example).\n"
        if _is_stochastic_kind(kind)
        else ""
    )
    return (
        f"# RigorLoop report — run `{run_id}`\n\n"
        f"- Artifact kind: **{_kind_name(kind)}**\n"
        f"- Winning candidate: **{winner.candidate_id}**\n"
        f"- Stop reason: {stop_reason_label(stop_reason)}\n"
        f"- Loops completed: {state.loops_completed}\n"
        f"- Validation peeks used: {state.peeks_used}\n"
        f"- Agent calls made: {agent_calls_made}\n\n"
        "## Scores (95% Wilson intervals)\n\n"
        "| Set | Pass rate | CI | Passes |\n|---|---|---|---|\n"
        f"{_score_line('dev', winner.dev_score)}\n"
        f"{_score_line('validation', winner.val_score)}\n"
        f"{_score_line('test', test_score)}\n\n"
        f"Generalization gaps: dev→val {dev_rate - val_rate:+.1%}, "
        f"val→test {val_rate - test_rate:+.1%}, dev→test {dev_rate - test_rate:+.1%}\n\n"
        "> **Selection-bias caveat:** validation scores both steered the search "
        "between loops and *selected* this winner, so the validation number is "
        "optimistically biased. The test score — computed exactly once, on examples "
        "no agent ever saw — is the honest number.\n"
        f"{stochastic_note}\n"
        "## Per-check breakdown on the test set\n\n"
        "| Check | Passes |\n|---|---|\n"
        f"{per_check}\n\n"
        "## Loop history\n\n"
        f"{history}\n\n"
        "---\n"
        "Re-running RigorLoop after seeing this test score burns the holdout: treat "
        "this test set as spent and supply fresh examples for the next run.\n"
    )
