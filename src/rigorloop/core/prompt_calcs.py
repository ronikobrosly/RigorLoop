"""Pure prompt-string builders, split into two typed channels.

Agent-context builders (strategy, executor) accept Dev-typed examples and
aggregate scores only — passing val/test examples is a type error. Evaluation
builders (solution-under-test, judge) run ONE example of any split and return
their output to the harness as data, never into an agent-context prompt."""

from __future__ import annotations

from rigorloop.core.types import (
    AgentContextPrompt,
    Check,
    CustomPython,
    DevExample,
    Directive,
    EvalPrompt,
    ExactMatch,
    Example,
    ExecutorRole,
    GuidanceSolution,
    JsonEquality,
    LlmJudge,
    NormalizedMatch,
    Nothing,
    NumericTolerance,
    RegexMatch,
    ScriptSolution,
    SkillSolution,
    SolutionKind,
    Some,
    StrategyContext,
    StrategyLogEntry,
    StrategyRole,
)

_MAX_EXAMPLE_CHARS = 1500


def kind_label(kind: SolutionKind) -> str:
    match kind:
        case ScriptSolution():
            return "an executable Python script"
        case SkillSolution():
            return "a skill markdown file for an AI agent (SKILL.md style)"
        case GuidanceSolution():
            return "a guidance markdown file for AI agents (AGENTS.md / CLAUDE.md style)"


def describe_check(check: Check) -> str:
    match check:
        case ExactMatch():
            return "exact_match: the output must equal the expected output exactly"
        case NormalizedMatch(lowercase, strip, collapse):
            rules = ", ".join(
                name
                for name, on in (
                    ("lowercased", lowercase),
                    ("stripped", strip),
                    ("whitespace-collapsed", collapse),
                )
                if on
            )
            return f"normalized_match: outputs must match after being {rules}"
        case JsonEquality():
            return "json_equality: the output must parse as JSON equal to the expected output"
        case RegexMatch(pattern):
            return f"regex_match: the output must contain a match for the pattern {pattern!r}"
        case NumericTolerance(atol, rtol):
            return (
                f"numeric_tolerance: the output must be a number within atol={atol}, "
                f"rtol={rtol} of the expected output"
            )
        case CustomPython(script_path):
            return f"custom_python: a user-supplied checker script ({script_path}) must accept it"
        case LlmJudge(rubric, n_samples, pass_threshold):
            return (
                f"llm_judge (majority of {n_samples} samples, threshold {pass_threshold}): {rubric}"
            )


