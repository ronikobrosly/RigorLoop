"""Core dataset logic: parsing, dedup, splitting, manifests, power warnings."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rigorloop.core import dataset_calcs
from rigorloop.core.types import (
    BadRatios,
    Err,
    Example,
    InvalidJsonLine,
    MissingField,
    NotAnObject,
    Ok,
    SplitRatios,
    TooFewExamples,
)
from tests.conftest import toy_examples, toy_examples_jsonl

pytestmark = pytest.mark.unit

RATIOS = SplitRatios(0.6, 0.2, 0.2)


class TestParseExamples:
    def test_parses_valid_jsonl(self) -> None:
        result = dataset_calcs.parse_examples(toy_examples_jsonl(8))
        assert isinstance(result, Ok)
        assert len(result.value) == 8
        assert result.value[0].expected_output == "ITEM 00 ALPHA BRAVO"

    def test_skips_blank_lines(self) -> None:
        text = (
            '{"input": "a", "expected_output": "b"}\n\n\n{"input": "c", "expected_output": "d"}\n'
        )
        result = dataset_calcs.parse_examples(text)
        assert isinstance(result, Ok)
        assert len(result.value) == 2

    def test_ids_are_stable_content_hashes(self) -> None:
        first = dataset_calcs.parse_examples('{"input": "a", "expected_output": "b"}')
        second = dataset_calcs.parse_examples('{"input": "a", "expected_output": "b"}')
        assert isinstance(first, Ok) and isinstance(second, Ok)
        assert first.value[0].example_id == second.value[0].example_id

    def test_structured_input_is_json_encoded(self) -> None:
        text = json.dumps({"input": {"k": [1, 2]}, "expected_output": "x"})
        result = dataset_calcs.parse_examples(text)
        assert isinstance(result, Ok)
        assert result.value[0].input_text == '{"k": [1, 2]}'

    def test_invalid_json_line(self) -> None:
        result = dataset_calcs.parse_examples('{"input": "a", "expected_output": "b"}\nnot json')
        assert isinstance(result, Err)
        assert isinstance(result.error, InvalidJsonLine)
        assert result.error.line_number == 2

    def test_non_object_line(self) -> None:
        result = dataset_calcs.parse_examples("[1, 2]")
        assert isinstance(result, Err)
        assert isinstance(result.error, NotAnObject)

    def test_missing_fields(self) -> None:
        no_input = dataset_calcs.parse_examples('{"expected_output": "b"}')
        assert isinstance(no_input, Err)
        assert no_input.error == MissingField(1, "input")
        no_expected = dataset_calcs.parse_examples('{"input": "a"}')
        assert isinstance(no_expected, Err)
        assert no_expected.error == MissingField(1, "expected_output")


class TestDedupe:
    def test_no_duplicates_is_identity(self) -> None:
        examples = toy_examples(6)
        unique, warnings = dataset_calcs.dedupe_examples(examples)
        assert unique == examples
        assert warnings == ()

    def test_collapses_identical_inputs_keeping_first(self) -> None:
        examples = toy_examples(4)
        duplicated = (*examples, examples[1], examples[1])
        unique, warnings = dataset_calcs.dedupe_examples(duplicated)
        assert unique == examples
        assert len(warnings) == 1
        assert warnings[0].occurrences == 3
        assert warnings[0].input_preview.startswith("item 01")


class TestSplit:
    def test_default_sizes(self) -> None:
        result = dataset_calcs.split_examples(toy_examples(30), RATIOS, seed=17)
        assert isinstance(result, Ok)
        split = result.value
        assert (len(split.dev), len(split.val), len(split.test)) == (18, 6, 6)

    def test_disjoint_and_exhaustive(self) -> None:
        examples = toy_examples(23)
        result = dataset_calcs.split_examples(examples, RATIOS, seed=3)
        assert isinstance(result, Ok)
        split = result.value
        ids = (
            [d.example.example_id for d in split.dev]
            + [v.example.example_id for v in split.val]
            + [t.example.example_id for t in split.test]
        )
        assert len(ids) == len(examples)
        assert set(ids) == {e.example_id for e in examples}

    def test_deterministic_for_seed_and_input_order(self) -> None:
        examples = toy_examples(20)
        first = dataset_calcs.split_examples(examples, RATIOS, seed=5)
        second = dataset_calcs.split_examples(tuple(reversed(examples)), RATIOS, seed=5)
        assert isinstance(first, Ok) and isinstance(second, Ok)
        assert first.value == second.value

    def test_different_seed_shuffles(self) -> None:
        examples = toy_examples(20)
        first = dataset_calcs.split_examples(examples, RATIOS, seed=1)
        second = dataset_calcs.split_examples(examples, RATIOS, seed=2)
        assert isinstance(first, Ok) and isinstance(second, Ok)
        assert first.value != second.value

    def test_too_few_examples(self) -> None:
        result = dataset_calcs.split_examples(toy_examples(4), RATIOS, seed=1)
        assert isinstance(result, Err)
        assert result.error == TooFewExamples(4, 5)

    def test_bad_ratios(self) -> None:
        not_summing = dataset_calcs.split_examples(
            toy_examples(10), SplitRatios(0.5, 0.2, 0.2), seed=1
        )
        assert isinstance(not_summing, Err)
        assert isinstance(not_summing.error, BadRatios)
        zero = dataset_calcs.split_examples(toy_examples(10), SplitRatios(1.0, 0.0, 0.0), seed=1)
        assert isinstance(zero, Err)
        assert isinstance(zero.error, BadRatios)

    @given(
        n=st.integers(min_value=5, max_value=200),
        seed=st.integers(min_value=0, max_value=2**32),
    )
    def test_property_disjoint_exhaustive_nonempty(self, n: int, seed: int) -> None:
        examples = tuple(Example(f"ex-{i:04d}", f"input {i}", f"out {i}") for i in range(n))
        result = dataset_calcs.split_examples(examples, RATIOS, seed)
        assert isinstance(result, Ok)
        split = result.value
        all_ids = (
            [d.example.example_id for d in split.dev]
            + [v.example.example_id for v in split.val]
            + [t.example.example_id for t in split.test]
        )
        assert len(all_ids) == n == len(set(all_ids))
        assert min(len(split.dev), len(split.val), len(split.test)) >= 1


class TestManifest:
    def test_manifest_matches_own_split(self) -> None:
        result = dataset_calcs.split_examples(toy_examples(12), RATIOS, seed=17)
        assert isinstance(result, Ok)
        manifest = dataset_calcs.build_manifest(result.value, RATIOS, 17, "model-x", "cli-1")
        assert dataset_calcs.manifest_matches(manifest, result.value)
        assert manifest.eval_model == "model-x"
        assert manifest.cli_version == "cli-1"

    def test_manifest_detects_drift(self) -> None:
        first = dataset_calcs.split_examples(toy_examples(12), RATIOS, seed=17)
        second = dataset_calcs.split_examples(toy_examples(13), RATIOS, seed=17)
        assert isinstance(first, Ok) and isinstance(second, Ok)
        manifest = dataset_calcs.build_manifest(first.value, RATIOS, 17, "m", "c")
        assert not dataset_calcs.manifest_matches(manifest, second.value)


class TestPowerWarnings:
    def test_small_splits_warn(self) -> None:
        result = dataset_calcs.split_examples(toy_examples(15), RATIOS, seed=17)
        assert isinstance(result, Ok)
        warnings = dataset_calcs.power_warnings(result.value)
        names = {w.split_name for w in warnings}
        assert {"validation", "test"} <= names
        val_warning = next(w for w in warnings if w.split_name == "validation")
        assert "can only distinguish" in val_warning.message
        assert val_warning.half_width > 0.10

    def test_large_splits_do_not_warn(self) -> None:
        examples = tuple(Example(f"ex-{i:04d}", f"in {i}", f"out {i}") for i in range(600))
        result = dataset_calcs.split_examples(examples, RATIOS, seed=17)
        assert isinstance(result, Ok)
        assert dataset_calcs.power_warnings(result.value) == ()
