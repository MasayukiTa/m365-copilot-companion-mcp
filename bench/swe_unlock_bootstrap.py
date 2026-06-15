"""One-time unlock of the Copilot backend IP for the fleet (30-day TTL; the prior unlock expired).

Drives the M365 Copilot agent (via the already-running CDP Edge on :9222) to call the MCP
`unlock(password=...)` tool once, then `list_unlocked` to confirm. Reuses the proven relay driver,
so it doubles as an end-to-end CDP smoke. The unlock password + agent URL are read from .env
IN-PROCESS -- they never appear on a command line or in stdout.

  .venv\\Scripts\\python.exe bench/swe_unlock_bootstrap.py
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dotenv import load_dotenv
load_dotenv(os.path.join(REPO, ".env"))

agent = os.environ.get("MCP_FLEET_AGENT_URL") or os.environ.get("MCP_IMPL_AGENT_URL")
pw = os.environ.get("MCP_UNLOCK_PASSWORD")
cdp = os.environ.get("MCP_CDP_URL") or os.environ.get("MCP_CDP_PORT") and \
    ("http://localhost:" + os.environ["MCP_CDP_PORT"]) or "http://localhost:9222"
if not agent or not pw:
    print("MISSING agent url or unlock password in .env"); sys.exit(2)

# Goal: unlock this IP, then confirm. Kept terse so the agent does exactly these two tool calls.
goal = ('First call the tool `unlock` with password="%s" (this unlocks mutating tools for this '
        'backend IP). Then call `list_unlocked` and tell me whether this IP is now unlocked. '
        'Do not do anything else. When you have confirmed it, write DONE.' % pw)

# Invoke the tested relay main() with a constructed argv (password stays in-process argv only).
sys.argv = ["relay", "--cdp-url", cdp, "--conversation-url", agent,
            "--goal", goal, "--run-id", "unlock_bootstrap", "--max-turns", "6",
            "--per-turn-timeout", "300", "--no-research"]
# (--max-turns 6: unlock+list_unlocked = 2 calls, +margin for tool discovery on first turn)
from relay.copilot_autopilot_relay import main
main()
print("\n[unlock_bootstrap] relay finished")