def _clip(text: str, limit: int = _MAX_EXAMPLE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_dev_example(index: int, dev: DevExample) -> str:
    return (
        f"### Example {index}\n"
        f"Input:\n{_clip(dev.example.input_text)}\n"
        f"Expected output:\n{_clip(dev.example.expected_output)}"
    )


def render_directive(directive: Directive) -> str:
    """Directives carry champion solution CONTENT only — never scores,
    mistakes, or per-example failures from prior loops."""
    base_text = ""
    match directive.base:
        case Some(artifact):
            base_text = (
                "\n\nStart from this existing solution and refine it "
                "(keep what works, change what the instructions call out):\n"
                f"<current-solution>\n{artifact.content}\n</current-solution>"
            )
        case Nothing():
            base_text = ""
    return (
        f"Approach: {directive.approach_summary}\nInstructions: {directive.instructions}{base_text}"
    )


# --------------------------------------------------------------------------
# Agent-context channel
# --------------------------------------------------------------------------


def _output_contract(kind: SolutionKind) -> str:
    artifact = {
        "script": (
            "a single self-contained Python 3 script that reads the raw input text "
            "from stdin and writes exactly the required output to stdout (no prompts, "
            "no logging, no extra whitespace). Standard library only."
        ),
        "skill": (
            "a complete skill markdown document that tells an AI agent, precisely and "
            "step by step, how to transform an input of this kind into the required "
            "output. The agent following it will reply with the output only."
        ),
        "guidance": (
            "a complete guidance markdown document (AGENTS.md / CLAUDE.md style) that "
            "steers an AI agent to transform an input of this kind into the required "
            "output. The agent following it will reply with the output only."
        ),
    }
    key = (
        "script"
        if isinstance(kind, ScriptSolution)
        else "skill"
        if isinstance(kind, SkillSolution)
        else "guidance"
    )
    return (
        f"Produce {artifact[key]}\n\n"
        "Reply with EXACTLY ONE fenced code block tagged `solution`, and make it the "
        "last thing in your reply:\n"
        "```solution\n<your full artifact here>\n```\n"
        "Do not put anything after the closing fence. Do not use a ```solution fence "
        "anywhere else in your reply."
    )


def build_executor_prompt(
    task_description: str,
    kind: SolutionKind,
    directive: Directive,
    checks: tuple[Check, ...],
    dev_sample: tuple[DevExample, ...],
) -> AgentContextPrompt:
    """Executors see the task, the current directive, the checks, and a dev
    sample. Nothing else about prior loops (leakage guarantee)."""
    checks_text = "\n".join(f"- {describe_check(c)}" for c in checks)
    examples_text = "\n\n".join(
        _render_dev_example(i, d) for i, d in enumerate(dev_sample, start=1)
    )
    text = (
        "You are an executor agent inside RigorLoop, an iterative build framework. "
        "Your job is to produce one candidate solution for the task below.\n\n"
        f"# Task\n{task_description}\n\n"
        f"# Directive for this attempt\n{render_directive(directive)}\n\n"
        f"# How the solution will be verified (every check must pass per example)\n"
        f"{checks_text}\n\n"
        f"# Representative examples\n{examples_text}\n\n"
        f"# Required output\n{_output_contract(kind)}"
    )
    return AgentContextPrompt(role=ExecutorRole(), text=text)


def _render_log_entry(entry: StrategyLogEntry) -> str:
    directives = "\n".join(
        f"  {i}. {d.approach_summary}"
        + (" (based on champion)" if isinstance(d.base, Some) else "")
        for i, d in enumerate(entry.directives, start=1)
    )
    val_text = ""
    match entry.val_summary:
        case Some(summary):
            val_text = f"\n- validation: {summary}"
        case Nothing():
            val_text = ""
    fallback_text = (
        "\n- (this loop used a fallback directive: your reply was malformed)"
        if entry.fallback
        else ""
    )
    return (
        f"## Loop {entry.loop_index}\n"
        f"- observations: {entry.observations}\n"
        f"- hypotheses: {entry.hypotheses}\n"
        f"- directives issued:\n{directives}\n"
        f"- dev results: {entry.dev_summary}{val_text}{fallback_text}"
    )


def build_strategy_prompt(context: StrategyContext) -> AgentContextPrompt:
    checks_text = "\n".join(f"- {name}" for name in context.check_names)
    compacted = (
        "\n".join(f"- {line}" for line in context.compacted_log)
        if context.compacted_log
        else "(none)"
    )
    recent = (
        "\n\n".join(_render_log_entry(e) for e in context.recent_log)
        if context.recent_log
        else "(no loops yet — this is the first loop; propose diverse initial approaches)"
    )
    leaderboard = "\n".join(context.leaderboard_lines) if context.leaderboard_lines else "(empty)"
    champion_text = "(none yet)"
    match context.champion, context.champion_dev_line:
        case Some(artifact), Some(dev_line):
            champion_text = (
                f"{dev_line}\n<current-champion>\n{artifact.content}\n</current-champion>"
            )
        case _, _:
            pass
    failures = (
        "\n\n".join(
            f"- Input:\n{_clip(s.dev_example.example.input_text)}\n"
            f"  Expected:\n{_clip(s.dev_example.example.expected_output)}\n"
            f"  Actual:\n{_clip(s.actual_output)}\n"
            f"  Failed checks: {', '.join(s.failed_checks)}"
            for s in context.failure_samples
        )
        if context.failure_samples
        else "(none collected)"
    )
    val_text = "\n".join(context.val_lines) if context.val_lines else "(no checkpoints yet)"
    gap_text = ""
    match context.dev_val_gap_warning:
        case Some(warning):
            gap_text = f"\n{warning}"
        case Nothing():
            gap_text = ""

    text = (
        "You are the strategy agent of RigorLoop, an iterative agentic build framework. "
        "Each loop you review results on the DEV set and direct a pool of executor "
        "agents. Executors are stateless: they see only your directive, the task, the "
        "checks, and a dev sample — never scores or prior mistakes. If you set "
        "base_on_champion, the harness embeds the current champion's solution content "
        "(content only) into that directive.\n\n"
        f"# Task\n{context.task_description}\n\n"
        f"Target artifact: {kind_label(context.solution_kind)}\n\n"
        f"# Verification checks (all must pass per example)\n{checks_text}\n\n"
        f"# Run state\n"
        f"Loops completed: {context.loops_completed} of {context.max_loops}. "
        f"Validation peeks used: {context.peeks_used} of {context.max_peeks}.\n"
        f"Note: {context.dev_subset_note}\n\n"
        f"# Your log — older loops (compacted)\n{compacted}\n\n"
        f"# Your log — recent loops (full detail)\n{recent}\n\n"
        f"# Dev leaderboard (95% Wilson intervals)\n{leaderboard}\n\n"
        f"# Current champion (dev-best) solution\n{champion_text}\n\n"
        f"# Failure patterns on dev (champion candidate, sampled)\n{failures}\n\n"
        f"# Validation checkpoints (aggregate scores only){gap_text}\n{val_text}\n\n"
        "# Your reply\n"
        "Reply with a single JSON object and nothing else.\n"
        "To continue:\n"
        '{"action": "continue", "observations": "<what the results tell you>", '
        '"hypotheses": "<what you believe will improve scores>", '
        '"directives": [{"approach_summary": "<one line>", '
        '"instructions": "<precise instructions for one executor>", '
        '"base_on_champion": true|false}], '
        '"request_validation": true|false}\n'
        f"Issue 1 to {context.executors_per_loop} directives. Make them diverse when "
        "exploring; converge on refinement when a champion is close. Set "
        "request_validation sparingly — peeks are budgeted.\n"
        "To stop (only when further loops are clearly wasted): "
        '{"action": "stop", "reason": "<why>"}'
    )
    return AgentContextPrompt(role=StrategyRole(), text=text)


def reformat_context_prompt(prompt: AgentContextPrompt, detail: str) -> AgentContextPrompt:
    return AgentContextPrompt(
        role=prompt.role,
        text=(
            f"{prompt.text}\n\n"
            f"IMPORTANT: your previous reply was rejected: {detail}. "
            "Reply again, following the required format exactly."
        ),
    )


# --------------------------------------------------------------------------
# Evaluation channel (sanctioned to embed ONE example of any split)
# --------------------------------------------------------------------------


def build_solution_eval_prompt(solution_content: str, example: Example) -> EvalPrompt:
    """Runs a skill/guidance candidate on one example. The reply is the raw
    output to score; it returns to the harness as data only."""
    system = (
        "Follow the document below to transform the user's input into the required "
        "output. Reply with the output only — no preamble, no explanation, no fences.\n\n"
        f"{solution_content}"
    )
    return EvalPrompt(system_prompt=Some(system), user_prompt=example.input_text)


def build_judge_prompt(rubric: str, example: Example, actual_output: str) -> EvalPrompt:
    text = (
        "You are grading one candidate output against a rubric.\n\n"
        f"# Rubric\n{rubric}\n\n"
        f"# Input\n{example.input_text}\n\n"
        f"# Expected output (gold standard)\n{example.expected_output}\n\n"
        f"# Candidate output\n{actual_output}\n\n"
        "Reply with a single JSON object and nothing else: "
        '{"pass": true|false, "reason": "<one sentence>"}'
    )
    return EvalPrompt(system_prompt=Nothing(), user_prompt=text)


def reformat_eval_prompt(prompt: EvalPrompt, detail: str) -> EvalPrompt:
    return EvalPrompt(
        system_prompt=prompt.system_prompt,
        user_prompt=(
            f"{prompt.user_prompt}\n\n"
            f"IMPORTANT: your previous reply was rejected: {detail}. "
            "Reply again, following the required format exactly."
        ),
    )
