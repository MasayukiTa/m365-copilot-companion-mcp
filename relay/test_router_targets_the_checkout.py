"""The file tools must edit the CHECKOUT, not the scratch mount.

read_file and write_file were implemented with broker_client.get/put. Those address
$WORK_ROOT/<instance> on the host, which the container sees at /work. The instance's
repository is at /app. So a worker calling write_file on a source file got "wrote <path>",
read_file handed the same bytes straight back, and the file the build, the tests and the graded
diff read was never touched.

Self-consistently wrong is the hardest kind of broken to notice, and it survived the first
end-to-end probe for exactly that reason: the probe wrote and read through the same wrong door.
broker.sh warns about this in its own comments.

These tests use a fake broker that records what was asked of it, so the assertion is about
WHERE the call went rather than about whether it returned something plausible.
"""
import pytest

from relay import fleet_tool_router as R


class FakeBroker:
    """Records every call. exec_ answers a base64 read; get/put record that they were used."""
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.execs, self.gets, self.puts = [], [], []

    def enabled(self):
        return True

    def exec_(self, inst, cmd, timeout=None):
        self.execs.append(cmd)
        if cmd.startswith("base64 -w0 --"):
            import base64
            path = cmd.split("--", 1)[1].split("2>/dev/null")[0].strip().strip("'")
            if path not in self.files:
                return {"rc": 1, "output": ""}
            return {"rc": 0,
                    "output": base64.b64encode(self.files[path].encode()).decode()}
        return {"rc": 0, "output": ""}

    def get(self, inst, path):
        self.gets.append(path)
        return "FROM THE SCRATCH MOUNT"

    def put(self, inst, path, content):
        self.puts.append(path)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    root = tmp_path / "work" / "p00"
    root.mkdir(parents=True)
    monkeypatch.setattr(R, "_worktrees", lambda: {"inst1": str(root).lower()})
    monkeypatch.setattr(R, "STAGING_ROOT", str(tmp_path / "work"))
    fake = FakeBroker()
    import sys
    import types
    mod = types.ModuleType("relay.broker_client")
    mod.enabled = fake.enabled
    mod.exec_ = fake.exec_
    mod.get = fake.get
    mod.put = fake.put
    monkeypatch.setitem(sys.modules, "relay.broker_client", mod)
    return fake, str(root)


def test_write_file_does_not_go_through_the_scratch_mount(wired):
    fake, root = wired
    R.route("write_file", {"path": root + "/lib/x.py", "content": "print(1)\n"})
    assert not fake.puts, "write_file used broker put, which addresses /work, not the checkout"
    assert any("/app/lib/x.py" in c for c in fake.execs), fake.execs


def test_read_file_does_not_go_through_the_scratch_mount(wired):
    fake, root = wired
    R.route("read_file", {"path": root + "/lib/x.py"})
    assert not fake.gets, "read_file used broker get, which addresses /work, not the checkout"
    assert any("/app/lib/x.py" in c for c in fake.execs), fake.execs


def test_the_edit_tools_read_and_write_the_same_place(wired):
    fake, root = wired
    fake.files["/app/lib/x.py"] = "alpha\n"
    out = R.route("replace_in_file", {"path": root + "/lib/x.py", "old": "alpha", "new": "beta"})
    assert "replaced 1" in out, out
    assert not fake.gets and not fake.puts


def test_replace_refuses_when_the_count_is_not_what_the_caller_expected(wired):
    fake, root = wired
    fake.files["/app/lib/x.py"] = "alpha alpha\n"
    out = R.route("replace_in_file", {"path": root + "/lib/x.py", "old": "alpha",
                                      "new": "beta", "expected_replacements": 1})
    assert "expected 1 replacements, found 2" in out
    assert all("base64 -d" not in c for c in fake.execs), "it wrote anyway"


def test_multi_edit_is_all_or_nothing(wired):
    fake, root = wired
    fake.files["/app/lib/x.py"] = "alpha\n"
    out = R.route("multi_edit", {"path": root + "/lib/x.py",
                                 "edits": [{"old": "alpha", "new": "A"},
                                           {"old": "nothing here", "new": "B"}]})
    assert "did not match" in out and "nothing was written" in out
    assert all("base64 -d" not in c for c in fake.execs), (
        "a partially applied multi_edit leaves a file matching neither shape")
