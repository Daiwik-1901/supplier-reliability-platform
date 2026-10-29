# AI Usage Disclosure

How AI was used on this project, as required by submission guideline 03. The full prompt
history is in [`PROMPT_HISTORY.md`](PROMPT_HISTORY.md).

## Tools used

- **Claude Code** (VS Code extension). The coordinating session ran on Claude Opus 5.5 (an
  earlier part of the session briefly ran on Sonnet 5.5).
- **Claude Code subagents** dispatched by the coordinating session: for each plan task, one
  implementer, one independent reviewer, and a scoped re-reviewer after each fix round.
  Implementers and reviewers ran on Claude Sonnet 5.5 or Claude Opus 5.5, depending on task
  difficulty. Some commits therefore carry a `Co-Authored-By: Claude Sonnet 5.5` trailer and
  others `Claude Opus 5.5`.

## Where AI accelerated the work

- Turning the brief (and the addendum) into a 27-task implementation plan with explicit
  domain decisions (D1–D13), file layout, interfaces and test lists.
- Writing nearly all code and tests: models and migrations, the deterministic generator,
  the scoring engine, validation rules, anomaly injection and detection, evaluation,
  reporting, the pipeline, the CLI and the API.
- Code review: every task was reviewed by a separate AI reviewer against the plan and the
  spec. Reviews found real defects that were then fixed, for example:
  - a historical-score eligibility gap where shipments recorded after the cutoff could
    change an old score;
  - raw anomaly ids that leaked the ground-truth class to a detector;
  - a `--replace` flow that emptied the database before validating its own safety checks;
  - a job-status write path that could leave a generation job stuck forever.

## Where I relied heavily on AI

- Implementation detail across the whole codebase (about 900 automated tests).
- Running and interpreting test suites, lint and performance measurements.
- About 50 recorded design rulings made by the coordinating AI during execution when the
  plan was ambiguous or contradicted itself (for example switching to trailing 12-month
  scorecard windows because quarterly windows left most suppliers with too little evidence).
  These rulings are listed in the execution ledger and summarised in the README's limitations.

## Decisions I made myself

- Supplying the brief and the addendum, and choosing to adopt the addendum in full after a
  gap analysis.
- Approving the plan and choosing subagent-driven execution.
- Choosing the commit identity and the GitHub account.
- Deciding to submit at this point, with Tasks 22–24 (reproducibility command, full docs
  set, Docker, acceptance-gate runner, consolidated full-scale results) not finished. See
  the README "Status" section.

## What I personally reviewed, changed, validated

> **To be written by the candidate in their own words before final submission.** This
> section must describe what you actually read, ran, questioned or changed. It is
> deliberately not filled in by the AI.
