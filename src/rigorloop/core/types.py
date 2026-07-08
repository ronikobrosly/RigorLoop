"""Domain model: algebraic data types shared by the core and the shell.

Products are frozen dataclasses; sums are `type` unions matched exhaustively.
The dev/val/test split is encoded in the type system (`DevExample`,
`ValExample`, `TestExample`) so that leakage into agent-context prompts is a
type error, not a runtime bug.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# Foundations
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Some[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Nothing:
    pass


type Option[T] = Some[T] | Nothing

NOTHING = Nothing()


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E


type Result[T, E] = Ok[T] | Err[E]

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


# --------------------------------------------------------------------------
# Examples and splits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Example:
    example_id: str
    input_text: str
    expected_output: str


@dataclass(frozen=True, slots=True)
class DevExample:
    """The only example type agent-context prompt builders accept."""

    example: Example


@dataclass(frozen=True, slots=True)
class ValExample:
    """Never enters any agent-context prompt."""

    example: Example


@dataclass(frozen=True, slots=True)
class TestExample:
    """Never enters any agent-context prompt; read once, at finalization."""

    example: Example


@dataclass(frozen=True, slots=True)
class SplitRatios:
    dev: float
    val: float
    test: float


@dataclass(frozen=True, slots=True)
class Split:
    dev: tuple[DevExample, ...]
    val: tuple[ValExample, ...]
    test: tuple[TestExample, ...]


@dataclass(frozen=True, slots=True)
class ExampleDigest:
    example_id: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class SplitManifest:
    seed: int
    ratios: SplitRatios
    dev: tuple[ExampleDigest, ...]
    val: tuple[ExampleDigest, ...]
    test: tuple[ExampleDigest, ...]
    eval_model: str
    cli_version: str


@dataclass(frozen=True, slots=True)
class DuplicateWarning:
    input_preview: str
    occurrences: int


@dataclass(frozen=True, slots=True)
class PowerWarning:
    split_name: str
    n: int
    half_width: float
    message: str


# --------------------------------------------------------------------------
# Solutions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScriptSolution:
    pass


@dataclass(frozen=True, slots=True)
class SkillSolution:
    pass


@dataclass(frozen=True, slots=True)
class GuidanceSolution:
    pass


type SolutionKind = ScriptSolution | SkillSolution | GuidanceSolution


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    loop_index: int
    kind: SolutionKind
    content: str
    directive_id: str


# --------------------------------------------------------------------------
# Checks and outcomes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExactMatch:
    pass


@dataclass(frozen=True, slots=True)
class NormalizedMatch:
    lowercase: bool
    strip: bool
    collapse_whitespace: bool


@dataclass(frozen=True, slots=True)
class JsonEquality:
    pass


@dataclass(frozen=True, slots=True)
class RegexMatch:
    pattern: str


@dataclass(frozen=True, slots=True)
class NumericTolerance:
    atol: float
    rtol: float


@dataclass(frozen=True, slots=True)
class CustomPython:
    script_path: str


@dataclass(frozen=True, slots=True)
class LlmJudge:
    rubric: str
    n_samples: int
    pass_threshold: float


type DeterministicCheck = (
    ExactMatch | NormalizedMatch | JsonEquality | RegexMatch | NumericTolerance
)
type Check = DeterministicCheck | CustomPython | LlmJudge


@dataclass(frozen=True, slots=True)
class Passed:
    pass


@dataclass(frozen=True, slots=True)
class Failed:
    reason: str


@dataclass(frozen=True, slots=True)
class Errored:
    detail: str


type CheckOutcome = Passed | Failed | Errored


@dataclass(frozen=True, slots=True)
class NamedOutcome:
    check_name: str
    outcome: CheckOutcome


@dataclass(frozen=True, slots=True)
class ExecutionOk:
    output_text: str


@dataclass(frozen=True, slots=True)
class ExecutionFailed:
    detail: str


type ExecutionResult = ExecutionOk | ExecutionFailed


@dataclass(frozen=True, slots=True)
class ExampleResult:
    example_id: str
    execution: ExecutionResult
    outcomes: tuple[NamedOutcome, ...]


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    passed: bool
    reason: str


# --------------------------------------------------------------------------
# Scores
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckPassRate:
    check_name: str
    passes: int
    n: int


@dataclass(frozen=True, slots=True)
class CandidateScore:
    n: int
    passes: int
    pass_rate: float
    ci_low: float
    ci_high: float
    per_check: tuple[CheckPassRate, ...]
    pass_vector: tuple[bool, ...]  # aligned to example_id sort order of the set
    eval_aborted: bool


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    candidate_id: str
    loop_index: int
    kind: SolutionKind
    content: str
    score: CandidateScore


# --------------------------------------------------------------------------
# Strategy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChampionArtifact:
    """Solution content ONLY — never scores, mistakes, or per-example failures."""

    candidate_id: str
    kind: SolutionKind
    content: str


@dataclass(frozen=True, slots=True)
class DirectiveSpec:
    """What the strategy agent asked for; the harness attaches the artifact."""

    approach_summary: str
    instructions: str
    base_on_champion: bool


@dataclass(frozen=True, slots=True)
class Directive:
    directive_id: str
    approach_summary: str
    instructions: str
    base: Option[ChampionArtifact]


@dataclass(frozen=True, slots=True)
class ContinueDecision:
    observations: str
    hypotheses: str
    directive_specs: tuple[DirectiveSpec, ...]
    request_validation: bool


@dataclass(frozen=True, slots=True)
class StopRequested:
    reason: str


type StrategyDecision = ContinueDecision | StopRequested


@dataclass(frozen=True, slots=True)
class BudgetExhausted:
    max_loops: int


@dataclass(frozen=True, slots=True)
class ValidationPlateau:
    checkpoints_without_improvement: int


@dataclass(frozen=True, slots=True)
class TargetReached:
    pass_rate: float


@dataclass(frozen=True, slots=True)
class StrategyRequestedStop:
    reason: str


@dataclass(frozen=True, slots=True)
class StrategyUnresponsive:
    consecutive_fallbacks: int


type StopReason = (
    BudgetExhausted
    | ValidationPlateau
    | TargetReached
    | StrategyRequestedStop
    | StrategyUnresponsive
)


@dataclass(frozen=True, slots=True)
class StrategyLogEntry:
    loop_index: int
    observations: str
    hypotheses: str
    directives: tuple[Directive, ...]
    dev_summary: str
    val_summary: Option[str]
    fallback: bool


@dataclass(frozen=True, slots=True)
class ValidatedCandidate:
    candidate_id: str
    kind: SolutionKind
    content: str
    dev_score: CandidateScore
    val_score: CandidateScore


@dataclass(frozen=True, slots=True)
class ValCheckpoint:
    loop_index: int
    candidate_id: str
    dev_pass_rate: float
    val_pass_rate: float
    displaced_champion: bool


@dataclass(frozen=True, slots=True)
class RunState:
    """Everything the loop needs between iterations; persisted for resume."""

    loops_completed: int
    leaderboard: tuple[LeaderboardEntry, ...]
    strategy_log: tuple[StrategyLogEntry, ...]
    val_champion: Option[ValidatedCandidate]
    checkpoints: tuple[ValCheckpoint, ...]
    peeks_used: int
    last_peek_loop: Option[int]
    consecutive_fallbacks: int


@dataclass(frozen=True, slots=True)
class FailureSample:
    """A failing dev example shown to the strategy agent (dev-only by type)."""

    dev_example: DevExample
    actual_output: str
    failed_checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything the strategy agent is allowed to see, assembled as data.

    Contains Dev-typed examples and aggregate scores only; the val/test
    channel appears solely as pre-aggregated score lines."""

    task_description: str
    solution_kind: SolutionKind
    loops_completed: int
    max_loops: int
    executors_per_loop: int
    check_names: tuple[str, ...]
    recent_log: tuple[StrategyLogEntry, ...]
    compacted_log: tuple[str, ...]
    leaderboard_lines: tuple[str, ...]
    failure_samples: tuple[FailureSample, ...]
    champion: Option[ChampionArtifact]
    champion_dev_line: Option[str]
    val_lines: tuple[str, ...]
    dev_val_gap_warning: Option[str]
    peeks_used: int
    max_peeks: int
    dev_subset_note: str


