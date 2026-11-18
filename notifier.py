"""GitHub -> Slack notifier.

Polls the GitHub notifications API and posts filtered items
(my PRs, review requests on me, @-mentions, team mentions,
security alerts) to a Slack DM.

Run: notifier.py            (cron runs this every 5 minutes; posts Mon-Fri 07:00-18:59 local only)
     notifier.py --force    (run a check now, ignoring quiet hours)
Tests: python3 -m pytest test_notifier.py

Notifies on review requests only (config.json "reasons"); other categories are skipped.
"""
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = pathlib.Path(__file__).resolve().parent


def _xdg_dir(env_name, default):
    """Directory from env override, else an XDG-style default."""
    if os.environ.get(env_name):
        return pathlib.Path(os.environ[env_name]).expanduser()
    return pathlib.Path(default).expanduser()


# Legacy layout (config.json beside the script) keeps everything beside the
# script; otherwise config lives under ~/.config and state/log under
# ~/.local/state (or their XDG overrides).
_LEGACY = (BASE / "config.json").exists()
CONFIG_DIR = BASE if _LEGACY else _xdg_dir("GHN_CONFIG_DIR", "~/.config/gh-notifier")
STATE_DIR = (
    BASE if _LEGACY
    else _xdg_dir("GHN_STATE_DIR", os.path.join(
        os.environ.get("XDG_STATE_HOME", "~/.local/state"), "gh-notifier"))
)

CONFIG_PATH = str(CONFIG_DIR / "config.json")
STATE_PATH = str(STATE_DIR / "state.json")
LOG_PATH = str(STATE_DIR / "notifier.log")

STATE_MAX_KEYS = 2000


# ---------- pure logic (covered by test_notifier.py) ----------

def filter_threads(threads, allowed_reasons):
    """Keep only notification threads whose reason is allowed."""
    return [t for t in threads if t.get("reason") in allowed_reasons]


def thread_key(thread):
    """Identity of a thread state: same thread + same updated_at == same key."""
    return "%s@%s" % (thread.get("id"), thread.get("updated_at"))


def is_new(thread, seen):
    """True if this thread state has not been posted before."""
    return thread_key(thread) not in seen


LABELS = {
    "author": "🚨 *MY PR* — ",
    "review_requested": "🔍 *REVIEW REQUESTED* — ",
    "mention": "📣 *MENTION* — ",
    "team_mention": "👥 *TEAM MENTION* — ",
    "security_alert": "🛡 *SECURITY ALERT* — ",
}


def api_to_html_url(url):
    """Rewrite api.github.com URLs to browser github.com URLs."""
    prefix = "https://api.github.com/"
    if not url.startswith(prefix):
        return url
    parts = url[len(prefix):].split("/")
    # repos/{owner}/{repo}/pulls/{n} -> {owner}/{repo}/pull/{n}
    if len(parts) == 5 and parts[0] == "repos":
        if parts[3] == "pulls":
            parts[3] = "pull"
        return "https://github.com/" + "/".join(parts[1:])
    return "https://github.com/" + "/".join(parts)


def subject_url(thread):
    """Browser URL for a notification subject (API supplies api.github.com URLs)."""
    subject = thread.get("subject", {})
    return api_to_html_url(subject.get("html_url") or subject.get("url", ""))


def build_message(thread):
    """Slack message text for one notification thread."""
    repo = thread.get("repository", {}).get("full_name", "?")
    subject = thread.get("subject", {})
    label = LABELS.get(thread.get("reason"), "ℹ️ *GITHUB* — ")
    return "%s%s\n%s\n%s" % (label, repo, subject.get("title", ""), subject_url(thread))


def collect_messages(threads, seen, cap):
    """Messages to post this run: each new thread, capped, plus a summary line for overflow."""
    fresh = [t for t in threads if is_new(t, seen)]
    if not fresh:
        return []
    posted = [build_message(t) for t in fresh[:cap]]
    if len(fresh) > cap:
        posted.append("⏩ %d more — https://github.com/notifications" % (len(fresh) - cap))
    return posted


# A quiet night/weekend leaves the cursor idle for days; do not reseed on it
# or Monday morning's buffered backlog would be silently dropped.
# (Fri 19:00 -> Mon 07:00 is ~60h; 8 days leaves margin. A box off longer
#  than this reseeds and loses the queue - acceptable.)
STALE_AFTER_SECONDS = 8 * 24 * 3600

