"""Pure parsing of rigorloop.toml text into a typed RunConfig.

"Parse, don't validate": the shell reads the raw file; past this boundary the
core never re-checks configuration validity."""

from __future__ import annotations

import tomllib

from rigorloop.core.types import (
    NOTHING,
    AgentConfig,
    Check,
    ConfigParseError,
    CustomPython,
    Err,
    ExactMatch,
    GuidanceSolution,
    InvalidValue,
    JsonEquality,
    LlmJudge,
    LoopConfig,
    MissingKey,
    NormalizedMatch,
    Nothing,
    NumericTolerance,
    Ok,
    Option,
    RegexMatch,
    Result,
    RunConfig,
    ScriptSolution,
    SkillSolution,
    SolutionKind,
    Some,
    SplitConfig,
    SplitRatios,
    TaskConfig,
    TomlSyntax,
    ValidationConfig,
)

DEFAULT_MODEL = "claude-sonnet-5"


def _get_str(
    table: dict[str, object], key: str, qualified: str, default: Option[str]
) -> Result[str, ConfigParseError]:
    match table.get(key), default:
        case None, Nothing():
            return Err(MissingKey(qualified))
        case None, Some(value):
            return Ok(value)
        case str(value), _:
            return Ok(value)
        case other, _:
            return Err(InvalidValue(qualified, f"expected a string, got {other!r}"))


def _get_int(
    table: dict[str, object], key: str, qualified: str, default: int
) -> Result[int, ConfigParseError]:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return Err(InvalidValue(qualified, f"expected an integer, got {value!r}"))
    if value <= 0:
        return Err(InvalidValue(qualified, f"must be positive, got {value}"))
    return Ok(value)


def _get_float(
    table: dict[str, object], key: str, qualified: str, default: float
) -> Result[float, ConfigParseError]:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return Err(InvalidValue(qualified, f"expected a number, got {value!r}"))
    return Ok(float(value))


def _get_bool(
    table: dict[str, object], key: str, qualified: str, default: bool
) -> Result[bool, ConfigParseError]:
    value = table.get(key, default)
    if not isinstance(value, bool):
        return Err(InvalidValue(qualified, f"expected a boolean, got {value!r}"))
    return Ok(value)


def _table(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _parse_kind(name: str) -> Result[SolutionKind, ConfigParseError]:
    match name:
        case "script":
            return Ok(ScriptSolution())
        case "skill":
            return Ok(SkillSolution())
        case "guidance":
            return Ok(GuidanceSolution())
        case other:
            return Err(
                InvalidValue("task.solution_kind", f"expected script|skill|guidance, got {other!r}")
            )


def _parse_task(data: dict[str, object]) -> Result[TaskConfig, ConfigParseError]:
    table = _table(data, "task")
    description = _get_str(table, "description_file", "task.description_file", NOTHING)
    if isinstance(description, Err):
        return description
    examples = _get_str(table, "examples_file", "task.examples_file", NOTHING)
    if isinstance(examples, Err):
        return examples
    kind_name = _get_str(table, "solution_kind", "task.solution_kind", Some("script"))
    if isinstance(kind_name, Err):
        return kind_name
    kind = _parse_kind(kind_name.value)
    if isinstance(kind, Err):
        return kind
    return Ok(TaskConfig(description.value, kind.value, examples.value))


def _parse_split(data: dict[str, object]) -> Result[SplitConfig, ConfigParseError]:
    table = _table(data, "split")
    seed = _get_int(table, "seed", "split.seed", 17)
    if isinstance(seed, Err):
        return seed
    raw = table.get("ratios", [0.6, 0.2, 0.2])
    if (
        not isinstance(raw, list)
        or len(raw) != 3
        or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in raw)
    ):
        return Err(InvalidValue("split.ratios", f"expected three numbers, got {raw!r}"))
    ratios = SplitRatios(float(raw[0]), float(raw[1]), float(raw[2]))
    return Ok(SplitConfig(ratios, seed.value))


def _parse_loop(data: dict[str, object]) -> Result[LoopConfig, ConfigParseError]:
    table = _table(data, "loop")
    max_loops = _get_int(table, "max_loops", "loop.max_loops", 12)
    if isinstance(max_loops, Err):
        return max_loops
    executors = _get_int(table, "executors_per_loop", "loop.executors_per_loop", 4)
    if isinstance(executors, Err):
        return executors
    in_prompt = _get_int(table, "dev_examples_in_prompt", "loop.dev_examples_in_prompt", 30)
    if isinstance(in_prompt, Err):
        return in_prompt
    max_fail = _get_int(
        table, "max_consecutive_eval_failures", "loop.max_consecutive_eval_failures", 5
    )
    if isinstance(max_fail, Err):
        return max_fail
    detail = _get_int(table, "strategy_full_detail_loops", "loop.strategy_full_detail_loops", 4)
    if isinstance(detail, Err):
        return detail
    return Ok(
        LoopConfig(max_loops.value, executors.value, in_prompt.value, max_fail.value, detail.value)
    )


