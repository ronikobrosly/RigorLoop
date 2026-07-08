"""Config parsing: defaults, full round-trips, and every error case."""

from __future__ import annotations

import pytest

from rigorloop.core import config_calcs
from rigorloop.core.types import (
    CustomPython,
    Err,
    ExactMatch,
    GuidanceSolution,
    InvalidValue,
    JsonEquality,
    LlmJudge,
    MissingKey,
    NormalizedMatch,
    Nothing,
    NumericTolerance,
    Ok,
    RegexMatch,
    ScriptSolution,
    SkillSolution,
    Some,
    TomlSyntax,
)

pytestmark = pytest.mark.unit

MINIMAL = """\
[task]
description_file = "task.md"
examples_file = "examples.jsonl"

[[checks]]
type = "exact_match"
"""


def test_minimal_config_gets_documented_defaults() -> None:
    result = config_calcs.parse_config(MINIMAL)
    assert isinstance(result, Ok)
    config = result.value
    assert config.task.solution_kind == ScriptSolution()
    assert (config.split.ratios.dev, config.split.ratios.val, config.split.ratios.test) == (
        0.6,
        0.2,
        0.2,
    )
    assert config.split.seed == 17
    assert config.loop.max_loops == 12
    assert config.loop.executors_per_loop == 4
    assert config.loop.dev_examples_in_prompt == 30
    assert config.loop.max_consecutive_eval_failures == 5
    assert config.loop.strategy_full_detail_loops == 4
    assert config.validation.val_every == 3
    assert config.validation.max_peeks == 10
    assert config.validation.min_loops_between_peeks == 2
    assert config.validation.patience == 2
    assert config.validation.target_pass_rate == Nothing()
    assert config.agents.model == config_calcs.DEFAULT_MODEL
    assert config.agents.timeout_s == 300.0
    assert config.checks == (ExactMatch(),)


def test_full_config_parses_every_check_type() -> None:
    text = """\
[task]
description_file = "task.md"
solution_kind = "skill"
examples_file = "ex.jsonl"

[split]
ratios = [0.5, 0.25, 0.25]
seed = 99

[validation]
target_pass_rate = 0.9

[[checks]]
type = "exact_match"

[[checks]]
type = "normalized_match"
lowercase = false

[[checks]]
type = "json_equality"

[[checks]]
type = "regex_match"
pattern = "^\\\\d+$"

[[checks]]
type = "numeric_tolerance"
atol = 0.5
rtol = 0.01

[[checks]]
type = "custom_python"
script_path = "check.py"

[[checks]]
type = "llm_judge"
rubric = "Judge it."
n_samples = 5
pass_threshold = 0.6
"""
    result = config_calcs.parse_config(text)
    assert isinstance(result, Ok)
    config = result.value
    assert config.task.solution_kind == SkillSolution()
    assert config.split.ratios.dev == 0.5
    assert config.validation.target_pass_rate == Some(0.9)
    assert config.checks == (
        ExactMatch(),
        NormalizedMatch(lowercase=False, strip=True, collapse_whitespace=True),
        JsonEquality(),
        RegexMatch(pattern="^\\d+$"),
        NumericTolerance(atol=0.5, rtol=0.01),
        CustomPython(script_path="check.py"),
        LlmJudge(rubric="Judge it.", n_samples=5, pass_threshold=0.6),
    )


def test_guidance_kind() -> None:
    text = MINIMAL.replace(
        'examples_file = "examples.jsonl"', 'examples_file = "e.jsonl"\nsolution_kind = "guidance"'
    )
    result = config_calcs.parse_config(text)
    assert isinstance(result, Ok)
    assert result.value.task.solution_kind == GuidanceSolution()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("not toml [", TomlSyntax),
        ("[task]\nexamples_file = 'e'\n[[checks]]\ntype = 'exact_match'", MissingKey),
        (MINIMAL.replace('type = "exact_match"', 'type = "bogus"'), InvalidValue),
        (
            MINIMAL.replace(
                'description_file = "task.md"',
                'description_file = "task.md"\nsolution_kind = "notakind"',
            ),
            InvalidValue,
        ),
    ],
)
def test_error_cases(text: str, expected: type) -> None:
    result = config_calcs.parse_config(text)
    assert isinstance(result, Err)
    assert isinstance(result.error, expected)


def test_no_checks_is_an_error() -> None:
    text = '[task]\ndescription_file = "t"\nexamples_file = "e"\n'
    result = config_calcs.parse_config(text)
    assert isinstance(result, Err)
    assert result.error == MissingKey("checks")


