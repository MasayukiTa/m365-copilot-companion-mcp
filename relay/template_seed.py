# -*- coding: utf-8 -*-
"""Seed the request template for a new agent from one that already works, without a Copilot turn.

WHY THIS EXISTS. The socket route needs, per agent, a request template: the shape of a chat request plus
the id of the agent it is for (`relay.profile_token.template_path`). The only writer was a capture that sent
a real turn in a browser page, and that was removed on purpose (nothing runs Copilot in a tab). So a new
agent had no way in.

WHAT A TEMPLATE IS. About twenty keys describing the client and the turn, and exactly three that name the
agent: `query.gptId`, `frame.gpts[0].id` and `frame.threadLevelGptId.id`. Everything else is the same for
every agent of one kind. So a template for a new agent is a template that already works with those three
strings replaced by the new agent's id, which is in the address of its chat page and needs no request.

WHAT IT REFUSES. A template that names no agent reaches the default Copilot -- no connectors, no tenant
grounding -- and answers fluently while the route reports success. So the new id must parse, must be of
the same kind as the donor (`T_` with `T_`, `P_` with `P_`), and the result is checked to carry it in all
three places before anything is written.

NOTHING HERE TOUCHES THE NETWORK OR A BROWSER. It reads a donor file, edits three strings and calls the
existing writer.
"""
from __future__ import annotations

import copy
import re

_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
#: The agent id as it appears in a chat page's address: `T_<guid>.<guid>` (the second part is the agent).
_AGENT_IN_URL = re.compile(r"/agent/(T_%s\.%s)(?=[?#/]|$)" % (_GUID, _GUID))
_BARE_ID = re.compile(r"^(T_%s\.%s)$" % (_GUID, _GUID))


class SeedError(ValueError):
    """The template could not be derived. Callers report it and do not guess."""


def agent_id_from_url(url_or_id: str) -> str:
    """The agent id (`T_<guid>.<guid>`) from a chat page address, or from the bare id."""
    text = (url_or_id or "").strip()
    m = _BARE_ID.match(text) or _AGENT_IN_URL.search(text)
    if not m:
        raise SeedError("no agent id (T_<guid>.<guid>) found in the address")
    return m.group(1)


def _kind(agent_id: str) -> str:
    return (agent_id or "")[:2]


def derive(donor, agent_url: str):
    """A new `(query, frame)` for the agent in `agent_url`, from a donor RequestTemplate-like object
    with `.query` and `.frame`. The donor is not modified."""
    new_id = agent_id_from_url(agent_url)
    query = copy.deepcopy(dict(donor.query or {}))
    frame = copy.deepcopy(dict(donor.frame or {}))

    donor_frame_id = ((frame.get("gpts") or [{}])[0] or {}).get("id") or ""
    if not donor_frame_id:
        raise SeedError("the donor template names no agent; it cannot be used as a donor")
    if _kind(donor_frame_id) != _kind(new_id):
        raise SeedError("the donor is a %r agent and the new one is %r; a template is only "
                        "derived within one kind" % (_kind(donor_frame_id), _kind(new_id)))

    # query.gptId carries a platform suffix after the id (for example `@MOS3`); keep whatever the donor had.
    old_query_id = str(query.get("gptId") or "")
    suffix = ""
    if old_query_id.startswith(donor_frame_id):
        suffix = old_query_id[len(donor_frame_id):]
    elif "@" in old_query_id:
        suffix = "@" + old_query_id.split("@", 1)[1]
    query["gptId"] = new_id + suffix

    gpts = frame.get("gpts") or [{}]
    gpts[0] = dict(gpts[0] or {})
    gpts[0]["id"] = new_id
    frame["gpts"] = gpts
    thread = dict(frame.get("threadLevelGptId") or {})
    thread["id"] = new_id
    frame["threadLevelGptId"] = thread

    # Check the result, not the intention: the id must be in all three places and the donor's must be gone.
    for where, value in (("query.gptId", query["gptId"]), ("frame.gpts[0].id", frame["gpts"][0]["id"]),
                         ("frame.threadLevelGptId.id", frame["threadLevelGptId"]["id"])):
        if new_id not in value:
            raise SeedError("%s does not carry the new agent id" % where)
        if donor_frame_id != new_id and donor_frame_id in value:
            raise SeedError("%s still carries the donor's agent id" % where)
    return query, frame


def seed(donor_agent_url: str, new_agent_url: str, directory=None) -> str:
    """Write the template for `new_agent_url` derived from the working template of `donor_agent_url`.
    Returns the path written. Raises SeedError if the donor is missing or the result is not safe."""
    from relay import profile_token as PT
    from relay.chathub import RequestTemplate

    donor = PT.load_template(donor_agent_url, directory)
    if donor is None:
        raise SeedError("no usable template is cached for the donor agent")
    query, frame = derive(donor, new_agent_url)
    template = RequestTemplate(query, frame)
    if not template.gpt_id:
        raise SeedError("the derived template names no agent")
    if not PT.save_template(template, new_agent_url, directory):
        raise SeedError("the template could not be written")
    return PT.template_path(new_agent_url, directory)
