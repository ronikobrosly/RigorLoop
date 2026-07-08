# RigorLoop Implementation Plan

This plan describes how to build RigorLoop: a statistically-sound agentic build
framework that iteratively produces a coding solution (script, agent skill, or
guidance markdown) from user-provided gold-standard input/output examples, using
a strict dev / validation / final-test split to avoid overfitting.

It is written against the requirements in `CLAUDE.md` and the architectural
constraints in `CODING_STYLE.md` (functional core / imperative shell).

---

## 1. Goals and non-goals

**Goals**

- Accept a user-provided task description, a set of gold-standard
  `(input, expected_output)` examples, and a set of verification checks.
- Split examples into disjoint **dev**, **validation**, and **test** sets, and
  enforce that split throughout the run with type-level and process-level
  leakage guards.
- Run an iterative loop: a **strategy agent** (with persistent private memory)
  reviews dev-set results and directs a pool of **executor agents** (stateless,
  concurrent, seeing only the current directive) that each produce a candidate
  solution.
- Score candidates against the dev set with the user's checks; periodically
  evaluate the current best candidate on the validation set; evaluate the final
  chosen solution **exactly once** on the test set.
- Emit a portable final artifact: the winning solution plus a statistical
  report (scores with confidence intervals on all three sets).

**Non-goals**

- Building deterministic scripts to pass simple unit tests (explicitly out of
  scope per `CLAUDE.md`).
- Training or fine-tuning models. All "learning" happens in the strategy
  agent's log and directives.
- Giving agents tools. Every agent call is `claude -p --tools ""` — headless,
  no tools; the RigorLoop shell performs all execution and I/O itself.

## 2. Key decisions (assumptions to confirm)

| Decision | Choice | Rationale |
|---|---|---|
| Language | Python ≥ 3.12 | `CODING_STYLE.md` examples are Python; `.gitignore` is Python. 3.12 gives `match` statements, `typing.assert_never`, and stdlib `tomllib`. |
| Dependencies | Stdlib-only core; minimal shell deps | Purity rules make heavy frameworks unhelpful. No FP library — a small handwritten `Result`/`Option` in `core/types.py`. |
| Example format | JSONL: one `{"input": ..., "expected_output": ...}` per line | Simple, streamable, natural for text-in/structure-out tasks. |
| Config format | `rigorloop.toml` | Stdlib `tomllib`; human-editable. |
| Agent invocation | `claude -p --tools "" --output-format json --model <m>` via subprocess | Mandated by `CLAUDE.md`; `--output-format json` gives parseable envelopes (verified against the installed CLI). |
| Concurrency | `concurrent.futures.ThreadPoolExecutor` around subprocess calls | Executor agents are subprocess-bound; threads are sufficient and simple. |
| Packaging | `pyproject.toml`, `src/` layout, console script `rigorloop` | Standard modern Python packaging. |

## 3. Architecture: functional core / imperative shell

Everything that *decides* is a pure function; everything that *acts* is a thin
shell that executes the decisions. The core never touches I/O, time,
randomness, the network, or subprocesses — those arrive as plain-data inputs.

```
src/rigorloop/
├── core/                     # 100% pure; testable with zero mocks
│   ├── types.py              # Result/Option, domain ADTs, effect descriptions
│   ├── dataset_calcs.py      # parsing examples, splitting, split manifests
│   ├── scoring_calcs.py      # check evaluation, aggregation, statistics
│   ├── strategy_calcs.py     # context assembly, decision & response parsing,
│   │                         # cadence rules, stopping rules, leaderboard
│   └── prompt_calcs.py       # pure prompt-string builders for both agent roles
└── shell/
    ├── agent_calls.py        # claude subprocess wrapper, concurrency, retries
    ├── io_actions.py         # run directory, file read/write, sandboxed
    │                         # execution of candidate scripts & custom checks
    └── cli.py                # argparse entry point; the orchestration loop
```

Notes:

- `prompt_calcs.py` and `types.py` extend the layout sketched in
  `CODING_STYLE.md`; the three prescribed core modules and two shell modules
  are kept as named (snake_case per Python convention: `io_actions`).
