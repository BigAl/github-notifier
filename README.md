# github-notifier

Personal GitHub → Slack DM notifier. Polls the GitHub notifications API every 5
minutes (crontab) and posts filtered items to a Slack DM.

- Scope: `review_requested` only (config.json `"reasons"`)
- Quiet hours: posts Mon–Fri 07:00–18:59 machine-local only; overnight and
  weekend items are delivered at the next in-hours tick (nothing is dropped)
- Dedupe: per-thread `id` + `updated_at`; any further update to a PR you're
  already reviewing is held back for 24h since the last post about that PR
  (commit/comment/CI churn doesn't re-post; a re-request the next day does)
- Runs via the `gh` CLI (`gh api notifications`), so auth is inherited from
  your logged-in `gh` session

## Files

| File | Committed | Purpose |
| --- | --- | --- |
| `notifier.py` | yes | everything: polling, filtering, Slack posting, DM commands |
| `test_notifier.py` | yes | unit tests for the pure logic |
| `config.example.json` | yes | copy to `config.json` and fill in your own IDs |
| `config.json` | no | live config: Slack channel + user IDs, token path |
| `state.json` | no | seen-thread state (dedupe + queue cursor) |
| `notifier.log` | no | one line per cron run |

## Install (Linux)

```
git clone https://github.com/BigAl/github-notifier.git
cd github-notifier
bash install.sh
```

`install.sh` checks for `python3`/`gh`, copies the script to
`~/.local/share/github-notifier`, puts a `github-notifier` command on PATH,
prompts once for the Slack channel ID / user ID / bot-token path, seeds state,
and offers to add the crontab line. Re-run it after a `git pull` to update the
code (your config and state are kept).

Prefer uv (or pipx)? Standard Python package, no dependencies:

```
uv tool install git+https://github.com/BigAl/github-notifier.git    # or: pipx install ...
```

Then create `~/.config/gh-notifier/config.json` (copy of `config.example.json`,
filled in); state and log go to `~/.local/state/gh-notifier/`. `install.sh`
does the same wiring with prompts if you'd rather not hand-edit.

Paths can be moved with `GHN_CONFIG_DIR` / `GHN_STATE_DIR` / `GHN_PREFIX`.

## Setup

1. Create a Slack app with a bot token (`chat:write`, `channels:history` or
   `im:history` for the DM), install it, open a DM with the bot, and put the
   channel ID, your user ID and the token file path in `config.json`.
2. Schedule it (every 5 minutes):
   `*/5 * * * * github-notifier >/dev/null 2>&1`
3. First run seeds state silently; after that new items are posted.

## Run / test

```
github-notifier            # one check (quiet hours respected)
github-notifier --force    # run a check now, ignoring quiet hours
python3 -m pytest test_notifier.py
```

## DM commands

The bot replies in the DM to: `pending`, `mark read`, `pause`, `resume`, `status`.
Commands are read from the DM history and only acted on once.
