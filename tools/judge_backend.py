"""Where the judging question is actually asked, and nothing else.

SEPARATE FROM THE POLICY ON PURPOSE. tools/command_judge.py decides; this decides who to ask.
Keeping them apart is what lets the policy be tested exhaustively without a model, and what
lets a deployment with no judge available be a first-class state rather than an import error.

WITH NO BACKEND CONFIGURED THIS RETURNS None, and command_judge turns that into REQUIRE_HUMAN.
In shadow mode that is recorded; in enforce mode it is a refusal. That is the intended shape:
"no judge and no human" must mean "no execution", so a deployment that switches enforcement on
without a working judge finds out immediately rather than running unjudged.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

BACKEND_ENV = "MCP_JUDGE_BACKEND"


def get() -> Optional[Callable[[str], str]]:
    """The judge callable for this deployment, or None.

        MCP_JUDGE_BACKEND=agent   ask the Copilot agent over the socket route, in a fresh
                                  conversation carrying only the request
        MCP_JUDGE_BACKEND=none    no judge (default)

    "agent" reuses relay/chathub.py's one-turn API. The separation that matters is not a
    different model -- it is a separate call, a fixed system prompt, input assembled by this
    server rather than by the caller being judged, and no tools available to the judge. A fresh
    conversation gives all four even when the same model answers.
    """
    name = (os.environ.get(BACKEND_ENV) or "none").strip().lower()
    if name in ("", "none", "off"):
        return None
    if name == "agent":
        return _agent_judge
    return None


def _agent_judge(request_json: str) -> str:
    """Ask the agent one question, in a conversation that has never seen anything else.

    Raises on any transport problem, which command_judge turns into REQUIRE_HUMAN rather than
    into an allow.
    """
    from relay import chathub
    from relay import profile_token
    from tools.command_judge import SYSTEM_PROMPT

    # The command travels inside a typed field of the JSON payload, never interpolated into the
    # instructions -- so text inside it cannot end the instruction block and start a new one.
    prompt = SYSTEM_PROMPT + "\n\nREQUEST:\n" + request_json

    conv = chathub.Conversation(token_supplier=profile_token.supplier())
    return conv.ask(prompt, connect=chathub.default_connect)