- The orchestration loop lives in the shell (`cli.py`), but it is a *dumb
  driver*: at each step it hands current state to a core function and receives
  back a value describing what to do next (an effect description), then
  performs it. The core sequences nothing; it returns plans.
- Dependencies are injected: the RNG seed, the clock, the run-id generator,
  and the "run an agent" function are parameters, never imported globals.
  Integration tests swap in a fake agent function.

## 4. Domain model (core/types.py)

All data is frozen dataclasses (products) and tagged unions typed as
`Union[...]` matched exhaustively with `match`/`assert_never` (sums). No
`None`-as-maybe: `Option[T]`. No exceptions for expected failure: `Result[T, E]`
with meaningful error sum types.

Key types (illustrative, not exhaustive):

```python
# Foundations
Option[T]  = Some(value) | Nothing
Result[T, E] = Ok(value) | Err(error)

# Examples — the split is encoded in the TYPE, not a field.
Example        = (example_id: str, input_text: str, expected_output: str)
DevExample     = wraps Example      # only type prompt builders accept
ValExample     = wraps Example      # never enters any prompt
TestExample    = wraps Example      # never enters any prompt
SplitManifest  = (seed, ratios, per-split example-id hashes)

# What we are building
SolutionKind   = ScriptSolution | SkillSolution | GuidanceSolution
Candidate      = (candidate_id, loop_index, kind, content, directive_id)

# Verification checks (user-configured)
Check          = ExactMatch | NormalizedMatch(rules) | JsonEquality
               | RegexMatch(pattern) | NumericTolerance(atol, rtol)
               | CustomPython(script_path)          # run by the shell
               | LlmJudge(rubric, n_samples, pass_threshold)
CheckOutcome   = Passed | Failed(reason) | Errored(detail)

# Scores
ExampleResult  = (example_id, raw_output, outcomes per check)
CandidateScore = (pass_rate, ci_low, ci_high, per_check_breakdown, n)

# Strategy
StrategyLogEntry  = (loop_index, observations, hypotheses, directives_issued,
                     dev_summary, Option[val_summary])
Directive         = (directive_id, approach_summary, instructions)
StrategyDecision  = Continue(directives) | RequestValidation(candidate_id)
                  | Stop(StopReason)
StopReason        = BudgetExhausted | ValidationPlateau | TargetReached
                  | StrategyRequestedStop(reason)

# Effects — descriptions the core returns and the shell executes
AgentRequest   = (role, prompt, model, timeout_s)
RunScript      = (script_path, stdin_text, timeout_s)
EffectPlan     = list of the above + persistence descriptions
```

**"Parse, don't validate":** the shell reads raw JSONL/TOML and immediately
calls core parsers that return `Result[TypedThing, ParseError]`. Past that
boundary the core never re-checks validity.

## 5. The run protocol

### Phase A — Intake and split (once per run)

1. Shell reads `rigorloop.toml` + examples JSONL; core parses both into typed
   config and `list[Example]` (`Result`-returning).
2. `dataset_calcs.split(examples, ratios, seed)` deterministically partitions
   into dev/val/test (default **60/20/20**, configurable). Pure: the seed is a
   parameter. Guarantees: disjoint, exhaustive, stable for a given seed.
3. Core emits a `SplitManifest` with content hashes; shell persists it. On
   resume, the manifest is re-verified so a re-run can never reshuffle examples
   across splits (a silent-leakage guard).
4. Core computes power warnings (see §8) — e.g. "validation set of 12 examples
   can only distinguish pass-rate differences of ~±25 points" — and the shell
   surfaces them before spending tokens.

### Phase B — The loop (repeated up to `max_loops`)

Each iteration:

1. **Strategy turn.** Core assembles the strategy context — its own full log,
   the dev leaderboard with confidence intervals, aggregated dev failure
   patterns for recent candidates, and (if a validation was run) the aggregate
   validation score. `prompt_calcs.build_strategy_prompt` renders it. Shell
   runs the agent; core parses the JSON reply into a `StrategyDecision`
   (`Result`; one reformat-retry on parse failure).