# --------------------------------------------------------------------------
# Prompt channels — two distinct types; leakage guards apply to the first
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyRole:
    pass


@dataclass(frozen=True, slots=True)
class ExecutorRole:
    pass


type AgentRole = StrategyRole | ExecutorRole


@dataclass(frozen=True, slots=True)
class AgentContextPrompt:
    """Prompt for the strategy or executor agents. Builders accept Dev-typed
    examples and aggregate scores only; val/test content is a type error."""

    role: AgentRole
    text: str


@dataclass(frozen=True, slots=True)
class EvalPrompt:
    """Prompt that runs a solution-under-test or an LLM judge on ONE example
    (any split). Its output returns to the harness as data, never into an
    AgentContextPrompt."""

    system_prompt: Option[str]
    user_prompt: str


# --------------------------------------------------------------------------
# Effect descriptions the shell executes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentTextRequest:
    user_prompt: str
    system_prompt: Option[str]
    model: str
    timeout_s: float


@dataclass(frozen=True, slots=True)
class RunScriptRequest:
    script_path: str
    stdin_text: str
    timeout_s: float


@dataclass(frozen=True, slots=True)
class BudgetEstimate:
    """Kind-aware pre-run estimate of `claude` calls (upper bound)."""

    strategy_calls: int
    executor_calls: int
    solution_eval_calls: int
    judge_calls: int
    total_calls: int


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvalidJsonLine:
    line_number: int
    detail: str


