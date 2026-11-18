"""Tests for the GitHub -> Slack notifier core logic.

Run: python3 -m pytest test_notifier.py -v
"""
import pytest

from datetime import datetime

from notifier import (
    filter_threads, is_new, thread_key, build_message, collect_messages,
    decide_action, parse_command, format_pending_report, format_help,
    select_commands, in_work_hours,
)


def make_thread(
    reason="author",
    id="abc",
    updated="2026-11-04T03:00:00Z",
    repo="acme/widgets",
    title="feat: x",
    url="https://github.com/acme/widgets/pull/1",
    subject_type="PullRequest",
):
    return {
        "id": id,
        "reason": reason,
        "updated_at": updated,
        "unread": True,
        "subject": {"type": subject_type, "title": title, "url": url},
        "repository": {"full_name": repo},
    }


ALLOWED = ["author", "review_requested", "mention", "team_mention", "security_alert"]


def test_filter_keeps_allowed_reasons():
    threads = [
        make_thread(reason="author"),
        make_thread(reason="review_requested", id="b"),
        make_thread(reason="mention", id="c"),
        make_thread(reason="team_mention", id="d"),
        make_thread(reason="security_alert", id="e"),
    ]
    result = filter_threads(threads, ALLOWED)
    assert [t["id"] for t in result] == ["abc", "b", "c", "d", "e"]


def test_filter_drops_unlisted_reasons():
    threads = [
        make_thread(reason="subscribed", id="s"),
        make_thread(reason="comment", id="cm"),
        make_thread(reason="author", id="a"),
    ]
    result = filter_threads(threads, ["author"])
    assert [t["id"] for t in result] == ["a"]


def test_thread_key_identical_for_unchanged_thread():
    t1 = make_thread(id="a", updated="2026-11-04T03:00:00Z")
    t2 = make_thread(id="a", updated="2026-11-04T03:00:00Z")
    assert thread_key(t1) == thread_key(t2)


def test_thread_key_changes_when_thread_updated():
    t1 = make_thread(id="a", updated="2026-11-04T03:00:00Z")
    t2 = make_thread(id="a", updated="2026-11-04T03:05:00Z")
    assert thread_key(t1) != thread_key(t2)


def test_is_new_true_for_unseen_thread():
    assert is_new(make_thread(id="a"), seen={"b"}) is True


def test_is_new_false_for_already_posted_thread():
    t = make_thread(id="a", updated="2026-11-04T03:00:00Z")
    seen = {thread_key(t)}
    assert is_new(t, seen) is False


def test_message_layout_for_review_request():
    t = make_thread(reason="review_requested")
    assert build_message(t) == (
        "🔍 *REVIEW REQUESTED* — acme/widgets\n"
        "feat: x\n"
        "https://github.com/acme/widgets/pull/1"
    )


def test_message_label_for_my_pr():
    t = make_thread(reason="author")
    assert build_message(t).startswith("🚨 *MY PR* — ")


def test_message_label_for_mention():
    t = make_thread(reason="mention")
    assert build_message(t).startswith("📣 *MENTION* — ")


def test_message_label_for_team_mention():
    t = make_thread(reason="team_mention")
    assert build_message(t).startswith("👥 *TEAM MENTION* — ")


def test_message_label_for_security_alert():
    t = make_thread(reason="security_alert")
    assert build_message(t).startswith("🛡 *SECURITY ALERT* — ")


def test_message_uses_browser_url_for_pull_request():
    t = make_thread(
        reason="review_requested",
        url="https://api.github.com/repos/acme/widgets/pulls/16",
    )
    assert build_message(t).endswith("https://github.com/acme/widgets/pull/16")


def test_message_uses_browser_url_for_issue():
    t = make_thread(
        reason="mention",
        subject_type="Issue",
        url="https://api.github.com/repos/acme/widgets/issues/7",
    )
    assert build_message(t).endswith("https://github.com/acme/widgets/issues/7")


def test_collect_posts_each_new_thread_within_cap():
    threads = [
        make_thread(reason="author", id=str(i)) for i in range(3)
    ]
    msgs = collect_messages(threads, seen=set(), cap=8)
    assert len(msgs) == 3
    assert all("🚨 *MY PR*" in m for m in msgs)


def test_collect_skips_already_seen_threads():
    t = make_thread(reason="author", id="a")
    seen = {thread_key(t)}
    assert collect_messages([t], seen=seen, cap=8) == []


def test_collect_caps_at_limit_and_adds_summary():
    threads = [make_thread(reason="author", id=str(i)) for i in range(10)]
    msgs = collect_messages(threads, seen=set(), cap=8)
    assert len(msgs) == 9  # 8 items + 1 summary
    assert msgs[8].startswith("⏩ 2 more")
    assert "github.com/notifications" in msgs[8]


def test_first_run_seeds_without_posting():
    assert decide_action(None, "2026-11-04T03:00:00Z") == ("seed", None)