2. **Executor fan-out.** For `Continue(directives)`, core builds one executor
   prompt per directive: task description, the directive, the output contract,
   the check descriptions, and a sampled subset of dev examples (default
   `min(30, all)`, sample chosen by core from an injected seed). Shell runs
   the K agents concurrently (default 4).
3. **Materialize & execute.** Core parses each reply's single fenced
   `solution` block into a `Candidate` (malformed → one retry, then recorded
   as a failed candidate, never crashing the loop). Shell materializes and
   evaluates it against every dev example, per solution kind (§7), collecting
   raw outputs as plain data.
4. **Score.** `scoring_calcs` evaluates deterministic checks purely; for
   `LlmJudge` checks the shell first collects judge verdicts
   (n-sample majority vote via `claude -p --tools ""`), then hands the verdict
   data to the core for aggregation. Output: `CandidateScore` with a Wilson
   interval, plus per-example results.
5. **Bookkeeping.** Core folds the new scores into the leaderboard and
   produces the next `StrategyLogEntry`; shell persists loop artifacts.

### Phase C — Validation checkpoints

- Cadence decided by a pure rule in `strategy_calcs`: validate the current
  dev-best candidate every `val_every` loops (default 3), or when a new
  candidate beats the previous dev-best by a configured margin, or when the
  strategy agent explicitly issues `RequestValidation`.
- Hard cap on total validation evaluations (default 10). Every peek at
  validation weakens it as an unbiased signal, so peeks are budgeted, counted,
  and reported.
- Only the **aggregate** validation score (and dev–val gap) is ever fed back
  to the strategy agent — never raw validation examples or per-example
  failures.

### Phase D — Finalization (once)

1. Stopping rule fires (budget exhausted, validation plateau over
   `patience` checkpoints, target score reached, or strategy stop).
2. Winner = best **validation** score (dev breaks ties). Selecting on
   validation rather than dev is the core anti-overfitting mechanism.
3. Shell evaluates the winner on the test set — the only time test examples
   are ever read after splitting. No agent sees test data or test results;
   this is purely a harness computation, run exactly once.
4. Shell writes `final/`: the solution artifact (directly usable outside the
   framework), plus `report.md` with dev/val/test scores + CIs, the dev–val–test
   gaps, loop history, validation-peek count, and per-check breakdowns.

## 6. Agent roles and leakage controls

| | Strategy agent | Executor agents | LLM judge |
|---|---|---|---|
| Cardinality | 1, logically persistent across loops (via its log; each call is still stateless `claude -p`) | K per loop, concurrent | n samples per (example, judge check) |
| Sees | Own prior log, dev leaderboard + CIs, dev failure patterns, **aggregate** val scores | Task, current directive, check descriptions, dev-example sample. **Nothing about prior loops.** | Rubric, one candidate output, one expected output |
| Never sees | Raw val/test examples, per-example val results | Other executors' work, strategy log, any val/test data, prior mistakes | Anything else |
| Produces | JSON `StrategyDecision` | One fenced `solution` block | JSON verdict |

Leakage is enforced twice:

- **By type:** prompt builders accept only `DevExample` values (and aggregate
  score types). Passing a `ValExample` or `TestExample` is a type error, and
  there is no function anywhere that renders val/test example content into a
  prompt string.
- **By test:** dedicated tests assert that no prompt produced during a
  simulated full run contains any val/test example content (substring scan
  over every prompt built with a fake agent runner).

The requirement that "each execution agent only sees the current loop's
strategy" falls out of construction: executor prompts are built from the
directive alone, and the strategy log is a distinct type that the executor
prompt builder cannot accept.

## 7. Solution kinds and how each is evaluated

| Kind | Artifact | Evaluation of one example |
|---|---|---|
| `ScriptSolution` | Executable Python script | Shell runs it in a subprocess (`RunScript` effect): example input on stdin, structured output expected on stdout, timeout + output-size cap. Non-zero exit / timeout → `Errored`. |
| `SkillSolution` | Skill markdown (e.g. Claude Skill `SKILL.md`) | Shell runs `claude -p --tools ""` with the skill content injected via `--append-system-prompt` and the example input as the prompt; the reply is the raw output to score. |
| `GuidanceSolution` | Guidance markdown (AGENTS.md / CLAUDE.md style) | Same harness as skills: guidance prepended as system prompt, input as prompt. |

