# -*- coding: utf-8 -*-
"""A settings control must say when it takes effect, and be right about it.

The panel shows a dozen controls in one column. Three of them change a fleet that is already
running, within a second. The rest do nothing at all to that fleet -- they are read once,
before the sweep starts, and the run keeps the number it was born with. Nothing on the screen
distinguishes the two, so "I changed it and nothing happened" is an ordinary experience with
no way to tell it from a defect. That ambiguity is what made the 2026-09-16 split survivable
for a month: a panel that disagreed with the fleet had a ready explanation.

tools/settings_keys.py declares the timing per key. These tests make the declaration
falsifiable. What they deliberately do NOT do is let the implementation derive itself from
the declaration -- two things that agree by construction cannot be caught disagreeing.

The live checks are BEHAVIOURAL: write a value, read, rewrite, read again, and require the
second read to see the change without anything being restarted. A test that merely asserted
"this key appears in the watch list" would pass for a watcher that was never polled.
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import settings_keys as SK            # noqa: E402
from tools import settings_path as SP            # noqa: E402
from tools.childproc import run as _run         # noqa: E402  -- locale-safe child output


def _read(rel, encoding="utf-8"):
    return io.open(os.path.join(REPO, rel), encoding=encoding, errors="replace").read()


def _write_settings(path, **pairs):
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        for k, v in pairs.items():
            fh.write("%s=%s\n" % (k, v))


# ------------------------------------------------------------------ the watch list agrees
def _watched_keys():
    src = _read("relay/fleet_runner.py")
    return set(re.findall(r'\.watch\(\s*"([a-z_]+)"', src))


def test_every_key_the_follower_watches_is_declared_live():
    """A key the follower watches but the table calls sweep_start would tell an operator to
    restart for nothing."""
    watched = _watched_keys()
    assert watched, "no Follower.watch calls found -- this test lost its subject"
    for key in sorted(watched):
        assert SK.effect(key) == SK.LIVE, \
            "%s is watched by the follower but declared %r" % (key, SK.effect(key))


def test_every_key_declared_live_is_actually_watched():
    """THE OTHER DIRECTION, which the first version of this file did not have. Checking only
    "watched implies live" lets a key be declared live and never wired -- and that failure is
    the one an operator experiences, because it promises a change that never arrives. Both
    directions are assertable now that LIVE means the follower and each_gate is separate."""
    for key in SK.names_with_effect(SK.LIVE):
        assert key in _watched_keys(), \
            "%s is declared live but nothing watches it; a change would never reach a run" % key


# ------------------------------------------------------------------ live means live
def _reader_disk():
    from relay.fleet_runner import settings_disk_floor
    return settings_disk_floor()


def _reader_ram():
    from relay.fleet_runner import settings_ram_floor
    return settings_ram_floor()


def _reader_maxtabs():
    from relay.fleet_runner import settings_maxtabs
    return settings_maxtabs()


def _reader_rate():
    from relay.relay_fleet import rate_ceiling
    return rate_ceiling()


#: The reader whose return value an operator would see change, per key that claims to follow
#: the file. A registry rather than a literal parameter list: a key declared live with no
#: entry here FAILS the next test instead of quietly not being checked, which is how the
#: first version of this file agreed with the declaration by being silent about it.
_LIVE_READERS = {
    "disk_floor_gb": (_reader_disk, "1", "9"),
    "ram_floor_mb": (_reader_ram, "512", "3072"),
    "maxtabs": (_reader_maxtabs, "2", "7"),
    "rate_ceiling_rpm": (_reader_rate, "40", "90"),
}

#: job_approval_mode follows the file too, but its reader short-circuits under pytest and is
#: checked in a subprocess below; it is not a gap, it is a different harness.
_CHECKED_ELSEWHERE = {"job_approval_mode"}


def test_every_following_key_has_a_reader_this_file_exercises():
    """Otherwise the behavioural check covers whatever someone remembered to list."""
    should = set(SK.names_with_effect(SK.LIVE)) | set(SK.names_with_effect(SK.EACH_GATE))
    missing = sorted(should - set(_LIVE_READERS) - _CHECKED_ELSEWHERE)
    assert not missing, ("these keys claim to follow the file but nothing here reads them: %r"
                         % missing)


@pytest.mark.parametrize("key", sorted(_LIVE_READERS))
def test_a_key_declared_live_is_seen_again_without_a_restart(key, tmp_path, monkeypatch):
    """The operator moves the knob while the fleet runs. Nothing restarts. The next read must
    see it -- that is the whole claim these boundaries make."""
    reader, first, second = _LIVE_READERS[key]
    assert SK.effect(key) in (SK.LIVE, SK.EACH_GATE), \
        "%s is declared %r; this test is about keys that follow the file" % (key,
                                                                             SK.effect(key))
    path = tmp_path / "settings.txt"
    monkeypatch.setattr(SP, "NEW_PATH", str(path))
    _write_settings(str(path), **{key: first})
    assert float(reader()) == float(first)
    _write_settings(str(path), **{key: second})
    assert float(reader()) == float(second), \
        "%s is declared live but a second read still returned the old value" % key


def test_the_approval_mode_is_live_outside_the_test_harness(tmp_path):
    """job_approval_mode is read at every gate -- but tools/approval_policy.py short-circuits
    whenever PYTEST_CURRENT_TEST is set, so a workstation's saved preference cannot leak into
    a test run. That safeguard also makes the liveness unobservable in-process, so this asks a
    SUBPROCESS, which is the only honest way to check the production path."""
    assert SK.effect("job_approval_mode") == SK.EACH_GATE
    path = tmp_path / "settings.txt"
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tools import settings_path as SP\n"
        "SP.NEW_PATH = %r\n"
        "from tools.approval_policy import current_approval_mode\n"
        "import io\n"
        "def put(mode):\n"
        "    io.open(%r, 'w', encoding='utf-8', newline='').write('job_approval_mode=' + mode + '\\n')\n"
        "put('bypass'); a = current_approval_mode()\n"
        "put('default'); b = current_approval_mode()\n"
        "print(a, b)\n" % (REPO, str(path), str(path))
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env.pop("TASK_JOB_APPROVAL_MODE", None)
    out = _run([sys.executable, "-c", script], env=env, cwd=REPO, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    said = out.stdout.strip().splitlines()[-1].split()
    assert said == ["bypass", "default"], \
        "job_approval_mode is declared live; the subprocess saw %r" % (said,)


# ------------------------------------------------------------------ nothing is undeclared
_PY_READ_PATTERNS = (
    r'_settings_int\(\s*"([a-z_]+)"',
    r'_settings_float\(\s*"([a-z_]+)"',
    r'_settings_text\(\s*"([a-z_]+)"',
    r'_setting\(\s*"([a-z_]+)"',
    r'startswith\(\s*"([a-z_]+)="',
)

_PY_ROOTS = ("relay", "bridge", "tools", "bench", "scripts")


def _python_sources():
    for root in _PY_ROOTS:
        base = os.path.join(REPO, root)
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__", "output")]
            for name in files:
                if not name.endswith(".py") or name.startswith("test_"):
                    continue
                if name == "settings_keys.py":
                    continue            # the declaration may name every key
                yield os.path.join(dirpath, name)


def test_every_key_python_reads_is_declared():
    """A key nothing declares is a control whose timing nobody has decided."""
    found = {}
    for path in _python_sources():
        body = io.open(path, encoding="utf-8", errors="replace").read()
        for pat in _PY_READ_PATTERNS:
            for key in re.findall(pat, body):
                found.setdefault(key, os.path.relpath(path, REPO))
    # `startswith("x=")` also matches parsing that has nothing to do with settings; only
    # require declaration for keys read out of a file this repo calls settings.
    suspects = {k: v for k, v in found.items() if k in SK.KEYS or "settings" in v.lower()}
    missing = {k: v for k, v in suspects.items() if k not in SK.KEYS}
    assert not missing, "undeclared settings keys read by Python: %r" % missing


def test_every_key_the_panel_writes_is_declared():
    """A new control in the settings panel is a new promise about timing."""
    written = set()
    for cs in ("ui/FleetCockpit.cs", "ui/CopilotChat.cs"):
        body = _read(cs, encoding="utf-8-sig")
        written |= set(re.findall(r'SaveKey\(\s*"([a-z_]+)"', body))
        written |= set(re.findall(r'"(job_approval_mode)="', body))
    assert written, "no SaveKey calls found -- this test lost its subject"
    missing = sorted(k for k in written if k not in SK.KEYS)
    assert not missing, "the panel writes keys nothing declares: %r" % missing


def test_a_ui_only_key_has_no_reader_outside_the_gui():
    """ui_only is a claim about the rest of the system, and it is the easy one to get wrong:
    a key starts as a window preference and later acquires a reader."""
    for key in SK.names_with_effect(SK.UI_ONLY):
        for path in _python_sources():
            body = io.open(path, encoding="utf-8", errors="replace").read()
            assert ('"%s"' % key) not in body and ("'%s'" % key) not in body, \
                "%s is declared ui_only but %s names it" % (key, os.path.relpath(path, REPO))


# ------------------------------------------------------------------ one default per key
def test_the_declared_default_is_the_one_the_code_uses():
    """A default is a fact with as many owners as it has readers. ram_floor_mb had three --
    2048 on the panel, 1400 from an unrelated flag, 512 in the admission gates -- and no two
    of them ever had to agree."""
    from relay import fleet_runner as FR
    from relay import relay_fleet as RF
    from bridge import session_store as SS
    assert RF.FLEET_RAM_FLOOR_MB == SK.default("ram_floor_mb")
    assert FR.RAM_FLOOR_DEFAULT_MB == SK.default("ram_floor_mb")
    assert RF.DEFAULT_DISK_FLOOR_GB == SK.default("disk_floor_gb")
    assert FR.DEFAULT_MAX_CONCURRENT == SK.default("maxtabs")
    assert FR.AUTOSCALE_CEILING_DEFAULT == SK.default("autoscale_max")
    # 2026-09-24 owner decision: "no value in settings.txt" for session_retention_days went
    # from forever to 90 days. Two readers of that default -- the bridge's own prune fallback
    # and this table -- must not be allowed to drift the way ram_floor_mb's three did.
    assert SS.DEFAULT_RETENTION_DAYS == SK.default("session_retention_days")


@pytest.mark.parametrize("field,key", [
    ("_ramFloor", "ram_floor_mb"),
    ("_diskFloor", "disk_floor_gb"),
    ("_maxtabs", "maxtabs"),
    ("_autoMax", "autoscale_max"),
    ("_rateCeiling", "rate_ceiling_rpm"),
    # Added with their controls on 2026-09-17. Until then the panel had no value for these at
    # all, so "the number an operator reads off the screen" did not exist and could not be
    # compared with what the fleet uses.
    ("_fleetScratchDays", "fleet_scratch_days"),
    ("_fleetCompressHours", "fleet_compress_hours"),
    # 2026-09-24: the panel's own default changed from 0 ("keep everything") to 90, to match
    # the bridge's new DEFAULT_RETENTION_DAYS -- see test_the_declared_default_is_the_one_the_
    # code_uses, above, for the Python side of the same agreement.
    ("_retDays", "session_retention_days"),
])
def test_the_panel_shows_the_same_default_the_fleet_uses(field, key):
    """The panel's own default is what an operator reads off the screen on a machine that has
    never been configured. When it differs from what the fleet does, the screen is wrong and
    nothing anywhere is broken -- which is the hardest kind of defect to be shown."""
    body = _read("ui/FleetCockpit.cs", encoding="utf-8-sig")
    m = re.search(r'\b(?:int|double)\s+%s\s*=\s*([0-9.]+)\s*;' % re.escape(field), body)
    assert m, "could not find the cockpit's default for %s" % field
    assert float(m.group(1)) == float(SK.default(key)), \
        "the panel starts at %s for %s, the fleet uses %s" % (m.group(1), key,
                                                              SK.default(key))


# ------------------------------------------------------------------ the table is usable
def test_every_declaration_says_something_an_operator_can_act_on():
    for key, k in SK.KEYS.items():
        assert k.note, key
        # A ui_only key's note is a label -- "Theme." is the whole truth about it. Every other
        # key has to tell an operator what changes and when.
        if k.effect != SK.UI_ONLY:
            assert len(k.note) > 10, key
        assert k.effect in SK.EFFECTS, key
        assert k.reader, key


def test_an_undeclared_key_is_not_silently_given_a_default():
    with pytest.raises(KeyError):
        SK.default("a_key_nobody_declared")
    assert SK.effect("a_key_nobody_declared") is None

# ------------------------------------------------------------------ the panel says it too
def _panel_timing_map():
    """Parse SettingsTiming's switch out of the cockpit: case labels fall through to a return."""
    body = _read("ui/FleetCockpit.cs", encoding="utf-8-sig")
    i = body.index("static string SettingsTiming(string key)")
    block = body[i:body.index("string TimingBadge(string key)", i)]
    mapping, pending = {}, []
    for line in block.splitlines():
        line = line.strip()
        m = re.match(r'case "([a-z_]+)":$', line)
        if m:
            pending.append(m.group(1))
            continue
        m = re.match(r'return "([a-z_]+)";$', line)
        if m and pending:
            for key in pending:
                mapping[key] = m.group(1)
            pending = []
    return mapping


