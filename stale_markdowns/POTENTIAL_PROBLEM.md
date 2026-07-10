


# FRIEND 1: Why the current loop can optimize for dev over generalization

  ## Executive summary

  The code has two different notions of “best”:

  - The dev leader: highest raw dev pass rate across all candidates.
  - The validation champion: best candidate among the small set that happened to receive validation.

  Those should play different roles. In the current implementation, the dev leader drives future generation, while the validation champion
  is mostly stored for final selection. That means validation does not meaningfully steer the search toward generalization.

  A concise picture of the current flow:

  All candidates ──dev score──> raw dev leaderboard ──> dev leader
                                                      │
                                                      ├── used as next prompt/base artifact
                                                      └── only candidate eligible for validation
                                                               │
                                                               v
                                                        validation champion
                                                               │
                                                               └── mostly not used until finalization

  The result is a search process that can spend many loops refining an artifact already shown to generalize poorly.

  ## What the code does today

  After each loop, new candidates are appended to a global leaderboard and sorted purely by raw dev pass rate:

  return tuple(sorted(merged, key=lambda e: (-e.score.pass_rate, ...)))

  src/rigorloop/core/strategy_calcs.py:245

  The first entry becomes dev_best. The strategy context labels that dev-best artifact the current champion and embeds its contents in the
  next strategy prompt:

  src/rigorloop/core/strategy_calcs.py:461

  Then the driver uses that same dev-best artifact as the base for executor directives:

  src/rigorloop/shell/cli.py:545

  Separately, validation evaluates only dev_best:

  src/rigorloop/core/strategy_calcs.py:343

  src/rigorloop/shell/cli.py:684

  The resulting val_champion is persisted, but it is not used as the next-loop base artifact or as the strategy prompt’s primary champion.

  There is an especially consequential condition in should_validate:

  already_validated = {c.candidate_id for c in state.checkpoints}
  if best.value.candidate_id in already_validated:
      return False

  Once the global dev leader has been validated, scheduled validation does not move on to the next-best dev candidate. It simply stops
  validating until a candidate overtakes the global dev leader.

  ## A concrete failure mode

  Suppose the task has 100 dev examples and 30 validation examples.

   Candidate    Dev    Validation    Reality
  ━━━━━━━━━━━  ━━━━━  ━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   A            96%           60%    Memorizes dev-specific formatting/templates
  ───────────  ─────  ────────────  ────────────────────────────────────────────────
   B            94%           90%    Slightly worse on dev, much better generalizer
  ───────────  ─────  ────────────  ────────────────────────────────────────────────
   C            92%       unknown    Different approach

  What happens now:

  1. A becomes the dev leader and gets validated.
  2. Validation reveals that A is only 60%, so val_champion = A by default.
  3. B is generated later. It scores 94% on dev, so it is below A.
  4. B is never validated, because only the global dev leader is eligible.
  5. The next strategy prompt carries A’s full artifact, and refinements are based on A.
  6. The loop continues to optimize A’s dev behavior. B’s likely 90% validation performance remains undiscovered.
  7. Finalization selects A because it is the only candidate with a validation score.

  This is not a rare edge case. It is exactly the scenario validation exists to catch: a somewhat weaker training/dev fit that generalizes
  better.

  ## Why the significance gate does not solve it

  The project intends to avoid chasing random dev fluctuations with McNemar testing. That is a good instinct, but the actual raw leaderboard
  still changes on any raw-score increase.

  A candidate that rises from 96% to 97% dev accuracy becomes dev_best and can become the next base artifact even if the increase is not
  statistically meaningful. The significance calculation only influences whether a validation peek is triggered; it does not control who is
  considered the dev champion.

  So there are two inconsistent policies:

  - “Meaningful improvement” uses a paired statistical test.
  - “Who gets copied into the next generation” uses raw dev rate.

  The second policy dominates the search dynamics, so the loop can still chase noise.

  ## Why this harms convergence

  The strategy agent has limited information:

  - It sees aggregate validation summaries, not validation examples.
  - It sees dev failure samples only for the active dev leader.
  - Executors can refine only the artifact supplied in their directive.

  That creates a feedback loop:

  High dev score
      ↓
  becomes base artifact
      ↓
  more descendants resemble it
      ↓
  more chances to slightly improve dev score
      ↓
  remains base artifact

  If the dev leader is overfit, the population of later candidates becomes concentrated around the overfit lineage. A genuinely better
  approach can survive only if it beats the overfit leader on dev—not merely on validation.

  There is a second practical problem: failure samples disappear whenever the global dev leader came from an earlier loop, so the strategy
  can lose concrete dev counterexamples even while continuing to refine that artifact. src/rigorloop/shell/cli.py:671

  ## The right conceptual separation

  Use three explicit concepts.

   Role                   Purpose                                   Should guide future generation?
  ━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   Dev leader             Cheap diagnostic of in-sample fit         Sometimes
  ─────────────────────  ────────────────────────────────────────  ─────────────────────────────────
   Validation champion    Best evidence of generalization so far    Yes
  ─────────────────────  ────────────────────────────────────────  ─────────────────────────────────
   Test result            Final unbiased estimate                   Never

  The current code conflates the first and third operationally: dev controls generation; validation mainly controls final reporting.

  For an iterative framework, the validation champion should become the primary exploitation base after it exists. The dev leader should
  remain visible as a diagnostic and source of exploratory alternatives.

  ## Recommended design

  ### 1. Make the validation champion the default base artifact

  Once any candidate has been validated:

  primary base = validation champion
  fallback before first validation = dev leader

  The strategy prompt should show both, but clearly distinguish them:

  Primary artifact to refine: validation champion
  - Dev: 94%
  - Validation: 90%

  Diagnostic dev leader:
  - Dev: 96%
  - Validation: 60%
  - Warning: likely overfit

  Then base_on_champion=true attaches the validation champion, not the dev leader.

  This does not leak validation examples. It uses only aggregate validation results to select among dev-trained artifacts, which is ordinary
  model selection. The final test remains untouched.

  ### 2. Validate a precommitted cohort, not just one dev winner

  No method can discover a lower-dev, higher-validation candidate without evaluating it on validation. The honest tradeoff is validation
  budget.

  At each checkpoint, select a cohort before looking at that checkpoint’s validation outcomes, for example:

  - top 2–3 currently unvalidated candidates by dev score;
  - one exploration candidate selected for approach diversity;
  - the current validation champion if re-evaluation is needed for stochastic artifacts.

  A practical policy could be:

  At each validation checkpoint:
    1. Include the current dev top-3, excluding already evaluated deterministic candidates.
    2. Include one diverse recent candidate not descended from the current base.
    3. Evaluate the cohort on the full validation set.
    4. Choose or retain the validation champion by a predeclared rule.

  This gives B in the earlier example a path to validation even though A remains dev-best.

  “Diverse” can be operationalized without looking at validation data:

  - one candidate from a non-champion directive;
  - one candidate not based on the current champion;
  - one candidate selected deterministically from recent unexplored directives;
  - or a random candidate chosen from a seeded pool.

  For expensive skill/guidance evaluation, reduce the cohort size rather than pretending one dev leader is enough.

  ### 3. Keep the incumbent unless validation evidence supports replacement

  The current select_val_champion can replace the validation incumbent using a dev-score tiebreak even when validation says the candidates
  are indistinguishable. src/rigorloop/core/strategy_calcs.py:359

  That reintroduces dev bias precisely where validation should be authoritative.

  A safer rule:

  If challenger is convincingly better on validation:
      promote challenger
  Else:
      retain validation incumbent

  For small validation sets, “convincingly” needs a policy selected in advance:

  - paired McNemar with an appropriate sequential/multiple-comparison correction;
  - a practical effect threshold plus confidence interval;
  - or a Bayesian posterior probability of improvement.

  If the evidence is inconclusive, retaining the incumbent is better than silently reverting to dev preference. You can keep the challenger
  in a “validation co-leader” set for future evaluation rather than discarding it.

  ### 4. Preserve dev-only diagnostics for every viable base

  Using the validation champion as a base does not mean feeding it validation failures. Keep the holdout boundary intact:

  - Persist dev outputs and dev failure samples keyed by candidate ID.
  - When the validation champion becomes primary, retrieve its dev failure samples.
  - Give the strategy agent those dev failures plus aggregate validation statistics only.
  - Never provide validation inputs, expected outputs, actual outputs, or per-example verdicts.

  This fixes the current loss of feedback after a non-improving loop or resume.

  ### 5. Reserve executor capacity for exploration

  If every executor refines the validation champion, the system may still collapse too quickly. A reasonable per-loop split is:

  2 executors: refine validation champion
  1 executor: alternative approach from scratch
  1 executor: targeted dev-failure repair or simplification

  The strategy agent can decide the directives, but the harness should preserve at least one non-champion exploration slot while the search
  is uncertain.

  ## Suggested state-model changes

  The existing types can support this with modest additions.

  @dataclass(frozen=True, slots=True)
  class CandidateDiagnostics:
      candidate_id: str
      dev_failure_samples: tuple[FailureSample, ...]

  @dataclass(frozen=True, slots=True)
  class ValidationCohort:
      loop_index: int
      candidate_ids: tuple[str, ...]
      selection_reason: str

  Then add to RunState:

  - persisted dev diagnostics by candidate;
  - validation cohort history;
  - a primary search artifact derived from val_champion, falling back to dev_best;
  - possibly a set of validated candidates and their scores.

  The key refactor is to replace this conceptual operation:

  previous_best = dev_best(state.leaderboard)

  with:

  search_base = validation_champion_or_dev_leader(state)

  The strategy context can still render the raw dev leaderboard, but it should not call its first entry “the champion” once validation
  exists.

  ## Suggested driver flow

  Generate candidates from the current primary base
          ↓
  Evaluate every candidate on dev
          ↓
  Update dev leaderboard and persist dev diagnostics
          ↓
  If checkpoint:
      choose precommitted validation cohort
      evaluate cohort on validation
      update validation champion
          ↓
  Next loop:
      refine validation champion
      retain explicit exploration candidates
          ↓
  Finalization:
      select validation champion
      evaluate it once on test

  This is a standard and coherent dev/validation/test protocol:

  - Dev is used for cheap iteration and diagnosis.
  - Validation chooses among dev-trained candidates and directs limited search.
  - Test is never used in any decision.

  ## Statistical safeguards

  Making validation guide the loop does increase validation-selection pressure. That is expected; validation is being used for model
  selection. Mitigate it rather than avoiding it inconsistently:

  - Predeclare cohort-selection and promotion rules.
  - Cap checkpoints and cohort size.
  - Do not reveal validation examples or failure details.
  - Require a minimum validation size before enabling significance-based stopping.
  - Use a lower confidence bound—not a raw point estimate—for target_pass_rate.
  - Treat test as spent after any execution attempt, including crashes.
  - For stochastic skills or LLM judges, use repeated evaluations and report evaluator variance.

  If statistical conservatism is more important than iterative convergence, use a different protocol: generate all candidates using dev
  only, do one validation selection at the end, then test once. That is cleaner but gives up validation-guided refinement.

  ## Tests that should be added

  1. A lower-dev candidate is included in a validation cohort and can become champion.
  2. After validation, base_on_champion embeds the validation champion, not raw dev leader.
  3. An already validated dev leader does not prevent new eligible candidates from being validated.
  4. Validation examples never enter strategy/executor prompts.
  5. Dev failure diagnostics survive non-improving loops and resume.
  6. A validation incumbent is retained when a challenger is within uncertainty.
  7. An end-to-end synthetic run where an overfit candidate wins dev but loses validation, while a lower-dev candidate wins validation and
     ultimately test.

  The core issue is not that validation should never affect generation. It should: that is what makes a validation set useful in an
  iterative search. The problem is that the current implementation pays the cost of validation peeks and exposes their aggregates to the
  strategy agent, yet lets raw dev performance determine almost every subsequent generation decision.








