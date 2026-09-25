# Delegating agent work to a second account

`fleet-agent` runs a task through Claude Code under a second account on the **same machine**. It
sees the calling session's files as they are right now, uncommitted changes included. The work
counts against the second account's plan, not the calling session's.

```bash
fleet-agent "where is the retry policy configured, and what does it default to?"
fleet-agent --write "add a unit test for parse() with empty input and make it pass"
fleet-agent --cwd ~/code/api --model sonnet "summarise what changed on this branch"
echo "long task text..." | fleet-agent -
```

The answer goes to stdout, and a one-line summary to stderr:

```
[fleet-agent · ops@example.com · read-only · 6 turns · 21s · ok]
```

The exit status is non-zero if the agent failed or ran past `--minutes` (default 30). Every run is
appended to `~/.fleet-bridge/delegate.log`.

From inside a Claude Code session, call it through Bash. For parallel or long work, use Bash's
`run_in_background`. Each run starts with no shared context, so put everything it needs in the
prompt.

## Setup

1. Make a token for the second account. Log in to Claude Code as that account, anywhere, and run
   `claude setup-token`. Save the token on the machine that will run delegated work, without it
   ever reaching your shell history:

   ```bash
   umask 077; mkdir -p ~/.fleet-bridge
   read -rs T && printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$T" > ~/.fleet-bridge/delegate.env; unset T
   ```

2. Install, naming the repos whose agent work should go to that account:

   ```bash
   delegate/install.sh --account ops@example.com ~/code/api ~/code/web
   ```

   This installs `fleet-agent` and `agent-guard.sh` to `~/.fleet-bridge/bin`, and writes a
   `delegate` section into `~/.fleet-bridge/config.json`. It also adds one `PreToolUse` hook to
   `~/.claude/settings.json`, backing up the file first to `settings.json.pre-fleet-agent`.

## The guard

In the listed repos, and anything below them (git worktrees under `.claude/worktrees` included),
the guard refuses Claude Code's built-in Agent tool. The refusal reason tells the session to use
`fleet-agent` with the same prompt, so the session reroutes the work on its own. Elsewhere the
guard stays silent.

- **Existing sessions keep the hooks they started with.** Only sessions started after the install
  get the guard. To tell running sessions, use `send.py --local --cwd <repo> "..."`, which sends
  a one-way notice to each live session there.
- **Inside a `fleet-agent` run the guard allows the Agent tool.** That session is already the
  delegate account, so its own subagents run there too.
- **`fleet-agent` refuses to run inside itself.** There's no reason to nest.

## Whole sessions on the delegate account

`fleet-agent` moves agent work. To move whole sessions, every turn and not just subagents, start
them through `delegate-session` and install with `--sessions`:

```bash
delegate/install.sh --account ops@example.com --sessions ~/code/api ~/code/web
```

`delegate-session <claude> [args...]` looks at the directory it starts in. Inside a listed repo,
worktrees included, it gives the session the delegate token. Anywhere else the session starts
unchanged.

- **IDE extension:** set `claudeCode.claudeProcessWrapper` to the full path of
  `~/.fleet-bridge/bin/delegate-session`. The extension runs its wrapper as
  `<wrapper> <real claude> <args>`. If you already have a wrapper, end it with
  `exec ~/.fleet-bridge/bin/delegate-session "$@"` instead of `exec "$@"`.
- **Terminal:** `~/.fleet-bridge/bin/delegate-session claude`.
- **Running sessions keep the account they started with.** To move one, close it and resume it.
  The conversation carries over, and its next turn counts against the delegate account.
- **It fails closed**, like `fleet-agent`. A token file readable by others, or one without a
  token, stops the session from starting. A revoked token fails with 401: Claude Code keeps a
  `CLAUDE_CODE_OAUTH_TOKEN` it was given rather than falling back to the stored login.
- The guard lets these sessions use the Agent tool, since their subagents already run on the
  delegate account.

## Permission modes

| | Tools | Enforced by |
|---|---|---|
| default (read-only) | Read, Grep, Glob, WebFetch, WebSearch, TodoWrite, and Bash limited to `git log/show/diff/status/blame/grep/branch`, `ls`, `rg`, `wc`; Edit, Write and NotebookEdit disallowed | Claude Code's permission layer. `claude -p` can't prompt, so anything else is denied. |
| `--write` | the above plus Edit, Write, NotebookEdit and any Bash command; edits accepted | the same layer, in `acceptEdits` mode |

These were measured, not just configured:
- In a read-only run, Edit, Write and NotebookEdit aren't offered at all. Told to run
  `echo x > file`, the Bash call was denied, the denial shows in the run's
  `permission_denials`, and no file appeared. That test ran without any "you are read-only"
  instruction, so only enforcement could have stopped it.
- With a bogus token in `delegate.env`, the run fails with `401 OAuth access token is invalid`.
  So the delegate token really is the credential in use, with no silent fallback to the
  machine's own login.

Run those two controls again after upgrading Claude Code.

## What the child process gets

- `CLAUDE_CODE_OAUTH_TOKEN` from `delegate.env`. The file must be mode `0600`, or `fleet-agent`
  refuses to run.
- `FLEET_AGENT=1`, which the guard and the relay hook recognise.
- Removed from its environment:
  - `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and the caller's `CLAUDE_CODE_OAUTH_TOKEN`, since
    any of them could take precedence over the delegate token;
  - the caller's inbox (`CLAUDE_CODE_MESSAGING_*`);
  - the IDE link (`CLAUDE_CODE_SSE_PORT`);
  - `CLAUDECODE`.
- `--no-session-persistence`, so delegated runs don't fill `/resume`.
