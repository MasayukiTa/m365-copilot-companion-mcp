# -*- coding: utf-8 -*-
"""An expired-token response triggers an IMMEDIATE recapture, and a capture that cannot
recover leaves its reason where status.json shows it.

The defect (2026-10-28, socket_route.jsonl socket_turn_stats at 22:44:00/03/11, three workers
refused with "the supplied token has expired"): the whole fleet shares one token `exp`, so an
expiry hits every worker within seconds. The capture meant to replace it was then held back by
the capture floor (120 s) or by a CapturePolicy backoff that had been set while the token still
had life, so the fresh token arrived minutes late and goals sat in a silent WAIT. And a capture
that failed outright (NotAnAgentSurface, a crashed helper) left `token_wait` empty whenever no
goal happened to be held on that lap, so there was no reason to read at all.

These tests pin the two halves of the fix on the policy/state layer, with no browser:
  (a) time-to-recovery: once `arm_expired` fires, `allow` returns True immediately even inside
      the floor and inside a same-exp backoff -- one capture, not a wait;
  (b) non-recoverable: a failed capture's reason is still reported by status_block() on a lap
      when nothing is held.
"""
import relay.token_provider as TP


def _clock(start=1000.0):
    t = {"v": float(start)}
    return (lambda: t["v"]), t


def _jwt(exp):
    """A minimal unsigned JWT whose only claim is exp, enough for profile_token._claims."""
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return "%s.%s.x" % (b64({"alg": "none"}), b64({"exp": int(exp)}))


def test_expiry_arms_an_immediate_capture_through_the_floor():
    # A policy whose backoff was set while the token had life would normally hold the next
    # attempt for base_s; arm_expired must cut straight through it exactly once.
    now, t = _clock()
    pol = TP.CapturePolicy(base_s=120.0, max_s=600.0, max_no_token_s=120.0, now=now)
    key = "agent"
    # A same-exp success a moment ago pushed next_at into the future (normal pacing).
    pol.on_success(key, token="", have_token=True)
    pol.on_success(key, token="", have_token=True)
    assert pol.allow(key, held=True) is False  # held back by the backoff just set

    # The turn is refused with an expired token: the shared exp really is past.
    pol.arm_expired(key)
    # Time has NOT advanced; the only thing that changed is the proven expiry.
    assert pol.allow(key, held=True) is True, "expiry did not open an immediate window"
    # (a) recovery is immediate: zero wall-clock between the refusal and the allowed capture.


def test_the_window_is_one_shot_not_a_bypass_switch():
    now, t = _clock()
    pol = TP.CapturePolicy(base_s=120.0, now=now)
    key = "agent"
    pol.on_failure(key, have_token=False, why="boom")  # sets a backoff
    pol.arm_expired(key)
    assert pol.allow(key, held=True) is True           # consumed here
    # The next question is governed by the ordinary backoff again -- no permanent bypass.
    assert pol.allow(key, held=True) is False


def test_a_genuine_recapture_closes_the_window_by_resetting_the_streak():
    now, t = _clock()
    pol = TP.CapturePolicy(base_s=120.0, now=now)
    key = "agent"
    pol.arm_expired(key)
    assert pol.allow(key, held=True) is True
    # A real capture with a NEWER exp resets the streak; nothing is armed any more.
    t["v"] += 1
    pol.on_success(key, token=_jwt(exp=t["v"] + 3600), have_token=True)
    # Without a new arm, the floor governs again.
    assert pol.allow(key, held=True) is False


def test_a_failed_capture_reason_is_visible_even_with_nothing_held():
    # (b) non-recoverable: the capture raised and no goal is held this instant.
    now, t = _clock()
    cap = TP._CaptureState(now=now)
    cap.attempt()
    cap.fail("NotAnAgentSurface: the captured request names no agent", next_in_s=30.0)
    assert cap.unresolved_failure() is True

    import relay.token_provider as mod
    old = mod._CAPTURE
    try:
        mod._CAPTURE = cap
        blk = mod.status_block()
    finally:
        mod._CAPTURE = old
    assert blk, "a failed capture with nobody held produced an empty token_wait"
    assert "NotAnAgentSurface" in blk["capture"]["last_error"]


def test_a_success_clears_the_unresolved_flag():
    now, t = _clock()
    cap = TP._CaptureState(now=now)
    cap.attempt()
    cap.fail("boom", next_in_s=30.0)
    t["v"] += 1
    cap.attempt()
    cap.ok(next_in_s=600.0)
    assert cap.unresolved_failure() is False