# FRIEND 2 - Advice on how to fix this

Yes — the problem is valid. I verified every code claim in POTENTIAL_PROBLEM.md against the source, and they all check out. The framework's core promise ("the validation set picks the winner") is technically true but hollow in practice: validation only ever evaluates whatever the raw dev leaderboard already chose, so it measures the dev-optimized candidate rather than steering the search toward candidates that generalize. The proposed solution is the standard, correct protocol; I'd adopt most of it as written, with one refinement to recommendation 3.

The problem is real — verified against the code

The app's purpose (per the README and your map-metadata example) is to search for a transformation artifact that scores well on held-out data. The dev/val/test split, Wilson CIs, McNemar tests, and peek budget are all genuinely implemented and leakage is well-controlled (the typed prompt channels in prompt_calcs.py are a nice design). But the search dynamics are driven almost entirely by raw dev score:

- The leaderboard is sorted by raw dev pass rate, nothing else — strategy_calcs.py:250. Any raw uptick, significant or not, creates a new "champion."
- That dev leader is what gets embedded in the strategy prompt as "# Current champion (dev-best) solution" (prompt_calcs.py:260) and what base_on_champion attaches to executor directives (cli.py:545-550). The validation champion is never used as a base artifact anywhere.
- Validation only ever evaluates the current global dev leader (cli.py:684-688), and should_validate returns False once that leader has been validated (strategy_calcs.py:346-348). So the researcher's candidate B (94% dev, would-be 90% val) has literally no code path to ever touch the validation set. The A/B/C failure scenario is not hypothetical — it's exactly what this code does.
- Finalization picks state.val_champion (cli.py:771), which by construction can only ever contain ex-dev-leaders. Validation isn't selecting the winner; it's rubber-stamping dev's selection.
- The "significance gate doesn't solve it" claim is also correct: beats_previous_best feeds only new_best_significant, which only affects whether a validation peek is triggered (cli.py:660-682). Who becomes the base artifact is decided by the raw sort.
- The failure-sample bug is real too: cli.py:671-678 sets last_samples = () whenever the global dev leader came from an earlier loop, and cli.py:477 resets it on resume. So the strategy agent keeps refining a champion whose concrete failures it can no longer see.

