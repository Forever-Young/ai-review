# ai-review

`ai-review` is a Python CLI package for running AI-assisted reviews of GitHub
pull requests from a PR URL. It caches repository mirrors and PR worktrees,
collects the PR diff through `gh`, optionally loads Jira ticket context through
MCPorter, asks the configured AI provider for structured findings, validates
inline findings against changed diff lines, and can create an unsubmitted
GitHub pending review for the user to edit before submission.

## Project Conventions

- Keep the package stdlib-only unless a dependency has a clear operational
  benefit.
- The installed CLI entry point is `ai-review`; `python -m ai_review` should
  keep working too.
- Configuration is read from `~/.config/ai-review/ai-review.conf`, with CLI
  arguments taking precedence.
- The default cache directory is `~/.local/cache/ai-review`.
- `ai.provider` is a provider type. The only supported type for now is `codex`.
- Keep Codex-specific options under the `[codex]` config section, including
  `model`.
- Do not add a generic shell-command AI provider unless the project direction
  explicitly changes.
- Jira support is optional context loading through `mcporter`; keep it separate
  from the AI provider implementation.
- The public Jira configuration should stay enabled/disabled only:
  `[jira] enabled = yes|no`. Do not expose MCP tool selectors in the user config
  unless the project direction explicitly changes.
- Jira auth is handled through `mcporter auth jira`. If Jira MCP calls fail with
  stale or missing auth, the CLI should authenticate once and retry the failed
  context request once.
- The review result supports both inline `comments` and PR-level `pr_comments`.
  Use PR-level comments for ticket-scope gaps or broad findings that do not map
  to a changed diff line.

## Verification

- Run `python3 -m unittest discover` after code changes.
- Run `python3 -m py_compile ai_review/cli.py ai_review/__main__.py tests/test_cli.py`
  after CLI or packaging changes.
- For packaging changes, verify `pip install .` in a temporary virtualenv and
  confirm `ai-review --help` works.

## Git

Use Conventional Commits for commit messages, for example:

- `feat: add pending review publishing`
- `fix: validate generated comment lines`
- `docs: document configuration file`
