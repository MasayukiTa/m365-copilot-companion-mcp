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
import json
import os
import re
import sys
import time

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


#: The generic donor that ships with the product: a working `T_` template with the agent id, the per-turn
#: ids and the machine's locale / time zone replaced by neutral values. Nothing in it belongs to a user,
#: a tenant or a machine (relay/test_template_seed_bundled.py fails if a GUID other than the placeholder,
#: an e-mail address or a token-shaped string appears). `seed_from_bundled` is what gives a new PC its
#: first template without a Copilot turn and without another PC to copy from.
DONOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_donor")
PLACEHOLDER_GUID = "00000000-0000-0000-0000-000000000000"
PLACEHOLDER_AGENT_ID = "T_%s.%s" % (PLACEHOLDER_GUID, PLACEHOLDER_GUID)


def donor_path(kind: str = "T") -> str:
    return os.path.join(DONOR_DIR, "donor_%s.json" % kind)


def bundled_donor(kind: str = "T"):
    """The bundled donor as a RequestTemplate-like object, or raises SeedError when this kind has none."""
    from relay.chathub import RequestTemplate
    try:
        with open(donor_path(kind), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        raise SeedError("there is no bundled donor template for agents of kind %r" % kind)
    return RequestTemplate(data.get("query") or {}, data.get("frame") or {})


def machine_locale(default: str = "en-us") -> str:
    """This machine's UI locale as the client writes it (`ja-jp`), or `default`."""
    try:
        if sys.platform == "win32":
            import ctypes
            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
                name = buf.value
                if name:
                    return name.lower()
        import locale
        name = (locale.getlocale()[0] or "").replace("_", "-")
        if name:
            return name.lower()
    except Exception:
        pass
    return default


def machine_location() -> dict:
    """This machine's offset and a valid IANA zone name for it (`Etc/GMT-9` for UTC+9: the sign is
    inverted by the IANA convention). Python cannot name the region on Windows without a table, and an
    honest offset is what resolves "tomorrow" and "this morning"."""
    off = -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone)
    hours = int(off // 3600)
    if off % 3600 == 0 and hours != 0 and abs(hours) <= 12:
        zone = "Etc/GMT%+d" % (-hours)
    else:
        zone = "UTC"
    return {"timeZoneOffset": hours, "timeZone": zone}


def seed_from_bundled(new_agent_url: str, directory=None) -> str:
    """Write the template for `new_agent_url` from the bundled donor. Returns the path written.

    Touches no network and no browser. The agent id comes from the address; the machine's locale and
    time zone are written in; the result is checked to carry the new id and no placeholder.
    Raises SeedError (and writes nothing) when the address is not a `T_` agent chat address or the
    result is not safe."""
    from relay import profile_token as PT
    from relay.chathub import RequestTemplate

    new_id = agent_id_from_url(new_agent_url)
    donor = bundled_donor(_kind(new_id)[:1])
    query, frame = derive(donor, new_agent_url)
    msg = frame.setdefault("message", {})
    msg["locale"] = machine_locale(str(msg.get("locale") or "en-us"))
    msg["locationInfo"] = machine_location()
    template = RequestTemplate(query, frame)
    blob = json.dumps({"q": template.query, "f": template.frame})
    # (the all-zero GUID also stands in for the per-turn session ids, which every turn overwrites; the
    # placeholder AGENT id is the one that must be gone)
    if not template.gpt_id or PLACEHOLDER_AGENT_ID in blob:
        raise SeedError("the derived template still carries the placeholder agent id")
    if not PT.save_template(template, new_agent_url, directory):
        raise SeedError("the template could not be written")
    return PT.template_path(new_agent_url, directory)


#: .env keys whose agent URL gets a template. Researcher / Analyst are first-party `P_` agents with no
#: bundled donor (and a built-in default URL), so they are not listed.
AGENT_URL_KEYS = ("MCP_IMPL_AGENT_URL", "MCP_FLEET_AGENT_URL")


def configured_agent_urls(env: dict = None) -> dict:
    """{key: url} for the `T_` agent addresses configured in `env` (default: .env, then the process)."""
    if env is None:
        env = {}
        try:
            from dotenv import dotenv_values
            env.update({k: v for k, v in dotenv_values(
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")).items() if v})
        except Exception:
            pass
        for k in AGENT_URL_KEYS:
            if os.environ.get(k, "").strip():
                env.setdefault(k, os.environ[k])
    return {k: str(env[k]).strip() for k in AGENT_URL_KEYS if str(env.get(k) or "").strip()}


def ensure(urls: dict, directory=None) -> list:
    """For each configured agent URL, make sure a template exists. Returns [(key, status, detail)] with
    status in `present`, `seeded`, `skipped` (not a `T_` agent address: nothing to derive from) or
    `failed`. An existing template is never overwritten: it may be a real capture."""
    from relay import profile_token as PT
    out = []
    done = {}
    for key, url in urls.items():
        if url in done:
            out.append((key, done[url][0], done[url][1]))
            continue
        try:
            agent_id_from_url(url)
        except SeedError as exc:
            res = ("skipped", str(exc))
        else:
            if PT.load_template(url, directory) is not None:
                res = ("present", PT.template_path(url, directory))
            else:
                try:
                    res = ("seeded", seed_from_bundled(url, directory))
                except SeedError as exc:
                    res = ("failed", str(exc))
        done[url] = res
        out.append((key, res[0], res[1]))
    return out


def main(argv=None) -> int:
    """`python -m relay.template_seed ensure|check`. ensure: seed what is missing. check: report only.
    Exit 0 when every configured `T_` agent has a template, 1 when one is missing or failed."""
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "ensure"
    if cmd not in ("ensure", "check"):
        print("usage: python -m relay.template_seed ensure|check")
        return 2
    from relay import profile_token as PT
    urls = configured_agent_urls()
    if not urls:
        print("[template] no agent URL is configured yet (MCP_IMPL_AGENT_URL); nothing to do")
        return 0
    if cmd == "ensure":
        results = ensure(urls)
    else:
        results = []
        for key, url in urls.items():
            try:
                agent_id_from_url(url)
            except SeedError as exc:
                results.append((key, "skipped", str(exc)))
                continue
            has = PT.load_template(url) is not None
            results.append((key, "present" if has else "missing", ""))
    bad = 0
    for key, status, detail in results:
        if status in ("failed", "missing"):
            bad += 1
            print("[template] %s: NO request template for this agent (%s). Goals will wait for a token "
                  "until one exists. Fix: run start_all.bat (it seeds one), or "
                  "`python -m relay.template_seed ensure` in the repository." % (key, detail or status))
        elif status == "seeded":
            print("[template] %s: request template created from the bundled donor (no Copilot turn)" % key)
        elif status == "present":
            print("[template] %s: request template present" % key)
        else:
            print("[template] %s: not a T_ agent chat address, no template can be derived (%s)" % (key, detail))
    return 1 if bad else 0


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


if __name__ == "__main__":
    sys.exit(main())
