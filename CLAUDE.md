<!-- agent-rules:
source=https://github.com/jwsung91/agent-rules
profile=all
source_ref=main
source_commit=78385284ebadc0c71ea60d04354e011a85b85d8e
generated_at=2026-10-06T09:37:48+00:00
managed_block=true
-->

# platmon

Lightweight platform monitor (board/system status) for Jetson, Raspberry Pi, PCs and other Linux hosts. Personal project, licensed Apache-2.0.

## Dependency rules

- Allowed licenses only: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0.
- Forbidden: GPL, LGPL, AGPL (e.g. jetson-stats/jtop), unlicensed or unknown-license code.
- Check the license before adding any dependency, including transitive ones. Do not copy code from forbidden sources.
- Read system data directly from `/proc`, `/sys`, or `tegrastats` output. Call `tegrastats` at runtime; never bundle NVIDIA binaries.
- Containers: ship only the `deploy/docker/Dockerfile` and compose files; users build the image themselves. Never publish images (registry pushes, CI publishing): every base image carries GPL/LGPL userland. Use a plain Python base, never an NVIDIA L4T image. See README "Container image licensing".

## Scope

- Keep it generic. No company-, AMR-, or customer-specific code, configs, or names.
- "NVIDIA" / "Jetson" only to describe compatibility, never as the product name.

<!-- agent-rules-managed:start -->

This is the Claude instruction entrypoint for this repository.

## Agent Usage Model

Claude may operate in either mode:

- **Primary Mode**: implementation, documentation update, investigation, or refactoring.
- **Review Mode**: cross-check, risk analysis, scope review, and validation gap review.

Use the mode requested by the task.

## Core Rules

- Investigate existing code, documentation, and behavior before editing.
- Keep changes scoped to the requested task.
- Preserve the agreed objective, authorized scope, and completion criteria across follow-ups. A status question does not cancel ongoing work; a short continuation resumes the agreed next step without expanding authorization.
- Separate explicitly requested additional work into stages or commits and keep it in the task; do not silently discard it as scope creep. Unrequested work stays out.
- For long-running work, retain a compact checkpoint of the repository, branch, execution environment, decisions, remaining steps, and latest evidence. Verify it against current state before resuming.
- Before performance experiments, define the baseline, target, allowed regressions, and experiment budget; stop or reassess when the budget is spent or evidence rejects the hypothesis.
- Before publishing a PR or release, inspect the destination repository's template and required submission procedure. Before merging, verify the intended head, required checks, and merge result.
- Never bypass commit signing, hooks, or required checks merely to make progress. Use an authorized working path or report the blocker; bypass requires an explicit user request.
- Do not refactor unrelated files, or rename public APIs, files, directories, or user-facing concepts, unless explicitly requested.
- Prefer simple, explicit, maintainable changes.
- After understanding the problem, prefer existing code, standard libraries, native platform features, and installed dependencies before new code, when they satisfy the required behavior and project conventions.
- Simplify implementation without dropping agreed requirements, compatibility, safety controls, or risk-appropriate validation; readable code matters more than minimum line counts.
- Preserve existing structure, naming, and documentation tone.
- Avoid new dependencies unless they have a clear, task-specific justification.
- Follow repository-local formatter, linter, test, PR template, and verification conventions.
- Consider risks, compatibility concerns, and validation gaps appropriate to the task.
- Ask for clarification before proceeding when scope is ambiguous, instructions conflict, or a destructive action lacks explicit authorization.

## Commit Messages

Use Conventional Commits:

