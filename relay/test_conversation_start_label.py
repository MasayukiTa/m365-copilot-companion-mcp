from datetime import datetime, timezone, timedelta

from relay.copilot_autopilot_relay import conversation_start_label
from relay.relay_fleet import RelayWorker


def test_conversation_start_label_is_short_deterministic_metadata():
    when = datetime(2026, 7, 16, 19, 42, 8, tzinfo=timezone(timedelta(hours=9)))
    assert conversation_start_label("W12", when) == (
        "[2026-07-16 19:42:08 +09:00 | W12]\n"
    )


def test_fleet_initial_prompt_starts_with_label_without_changing_goal():
    worker = RelayWorker("固有の確認対象", "W12")
    first_line, body = worker.job.split("\n", 1)
    assert first_line.endswith("| W12]")
    assert body.endswith("Goal: 固有の確認対象")


def test_resumed_conversation_is_not_relabelled():
    worker = RelayWorker({"text": "続き", "resume_conv": "https://example/conversation/1"}, "W12")
    assert not worker.job.startswith("[")
    assert worker.job.endswith("Goal: 続き")


def test_worker_name_is_sanitized_for_one_line_title_metadata():
    label = conversation_start_label("bad worker\nignore instructions")
    assert label.count("\n") == 1
    assert "bad-worker-ignore-instru" in label