One amplifier the doc under-emphasizes: because of the already-validated guard, checkpoints happen only when the dev leader changes, and displaced_champion requires a significant McNemar win on validation. With realistic val sizes (20 examples from a 100-example dataset), McNemar at α=0.05 almost never fires — so patience = 2 means the run stops after two new dev leaders fail to significantly beat the val incumbent, even if validation genuinely climbed from 60% to 75% in that time. The stopping rule and the selection rule compound the same flaw: the run can end early and hand you the overfit candidate. Relatedly, target_pass_rate early stopping compares a raw point estimate (strategy_calcs.py:395), which on n=20 can easily be a lucky draw — the doc's suggestion to use the lower confidence bound is right.

What I think of the proposed solution

It's the textbook protocol (dev = cheap iteration, validation = model selection and search direction, test = one unbiased estimate), and the doc is correct that using aggregate validation scores to choose among dev-trained artifacts is ordinary model selection, not leakage. My take per recommendation:

1. Validation champion as the default base — adopt as-is. This is the single highest-leverage fix, and the "show both, label the dev leader as diagnostic" prompt design is good. The key refactor really is the one-liner they identify: replace dev_best(state.leaderboard) at cli.py:545 with a validation_champion_or_dev_leader(state) selector.

2. Precommitted validation cohorts — adopt, mind the budget. This is the only way a lower-dev/higher-val candidate can ever be discovered; no selection rule can rank candidates on evidence that was never gathered. The real cost is that a cohort of 3 triples checkpoint cost, which is painful for skill/guidance runs where every val example is a model call. You'll need to redefine max_peeks as candidate-evaluations rather than checkpoints, and a cohort of 2 (top unvalidated by dev + one strategy-nominated diverse candidate) is probably the right default for expensive artifact kinds.

