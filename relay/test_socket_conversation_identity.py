"""A socket worker used to end without saying which conversation it had held.

The server keeps conversation history -- turn two arrives on a physically different socket
and still continues, which is only possible if the backend is holding it keyed by the
conversation id. What this system threw away was the key. So a follow-up instruction after a
fleet run had nothing to reattach to and started a conversation that had never heard the
first one, and 531 of the 542 sessions on this machine carry no way back at all.

Recording only. Nothing reuses these ids yet: whether the id the client proposes and the one
the backend answers with are the same is a question this data exists to settle, and reusing
one before that is answered is how "it worked, but it was a different conversation" ships.

Run: pytest -q relay/test_socket_conversation_identity.py
"""
import relay.socket_driver as SD


class _Conv(object):
    def __init__(self, client="c1", server="s1", session="k1", turns=3):
        self.conversation_id = client
        self.server_conversation_id = server
        self.session_id = session
        self.turns = turns


def _drv(conv):
    return SD.CopilotSocketDriver(conv, connect=lambda *a, **k: None)


def test_the_driver_can_say_which_conversation_it_held():
    got = _drv(_Conv()).conversation_ids()
    assert got["client"] == "c1"
    assert got["server"] == "s1"
    assert got["session"] == "k1"
    assert got["turns"] == 3


def test_both_ids_are_exposed_because_they_are_not_known_to_agree():
    """conversation_id_of already suspects they can differ: a backend that did not create the
    conversation will not continue it."""
    got = _drv(_Conv(client="mine", server="theirs")).conversation_ids()
    assert got["client"] != got["server"]
    assert got["client"] and got["server"]


def test_a_conversation_the_backend_never_named_reports_an_empty_server_id():
    """Rather than quietly reporting the client's id as if the server had confirmed it."""
    got = _drv(_Conv(server="")).conversation_ids()
    assert got["server"] == ""
    assert got["client"] == "c1"


def test_asking_never_raises():
    """It is called on the way out of a worker, where an exception would cost the record of
    everything else that happened."""
    class Broken(object):
        @property
        def conversation_id(self):
            raise RuntimeError("gone")

    assert _drv(Broken()).conversation_ids() == {}


def test_the_fleet_records_it_beside_the_outcome():
    from pathlib import Path
    src = (Path(SD.__file__).parent / "relay_fleet.py").read_text(encoding="utf-8")
    body = src[src.index('"worker_done", worker=self.name') - 1500:]
    body = body[:body.index('reason=(self.reason or "")[:200])') + 40]
    assert "self.drv.conversation_ids()" in body
    assert 'conv_client=ids.get("client", "")' in body
    assert 'conv_server=ids.get("server", "")' in body


def test_a_tab_worker_records_no_conversation_ids():
    """The tab route has its own identity (a conversation URL) and this is not it."""
    from pathlib import Path
    src = (Path(SD.__file__).parent / "relay_fleet.py").read_text(encoding="utf-8")
    assert 'if getattr(self, "socket", False) and self.drv is not None:\n                    ids = self.drv.conversation_ids()' in src


def test_nothing_reuses_the_recorded_ids_yet():
    """Reusing one before it is established which is authoritative is how a follow-up lands in
    a different conversation while looking like it worked."""
    from pathlib import Path
    root = Path(SD.__file__).parent.parent
    for name in ("relay/socket_route.py", "relay/chathub.py", "relay/relay_fleet.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "conv_server" not in src or name == "relay/relay_fleet.py"
    fleet = (root / "relay" / "relay_fleet.py").read_text(encoding="utf-8")
    assert fleet.count("conv_server") == 1, "recorded once, read nowhere"


# ── continuing one, on the strength of a measurement ────────────────────────────────

def test_a_driver_can_be_asked_to_continue_a_conversation():
    """MEASURED 2026-08-24: a passphrase planted in one process came back verbatim in a second
    process holding only this id -- across a fresh token, a fresh session id, and either value
    of isStartOfSession. The control arm, an unused id, said it did not know."""
    import inspect

    import relay.socket_route as SR
    sig = inspect.signature(SR.SocketRoute.driver_for)
    assert "conversation_id" in sig.parameters
    assert sig.parameters["conversation_id"].default == ""


def test_starting_a_conversation_stays_the_default():
    """Continuing one is something a caller asks for by name."""
    import inspect

    import relay.socket_route as SR
    src = inspect.getsource(SR.SocketRoute.driver_for)
    assert "if conversation_id:" in src
    assert "conv.conversation_id = str(conversation_id)" in src


def test_the_measurement_is_recorded_where_the_behaviour_is():
    """So the next reader does not have to re-derive whether this works, or assume it does."""
    import inspect

    import relay.socket_route as SR
    doc = inspect.getdoc(SR.SocketRoute.driver_for) or ""
    assert "MEASURED" in doc
    assert "control" in doc


def test_the_route_does_not_invent_a_conversation_to_continue():
    """An empty id must start a fresh one rather than reach for a remembered default: resuming
    the wrong conversation looks exactly like resuming the right one."""
    import inspect

    import relay.socket_route as SR
    src = inspect.getsource(SR.SocketRoute.driver_for)
    assert "self.last_conversation" not in src
    assert "or self._last" not in src
