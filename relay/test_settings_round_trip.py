"""The cockpit writes a settings key and Python reads one. Nothing checked they matched.

This is the same shape as the compression bug: a writer and a reader that are correct
separately and silently disagree. A knob whose C# key is `fleet_log_days` and whose Python
reader looks for `fleet_retention_days` moves a number in the UI and changes nothing at all,
and there is no error anywhere to notice.
"""
import io
import os
import re

import pytest

CS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "ui", "FleetCockpit.cs")

#: key -> the reader that must honour it
CONTRACT = ("rate_ceiling_rpm", "fleet_log_days", "fleet_store_days")


def _settings(tmp_path, **pairs):
    p = tmp_path / "settings.txt"
    io.open(str(p), "w", encoding="utf-8").write(
        "".join("%s=%s\n" % (k, v) for k, v in pairs.items()))
    return str(p)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the shared settings reader at a temporary file."""
    def _use(**pairs):
        path = _settings(tmp_path, **pairs)
        monkeypatch.setattr("relay.fleet_runner._settings_path", lambda: path)
        return path
    return _use


def test_the_cockpit_writes_every_key_python_reads(wired):
    # Source-level, because no runtime path spans C# and Python -- but paired below with tests
    # that actually exercise each reader, so this is the contract and not the evidence.
    body = io.open(CS, encoding="utf-8-sig").read()
    written = set(re.findall(r'SaveKey\("([a-z_]+)"', body))
    missing = [k for k in CONTRACT if k not in written]
    assert not missing, "the cockpit cannot write: %s" % missing


def test_the_cockpit_also_parses_back_what_it_writes(wired):
    # A key it can write but not re-read resets to the default on the next launch, which looks
    # exactly like the setting being ignored.
    body = io.open(CS, encoding="utf-8-sig").read()
    for k in CONTRACT:
        assert ('StartsWith("%s=' % k) in body, "cockpit never reads back %s" % k


def test_the_substring_offset_matches_the_key_length(wired):
    # ln.Substring(N) with the wrong N silently parses "0_days=14" or drops a character, and
    # int.TryParse then fails and leaves the default. Every one of these was hand-counted.
    body = io.open(CS, encoding="utf-8-sig").read()
    for k, n in re.findall(r'StartsWith\("([a-z_]+=)"\)\s*&&\s*int\.TryParse\(ln\.Substring\((\d+)\)', body):
        assert len(k) == int(n), "Substring(%s) does not match len('%s')=%d" % (n, k, len(k))


def test_the_rate_ceiling_is_honoured_by_admission(wired):
    wired(rate_ceiling_rpm=42)
    from relay import relay_fleet as F
    assert F.rate_ceiling() == 42.0


def test_a_zero_ceiling_survives_the_round_trip(wired):
    # 0 means "no ceiling" and must not be mistaken for "unset" by a falsy check on the way
    # through -- that would silently restore the default 100 the moment it is turned off.
    wired(rate_ceiling_rpm=0)
    from relay import relay_fleet as F
    assert F.rate_ceiling() == 0.0
    ok, _why = F.rate_headroom_ok()
    assert ok is True


def test_the_fleet_retention_days_are_honoured(wired, tmp_path):
    import time
    wired(fleet_log_days=1)
    from relay import fleet_retention as R
    now = time.time()
    d = tmp_path / "fleetdir"
    d.mkdir()
    for i in range(30):
        p = str(d / ("coordinator_%02d.log" % i))
        io.open(p, "wb").write(b"x" * 100)
        t = now - 3 * 86400          # three days old: kept at 14, removed at 1
        os.utime(p, (t, t))
    _freed, removed = R.coordinator_logs(str(d), now=now)
    assert removed, "fleet_log_days=1 did not shorten the window"


def test_the_store_days_are_honoured(wired, tmp_path):
    import time
    wired(fleet_store_days=1)
    from relay import fleet_retention as R
    now = time.time()
    d = tmp_path / "fleetdir2"
    (d / "transcripts").mkdir(parents=True)
    p = str(d / "transcripts" / "r1_a0_w0.jsonl")
    io.open(p, "wb").write(b"x" * 100)
    t = now - 3 * 86400
    os.utime(p, (t, t))
    _freed, removed = R.stores(str(d), now=now)
    assert removed, "fleet_store_days=1 did not shorten the window"


def test_an_absent_key_leaves_the_documented_default(wired):
    wired(maxtabs=4)
    from relay import relay_fleet as F
    from relay.quota_meter import LIMIT_RPM
    assert F.rate_ceiling() == LIMIT_RPM
