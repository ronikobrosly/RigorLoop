# Security Policy

## Supported versions

Only the latest release on PyPI is supported with fixes.

## Reporting a vulnerability

Please report vulnerabilities privately via GitHub security advisories:
**Security → Report a vulnerability** on this repository. Do not open a public
issue for anything exploitable.

## Things you should know before running RigorLoop

RigorLoop **executes generated code**. Candidate solutions produced by agents
(and any `custom_python` checks you configure) run as subprocesses on your
machine with your user's permissions. The built-in mitigations — hard
timeouts, output caps, a scratch working directory, no stdin inheritance —
are guardrails against runaway code, **not a security boundary**. If your
threat model includes actively malicious generated code, run RigorLoop inside
a container or VM. OS-level sandboxing is on the roadmap.

RigorLoop also sends your task description and **dev-split** example content
to the configured model via the `claude` CLI (validation/test examples are
additionally embedded, one at a time, in evaluation calls). Do not put data in
your examples file that must not leave your machine.
