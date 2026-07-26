# Contributing

Thanks for taking a look. This started as a one-person tool for tracking blood pressure in a case that needs per-person calibration, so the bar for contributing is "does this make the tool better or more correct," not "does this match some broader roadmap."

## Before you open a PR

| Type of change | What to do |
|---|---|
| Bug fixes, docs fixes, test additions | Just send the PR. No need to ask first. |
| New features | Open an issue first describing what you want to add and why. This is a small project with a specific real-world use case driving it; some features that seem obviously useful might not fit, and I'd rather tell you that before you write the code than after. |
| Signal processing or BP estimation math | Include your reasoning and, if possible, a reference (paper, existing implementation, etc.). This code affects what numbers a real person sees about their own blood pressure, so I'd rather have a slower review than a subtle bug that produces a plausible-looking wrong number. |

## Development setup

See the repo's README for the current setup instructions (dependencies, running tests, etc.). If you hit a setup problem the README doesn't cover, that's itself worth a PR to fix the README.

## Code style

Match the existing style in the file you're editing over any personal preference. For Python, use type hints on public functions and docstrings on anything non-obvious; there's no enforced formatter yet, so keep diffs focused. For Kotlin, follow the existing conventions in the file for naming, coroutine usage, and null-handling. Comments should explain why, not restate what the code does.

## Testing

If you're fixing a bug, add a test that would have caught it if one doesn't already exist. If you're adding a feature, add tests for the core logic (UI-only changes are more lenient). Run the existing test suite before opening a PR, and if a test is flaky or wrong, say so in the PR rather than silently working around it.

## What I will probably push back on

Adding a new dependency for something a few lines of code could do. Anything that makes the "single patient, self-hosted" happy path more complicated to support a multi-user or hosted-SaaS use case nobody's asked for yet. And silent fallback behavior on sensor or data errors: this project has already had one silent-failure bug in production, a sensor stream that died without logging anything. Prefer loud failures with good log messages over graceful-looking degradation that hides a problem.

## Reporting bugs

Open an issue describing what you expected versus what happened, steps to reproduce if you have them, and logs if relevant (strip out anything personal first, such as device IDs or tokens).

If you've found something that could cause silent data loss or a wrong BP number without any visible error, flag that clearly in the issue title. That class of bug is the highest priority in this project.
