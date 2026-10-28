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
- one AI provider CLI logged in and usable non-interactively:
  - `claude`
  - `codex`
  - `cursor` (via `cursor-agent`)
  - `gemini`

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
written into the new file. Existing config files are updated on startup with
missing default sections and keys while preserving configured values.

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

[cursor]
model =

[gemini]
model =

[claude]
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

[telegram]
bot_token =
authorized_user_id =
jira_auth_via_telegram = no
```

`ai.provider` selects which local CLI is used for review generation:
`claude`, `codex`, `cursor`, or `gemini`.

- `claude.model` maps to `claude --model ...`
- `codex.model` maps to `codex exec --model ...`
- `cursor.model` maps to `cursor-agent --model ...`
- `gemini.model` maps to `gemini --model ...`

Leave provider model values blank to let each CLI use its own default.

CLI arguments override the config file.

`smart_review = no` keeps review generation limited to the PR title, PR body,
existing PR conversation comments, existing review comments, optional Jira
context, and the unified diff. In this mode the selected AI provider runs from an
empty workspace, so repository files are not available to inspect. Set
`smart_review = yes` or pass `--smart-review` to let the provider read files from the
checked-out PR worktree when the diff leaves an important review question
unclear. Smart review asks the selected AI provider to use file reads as a targeted fallback, not
as a whole-project investigation, and still runs with a read-only sandbox and
disabled approval prompts.

Set `git.ssh_key_path` when repository mirrors should be cloned and fetched
over SSH with a specific key. The same SSH settings apply to Git LFS downloads
during PR worktree creation. When this is set, `ai-review` uses GitHub's SSH
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

## Telegram Bot

Telegram bot support is optional:

```sh
python3 -m pip install ".[bot]"
```

Run the bot with the separate `bot` subcommand:

```sh
ai-review bot
```

Create a bot token in Telegram by opening `@BotFather`, running `/newbot`, and
following its prompts. Copy the HTTP API token into the config file:

```ini
[telegram]
bot_token = 123456789:example-token-from-botfather
authorized_user_id =
jira_auth_via_telegram = no
```

`authorized_user_id` is the Telegram user ID allowed to run reviews. Leave it
blank for the first run, send any message to the bot, and the bot replies with
your Telegram user ID. Put that number in `authorized_user_id` and restart the
bot. Once `authorized_user_id` is configured, messages from other Telegram users
are ignored with no reply.

After authorization is configured, send the bot a GitHub PR URL. It runs the
same review flow as the CLI using the same config file, including GitHub,
selected AI provider, Jira, smart review, and pending review settings. The bot edits one
progress message while the review runs. On success, the final chat message is
only the GitHub pending review link. On failure, the final chat message is the
error instead.

When you send a non-PR message, the bot shows inline provider buttons with the
label `choose model or paste PR link`. The selected provider is marked with
`[current]`. Provider buttons are shown only for provider CLIs that are present
in `PATH` on the bot host (`claude`, `codex`, `cursor-agent`, `gemini`). Selection is chat
state only and does not modify `ai-review.conf`.

`jira_auth_via_telegram = no` keeps Jira reauthentication on the bot host, which
matches CLI behavior. Set it to `yes` to let the bot stream MCPorter auth output
to Telegram when a Jira MCP call reports an auth error. If MCPorter or the Jira
provider prints a URL or code that can be completed from another device, open it
and finish the auth flow, then the review retries the failed Jira request once.
If the provider requires a browser redirect to localhost on the bot machine,
remote Telegram-only reauthentication is not possible for that provider; the bot
will leave the MCPorter error in the chat.

### macOS LaunchAgent install

The repository includes a macOS installer for the bot service:

```sh
scripts/install-telegram-bot-macos.sh
```

It creates a virtualenv at `~/.local/share/ai-review-bot/venv`, installs
`ai-review[bot]`, creates or updates `~/.config/ai-review/ai-review.conf` with
default keys, and writes a LaunchAgent plist at:

```text
~/Library/LaunchAgents/com.ai-review.telegram-bot.plist
```

After filling `telegram.bot_token`, start the service:

```sh
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.ai-review.telegram-bot.plist"
launchctl kickstart -k gui/$(id -u)/com.ai-review.telegram-bot
```

After setting `telegram.authorized_user_id`, restart the service:

```sh
launchctl kickstart -k gui/$(id -u)/com.ai-review.telegram-bot
```

Logs are written to `~/Library/Logs/ai-review/telegram-bot.out.log` and
`~/Library/Logs/ai-review/telegram-bot.err.log`.

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
title, body, PR comments written by the PR author, and PR commit messages. It
recognizes keys like `ABC-123` and `ABC12-123`; comments such as `Refs: ABC-123`
also work. If a simple scan cannot
find the key, the selected AI provider is asked to extract it. If a parsed key is
found but Jira reports it missing, `ai-review` retries once with the AI-extracted
key. It then loads Jira issue description, comments, and attachments/images
through the configured MCPorter Jira server and includes that context in the
review prompt so the provider can flag PR-level gaps as well as inline code issues.

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

Use a specific provider and model:

```sh
ai-review https://github.com/OWNER/REPO/pull/123 --ai-provider codex --codex-model gpt-5.2
ai-review https://github.com/OWNER/REPO/pull/123 --ai-provider cursor --cursor-model any-supported-cursor-model
ai-review https://github.com/OWNER/REPO/pull/123 --ai-provider gemini --gemini-model gemini-2.5-pro
ai-review https://github.com/OWNER/REPO/pull/123 --ai-provider claude --claude-model sonnet
```

Allow the selected provider to inspect checked-out project files as read-only review context:

```sh
ai-review https://github.com/OWNER/REPO/pull/123 --smart-review
```

## Safety

By default the command creates a pending review with inline comments and no
submit event. You still review, edit, delete, and submit the comments in the
GitHub UI.