# ---- Requirement 2: the pre-expiry trigger is a RATIO of life, not a fixed margin --------------
#
# The fleet shares one token `exp`. A fixed 1500 s margin makes every worker's needs_refresh turn
# true in the same second (the 2026-10-27 16:00 burst: 498 refusals in one hour), and on a token
# whose whole life is under the margin it is true the instant a capture finishes, so the capture
# spins (measured 3.8/min). Both go away when the trigger is  remaining/life0 <= fraction+jitter.


def test_a_fresh_capture_is_not_immediately_due_again_on_a_short_token():
    # The fixed-margin spin: a 14-min token under a 25-min margin is "due" the instant it is
    # captured. With a ratio it is not -- remaining/life0 ~= 1.0, far above the 0.20 fraction.
    now, t = _clock()
    pol = TP.CapturePolicy(now=now)
    key = "agent"
    life = 14 * 60.0                      # a short 14-minute token
    tok = _jwt(exp=t["v"] + life)
    pol.on_success(key, token=tok, have_token=True)
    # Just captured: NOT due. remaining/life0 ~= 1.0 >> any fraction in (0,1).
    assert pol.due_for_preemptive_refresh(key, token=tok) is False, \
        "a just-captured short token is already 'due' -- that is the fixed-margin spin"
    # It only becomes due once the token has spent (1 - thr) of its OWN life, whatever that is.
    import relay.token_provider as _tp
    thr = _tp.PREEMPT_REFRESH_FRACTION + _tp._key_jitter(key, _tp.PREEMPT_JITTER_BAND)
    t["v"] += life * (1.0 - thr) + 1.0    # step just past the crossing point
    assert pol.due_for_preemptive_refresh(key, token=tok) is True
    # And the head-room scales with life: a 52-min token crosses at the SAME fraction, not at a
    # fixed 1500 s that a 14-min token could never reach.
    now2, t2 = _clock()
    pol2 = TP.CapturePolicy(now=now2)
    long_life = 52 * 60.0
    tok2 = _jwt(exp=t2["v"] + long_life)
    pol2.on_success(key, token=tok2, have_token=True)
    assert pol2.due_for_preemptive_refresh(key, token=tok2) is False
    t2["v"] += long_life * (1.0 - thr) + 1.0
    assert pol2.due_for_preemptive_refresh(key, token=tok2) is True


def test_the_shared_exp_fleet_does_not_all_cross_in_the_same_second(capsys):
    # One `exp`, many workers. With a per-key jitter band the crossing times spread out instead
    # of stampeding. Show the spread in seconds for a realistic fleet and life.
    import relay.token_provider as _tp
    life = 58 * 60.0
    # Each worker crosses when remaining/life == fraction + jitter(key), i.e. at
    # elapsed = life * (1 - (fraction + jitter(key))).  Compute that elapsed for many keys.
    keys = ["w%02d" % i for i in range(1, 91)]   # a 90-worker fleet
    thrs = [_tp.PREEMPT_REFRESH_FRACTION + _tp._key_jitter(k, _tp.PREEMPT_JITTER_BAND)
            for k in keys]
    crossings = sorted(life * (1.0 - thr) for thr in thrs)
    spread = crossings[-1] - crossings[0]
    # The band is +/-0.08 of life -> up to 0.16*life of spread. On a 58-min token that is minutes,
    # not one shared second. Require a real spread so a regression to a fixed margin fails here.
    print("fleet crossing spread over %.0f-min life: %.1f s (min=%.1f max=%.1f)"
          % (life / 60.0, spread, crossings[0], crossings[-1]))
    assert spread > 60.0, "the shared-exp fleet still crosses within one minute -- no staggering"


def test_an_unreadable_or_unknown_life_refuses_to_guess():
    # No life0 recorded yet (no successful capture): nothing is proven, so it is not 'due'.
    now, t = _clock()
    pol = TP.CapturePolicy(now=now)
    assert pol.due_for_preemptive_refresh("agent", token="") is False


def test_a_token_already_past_exp_is_due():
    now, t = _clock()
    pol = TP.CapturePolicy(now=now)
    key = "agent"
    tok = _jwt(exp=t["v"] + 600.0)
    pol.on_success(key, token=tok, have_token=True)
    t["v"] += 601.0                       # walk past exp
    assert pol.due_for_preemptive_refresh(key, token=tok) is True
