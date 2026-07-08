# CLAUDE.md for RigorLoop

## Project overview

This a is statistically-sound agentic build framework that employs agentic loops to create code artifacts (whether a script, a skill markdown file, etc). Crucially, it splits verification data into the classic data science-like dev, validation, and final test sets to avoid overfitting. 

This framework IS NOT meant to be used when someone wants to create an agentic engineering loop to produce a simple, deterministic script that will pass a series of simple unit tests. Instead, it is meant to be used for data science-like efforts like when the user needs to create a coding solution to take a set of inputs (structured or unstructured text data) and convert it into a set of structured outputs. The user would provide this frame with a (hopefully) large set of gold-standard, correct example inputs and outputs, and this agentic framework attempts to create a coding solution through statistically-rigorous loops and verifications. 

The "coding solution" can take a number of forms:

- a set of executable scripts
- a "skill" for an agentic coding harness (e.g. Claude Skills)
- a markdown file to guide agents (e.g. AGENTS.md, CLAUDE.md, etc.)
  

## Requirements

* A strict splitting of a "development", "validation", and "final test" set must be made out of the input data the user provides.
* The user must provide structured input text with the expected output too. The user must provide a set of either deterministic or probablistic verifications/checks of the proposed solutions. This framework must flexibly allow for whatever the user provides (within realistic bounds and with guidelines). The examples the user provides should be highly-representative of the universe of inputs and outputs they might encounter.
* The framework will generate a series of solutions that iteratively improve through a series of loops. 
* The framework will employ a strategy agent (with its own prompt) and a series of execution agents (with their own prompts). The strategy agent will review each iterations results on the "development" set, and periodically spin up an agent to test the best approach on the "validation" set. Each execution agent will only be able to see the current loop's strategy and not know about prior loops. The one sanctioned exception: the strategy agent may embed the current champion solution's *content* in a directive as a refinement starting point — solution content only, never scores, mistakes, or per-example failures from prior loops. 
* Protection must be taken to prevent data leakage among the agents: e.g. an execution agent sees all mistakes from prior loops. Again, the idea is that the strategy agent understands what works and doesn't work, loop-to-loop, keeps a log for itself, and farms out the pure execution to a set of concurrent executor agents. This leakage guarantee applies to *agent-context* prompts (strategy and executor). *Evaluation* prompts — running a solution-under-test or an LLM judge on a single example — necessarily embed that example (whatever its split) and are a separate, sanctioned channel whose outputs return only to the harness as scores. 
* The final loop will produce the best performing coding solution, which should be able to be copied by the user, used outside of this framework, and produce good results for them on new input data. 
* Claude will be called in headless model and without tools (`claude -p --tools ""`) to spin up the above agents.


## Comprehensive implementation plan

See the plan in `PLAN.md`


## Coding style

See the rules and guidelines described in `CODING_STYLE.md`


## Packaging as an open source release

See the `PACKAGING_PLAN.md` file for instructions on how to setup the packaging around this framework. Includes details on dependencies, the `pyproject.toml` file, linting, testing, and deploying to PyPi.