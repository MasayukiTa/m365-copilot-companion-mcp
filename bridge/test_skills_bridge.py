from pathlib import Path

from bridge import copilot_bridge as bridge
from relay.skills import SkillStore


class DummyHandler:
    def __init__(self):
        self.events = []
        self.prompts = []

    def _sse(self, data, event=None):
        self.events.append((data, event))

    def _stream_text(self, prompt):
        self.prompts.append(prompt)

    def send_response(self, _code):
        pass

    def send_header(self, _name, _value):
        pass

    def end_headers(self):
        pass


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return SkillStore(tmp_path / "project", tmp_path / "skills.sqlite3", tmp_path / "gates")


def test_explicit_dynamic_skill_command_loads_trusted_prompt(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.create_local("model-review", "Review Python model code", "Review $ARGUMENTS now.")
    monkeypatch.setattr(bridge, "SKILL_STORE", store)
    handler = DummyHandler()
    bridge.Handler._command(handler, "/model-review src/model.py")
    assert len(handler.prompts) == 1
    assert "Trusted Skill: model-review" in handler.prompts[0]
    assert "Review src/model.py now" in handler.prompts[0]


def test_plain_message_uses_only_confident_trusted_match(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.create_local(
        "model-review", "Python model code review and validation", "Perform the model review."
    )
    monkeypatch.setattr(bridge, "SKILL_STORE", store)
    handler = DummyHandler()
    bridge.Handler._stream(handler, "Python model code reviewを実施して")
    assert "Trusted Skill: model-review" in handler.prompts[0]
    assert "Original user request" in handler.prompts[0]


def test_skill_approval_command_is_never_forwarded_to_model(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "SKILL_STORE", _store(tmp_path, monkeypatch))
    handler = DummyHandler()
    bridge.Handler._command(handler, "/skill-approve anything")
    assert handler.prompts == []
    assert any("ローカル端末" in data.get("delta", "") for data, _ in handler.events)
