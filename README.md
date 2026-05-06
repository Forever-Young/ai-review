# ai-review

CLI for running an AI review against a GitHub pull request URL.

It caches repositories under `~/.local/cache/ai-review/` by default, checks out the PR
head into a reusable worktree, asks an AI reviewer for inline findings, and can
create a GitHub pending review. Pending review comments are visible only as your
unsubmitted review draft until you submit them in GitHub.

## Requirements

- Python 3.11+
- `git`
- GitHub CLI (`gh`) authenticated with pull request read access
- For posting pending review comments: `gh` needs pull request write access
- `codex` CLI logged in and usable non-interactively

## Install

From this repository:

```sh
python3 -m pip install .
```

For editable local development:

```sh
python3 -m pip install -e .
```

After installation, run:

```sh
ai-review https://github.com/OWNER/REPO/pull/123
```

## Configuration

The default config file is:

```text
~/.config/ai-review/ai-review.conf
```

If the file does not exist, `ai-review` creates it on startup, even when no PR
URL is supplied. Config-related CLI flags used on that first invocation are
written into the new file; existing config files are not rewritten.

Example:

```ini
[git]
cache_dir = ~/.local/cache/ai-review

[ai]
provider = codex

[codex]
model =

[review]
max_comments = 20
save_prompt = ~/tmp/ai-review-prompt.txt

[github]
publish_pending = yes
```

`provider = codex` is the default and currently the only supported provider
type. Leave `codex.model` blank to let the local Codex CLI choose its configured
default model.

CLI arguments override the config file.

`publish_pending = yes` means `ai-review` creates an unsubmitted GitHub pending
review with draft inline comments. It never submits the review; you still edit,
delete, or submit comments yourself in GitHub. Use `--no-publish-pending` or set
`publish_pending = no` for local output only.

## Usage

Create pending GitHub review comments:

```sh
ai-review https://github.com/OWNER/REPO/pull/123
```

Preview findings locally without writing to GitHub:

```sh
ai-review https://github.com/OWNER/REPO/pull/123 --no-publish-pending
```

Use a custom cache directory:

```sh
ai-review https://github.com/OWNER/REPO/pull/123 --cache-dir /path/to/review-cache
```

Use a specific Codex model:

```sh
ai-review https://github.com/OWNER/REPO/pull/123 --codex-model gpt-5.2
```

## Safety

By default the command creates a pending review with inline comments and no
submit event. You still review, edit, delete, and submit the comments in the
GitHub UI.
