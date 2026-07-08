# RigorLoop

A statistically-sound agentic build framework that employs agentic loops to create code artifacts (whether a script, a skill markdown file, etc) without overfitting.

## ⚠️ The final test set is only honest once

RigorLoop holds out a final test set and evaluates the winning solution on it **exactly once per run**. That guarantee cannot protect you from yourself *across* runs: if you look at the test score, tweak your task description or checks, and re-run on the same examples file, the "held-out" test set is no longer unseen. After a few such iterations it has effectively become a second validation set, and its scores will be optimistically biased.

If you iterate after seeing a test result, treat that test set as spent — supply fresh, never-before-used examples for the next run's holdout.

Related caveat: RigorLoop deduplicates *exact* duplicate inputs before splitting, but near-duplicates (the same example lightly reworded) can still straddle the dev/test boundary and quietly inflate test scores. If your dataset may contain near-duplicates, deduplicate it yourself before handing it to RigorLoop.
