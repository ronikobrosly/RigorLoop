"""Pure dataset logic: parsing examples, deduplication, splitting, manifests,
and statistical power warnings. No I/O; text in, typed values out."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter

from rigorloop.core.types import (
    BadRatios,
    DevExample,
    DuplicateWarning,
    Err,
    Example,
    ExampleDigest,
    ExampleParseError,
    InvalidJsonLine,
    MissingField,
    NotAnObject,
    Ok,
    PowerWarning,
    Result,
    Split,
    SplitError,
    SplitManifest,
    SplitRatios,
    TestExample,
    TooFewExamples,
    ValExample,
)

_MIN_EXAMPLES = 5
_PREVIEW_CHARS = 60


def _content_hash(input_text: str, expected_output: str) -> str:
    digest = hashlib.sha256(f"{input_text}\x1f{expected_output}".encode()).hexdigest()
    return digest[:16]


def _example_id(input_text: str) -> str:
    return "ex-" + hashlib.sha256(input_text.encode()).hexdigest()[:12]


def _field_as_text(value: object) -> str:
    """Structured inputs are JSON-encoded; plain strings pass through."""
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _parse_line(line_number: int, line: str) -> Result[Example, ExampleParseError]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        return Err(InvalidJsonLine(line_number, str(exc)))
    if not isinstance(obj, dict):
        return Err(NotAnObject(line_number))
    if "input" not in obj:
        return Err(MissingField(line_number, "input"))
    if "expected_output" not in obj:
        return Err(MissingField(line_number, "expected_output"))
    input_text = _field_as_text(obj["input"])
    expected = _field_as_text(obj["expected_output"])
    return Ok(Example(_example_id(input_text), input_text, expected))


def parse_examples(jsonl_text: str) -> Result[tuple[Example, ...], ExampleParseError]:
    """Parse a JSONL document of {"input": ..., "expected_output": ...} records.

    Blank lines are skipped. The first malformed line aborts the parse."""
    numbered = [
        (i, line) for i, line in enumerate(jsonl_text.splitlines(), start=1) if line.strip()
    ]
    parsed = [_parse_line(i, line) for i, line in numbered]
    failures = [r for r in parsed if isinstance(r, Err)]
    if failures:
        return failures[0]
    return Ok(tuple(r.value for r in parsed if isinstance(r, Ok)))


def dedupe_examples(
    examples: tuple[Example, ...],
) -> tuple[tuple[Example, ...], tuple[DuplicateWarning, ...]]:
    """Collapse examples with identical input text (first occurrence wins).

    A duplicate straddling dev and test would silently corrupt the holdout."""
    counts = Counter(ex.example_id for ex in examples)
    first_by_id = {ex.example_id: ex for ex in reversed(examples)}
    seen_order = tuple(dict.fromkeys(ex.example_id for ex in examples))
    unique = tuple(first_by_id[ex_id] for ex_id in seen_order)
    warnings = tuple(
        DuplicateWarning(first_by_id[ex_id].input_text[:_PREVIEW_CHARS], counts[ex_id])
        for ex_id in seen_order
        if counts[ex_id] > 1
    )
    return unique, warnings


def _split_sizes(n: int, ratios: SplitRatios) -> tuple[int, int, int]:
    """Largest-remainder allocation: disjoint, exhaustive, each split non-empty
    (callers guarantee n >= _MIN_EXAMPLES)."""
    raw = (n * ratios.dev, n * ratios.val, n * ratios.test)
    floors = tuple(max(1, math.floor(x)) for x in raw)
    delta = n - sum(floors)
    by_frac = sorted(range(3), key=lambda i: raw[i] - math.floor(raw[i]), reverse=True)
    bumped = tuple(floors[i] + (1 if delta > 0 and i in by_frac[:delta] else 0) for i in range(3))
    # If the minimum-1 bumps overshot (tiny n), take the excess from the largest split.
    excess = sum(bumped) - n
    largest = max(range(3), key=lambda i: bumped[i])
    sizes = tuple(bumped[i] - (excess if i == largest else 0) for i in range(3))
    return (sizes[0], sizes[1], sizes[2])


def split_examples(
    examples: tuple[Example, ...], ratios: SplitRatios, seed: int
) -> Result[Split, SplitError]:
    """Deterministically partition examples into dev/val/test.

    Guarantees: disjoint, exhaustive, stable for a given seed, each split
    non-empty. Input order does not matter (examples are sorted before the
    seeded shuffle)."""
    if abs(ratios.dev + ratios.val + ratios.test - 1.0) > 1e-9:
        return Err(BadRatios(f"ratios must sum to 1, got {ratios}"))
    if min(ratios.dev, ratios.val, ratios.test) <= 0:
        return Err(BadRatios("every split ratio must be positive"))
    if len(examples) < _MIN_EXAMPLES:
        return Err(TooFewExamples(len(examples), _MIN_EXAMPLES))

    ordered = sorted(examples, key=lambda e: e.example_id)
    # random.Random(seed) is a pure function of the seed parameter: same seed,
    # same shuffle, no ambient randomness touched.
    shuffled = random.Random(seed).sample(ordered, k=len(ordered))
    n_dev, n_val, _ = _split_sizes(len(shuffled), ratios)
    return Ok(
        Split(
            dev=tuple(DevExample(e) for e in shuffled[:n_dev]),
            val=tuple(ValExample(e) for e in shuffled[n_dev : n_dev + n_val]),
            test=tuple(TestExample(e) for e in shuffled[n_dev + n_val :]),
        )
    )


def build_manifest(
    split: Split, ratios: SplitRatios, seed: int, eval_model: str, cli_version: str
) -> SplitManifest:
    """Content-hash manifest pinning the split and the evaluating model, so a
    resume can never reshuffle examples across splits."""
    return SplitManifest(
        seed=seed,
        ratios=ratios,
        dev=tuple(_digest(d.example) for d in split.dev),
        val=tuple(_digest(v.example) for v in split.val),
        test=tuple(_digest(t.example) for t in split.test),
        eval_model=eval_model,
        cli_version=cli_version,
    )


def _digest(example: Example) -> ExampleDigest:
    return ExampleDigest(
        example.example_id, _content_hash(example.input_text, example.expected_output)
    )


def manifest_matches(manifest: SplitManifest, split: Split) -> bool:
    """True iff the split's membership and contents match the manifest exactly."""
    return (
        manifest.dev == tuple(_digest(d.example) for d in split.dev)
        and manifest.val == tuple(_digest(v.example) for v in split.val)
        and manifest.test == tuple(_digest(t.example) for t in split.test)
    )


def power_warnings(split: Split, threshold: float = 0.10) -> tuple[PowerWarning, ...]:
    """Warn when a split is too small to distinguish meaningful pass-rate
    differences. Half-width is the worst-case (p=0.5) 95% margin ~1.96*sqrt(.25/n)."""
    sizes = (("dev", len(split.dev)), ("validation", len(split.val)), ("test", len(split.test)))
    widths = tuple((name, n, 1.96 * math.sqrt(0.25 / n)) for name, n in sizes)
    return tuple(
        PowerWarning(
            split_name=name,
            n=n,
            half_width=hw,
            message=(
                f"{name} set of {n} examples can only distinguish pass-rate "
                f"differences of ~±{round(hw * 100)} points"
            ),
        )
        for name, n, hw in widths
        if hw > threshold
    )