def test_fresh_cursor_runs_with_since():
    state = {"seen": [], "last_success": "2026-11-04T02:55:00Z"}
    assert decide_action(state, "2026-11-04T03:00:00Z") == ("run", "2026-11-04T02:55:00Z")


def test_stale_cursor_triggers_reseed_after_8_days():
    state = {"seen": [], "last_success": "2026-11-01T03:00:00Z"}
    assert decide_action(state, "2026-11-10T03:00:00Z") == ("reseed", None)


def test_three_day_gap_still_runs_and_does_not_reseed():
    # a weekend or a few quiet days must not reseed the queue away
    state = {"seen": [], "last_success": "2026-11-01T03:00:00Z"}
    assert decide_action(state, "2026-11-04T03:00:00Z") == ("run", "2026-11-01T03:00:00Z")


def test_paused_state_never_reseeds_even_after_a_long_pause():
    state = {"seen": [], "last_success": "2026-11-01T03:00:00Z", "paused": True}
    assert decide_action(state, "2026-11-13T03:00:00Z") == ("run", "2026-11-01T03:00:00Z")


def test_work_hours_inside_window():
    assert in_work_hours(datetime(2026, 11, 16, 7, 0)) is True    # Mon 07:00
    assert in_work_hours(datetime(2026, 11, 16, 12, 30)) is True  # Mon midday
    assert in_work_hours(datetime(2026, 11, 20, 18, 59)) is True  # Fri 18:59


def test_work_hours_outside_window():
    assert in_work_hours(datetime(2026, 11, 16, 6, 59)) is False  # Mon 06:59
    assert in_work_hours(datetime(2026, 11, 16, 19, 0)) is False  # Mon 19:00
    assert in_work_hours(datetime(2026, 11, 21, 10, 0)) is False  # Sat
    assert in_work_hours(datetime(2026, 11, 22, 10, 0)) is False  # Sun


def test_parse_pending_command():
    assert parse_command("pending") == "pending"


def test_parse_command_is_case_and_space_insensitive():
    assert parse_command("  PENDING ") == "pending"
    assert parse_command("Mark   Read") == "mark_read"


def test_parse_pause_resume_status():
    assert parse_command("pause") == "pause"
    assert parse_command("RESUME") == "resume"
    assert parse_command("status") == "status"


def test_parse_unknown_or_empty_returns_help():
    assert parse_command("hello there") == "help"
    assert parse_command("") == "help"


def test_pending_report_empty():
    assert format_pending_report([]) == "✅ Nothing pending — your GitHub queue is clear"


def test_pending_report_counts_and_browser_urls():
    threads = [
        make_thread(reason="review_requested", id="1",
                    url="https://api.github.com/repos/acme/widgets/pulls/16"),
        make_thread(reason="review_requested", id="2",
                    url="https://api.github.com/repos/acme/widgets/pulls/17"),
        make_thread(reason="author", id="3",
                    url="https://api.github.com/repos/acme/widgets/pulls/1"),
    ]
    report = format_pending_report(threads)
    assert report.splitlines()[0] == "*Pending (3):* 2 review requests · 1 your PR"
    assert "https://github.com/acme/widgets/pull/16" in report
    assert "api.github.com" not in report


def test_pending_report_caps_items_and_mentions_overflow():
    threads = [
        make_thread(reason="author", id=str(i),
                    url="https://api.github.com/repos/acme/widgets/pulls/%d" % i)
        for i in range(12)
    ]
    report = format_pending_report(threads)
    assert "…and 2 more" in report
    assert "github.com/notifications" in report
    # 12 items -> header + 10 item lines + 1 overflow line
    assert len(report.splitlines()) == 12


def test_help_lists_all_commands():
    help_text = format_help()
    for cmd in ("pending", "mark read", "pause", "resume", "status"):
        assert cmd in help_text


ALAN = "U111TESTUSER"
BOT = "U222TESTBOT"


def test_select_commands_keeps_user_messages_even_with_bot_id():
    # API-posted user messages carry a bot_id (posted via user token) and must count
    msgs = [{"ts": "1", "user": ALAN, "text": "status", "bot_id": "B333FAKE1"}]
    assert select_commands(msgs, ALAN) == [("1", "status")]


def test_select_commands_excludes_notifier_bot_replies():
    msgs = [{"ts": "1", "user": BOT, "text": "✅ done", "bot_id": "B333FAKE2"}]
    assert select_commands(msgs, ALAN) == []


def test_select_commands_excludes_other_users():
    msgs = [{"ts": "1", "user": "USOMEONEELSE", "text": "pending"}]
    assert select_commands(msgs, ALAN) == []


def test_select_commands_returns_chronological_order():
    # Slack history arrives newest-first; commands must run in the order typed
    msgs = [
        {"ts": "2.0", "user": ALAN, "text": "pause"},
        {"ts": "1.0", "user": ALAN, "text": "resume"},
    ]
    assert select_commands(msgs, ALAN) == [("1.0", "resume"), ("2.0", "pause")]
