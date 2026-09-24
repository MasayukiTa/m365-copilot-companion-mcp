import os
import sqlite3
import threading
from pathlib import Path

import pytest

from relay.skills import SkillError, SkillStore, load_bundle


def _write_skill(root: Path, name="model-review", description="Review Python model code") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        "Review $ARGUMENTS carefully. First target: $0\n",
        encoding="utf-8",
    )
    return folder


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return SkillStore(
        tmp_path / "project", tmp_path / "state.sqlite3", tmp_path / "gates"
    )


def test_external_skill_requires_two_step_approval_and_renders(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "download")
    imported = store.import_external(source)
    assert imported.trust == "untrusted"
    model_row = store.list_metadata(model_safe=True)[0]
    assert model_row["description"] == "(hidden until human approval)"
    assert model_row["files"] == []
    with pytest.raises(SkillError, match="human must approve"):
        store.render("model-review", "src/model.py strict")

    challenge = store.request_approval("model-review")
    assert challenge["status"] == "confirmation-required"
    gate_path = Path(challenge["gate_path"])
    assert gate_path.is_file()
    gate = __import__("json").loads(gate_path.read_text(encoding="utf-8"))
    assert gate["expires_at"] > gate["asked_at"]
    assert "instruction_preview (UNTRUSTED DATA" in gate["context"]
    assert "Review $ARGUMENTS carefully" in gate["context"]
    assert "requested_tools:" in gate["context"]
    approved = store.confirm_approval("model-review", challenge["token"])
    assert approved["status"] == "trusted"
    rendered = store.render("model-review", "src/model.py strict")
    assert "src/model.py strict" in rendered
    assert "First target: src/model.py" in rendered
    assert "does not grant additional tool permissions" in rendered


def test_any_bundle_change_invalidates_trust(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "download")
    (source / "scripts").mkdir()
    (source / "scripts" / "check.py").write_text("print('one')\n", encoding="utf-8")
    store.import_external(source)
    challenge = store.request_approval("model-review")
    store.confirm_approval("model-review", challenge["token"])

    installed = tmp_path / "project" / "skills" / "model-review"
    (installed / "scripts" / "check.py").write_text("print('two')\n", encoding="utf-8")
    assert store.get("model-review").trust == "changed"
    with pytest.raises(SkillError, match="changed"):
        store.render("model-review")
    review = store.request_approval("model-review")
    assert review["changed_files"]["modified"] == ["scripts/check.py"]
    assert "Review $ARGUMENTS" in review["instruction_preview"]


