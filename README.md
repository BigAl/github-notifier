# github-notifier

Personal GitHub → Slack DM notifier. Polls the GitHub notifications API every 5
minutes (crontab) and posts filtered items to a Slack DM.

- Scope: `review_requested` only (config.json `"reasons"`)
- Quiet hours: posts Mon–Fri 07:00–18:59 machine-local only; overnight and
  weekend items are delivered at the next in-hours tick (nothing is dropped)
- Dedupe: per-thread `id` + `updated_at`, so a re-requested PR re-notifies
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

## Setup

1. Build against the Slack API: create a Slack app with a bot token (`chat:write`,
   `channels:history` or `im:history` for the DM), install it, open a DM with the
   bot, and put the channel ID, your user ID and the token file path in
   `config.json`.
2. Schedule it (every 5 minutes):
   `*/5 * * * * /usr/bin/python3 /path/to/github-notifier/notifier.py >/dev/null 2>&1`
3. First run seeds state silently; after that new items are posted.

## Run / test

```
python3 notifier.py            # one check (respect quiet hours)
python3 notifier.py --force    # run a check now, ignoring quiet hours
python3 -m pytest test_notifier.py
```

## DM commands

The bot replies in the DM to: `pending`, `mark read`, `pause`, `resume`, `status`.
Commands are read from the DM history and only acted on once.