def test_the_panel_states_the_same_timing_the_declaration_does():
    """A control that says "affects a running fleet" while the fleet reads it once at launch
    is worse than a control that says nothing: it converts an operator's correct observation
    into a reason to doubt themselves."""
    panel = _panel_timing_map()
    assert panel, "SettingsTiming's switch could not be parsed -- this test lost its subject"
    for key, said in sorted(panel.items()):
        assert SK.effect(key) == said, \
            "the panel calls %s %r; the declaration says %r" % (key, said, SK.effect(key))


def test_every_key_the_panel_can_change_carries_a_timing():
    """ui_only is the default arm of that switch, so a key the panel writes and forgets to
    name silently reads as window state -- the one answer that promises nothing."""
    written = set()
    body = _read("ui/FleetCockpit.cs", encoding="utf-8-sig")
    written |= set(re.findall(r'SaveKey\(\s*"([a-z_]+)"', body))
    panel = _panel_timing_map()
    missing = sorted(k for k in written
                     if SK.effect(k) != SK.UI_ONLY and k not in panel)
    assert not missing, ("the panel writes these but its own timing switch does not name "
                         "them, so they fall through to ui_only: %r" % missing)


def test_the_panel_never_claims_a_setting_was_applied():
    """THE 2026-09-16 MISREPORT, in one sentence: the panel knows it wrote a file, and that is
    not the same fact as the fleet having read it. For a month the screen said 1 GB while every
    run reserved 4, and nothing on the screen was lying about what it knew."""
    body = _read("ui/FleetCockpit.cs", encoding="utf-8-sig")
    i = body.index("string TimingBadge(string key)")
    block = body[i:i + 1200]
    for claim in ("適用済", "Applied", "applied"):
        assert claim not in block, \
            "TimingBadge says %r -- the panel cannot know the consumer has read the file" % claim
