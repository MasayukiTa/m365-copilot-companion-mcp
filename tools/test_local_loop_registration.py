import os
import subprocess
import sys

from tools.local_loop_ops import (
    abort_turn,
    claim_turn,
    commit_turn,
    get_job_status,
    heartbeat,
    read_job_context,
)
from tools.tool_annotations import derive_read_only_hints


def test_mutation_annotations_fail_closed():
    hints = derive_read_only_hints([
        claim_turn, heartbeat, commit_turn, abort_turn, read_job_context, get_job_status,
    ])
    assert hints["claim_turn"] is False
    assert hints["heartbeat"] is False
    assert hints["commit_turn"] is False
    assert hints["abort_turn"] is False
    assert hints["read_job_context"] is True
    assert hints["get_job_status"] is True


def test_feature_flag_registers_protocol_tools_ahead_of_map_limit(tmp_path):
    env = os.environ.copy()
    env.update({
        "MCP_API_KEY": "test-token",
        "MCP_EXECUTION_PROFILES": "1",
        "MCP_TOOL_MAP": "1",
        "MCP_TOOL_MAP_MAX": "8",
        "MCP_LOCAL_JOB_DB": str(tmp_path / "jobs.sqlite3"),
    })
    code = "import main; print(','.join(f.__name__ for f in main.TOOLS))"
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=os.getcwd(), env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    names = proc.stdout.strip().split(",")
    for name in ("claim_turn", "heartbeat", "commit_turn", "abort_turn",
                 "read_job_context", "get_job_status", "call_tool"):
        assert name in names
    assert len(names) == 8


def test_feature_flag_off_hides_protocol_tools(tmp_path):
    env = os.environ.copy()
    env.update({
        "MCP_API_KEY": "test-token",
        "MCP_EXECUTION_PROFILES": "0",
        "MCP_TOOL_MAP": "0",
        "MCP_LOCAL_JOB_DB": str(tmp_path / "jobs.sqlite3"),
    })
    code = "import main; print(','.join(f.__name__ for f in main.TOOLS))"
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=os.getcwd(), env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    names = proc.stdout.strip().split(",")
    for name in ("claim_turn", "heartbeat", "commit_turn", "abort_turn",
                 "read_job_context", "get_job_status"):
        assert name not in names
