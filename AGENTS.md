# ai-review

`ai-review` is a Python CLI package for running AI-assisted reviews of GitHub
pull requests from a PR URL. It caches repository mirrors and PR worktrees,
collects the PR diff through `gh`, asks the configured AI provider for
structured inline findings, validates those findings against changed diff lines,
and can create an unsubmitted GitHub pending review for the user to edit before
submission.

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