@dataclass(frozen=True, slots=True)
class NotAnObject:
    line_number: int


@dataclass(frozen=True, slots=True)
class MissingField:
    line_number: int
    field: str


type ExampleParseError = InvalidJsonLine | NotAnObject | MissingField


@dataclass(frozen=True, slots=True)
class TooFewExamples:
    n_total: int
    minimum: int


@dataclass(frozen=True, slots=True)
class BadRatios:
    detail: str


type SplitError = TooFewExamples | BadRatios


@dataclass(frozen=True, slots=True)
class TomlSyntax:
    detail: str


@dataclass(frozen=True, slots=True)
class MissingKey:
    key: str


@dataclass(frozen=True, slots=True)
class InvalidValue:
    key: str
    detail: str


type ConfigParseError = TomlSyntax | MissingKey | InvalidValue


@dataclass(frozen=True, slots=True)
class MalformedReply:
    detail: str


@dataclass(frozen=True, slots=True)
class CallTimeout:
    timeout_s: float


@dataclass(frozen=True, slots=True)
class CallFailed:
    detail: str


@dataclass(frozen=True, slots=True)
class EnvelopeError:
    detail: str


type AgentCallError = CallTimeout | CallFailed | EnvelopeError


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskConfig:
    description_file: str
    solution_kind: SolutionKind
    examples_file: str


@dataclass(frozen=True, slots=True)
class SplitConfig:
    ratios: SplitRatios
    seed: int


@dataclass(frozen=True, slots=True)
class LoopConfig:
    max_loops: int
    executors_per_loop: int
    dev_examples_in_prompt: int
    max_consecutive_eval_failures: int
    strategy_full_detail_loops: int


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    val_every: int
    max_peeks: int
    min_loops_between_peeks: int
    patience: int
    target_pass_rate: Option[float]


@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str
    timeout_s: float


@dataclass(frozen=True, slots=True)
class RunConfig:
    task: TaskConfig
    split: SplitConfig
    loop: LoopConfig
    validation: ValidationConfig
    agents: AgentConfig
    checks: tuple[Check, ...]
