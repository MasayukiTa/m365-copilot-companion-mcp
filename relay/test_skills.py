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

    installed = tmp_path / "project" / ".claude" / "skills" / "model-review"
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
    installed = tmp_path / "project" / ".claude" / "skills" / "model-review" / "SKILL.md"
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
