"""Check, against the RUNNING server, that a real unlock works and is not logged in clear.

Two things have to hold at once, and only one was ever in doubt: unlock has to keep WORKING --
the real password must reach the tool, or a redaction becomes an authentication failure that
looks like a wrong password -- and the ledger must not gain the plaintext.

Exercises the EXACT shape that leaked.

The row found in plaintext was tool=call_tool with args {"name": "unlock", "arguments":
{...}} -- the gateway wrapper, where the password sits one level down. Calling `unlock` as a
top-level tool does not go through the ledger at all, so it proves nothing about this.
"""
import asyncio
import io
import json
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
for ln in io.open(os.path.join(ROOT, ".env"), encoding="utf-8-sig"):
    ln = ln.strip()
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        env[k.strip()] = v.strip()
PW, KEY = env["MCP_UNLOCK_PASSWORD"], env["MCP_API_KEY"]
LED = os.path.join(ROOT, ".fleet", "tool_events.jsonl")

def leaked():
    return PW in io.open(LED, encoding="utf-8", errors="replace").read()

async def main():
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    print("plaintext before: %s" % leaked())
    t = StreamableHttpTransport(url="http://127.0.0.1:8000/mcp", headers={"Authorization": KEY})
    async with Client(t) as c:
        r = await c.call_tool("call_tool", {"name": "unlock", "arguments": {"password": PW}})
        txt = "".join(getattr(b, "text", "") for b in (r.content or []))
        print("gateway unlock replied: %s" % txt.strip()[:80])
    after = leaked()
    print("plaintext after : %s" % after)
    if after:
        print("FAIL: the ledger gained the password in clear text")
    rows = [json.loads(l) for l in io.open(LED, encoding="utf-8", errors="replace") if l.strip()]
    for row in reversed(rows):
        if row.get("event") == "call" and row.get("tool") == "call_tool":
            print("the recorded row: %s" % json.dumps(row.get("args", {}), ensure_ascii=False)[:220])
            break
    return 1 if leaked() else 0

sys.exit(asyncio.run(main()))