def test_regex_requires_pattern() -> None:
    text = MINIMAL.replace('type = "exact_match"', 'type = "regex_match"')
    result = config_calcs.parse_config(text)
    assert isinstance(result, Err)
    assert result.error == MissingKey("checks[0].pattern")


def test_judge_requires_rubric_and_valid_threshold() -> None:
    no_rubric = MINIMAL.replace('type = "exact_match"', 'type = "llm_judge"')
    result = config_calcs.parse_config(no_rubric)
    assert isinstance(result, Err)
    assert result.error == MissingKey("checks[0].rubric")

    bad_threshold = MINIMAL.replace(
        'type = "exact_match"', 'type = "llm_judge"\nrubric = "r"\npass_threshold = 1.5'
    )
    result = config_calcs.parse_config(bad_threshold)
    assert isinstance(result, Err)
    assert isinstance(result.error, InvalidValue)


@pytest.mark.parametrize(
    "snippet",
    [
        "[task]\ndescription_file = 3\nexamples_file = 'e'",
        "[task]\ndescription_file = 't'\nexamples_file = 3",
        "[task]\ndescription_file = 't'\nexamples_file = 'e'\nsolution_kind = 3",
        "[split]\nseed = 'x'",
        "[split]\nratios = [0.5, true, 0.3]",
        "[loop]\nexecutors_per_loop = 'x'",
        "[loop]\ndev_examples_in_prompt = 0",
        "[loop]\nmax_consecutive_eval_failures = 'x'",
        "[loop]\nstrategy_full_detail_loops = false",
        "[validation]\nval_every = 'x'",
        "[validation]\nmax_peeks = 0",
        "[validation]\nmin_loops_between_peeks = 'x'",
        "[validation]\npatience = -1",
        "[validation]\ntarget_pass_rate = 'high'",
        "[agents]\nmodel = 3",
        "[agents]\ntimeout_s = 'x'",
        "[agents]\ntimeout_s = -1",
    ],
)
def test_every_section_rejects_bad_values(snippet: str) -> None:
    task_section = '[task]\ndescription_file = "task.md"\nexamples_file = "examples.jsonl"'
    text = (
        MINIMAL.replace(task_section, "") + "\n" + snippet
        if snippet.startswith("[task]")
        else MINIMAL + "\n" + snippet
    )
    result = config_calcs.parse_config(text)
    assert isinstance(result, Err)
    assert isinstance(result.error, InvalidValue | MissingKey)


@pytest.mark.parametrize(
    "check_table",
    [
        "type = 3",
        'type = "normalized_match"\nlowercase = "yes"',
        'type = "normalized_match"\nstrip = 1',
        'type = "normalized_match"\ncollapse_whitespace = "x"',
        'type = "numeric_tolerance"\natol = "x"',
        'type = "numeric_tolerance"\nrtol = "x"',
        'type = "custom_python"',
        'type = "custom_python"\nscript_path = 3',
        'type = "llm_judge"\nrubric = "r"\nn_samples = "x"',
        'type = "llm_judge"\nrubric = "r"\npass_threshold = "x"',
        'type = "regex_match"\npattern = 3',
    ],
)
def test_every_check_type_rejects_bad_values(check_table: str) -> None:
    text = MINIMAL.replace('type = "exact_match"', check_table)
    result = config_calcs.parse_config(text)
    assert isinstance(result, Err)


def test_checks_must_be_an_array_of_tables() -> None:
    text = 'checks = 3\n[task]\ndescription_file = "t"\nexamples_file = "e"\n'
    result = config_calcs.parse_config(text)
    assert isinstance(result, Err)
    assert isinstance(result.error, InvalidValue)


def test_bad_scalar_types_are_rejected() -> None:
    bad_int = MINIMAL + '\n[loop]\nmax_loops = "many"\n'
    result = config_calcs.parse_config(bad_int)
    assert isinstance(result, Err)
    assert isinstance(result.error, InvalidValue)

    negative = MINIMAL + "\n[loop]\nmax_loops = -3\n"
    result = config_calcs.parse_config(negative)
    assert isinstance(result, Err)
    assert isinstance(result.error, InvalidValue)

    bad_ratios = MINIMAL + "\n[split]\nratios = [1.0, 0.5]\n"
    result = config_calcs.parse_config(bad_ratios)
    assert isinstance(result, Err)
    assert result.error == InvalidValue("split.ratios", "expected three numbers, got [1.0, 0.5]")

    bad_target = MINIMAL + "\n[validation]\ntarget_pass_rate = 0.0\n"
    result = config_calcs.parse_config(bad_target)
    assert isinstance(result, Err)
    assert result.error == InvalidValue("validation.target_pass_rate", "must be in (0, 1]")