# Quiet hours (archie-style): Mon-Fri 07:00-18:59, machine local time.
WORK_HOUR_MIN = 7
WORK_HOUR_MAX = 18  # inclusive hour, so 18:00-18:59 still counts


def decide_action(state, now):
    """What this run should do.

    ("seed", None)    - first run: record current queue, post nothing
    ("reseed", None)  - cursor too old while not paused: treat like first run
    ("run", since)    - normal poll from the last successful check
    """
    if state is None or not state.get("last_success"):
        return ("seed", None)

    def parse(ts):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    now_dt = parse(now)
    cursor = parse(state["last_success"])
    if not state.get("paused") and (now_dt - cursor).total_seconds() > STALE_AFTER_SECONDS:
        return ("reseed", None)
    return ("run", state["last_success"])


def in_work_hours(now=None):
    """True inside Mon-Fri WORK_HOUR_MIN..WORK_HOUR_MAX:59, machine local time."""
    if now is None:
        now = datetime.now()
    return now.weekday() <= 4 and WORK_HOUR_MIN <= now.hour <= WORK_HOUR_MAX


COMMANDS = {
    "pending": "pending",
    "mark read": "mark_read",
    "pause": "pause",
    "resume": "resume",
    "status": "status",
}


def parse_command(text):
    """Normalize a DM message into a command name (unknown/empty -> help)."""
    norm = " ".join((text or "").lower().split())
    return COMMANDS.get(norm, "help")


REASON_NAMES = {
    "author": "your PR",
    "review_requested": "review request",
    "mention": "mention",
    "team_mention": "team mention",
    "security_alert": "security alert",
}


def format_pending_report(threads, cap=10):
    """Compact multi-item report of the current notification queue."""
    if not threads:
        return "✅ Nothing pending — your GitHub queue is clear"
    counts = {}
    for t in threads:
        r = t.get("reason")
        counts[r] = counts.get(r, 0) + 1
    summary = " · ".join(
        "%d %s%s" % (n, REASON_NAMES.get(r, r), "s" if n > 1 else "")
        for r, n in counts.items()
    )
    lines = ["*Pending (%d):* %s" % (len(threads), summary)]
    for t in threads[:cap]:
        icon = LABELS.get(t.get("reason"), "ℹ️").split(" ")[0]
        repo = t.get("repository", {}).get("full_name", "?")
        subject = t.get("subject", {})
        lines.append("%s %s — %s %s" % (icon, subject.get("title", ""), repo, subject_url(t)))
    if len(threads) > cap:
        lines.append("…and %d more on https://github.com/notifications" % (len(threads) - cap))
    return "\n".join(lines)


def format_help():
    return (
        "*GitHub notifier commands* — replies within ~5 min\n"
        "`pending` — what's waiting on you\n"
        "`mark read` — clear your GitHub notification bell\n"
        "`pause` / `resume` — buffer notifications; backlog is delivered on resume\n"
        "`status` — notifier state\n"
        "Anything else → this help."
    )


# ---------- I/O ----------

