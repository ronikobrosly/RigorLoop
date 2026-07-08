# CODING_STYLE.md


This file governs how you write and modify code in this repository. These rules are
**not stylistic preferences**; treat them as constraints. 

## Operating principles

When a requested change can be implemented functionally, implement it functionally —
do not ask permission first. When a requested change *appears* to require violating
these rules:

1. First, restructure so the violation disappears (this is almost always possible).
2. If it cannot be removed, **isolate it at the imperative shell** (see below), never
   in the functional core.
3. If you must write impure or mutating code, mark it with a comment explaining why the
   functional version was not possible. Silent violations are the only forbidden move.

Never weaken these rules just to finish faster. Prefer correct-and-stricter over
quick-and-looser (with the exception of recursion, which we want to avoid).

Files will be named what they accomplish but will be segregated by whether they belong to "functional core" and "imperative shell". Details on the functional core and the imperative shell are below.

```
.
└── src/
    ├── core/
    │   ├── dataset_calcs
    │   ├── strategy_calcs
    │   └── scoring_calcs
    └── shell/
        ├── agent_calls
        └── IO_actions 
```

## Specific principles

### 1. Architecture: separation of a "functional core" and "imperative shell"

- The **core** holds all business logic and is 100% pure: no I/O, no mutation observable
  outside a function, no time, no randomness, no network, no environment access.
- The **shell** is a thin outer layer that performs effects (reads input, writes output,
  calls services) and hands plain data to the core. Keep it as small as possible.
- Data flows: shell gathers inputs → core computes a result (pure) → shell performs the
  effects the result describes. The core *decides*; the shell *acts*.
- A reader should be able to test the entire core with no mocks, fakes, or setup —
  only plain inputs and asserted outputs. If a test for core logic needs a mock, the
  boundary is in the wrong place.

### 2. Purity

- Every function in the core is **pure**: same inputs always produce the same output,
  and it has no observable side effects.
- A core function never reads or writes mutable global/module state, the clock, the
  random source, the filesystem, the network, the environment, or stdout/stderr.
- A function that returns nothing (`void`/unit) in the core is a red flag — it can only
  be doing a side effect. Core functions return values.
- **Referential transparency:** any call must be replaceable by its result without
  changing program behavior.

### 3. Immutability

- Never mutate a value after creation. Produce a new value instead.
- No in-place updates to arrays, maps, sets, records, or objects passed as arguments.
  A function must not modify its inputs.
- Prefer your language's immutable/persistent data structures or immutable bindings.
  Where the language only offers mutable structures, treat them as immutable by
  convention: copy-on-write, never edit in place.
- No reassignment of bindings for control flow. Bind once; derive new bindings.

### 4. Data modeling

- Model the domain with **algebraic data types** (records/products for "and", tagged
  unions/sum types for "or"). Use your language's closest equivalent.
- **Make illegal states unrepresentable.** If two fields can't both be set, don't model
  them as two optional fields — model the valid combinations as a sum type.
- **Parse, don't validate.** Validate untrusted input once at the shell boundary and
  convert it into a precise type. The core then receives already-valid data and never
  re-checks it.
- **No null / nil / undefined** as a stand-in for "maybe". Use an Option/Maybe type.
- Keep data and behavior separate. Data is plain and inert; transformations are
  free functions. Avoid objects that bundle mutable state with methods that mutate it.

### 5. Functions and composition

- Functions are first-class values: pass them, return them, compose them.
- Build behavior by **composing small, single-purpose functions** into pipelines rather
  than writing large procedures.
- Prefer composition over inheritance. Do not reach for class hierarchies to share
  behavior; share functions.
- Take **all dependencies as explicit parameters.** No hidden singletons, ambient
  context, service locators, or global config reads inside the core. If a function needs
  the clock, a logger, or an ID generator, it receives it as an argument.
- Keep functions **total**: defined for every input of their declared type. Do not write
  partial functions that throw on some inputs (e.g. `head` on an empty list). Either
  narrow the input type (e.g. a non-empty type) or return an Option.

### 6. Control flow

- Prefer **expressions over statements**: everything evaluates to a value, including
  conditionals.