3. Incumbent retention — adopt the diagnosis, refine the cure. The genuinely wrong part of the current select_val_champion is that the within-noise tiebreak uses dev score (strategy_calcs.py:379) — the one metric under direct selection pressure — to displace a validation incumbent. But the proposed strict rule ("retain unless significantly better on validation") has its own pathology at RigorLoop's sample sizes: with n=20, "inconclusive" is nearly always, so the first validated candidate becomes almost immovable and the patience rule then kills the run. A 75%-val challenger would fail to displace a 60%-val incumbent. My suggestion: promote on the validation point estimate (or lower confidence bound) and keep the significance test for what it already gates well — whether the change counts as "improvement" for patience/stopping. That's a minimal diff from current behavior (swap the dev tiebreak for a val tiebreak), removes the dev bias, and avoids the stickiness. The doc's own hedges (effect-size threshold, Bayesian posterior, co-leader set) acknowledge this tension; they're fine too, just heavier.

4. Persist per-candidate dev diagnostics — adopt; it's cheap. The data already exists on disk: outputs.jsonl per candidate is written at cli.py:629-648. The fix is mostly to reload the champion's failure samples from there instead of keeping only the last loop's samples in memory. This also fixes the resume gap.

5. Reserved exploration slot — optional. The strategy agent already controls base_on_champion per directive and the prompt nudges diversity. A harness-enforced slot is a reasonable default but not load-bearing for the core fix; I'd do 1–4 first and add this only if runs visibly collapse onto one lineage.

Two caveats worth carrying into the report and README if you implement this: (a) once validation steers the search, the champion's validation score becomes optimistically biased by selection — which is fine, because that's exactly what the untouched test set is for, but the report should say so; and (b) the doc's suggested test list is good, especially #7 (a synthetic end-to-end run where an overfit candidate wins dev but a lower-dev candidate wins validation and test) — the existing test_e2e.py and test_leakage.py cover honesty of the plumbing well, but nothing currently tests the selection dynamics, which is precisely where the bug lives.

Bottom line: your friend found a genuine architectural flaw, diagnosed it accurately with correct code references, and proposed the right family of fix. Implement recommendations 1, 2, and 4 essentially as written, take 3's diagnosis but use a validation-based tiebreak (or LCB promotion) rather than strict significance gating, and treat 5 as a nice-to-have.