def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    print(line)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    state["seen"] = state["seen"][-STATE_MAX_KEYS:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_threads(since=None):
    """Fetch ALL notification threads via the gh CLI (reuses its keyring auth).

    without since: the full current queue (used for seeding)
    with since:    everything updated after the cursor
    """
    url = "notifications?all=true&per_page=100"
    if since:
        url += "&since=" + urllib.parse.quote(since)
    proc = subprocess.run(
        ["gh", "api", url], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError("gh api failed: %s" % proc.stderr.strip())
    return json.loads(proc.stdout)


def fetch_unread():
    """Unread notification threads only (the API default)."""
    proc = subprocess.run(
        ["gh", "api", "notifications?per_page=100"], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError("gh api failed: %s" % proc.stderr.strip())
    return json.loads(proc.stdout)


def slack_post(token, channel, text):
    data = urllib.parse.urlencode({"token": token, "channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=data, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError("slack post failed: %s" % body.get("error"))


def read_token(path):
    with open(path) as f:
        return f.read().strip()


def select_commands(messages, user_id):
    """Pick user messages from a history payload.

    User messages posted via API carry a bot_id, so we filter on user
    identity only — the notifier bot's own replies have a different user.
    """
    return [
        (m["ts"], m.get("text", ""))
        for m in sorted(messages, key=lambda m: float(m.get("ts", 0)))
        if m.get("user") == user_id
    ]


def read_dm_commands(token, channel, user_id, oldest=None):
    """User messages in the DM since oldest (ts string); returns [(ts, text)]."""
    params = {"token": token, "channel": channel, "limit": "100"}
    if oldest:
        params["oldest"] = oldest
    # POST form, not GET query: this environment rejects query-string tokens
    req = urllib.request.Request(
        "https://slack.com/api/conversations.history",
        data=urllib.parse.urlencode(params).encode(), method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError("history failed: %s" % body.get("error"))
    return select_commands(body.get("messages", []), user_id)


def get_last_modified():
    """X-GitHub-Last-Modified header from the notifications list call."""
    proc = subprocess.run(
        ["gh", "api", "notifications", "-i"], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError("gh api -i failed: %s" % proc.stderr.strip())
    headers, _, _ = proc.stdout.partition("\n\n")
    for line in headers.splitlines():
        if line.lower().startswith("x-github-last-modified:"):
            return line.split(":", 1)[1].strip()
    return None


def mark_all_read():
    """Mark all GitHub notifications read; returns how many were unread."""
    unread = fetch_unread()
    lm = get_last_modified()
    if lm is None:
        return 0
    proc = subprocess.run(
        ["gh", "api", "-X", "PUT", "notifications",
         "-f", "all=true", "-f", "read=true",
         "-H", "X-GitHub-Last-Modified: " + lm],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError("mark read failed: %s" % proc.stderr.strip())
    return len(unread)


def handle_command(cmd, state, config):
    """Reply text for a parsed command."""
    if cmd == "pending":
        threads = filter_threads(fetch_unread(), config["reasons"])
        threads.sort(key=lambda t: t.get("updated_at", ""), reverse=True)
        return format_pending_report(threads)
    if cmd == "mark_read":
        return "✅ Marked %d notification(s) as read" % mark_all_read()
    if cmd == "pause":
        state["paused"] = True
        return "⏸️ Paused — new items are buffered and delivered when you resume"
    if cmd == "resume":
        state["paused"] = False
        return "▶️ Resumed — any buffered items will be delivered on the next check"
    if cmd == "status":
        mode = "⏸️ paused" if state.get("paused") else "▶️ running"
        return "%s · last check %s · unread now: %d" % (
            mode, state.get("last_success", "never"), len(fetch_unread()))
    return format_help()


def main():
    for d in (CONFIG_DIR, STATE_DIR):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        print("No config at %s - copy config.example.json there or run install.sh"
              % CONFIG_PATH, file=sys.stderr)
        return 2
    config = load_config()
    state = load_state() or {"seen": []}
    if "--force" not in sys.argv and not in_work_hours():
        log("quiet hours: skipping check")
        return 0
    now = now_iso()
    action, since = decide_action(state, now)
    paused = bool(state.get("paused"))
    token = read_token(config["token_file"])
    failures = 0

    # 1) GitHub notifications
    try:
        threads = fetch_threads(since)
    except Exception as e:
        log("ERROR fetch: %s" % e)
        return 1
    allowed = filter_threads(threads, config["reasons"])
    fresh = [t for t in allowed if is_new(t, set(state["seen"]))]

    if action in ("seed", "reseed"):
        state["seen"] = list(set(state["seen"]) | {thread_key(t) for t in allowed})
        state["last_success"] = now
        save_state(state)
        log("%s: recorded %d threads, posted nothing" % (action, len(allowed)))
    elif paused:
        if fresh:
            log("paused: %d new buffered, cursor not advanced" % len(fresh))
        save_state(state)
    else:
        messages = collect_messages(allowed, set(state["seen"]), config["max_messages"])
        posted = 0
        for i, msg in enumerate(messages):
            try:
                slack_post(token, config["channel"], msg)
                posted += 1
                if i < len(fresh):
                    state["seen"].append(thread_key(fresh[i]))
            except Exception as e:
                failures += 1
                log("ERROR post: %s" % e)
        if failures == 0:
            state["last_success"] = now
        save_state(state)
        log("run: %d new, posted %d, failed %d" % (len(fresh), posted, failures))

    # 2) DM commands — processed even while paused (that's how you resume)
    try:
        commands = read_dm_commands(token, config["channel"], config["user_id"],
                                    state.get("last_cmd_ts"))
        for ts, text in commands:
            cmd = parse_command(text)
            reply = handle_command(cmd, state, config)
            slack_post(token, config["channel"], reply)
            state["last_cmd_ts"] = ts
            log("cmd %r -> ok" % text[:40])
        save_state(state)
    except Exception as e:
        log("ERROR commands: %s" % e)
        failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