The executor **output contract** is strict and stated in the prompt: exactly
one fenced block tagged `solution`, nothing executable outside it. The core
parser returns `Result[Candidate, MalformedReply]`; the shell grants one
reformat retry.

Sandboxing: generated scripts and `CustomPython` checks are untrusted code.
V1 mitigations: subprocess with hard timeout, no stdin inheritance, output
caps, and a scratch working directory. Documented loudly as *not* a security
boundary; OS-level sandboxing is a listed future hardening item (§12).

## 8. Statistical methodology

This is the "rigor" in RigorLoop; all of it lives in `scoring_calcs.py` as
pure, individually testable functions.

- **Pass-rate uncertainty:** Wilson score intervals (95%) on every reported
  pass rate — honest at the small n this framework will often see.
- **Continuous scores** (e.g. judge scores averaged): bootstrap percentile
  CIs; the resample indices derive from an injected seed so results are
  reproducible.
- **Comparing candidates on the same set:** paired analysis — McNemar's test
  for pass/fail checks, paired bootstrap for continuous scores. The
  leaderboard marks differences that are within noise, and the strategy prompt
  states them as "not statistically distinguishable" so the strategy agent
  doesn't chase noise.
- **Overfitting signal:** dev–val gap tracked per checkpoint and plotted in
  the final report; a widening gap triggers a warning in the strategy context.
- **Selection & reporting discipline:** selection on validation, never dev;
  test evaluated once; validation-peek count reported; power warnings at
  intake when splits are too small to support the configured target margins.

## 9. Configuration and CLI

```toml
# rigorloop.toml
[task]
description_file = "task.md"
solution_kind    = "script"          # script | skill | guidance
examples_file    = "examples.jsonl"

[split]
ratios = [0.6, 0.2, 0.2]
seed   = 17

[loop]
max_loops            = 12
executors_per_loop   = 4
dev_examples_in_prompt = 30

[validation]
val_every  = 3
max_peeks  = 10
patience   = 3                       # checkpoints without improvement → stop
target_pass_rate = 0.95              # optional early-success stop

[agents]
model      = "claude-sonnet-5"
timeout_s  = 300

[[checks]]
type = "json_equality"

[[checks]]
type = "llm_judge"
rubric = "Output captures every entity mentioned in the input..."
n_samples = 3
pass_threshold = 0.67
```

CLI (argparse, in `shell/cli.py`):

- `rigorloop init` — scaffold `rigorloop.toml`, `task.md`, an example JSONL.
- `rigorloop check` — parse config/examples, print split sizes + power
  warnings, estimate agent-call budget. No tokens spent.
- `rigorloop run [--resume RUN_ID]` — execute the protocol.
- `rigorloop report RUN_ID` — re-render the report from persisted artifacts.

## 10. Run directory (persistence & resumability)

```
runs/<run_id>/
├── manifest.json            # config snapshot + SplitManifest (hashes)
├── splits/{dev,val,test}.jsonl
├── strategy_log.jsonl       # append-only; the strategy agent's memory
├── leaderboard.json
├── loops/<n>/
│   ├── strategy_{prompt,reply}.md
│   └── candidates/<id>/{solution.*, outputs.jsonl, scores.json}
└── final/
    ├── solution.*           # the portable deliverable
    ├── report.md
    └── test_results.json
```

Every artifact is plain JSON/JSONL/markdown written by `io_actions.py`
(append-only where possible). Resume = shell reloads state files, core
re-derives the in-memory state, loop continues. The split manifest hash check
on resume prevents dataset drift mid-run.

## 11. Testing strategy

- **Core, zero mocks (the bulk):** splitting determinism/disjointness/ratios;
  every check evaluator; Wilson/bootstrap/McNemar math against known values;
  strategy & executor reply parsers (valid, malformed, adversarial);
  cadence/stopping rules; prompt builders (golden-file tests).
- **Leakage tests (first-class):** the §6 substring-scan test over all prompts
  from a simulated run; a test asserting the test set is read at most once;
  type-level guards exercised.
