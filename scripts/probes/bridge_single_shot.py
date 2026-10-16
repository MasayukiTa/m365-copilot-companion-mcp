# -*- coding: utf-8 -*-
"""One live, harmless message to a tool-less Standard agent, to see whether the fenced-block bridge works.

Sends exactly ONE user message (plus the tool-result follow-up if the model calls the fake tool).
The tool is fake: it returns a random token generated here. Success = the final answer contains it.

Never touches the production .fleet, ports 8000/8765/9222-9224. Token comes from a bgedge Edge (CDP 9340)
via the light page (no turn). Template is derived into a temp dir from a COPY of a donor template.

usage: python bridge_single_shot.py <agent_url_file> <donor_template_json> [cdp_url]
"""
import json
import os
import secrets
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from relay import profile_token as PT          # noqa: E402
from relay import template_seed as TS          # noqa: E402


def main():
    url = open(sys.argv[1], encoding="ascii", errors="replace").read().strip()
    donor_file = sys.argv[2]
    cdp = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:9340"
    assert ":9222" not in cdp and ":9223" not in cdp and ":9224" not in cdp

    tmp = tempfile.mkdtemp(prefix="bridge_probe_")
    log = []
    try:
        standin = "https://donor.example.invalid/agent"
        shutil.copy(donor_file, PT.template_path(standin, tmp))
        TS.seed(standin, url, tmp)
        template = PT.load_template(url, tmp)
        assert template is not None
        log.append("template derived, names an agent: %s" % bool(template.gpt_id))

        # token: read-only light page, then leave Playwright BEFORE speaking (event-loop clash)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0]
            token = PT.token_via_light_page(ctx, url, log=lambda m: log.append(m))
        life = PT.token_life_s(token)
        log.append("token obtained, life %.0f min" % (life / 60))
        holder = {"t": token}

        from relay.chathub import Conversation
        from relay.socket_route import websocket_connect
        conv = Conversation(lambda: holder["t"], template=template)

        secret = "BRG-" + secrets.token_hex(4)
        calls = []

        def run_tool(name, args):
            calls.append((name, args))
            return True, json.dumps({"ok": True, "echo_code": secret})

        catalogue = [{
            "name": "call_tool",
            "description": "テスト用。name に 'ping' を指定して呼ぶと echo_code を返す。",
            "parameters": {"name": "ping", "arguments": {}},
        }]
        question = ("ツール call_tool を name='ping' で1回呼び、返ってきた echo_code の値をそのまま答えてください。"
                    "他のことはしないでください。")
        if os.environ.get("BRIDGE_GOAL_FILE"):      # real read-only task through the production gateway
            from relay import tool_bridge as TB
            from dotenv import load_dotenv      # the key lives in the server's .env; never printed
            load_dotenv(os.environ.get("BRIDGE_ENV_FILE", os.path.join(ROOT, ".env")), encoding="utf-8-sig")
            question = open(os.environ["BRIDGE_GOAL_FILE"], encoding="utf-8").read()
            catalogue = TB.catalogue()
            run_tool = TB.make_run_tool("bridge_probe")
        t0 = time.time()
        answer = conv.ask(question, connect=websocket_connect, run_tool=run_tool, catalogue=catalogue)
        dt = time.time() - t0
        log.append("answer in %.1fs, tool calls made: %d %s" % (dt, len(calls), [c[0] for c in calls]))
        log.append("last_result=%s turns=%s" % (conv.last_result, conv.turns))
        log.append("ANSWER: " + (answer or "")[:600])
        log.append("SUCCESS (secret echoed): %s" % (secret in (answer or "")))
    except Exception as e:                         # report, never hide
        log.append("FAILED: %s: %s" % (type(e).__name__, str(e)[:500]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n".join(log))


if __name__ == "__main__":
    main()