def test_change_during_approval_requires_new_review(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "download")
    store.import_external(source)
    challenge = store.request_approval("model-review")
    installed = tmp_path / "project" / "skills" / "model-review" / "SKILL.md"
    installed.write_text(installed.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
    with pytest.raises(SkillError, match="changed after review"):
        store.confirm_approval("model-review", challenge["token"])


def test_fleet_cockpit_gate_click_approves_exact_digest(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.import_external(_write_skill(tmp_path / "download"))
    challenge = store.request_approval("model-review")
    gate_path = Path(challenge["gate_path"])
    gate = __import__("json").loads(gate_path.read_text(encoding="utf-8"))
    gate.update({"answered": True, "answer": "approved"})
    gate_path.write_text(__import__("json").dumps(gate), encoding="utf-8")
    assert store.get("model-review").trust == "trusted"


def test_repeated_request_reuses_pending_gate_and_denial_stays_untrusted(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.import_external(_write_skill(tmp_path / "download"))
    first = store.request_approval("model-review")
    second = store.request_approval("model-review")
    assert second["token"] == first["token"]
    gate_path = Path(first["gate_path"])
    gate = __import__("json").loads(gate_path.read_text(encoding="utf-8"))
    gate.update({"answered": True, "answer": "denied"})
    gate_path.write_text(__import__("json").dumps(gate), encoding="utf-8")
    assert store.get("model-review").trust == "untrusted"


def test_locally_created_skill_is_trusted_exact_digest(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    skill = store.create_local("my-skill", "My own workflow", "Do the local workflow.")
    assert skill.provenance == "local-authored"
    assert skill.trust == "trusted"
    assert "Do the local workflow" in store.render("my-skill")
    assert skill.path == tmp_path / "project" / "skills" / "my-skill"


def test_discovers_native_and_claude_compatible_roots(tmp_path, monkeypatch):
    # OPTS IN, because the personal scope is no longer served by default -- see
    # test_the_personal_library_is_not_served_by_default. What this test still checks is that
    # all four roots work and carry the right scope WHEN they are served.
    monkeypatch.setenv("MCP_SKILLS_INCLUDE_PERSONAL", "1")
    store = _store(tmp_path, monkeypatch)
    project = tmp_path / "project"
    home = tmp_path / "home"
    expected = {
        "project-native": project / "skills",
        "project-claude": project / ".claude" / "skills",
        "personal-native": home / "skills",
        "personal-claude": home / ".claude" / "skills",
    }
    for name, root in expected.items():
        _write_skill(root, name=name, description=name)

    found = {skill.name: skill for skill in store.discover()}
    assert set(found) == set(expected)
    assert found["project-native"].scope == "project"
    assert found["project-claude"].scope == "project"
    assert found["personal-native"].scope == "personal"
    assert found["personal-claude"].scope == "personal"
    for name, root in expected.items():
        assert found[name].path == root / name


def test_native_and_personal_roots_win_same_name_collisions(tmp_path, monkeypatch):
    # Same opt-in as above: precedence among the four roots is unchanged, but the personal
    # pair only exists when the operator asks for it.
    monkeypatch.setenv("MCP_SKILLS_INCLUDE_PERSONAL", "1")
    store = _store(tmp_path, monkeypatch)
    project = tmp_path / "project"
    home = tmp_path / "home"
    roots = [
        project / ".claude" / "skills",
        project / "skills",
        home / ".claude" / "skills",
        home / "skills",
    ]
    for index, root in enumerate(roots):
        _write_skill(root, description=f"candidate {index}")

    selected = store.get("model-review")
    assert selected.path == home / "skills" / "model-review"
    assert selected.description == "candidate 3"


def test_resource_traversal_is_rejected(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.create_local("safe-skill", "Safe workflow", "Read references/info.md")
    with pytest.raises(SkillError, match="stay inside"):
        store.read_resource("safe-skill", "../secret.txt")


def test_match_uses_only_trusted_model_invocable_metadata(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.create_local(
        "model-review", "Python model code review and validation", "Review the model."
    )
    found = store.match("Python model code reviewを実施して")
    assert found and found["name"] == "model-review"
    assert store.match("今日の天気") is None


def test_invalid_name_is_rejected(tmp_path):
    folder = _write_skill(tmp_path, name="Bad_Name")
    with pytest.raises(SkillError, match="lowercase"):
        load_bundle(folder)


# --- the personal scope, which this server should not be serving ---------------------------

def test_the_personal_library_is_not_served_by_default(monkeypatch, tmp_path):
    """MEASURED LEAK. ~/.claude/skills is the library of whatever assistant the OPERATOR runs
    on this machine, and this server hands Skills to agents that have nothing to do with it.

    On 2026-08-31, a real SWE-bench goal --

        skill_match("You are fixing a real bug in ... **ansible/ansible** ...")
            -> delegation-commander   score 1.0    (personal scope)

    -- returned a Claude Code playbook telling the worker to dispatch the work to subagents it
    does not have. It won at 1.0 ahead of every project Skill because personal roots come last
    and last wins, and the trust check could not catch it: that library is legitimately trusted
    where it lives.
    """
    monkeypatch.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    scopes = {scope for scope, _root in SkillStore(str(tmp_path)).roots()}
    assert scopes == {"project"}, "a personal root is being served: %s" % scopes


def test_nothing_discovered_by_default_comes_from_the_personal_scope(monkeypatch):
    """The roots test above is about configuration; this is about what actually comes back."""
    monkeypatch.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for skill in SkillStore(repo).discover():
        assert skill.scope != "personal", "%s came from the personal library" % skill.name


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_an_operator_can_opt_the_personal_library_back_in(monkeypatch, tmp_path, value):
    """A decision with a name, rather than an accident of path layout."""
    monkeypatch.setenv("MCP_SKILLS_INCLUDE_PERSONAL", value)
    scopes = {scope for scope, _root in SkillStore(str(tmp_path)).roots()}
    assert "personal" in scopes


@pytest.mark.parametrize("value", ["", "0", "no", "off", "maybe"])
def test_anything_that_is_not_an_opt_in_leaves_it_off(monkeypatch, tmp_path, value):
    """Unrecognised must read as off. The failure direction is asymmetric: off costs a Skill
    the operator has to opt into, on serves another product's playbooks to fleet workers."""
    monkeypatch.setenv("MCP_SKILLS_INCLUDE_PERSONAL", value)
    scopes = {scope for scope, _root in SkillStore(str(tmp_path)).roots()}
    assert scopes == {"project"}


def test_a_store_opens_one_sqlite_connection_for_its_whole_lifetime(tmp_path, monkeypatch):
    """A regression guard for the churn behind the "disk I/O error" seen running the full
    suite (tests/test_skills_business_matching.py::test_matching_quality_on_office_requests):
    building an 18-Skill trusted store and then running 40 match() queries against it used to
    open ~230 separate sqlite3 connections (one per statement, via the old _connect() that
    called sqlite3.connect() every time) against one small WAL-mode file -- each open/close
    touching the main db plus its -wal/-shm siblings, so 230 chances for a transient Windows
    I/O error (real-time antivirus scanning the temp directory these files live in) to land
    mid-operation, in a file that needs none of that churn to hold a few hundred KB of trust
    rows. _connect() now hands back a `with`-usable wrapper around ONE connection created on
    first use and kept for the store's life -- this pins that count, not just today's symptom.
    """
    calls = []
    real_connect = sqlite3.connect

    def counted(*a, **k):
        calls.append(1)
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counted)
    root = tmp_path / "project"
    (root / "skills").mkdir(parents=True)
    _write_skill(root / "skills", name="probe-skill")
    store = SkillStore(root, tmp_path / "state.sqlite3", tmp_path / "gates")
    assert len(calls) == 1, "SkillStore() itself should open exactly one connection"

    req = store.request_approval("probe-skill")
    store.confirm_approval("probe-skill", req["token"])
    for _ in range(20):
        store.match("review my python model code")
    assert len(calls) == 1, (
        "approving a Skill and matching 20 times opened %d sqlite3 connections; "
        "expected the store to keep reusing its one connection" % len(calls))


def test_one_store_serves_concurrent_threads_without_error(tmp_path, monkeypatch):
    """The store's one connection is opened with check_same_thread=False and every use is
    serialized by a lock, because it is NOT only used from the thread that created it:
    bridge/copilot_bridge.py keeps one process-wide SKILL_STORE serving a
    ThreadingHTTPServer's worker threads, and fastmcp 3 runs sync tools in a thread pool by
    default. Without check_same_thread=False, a second thread's first query would raise
    sqlite3.ProgrammingError ("SQLite objects created in a thread can only be used in that
    same thread"); without the lock, two threads' statements on the shared connection could
    interleave mid-transaction. Runs one thread reading (match(), repeatedly, the discover()
    path) against another writing (request_approval + confirm_approval for distinct Skills,
    the trust-write path) on the SAME store, and requires both to finish clean.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    root = tmp_path / "project"
    (root / "skills").mkdir(parents=True)
    _write_skill(root / "skills", name="model-review")
    for i in range(5):
        _write_skill(root / "skills", name="probe-skill-%d" % i,
                      description="Probe Skill number %d" % i)
    store = SkillStore(root, tmp_path / "state.sqlite3", tmp_path / "gates")

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def record(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(30):
                store.match("review my python model code")
        except BaseException as exc:  # noqa: BLE001 -- a thread-identity error IS the point
            record(exc)

    def writer() -> None:
        try:
            for i in range(5):
                req = store.request_approval("probe-skill-%d" % i)
                store.confirm_approval("probe-skill-%d" % i, req["token"])
        except BaseException as exc:  # noqa: BLE001
            record(exc)

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a thread hung against the shared connection"
    assert errors == [], errors
    for i in range(5):
        assert store.get("probe-skill-%d" % i).trust == "trusted"