- **Shell integration (thin):** `agent_calls` against a stub executable that
  mimics `claude -p --output-format json` (success, timeout, garbage output);
  script sandbox timeout/output-cap behavior; run-dir round-trip + resume.
- **End-to-end with a fake agent function:** a scripted fake plays strategy +
  executors, letting a full multi-loop run (with validation checkpoints and
  finalization) execute in milliseconds with deterministic assertions on the
  final report.
- Tooling: `pytest`, `ruff`, `mypy --strict` (strict typing is load-bearing —
  it enforces the exhaustive-match and split-type guarantees).

## 12. Implementation milestones

Each phase ends green: tests pass, `mypy --strict` clean.

1. **Scaffolding** — `pyproject.toml`, package layout, CI-ready test harness;
   `core/types.py` (`Result`, `Option`, first domain ADTs).
   *Accept:* `pytest` and `mypy --strict` run clean on the skeleton.
2. **Dataset core** — example parsing, splitting, manifests, power warnings.
   *Accept:* property-style tests for determinism/disjointness pass.
3. **Scoring core** — deterministic check evaluators, aggregation, Wilson +
   bootstrap + McNemar.
   *Accept:* statistics validated against hand-computed known values.
4. **Shell foundations** — `agent_calls.py` (subprocess wrapper, JSON envelope
   parsing → `Result`, retries, concurrency), `io_actions.py` (run dir,
   script sandbox), `cli.py` with `init`/`check`.
   *Accept:* stub-CLI integration tests pass; `rigorloop check` works on a
   sample project.
5. **Single-loop end-to-end (script kind)** — executor prompt building, reply
   parsing, candidate materialization/execution, dev scoring; one loop with a
   hard-coded strategy directive.
   *Accept:* fake-agent E2E produces a scored leaderboard.
6. **Strategy loop** — strategy prompts/parsing, strategy log, multi-loop
   orchestration, leakage tests.
   *Accept:* multi-loop fake-agent E2E; leakage scan test green.
7. **Validation & stopping** — checkpoints, peek budget, plateau/target
   stopping, finalization with one-shot test evaluation and `report.md`.
   *Accept:* E2E run yields full report; test set touched exactly once.
8. **Remaining kinds & judge checks** — `LlmJudge` (n-sample majority),
   `SkillSolution`/`GuidanceSolution` evaluation harness, `CustomPython`
   checks.
   *Accept:* per-kind E2E with fake agents.
9. **Hardening & docs** — resume, cost/budget accounting surfaced in `check`
   and the report, README + worked example project, live smoke test against
   the real `claude` CLI on a toy task.

Future (post-v1) items: OS-level sandboxing for generated code, stratified
splitting, adaptive validation cadence, HTML report.

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Malformed agent replies break loops | Strict output contracts; `Result`-returning parsers; one reformat retry; failed candidates recorded, never fatal. |
| Overfitting to dev despite the design | Selection on validation; capped peeks; dev–val gap surfaced to strategy agent and report; test untouched until the end. |
| Small datasets → meaningless stats | Power warnings at `check` time; CIs on every number; leaderboard marks statistically indistinguishable differences. |
| Token/cost blowout | `rigorloop check` pre-run budget estimate; per-run call ceiling; loop and executor caps in config. |
| Untrusted generated code | Timeouts, output caps, scratch dirs; documented as not a security boundary; OS sandboxing on the roadmap. |
| LLM nondeterminism muddies comparisons | n-sample judge voting; paired statistical tests; reproducible seeds for everything the harness controls. |
| `claude` CLI flag drift | All flags isolated in one shell function in `agent_calls.py`; stub-CLI tests define the expected envelope. |

## 14. Open questions (defaults chosen; flag if wrong)

1. **Split ratios** default to 60/20/20 — acceptable default?
2. **Structured inputs**: v1 treats `input`/`expected_output` as strings
   (JSON-encoded when structured). Native multi-field examples later?
3. **Model choice**: single configured model for all roles in v1; per-role
   models (cheaper executors, stronger strategist) is an easy follow-on.
4. **Judge budget**: `LlmJudge` on large dev sets is the main cost driver —
   is an n-sample majority of 3 per example acceptable, or should judges score
   only failures of deterministic checks first (tiered checking)?
