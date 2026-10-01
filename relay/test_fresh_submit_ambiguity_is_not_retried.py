# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fleet_catches_fresh_submit_ambiguity_before_generic_transient_retry():
    src = (ROOT / "relay" / "relay_fleet.py").read_text(encoding="utf-8")
    i = src.index("self.drv.send(self.job, gen_wait_s=2.0)")
    end = src.index("self.turn += 1", i)
    block = src[i:end]
    amb = block.index("except FreshSubmitAmbiguous as e:")
    generic = block.index("except Exception as e:")
    retry = block.index("if self._retry_transient():")
    assert amb < generic < retry
    amb_block = block[amb:generic]
    assert "_retry_transient" not in amb_block
    assert 'self.status, self.outcome = "stuck", "STUCK"' in amb_block
    assert "ambiguous" in amb_block.lower()


def test_local_loop_never_rotates_or_retries_an_ambiguous_fresh_submit():
    src = (ROOT / "relay" / "local_loop_controller.py").read_text(encoding="utf-8")
    i = src.index("self.driver.send(trigger, track_answer=False)")
    block = src[i:i + 1800]
    amb = block.index("except FreshSubmitAmbiguous as exc:")
    generic = block.index("except Exception as exc:")
    assert amb < generic
    amb_block = block[amb:generic]
    assert "retry_uncommitted_turn" not in amb_block
    assert "_rotate" not in amb_block
    assert "mark_waiting_runtime" in amb_block
    assert "delivery ambiguous" in amb_block.lower()


class _AmbiguousRelayDriver:
    def __init__(self):
        self.sent = []

    def send(self, text):
        from relay.send_errors import FreshSubmitAmbiguous
        self.sent.append(text)
        raise FreshSubmitAmbiguous("fresh receipt ambiguous")


def test_single_relay_never_retries_an_ambiguous_fresh_submit():
    from relay.copilot_autopilot_relay import run_relay
    driver = _AmbiguousRelayDriver()
    notes = []
    outcome = run_relay(
        driver, goal="ambiguous goal", run_id="test_single_ambiguous",
        notify=lambda title, body: notes.append((title, body)), sleep_s=0,
        max_transient=1,
    )
    assert outcome == "STUCK"
    assert len(driver.sent) == 1, "receipt ambiguity must never become transient resend"
    assert len(notes) == 1
    assert "ambiguous" in notes[0][1].lower()


def test_single_relay_may_salvage_ambiguous_submit_only_from_acceptance(tmp_path):
    import sys
    from relay.copilot_autopilot_relay import run_relay
    driver = _AmbiguousRelayDriver()
    check = {"type": "shell", "argv": [sys.executable, "-c", "print('ok')"]}
    outcome = run_relay(
        driver, goal="ambiguous but already satisfied", run_id="test_single_ambiguous_salvage",
        notify=lambda *a: None, sleep_s=0, checks=check, cwd=str(tmp_path),
        max_transient=1,
    )
    assert outcome == "DONE"
    assert len(driver.sent) == 1