- Replace imperative loops that accumulate into a mutable variable with `map` / `filter`
  / `reduce` / `fold` or equivalent higher-order operations.
- Generally, try to avoid recursion.
- Pattern-match **exhaustively** on sum types. Do not use a wildcard/`default` case to
  swallow unhandled variants — let the absence of a case be a compile error or explicit
  failure, so adding a new variant forces you to handle it everywhere.

### 7. Error handling

- **No exceptions for control flow.** Expected failures (not found, invalid input,
  conflict) are return values, not thrown.
- Represent fallible operations with a **Result/Either type** (success or typed error).
  Represent absence with **Option/Maybe**.
- Propagate and combine errors through the type, not by throwing across layers.
- Reserve thrown exceptions / panics for truly unrecoverable programmer errors
  (broken invariants), and only at the shell. The core does not throw for ordinary
  failure cases.
- Make error types meaningful (a sum type of failure cases), not a single opaque string.

### 8. Effects

- Effects (I/O, time, randomness, persistence, network) live in the shell and are
  performed at the edges of the program.
- Where the language supports it, represent effects as **descriptions/values** that the
  shell executes, or at minimum **inject effectful functions** as parameters so the core
  stays pure and testable.
- Time, randomness, and unique IDs are inputs, never grabbed implicitly inside logic.
  Pass `now`, the random value, or the generated ID in.
- Sequencing of effects belongs to the shell. The core may return a *plan* of effects;
  it does not run them.

### 9. Forbidden patterns (quick reference)

- Mutating an argument.
- Reading/writing global mutable state.
- `null` for optional values.
- Throwing for expected failures.
- Imperative accumulation loops in the core.
- Wildcard pattern cases that hide unhandled variants.
- Hidden dependencies (singletons, globals, ambient clock/RNG) inside core functions.
- Classes that pair mutable state with mutating methods.
- Void-returning functions in the core.

### 10. Permitted exceptions (escape hatches)

These are the *only* allowed deviations, and each requires a justification comment:

- **Encapsulated local mutation:** a function may use a local mutable variable for
  performance *if and only if* that mutation never escapes the function and the function
  remains pure and referentially transparent from the outside. Default to declarative;
  reach for this only when measured performance demands it.
- **Shell-level effects:** the imperative shell performs real effects by definition.
  This is expected, not an exception — but keep the shell thin.
- **Foreign/interop boundaries:** when calling a mutable or exception-throwing library,
  wrap it at the shell and convert immediately to immutable data and Result types so the
  impurity does not leak inward.

If a deviation does not fit one of these three, it is not allowed — restructure instead.

### 11. Self-review before finalizing any code

Answer these before considering a change complete:

- Is every core function pure and total? Could I replace each call with its result?
- Does any function mutate its input or any shared state? (Must be no.)
- Are all dependencies — including clock, randomness, IDs, I/O — passed in explicitly?
- Are optional values Option types and fallible operations Result types, with no `null`
  and no exceptions for expected failures?
- Is every sum type matched exhaustively, with no variant-swallowing wildcard?
- Could the core be tested with plain inputs and outputs and zero mocks?
- Is every deviation from these rules isolated at the shell and justified in a comment?

If any answer is unsatisfactory, revise before returning the code.


## Some specific key examples


### 1. Avoid recursion

For example, if we're trying to make a function to sum all numbers up to `n`, but only among the numbers that are divisible by 3 or 5, avoid the following:

```python
from collections.abc import Sequence, Callable
def until(
    limit: int,
    filter_func: Callable[[int], bool],
    v: int
) -> list[int]:
    if v == limit:
        return []
    elif filter_func(v):
        return [v] + until(limit, filter_func, v + 1)
    else:
        return until(limit, filter_func, v + 1)

def mult_3_5(x: int) -> bool:
    return x % 3 == 0 or x % 5 == 0

def sum_functional(limit: int = 10) -> int:
    return sumr(until(limit, mult_3_5, 0))
```

...and instead use the simpler (and mostly functional):

```python
def sum_divisible_by_3_or_5(n):
    return sum(x for x in range(1, n + 1) if x % 3 == 0 or x % 5 == 0)
```