def _parse_validation(data: dict[str, object]) -> Result[ValidationConfig, ConfigParseError]:
    table = _table(data, "validation")
    val_every = _get_int(table, "val_every", "validation.val_every", 3)
    if isinstance(val_every, Err):
        return val_every
    max_peeks = _get_int(table, "max_peeks", "validation.max_peeks", 10)
    if isinstance(max_peeks, Err):
        return max_peeks
    min_gap = _get_int(table, "min_loops_between_peeks", "validation.min_loops_between_peeks", 2)
    if isinstance(min_gap, Err):
        return min_gap
    patience = _get_int(table, "patience", "validation.patience", 2)
    if isinstance(patience, Err):
        return patience
    cohort_size = _get_int(table, "cohort_size", "validation.cohort_size", 2)
    if isinstance(cohort_size, Err):
        return cohort_size
    target: Option[float] = NOTHING
    if "target_pass_rate" in table:
        parsed = _get_float(table, "target_pass_rate", "validation.target_pass_rate", 0.0)
        if isinstance(parsed, Err):
            return parsed
        if not 0.0 < parsed.value <= 1.0:
            return Err(InvalidValue("validation.target_pass_rate", "must be in (0, 1]"))
        target = Some(parsed.value)
    return Ok(
        ValidationConfig(
            val_every.value,
            max_peeks.value,
            min_gap.value,
            patience.value,
            target,
            cohort_size.value,
        )
    )


def _parse_agents(data: dict[str, object]) -> Result[AgentConfig, ConfigParseError]:
    table = _table(data, "agents")
    model = _get_str(table, "model", "agents.model", Some(DEFAULT_MODEL))
    if isinstance(model, Err):
        return model
    timeout = _get_float(table, "timeout_s", "agents.timeout_s", 300.0)
    if isinstance(timeout, Err):
        return timeout
    if timeout.value <= 0:
        return Err(InvalidValue("agents.timeout_s", "must be positive"))
    return Ok(AgentConfig(model.value, timeout.value))


def _parse_check(index: int, table: dict[str, object]) -> Result[Check, ConfigParseError]:
    where = f"checks[{index}]"
    kind = _get_str(table, "type", f"{where}.type", NOTHING)
    if isinstance(kind, Err):
        return kind
    match kind.value:
        case "exact_match":
            return Ok(ExactMatch())
        case "normalized_match":
            lowercase = _get_bool(table, "lowercase", f"{where}.lowercase", True)
            strip = _get_bool(table, "strip", f"{where}.strip", True)
            collapse = _get_bool(table, "collapse_whitespace", f"{where}.collapse_whitespace", True)
            if isinstance(lowercase, Err):
                return lowercase
            if isinstance(strip, Err):
                return strip
            if isinstance(collapse, Err):
                return collapse
            return Ok(NormalizedMatch(lowercase.value, strip.value, collapse.value))
        case "json_equality":
            return Ok(JsonEquality())
        case "regex_match":
            pattern = _get_str(table, "pattern", f"{where}.pattern", NOTHING)
            if isinstance(pattern, Err):
                return pattern
            return Ok(RegexMatch(pattern.value))
        case "numeric_tolerance":
            atol = _get_float(table, "atol", f"{where}.atol", 1e-6)
            if isinstance(atol, Err):
                return atol
            rtol = _get_float(table, "rtol", f"{where}.rtol", 1e-6)
            if isinstance(rtol, Err):
                return rtol
            return Ok(NumericTolerance(atol.value, rtol.value))
        case "custom_python":
            path = _get_str(table, "script_path", f"{where}.script_path", NOTHING)
            if isinstance(path, Err):
                return path
            return Ok(CustomPython(path.value))
        case "llm_judge":
            rubric = _get_str(table, "rubric", f"{where}.rubric", NOTHING)
            if isinstance(rubric, Err):
                return rubric
            n_samples = _get_int(table, "n_samples", f"{where}.n_samples", 3)
            if isinstance(n_samples, Err):
                return n_samples
            threshold = _get_float(table, "pass_threshold", f"{where}.pass_threshold", 0.5)
            if isinstance(threshold, Err):
                return threshold
            if not 0.0 < threshold.value <= 1.0:
                return Err(InvalidValue(f"{where}.pass_threshold", "must be in (0, 1]"))
            return Ok(LlmJudge(rubric.value, n_samples.value, threshold.value))
        case other:
            return Err(InvalidValue(f"{where}.type", f"unknown check type {other!r}"))


def _parse_checks(data: dict[str, object]) -> Result[tuple[Check, ...], ConfigParseError]:
    raw = data.get("checks")
    if raw is None:
        return Err(MissingKey("checks"))
    if not isinstance(raw, list) or not all(isinstance(t, dict) for t in raw) or not raw:
        return Err(InvalidValue("checks", "expected a non-empty array of [[checks]] tables"))
    parsed = [_parse_check(i, t) for i, t in enumerate(raw)]
    failures = [p for p in parsed if isinstance(p, Err)]
    if failures:
        return failures[0]
    return Ok(tuple(p.value for p in parsed if isinstance(p, Ok)))


def parse_config(toml_text: str) -> Result[RunConfig, ConfigParseError]:
    # tomllib.loads is a pure parse; the exception is converted to a Result at
    # this boundary and never propagates inward.
    try:
        data: dict[str, object] = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        return Err(TomlSyntax(str(exc)))

    task = _parse_task(data)
    if isinstance(task, Err):
        return task
    split = _parse_split(data)
    if isinstance(split, Err):
        return split
    loop = _parse_loop(data)
    if isinstance(loop, Err):
        return loop
    validation = _parse_validation(data)
    if isinstance(validation, Err):
        return validation
    agents = _parse_agents(data)
    if isinstance(agents, Err):
        return agents
    checks = _parse_checks(data)
    if isinstance(checks, Err):
        return checks
    return Ok(
        RunConfig(task.value, split.value, loop.value, validation.value, agents.value, checks.value)
    )
