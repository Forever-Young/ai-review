# ai-review

CLI for running an AI review against a GitHub pull request URL.

It loads pull request metadata, existing PR discussion, review comments, and
the PR diff through `gh`, asks an AI reviewer for inline findings, and can
create a GitHub pending review. When smart review is enabled, it also caches
repositories under `~/.local/cache/ai-review/` and checks out the PR head into a
reusable read-only worktree for extra review context. Pending review comments
are visible only as your unsubmitted review draft until you submit them in
GitHub.

## Requirements

- Python 3.11+
- `git`
- GitHub CLI (`gh`) with pull request read access, either already authenticated
  or configured with a token in `ai-review.conf`
- For posting pending review comments: the GitHub token or `gh` login needs
  pull request write access
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
ssh_key_path =
ssh_command =

[ai]
provider = codex

[codex]
model =

[review]
max_comments = 20
smart_review = no
save_prompt = ~/tmp/ai-review-prompt.txt

[github]
publish_pending = yes
token =

[jira]
enabled = no
```

`provider = codex` uses the local Codex CLI for review generation. Leave
`codex.model` blank to let the local Codex CLI choose its configured default
model.

CLI arguments override the config file.

`smart_review = no` keeps review generation limited to the PR title, PR body,
existing PR conversation comments, existing review comments, optional Jira
context, and the unified diff. In this mode the Codex reviewer runs from an
empty workspace, so repository files are not available to inspect. Set
`smart_review = yes` or pass `--smart-review` to let Codex read files from the
checked-out PR worktree when the diff leaves an important review question
unclear. Smart review asks Codex to use file reads as a targeted fallback, not
as a whole-project investigation, and still runs with a read-only sandbox and
disabled approval prompts.

Set `git.ssh_key_path` when repository mirrors should be cloned and fetched
over SSH with a specific key. When this is set, `ai-review` uses GitHub's SSH
clone URL and passes git a `GIT_SSH_COMMAND` like:

```sh
ssh -i /path/to/key -o IdentitiesOnly=yes -o IdentityAgent=none
```

`IdentitiesOnly=yes` and `IdentityAgent=none` keep SSH from using agent-provided
keys. Set `git.ssh_command` instead when you want to provide the complete
`GIT_SSH_COMMAND` value yourself. Configure either `ssh_key_path` or
`ssh_command`, not both.

`publish_pending = yes` creates an unsubmitted GitHub pending review with draft
inline comments. The review remains editable in GitHub until you submit it. Use
`--no-publish-pending` or set `publish_pending = no` for local output.
When a pending review by the authenticated GitHub user already exists,
`ai-review` replaces that draft with the newly generated review.

Set `github.token` to a GitHub token when `gh` should run non-interactively
without relying on a saved GitHub CLI login. `ai-review` passes the value to
`gh api` as `GH_TOKEN`.

## Jira MCP Context

Jira context is optional and uses the `mcporter` CLI as the MCP bridge. Install
the package extra as a marker for Jira-enabled installs:

```sh
python3 -m pip install ".[jira]"
```

Then install MCPorter itself:

```sh
npm install -g mcporter
mcporter list
```

Configure a Jira MCP server in a config that MCPorter can discover, then enable
Jira context:

```ini
[jira]
enabled = yes
```

Authenticate the configured Jira MCP server:

```sh
ai-review --jira-auth
```

After authentication, MCPorter reuses cached credentials. If Jira is enabled and
a later MCP call fails with an authentication or expired-token error, `ai-review`
runs `mcporter auth jira` and retries the Jira context request. That may open a
browser OAuth page when cached credentials are missing or stale.

When Jira is enabled, `ai-review` extracts a ticket key from the PR branch,
title, body, or first PR comment. It recognizes keys like `ABC-123` and
`ABC12-123`; comments such as `Refs: ABC-123` also work. If a simple scan cannot
find the key, Codex is asked to extract it from the same PR metadata. It then
loads Jira issue description, comments, and attachments/images through the
configured MCPorter Jira server and includes that context in the review prompt
so Codex can flag PR-level gaps as well as inline code issues.

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

Allow Codex to inspect checked-out project files as read-only review context:

```sh
ai-review https://github.com/OWNER/REPO/pull/123 --smart-review
```

## Safety

By default the command creates a pending review with inline comments and no
submit event. You still review, edit, delete, and submit the comments in the
GitHub UI.