```text
<type>[optional scope]: <description>
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `style`, `perf`, `build`, `ci`, `chore`.

Use `!` or a `BREAKING CHANGE:` footer for compatibility-breaking changes.
Keep the subject concise, lowercase, imperative mood, no trailing period.

## Shared Skills

- This repository installs shared skills under `.claude/skills/`: `investigate-bug`, `review-change`, `validate-change`, `prepare-commit`.
- When a message reports a bug or unexpected behavior, invoke the `investigate-bug` skill before planning any fix — even when the same message also requests unrelated work such as refactoring, new tests, or cleanup. Investigate the bug first and keep its fix focused. Preserve other explicitly requested work as separate authorized stages or commits; do not silently drop it or ask for repeated approval merely because it is separate. Unrequested work remains out of scope.
- When asked to review code, documentation, a diff, working tree, commit, branch, patch, pull request, or completed implementation, invoke the `review-change` skill before reporting findings. Stay in Review Mode and do not modify files unless the user separately authorizes changes. If the requested review target cannot be inspected, report the review as blocked; never substitute a different accessible branch, pull request, commit, repository, or remote target.
- When asked to validate, test, verify, check, or perform pre-commit verification of an existing change, invoke the `validate-change` skill before running checks. Keep validation focused and non-mutating, record the initial worktree state, report exact commands and outcomes, and identify any validation-created changes without deleting or reverting them unless separately authorized.
- When asked to commit, prepare or stage a commit, or write a commit message for the current changes, invoke the `prepare-commit` skill before committing. Review the diff, commit only the requested logical change, run lightweight pre-commit checks including `git diff --check`, and write a Conventional Commits message. Do not amend or rewrite history, reformat code, or include unrelated changes unless separately authorized. Never bypass signing, hooks, or required checks without an explicit user request.
- When a request could match more than one shared skill's trigger (for example, reviewing a pull request that fixes a bug), prioritize `review-change` if the primary ask is judging the quality of an existing change, diff, or pull request; use `investigate-bug` if the primary ask is reproducing or root-causing a defect that has no fix yet; use `validate-change` if the primary ask is executing checks and reporting validation evidence for an existing change; use `prepare-commit` if the primary ask is composing a commit for the current changes. Report defects found while reviewing within `review-change`'s structure unless the user separately asks for a fix. Validation may support either workflow without replacing its primary purpose, and prepare-commit's lightweight pre-commit checks do not replace a full `validate-change` or `review-change` pass.

<!-- agent-rules-managed:end -->

## Repository-specific Boundaries

<!-- TODO(agent-rules): add repository-specific repository-specific boundaries guidance. -->

## Validation

- Run the narrowest relevant checks first.
- Add or update tests when behavior changes; explain when not.
- Do not claim validation was run if it was not.
- Before committing, run at minimum: `git diff --check`.
- Use resource-safe parallelism: prefer `-j2` by default, `-j1` under memory pressure or resource-constrained environments (e.g., WSL, VMs).

<!-- agent-rules-local:validation_commands:start -->
Confirmed for this repository:

```bash
git diff --check
pytest            # all tests (tests/, configured in pytest.ini); CI runs the same on Python 3.9 and 3.13
```
<!-- agent-rules-local:validation_commands:end -->

Report validation using this format:

```text
Validation:
- [x] Ran: ...
- [ ] Not run: ... because ...
- Tests: added / updated / not needed / not added because ...
- Documentation: updated / not needed / not updated because ...
```

## Final Report

For simple questions and progress updates, answer directly without mandatory headings.
For completed implementation or review reports, use the structure below. For PRs,
follow the destination repository template when one exists.

Before sending the response, verify that these Markdown headings appear verbatim, exactly once, and in this order; do not rename, omit, or combine them. Additional sections may appear after `## Changes` and before `## Validation`.

1. `## Summary`
2. `## Changes`
3. `## Validation`
4. `## Not Included`
5. `## Follow-up`

- **Summary**: what changed and why; begin with progress against the agreed plan
- **Changes**: files and behaviors affected
- **Validation**: what was run and results
- **Not Included**: what was intentionally left out
- **Follow-up**: known gaps or deferred work

For multi-step work, report completed stages out of the agreed total, the current
stage, remaining requested work, and blockers (or none) near the start of the
report, within Summary when that heading is required. Use meaningful stages and
completion evidence; do not invent percentages or split stages to inflate progress.
Stage counts describe scope completion, not elapsed time or effort. Include
requested PR, merge, or deployment steps before calling the whole task complete.
When additional requests change the plan, state the change and update the total;
do not silently drop unfinished work or count optional suggestions as requested work.
For small tasks, one sentence stating completion and remaining work is enough.
Progress reporting does not create a new approval gate or authorize extra actions.
