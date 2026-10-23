"""A token captured by one coordinator is reused by the next, safely.

Every coordinator is a new process and the capture runs in a helper process, so nothing the route
holds survived to the next run: each one opened a page for a token while its goals waited
(12-24 s measured, and 181 s / 84 s when the page timed out). relay/token_cache.py keeps the
token, encrypted, for the next process. These tests pin when it is reused, and above all every
case where it must NOT be: another agent, another account, an expired or refused token, and a
platform that cannot encrypt it.

They also pin the other half of the same fault: a page that holds a token with only a few
minutes left used to be rejected, and the capture then failed after the full 30 s page limit.
"""
import base64
import json
import os
import time

import pytest

from relay import profile_token as PT
from relay import token_cache as TC

AGENT = "https://m365.cloud.microsoft/chat/agent/T_x.y?titleId=T_x"


def _jwt(exp_in=3600, oid="user-a", tid="tenant-1", aud="https://substrate.office.com/sydney",
         now=None):
    """A token-shaped string with the claims these modules read. Not signed."""
    now = time.time() if now is None else now
    body = {"aud": aud, "exp": int(now + exp_in), "oid": oid, "tid": tid}
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return "hdr-%s.%s.sig-%s" % (oid, raw, oid)


def _protect(value):
    """A stand-in for DPAPI: reversible, and it does not contain its input."""
    return "dpapi:" + base64.b64encode(value[::-1].encode()).decode()


def _unprotect(value):
    return base64.b64decode(value[len("dpapi:"):]).decode()[::-1]


@pytest.fixture
def codec(monkeypatch):
    monkeypatch.setattr(TC, "_default_protect", _protect)
    monkeypatch.setattr(TC, "_default_unprotect", _unprotect)


def _save(token, agent=AGENT, d=None, **kw):
    return TC.save(token, agent, directory=str(d), protect=_protect, **kw)


def _load(agent=AGENT, d=None, **kw):
    return TC.load(agent, directory=str(d), unprotect=_unprotect, **kw)


# ---- the round trip, and what reaches the disk ----------------------------------------------

def test_a_saved_token_comes_back(tmp_path):
    tok = _jwt()
    assert _save(tok, d=tmp_path)
    assert _load(d=tmp_path) == tok


def test_the_token_is_never_on_disk_in_clear(tmp_path):
    """Neither the token, nor its payload, nor its signature part: only DPAPI ciphertext and
    metadata (agent hash, audience, expiry, an account fingerprint)."""
    tok = _jwt()
    assert _save(tok, d=tmp_path)
    path = TC.file_for(AGENT, str(tmp_path))
    raw = open(path, "rb").read().decode("utf-8")
    for part in tok.split("."):
        assert part not in raw
    meta = json.loads(raw)
    assert set(meta) == {"v", "agent_hash", "aud", "exp", "who", "saved_at", "blob"}
    assert meta["blob"].startswith("dpapi:")
    # the account fingerprint is a hash, not the object id
    assert "user-a" not in raw and "tenant-1" not in raw


def test_nothing_is_written_where_the_token_cannot_be_encrypted(tmp_path):
    """A platform without DPAPI makes protect_secret raise. That is a refusal, never a reason to
    write the token as it is."""
    def no_dpapi(_v):
        raise RuntimeError("DPAPI protection is only available on Windows")
    assert not TC.save(_jwt(), AGENT, directory=str(tmp_path), protect=no_dpapi)
    assert not os.path.exists(TC.file_for(AGENT, str(tmp_path)))


def test_a_protector_that_hands_the_token_back_is_refused(tmp_path):
    tok = _jwt()
    assert not TC.save(tok, AGENT, directory=str(tmp_path), protect=lambda v: v)
    assert not TC.save(tok, AGENT, directory=str(tmp_path), protect=lambda v: "x" + v)
    assert not os.path.exists(TC.file_for(AGENT, str(tmp_path)))


def test_only_the_default_protector_is_dpapi():
    """Reuse of tools/secret_store, not a second implementation."""
    import inspect
    assert "protect_secret" in inspect.getsource(TC._default_protect)
    assert "unprotect_secret" in inspect.getsource(TC._default_unprotect)


# ---- when a cached token must not be used -----------------------------------------------------

def test_an_expired_token_is_not_reused_and_the_file_goes(tmp_path):
    tok = _jwt(exp_in=3600)
    assert _save(tok, d=tmp_path)
    assert _load(d=tmp_path, now=time.time() + 7200) == ""
    assert not os.path.exists(TC.file_for(AGENT, str(tmp_path)))


def test_a_token_with_too_little_life_is_neither_saved_nor_reused(tmp_path):
    short = _jwt(exp_in=int(PT.MIN_LIFE_S) - 60)
    assert not _save(short, d=tmp_path)
    ok = _jwt(exp_in=int(PT.MIN_LIFE_S) + 600)
    assert _save(ok, d=tmp_path)
    # the same file, a few minutes later, is below the bar a fresh capture has to clear
    assert _load(d=tmp_path, now=time.time() + 700) == ""


def test_another_agent_surface_never_gets_the_token(tmp_path):
    assert _save(_jwt(), d=tmp_path)
    assert _load(agent="https://m365.cloud.microsoft/chat/agent/T_other", d=tmp_path) == ""
    # and a file copied under another agent's name is caught by the hash stored inside it
    other = "https://m365.cloud.microsoft/chat/agent/T_other"
    os.replace(TC.file_for(AGENT, str(tmp_path)), TC.file_for(other, str(tmp_path)))
    assert _load(agent=other, d=tmp_path) == ""
    assert not os.path.exists(TC.file_for(other, str(tmp_path)))


def test_a_token_for_another_resource_is_not_saved(tmp_path):
    assert not _save(_jwt(aud="https://graph.microsoft.com"), d=tmp_path)


def test_a_token_that_names_no_account_is_not_saved(tmp_path):
    body = {"aud": PT.AUDIENCE, "exp": int(time.time() + 3600)}
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    assert not _save("h." + raw + ".s", d=tmp_path)


def test_a_fingerprint_that_does_not_match_the_token_is_discarded(tmp_path):
    """The file says one account, the token inside is another: whatever made that, it is not
    something to hand to a worker."""
    assert _save(_jwt(oid="user-a"), d=tmp_path)
    path = TC.file_for(AGENT, str(tmp_path))
    meta = json.load(open(path, encoding="utf-8"))
    meta["who"] = TC.who_of(_jwt(oid="user-b"))
    json.dump(meta, open(path, "w", encoding="utf-8"))
    assert _load(d=tmp_path) == ""
    assert not os.path.exists(path)


def test_a_file_this_user_cannot_decrypt_is_discarded(tmp_path):
    """Another Windows user, or another machine: DPAPI refuses, which means 'not ours'."""
    assert _save(_jwt(), d=tmp_path)

    def refuse(_v):
        raise OSError("The data is invalid")
    assert TC.load(AGENT, directory=str(tmp_path), unprotect=refuse) == ""
    assert not os.path.exists(TC.file_for(AGENT, str(tmp_path)))


def test_corrupt_or_foreign_files_are_not_an_error(tmp_path):
    path = TC.file_for(AGENT, str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("{not json")
    assert _load(d=tmp_path) == ""
    open(path, "w", encoding="utf-8").write(json.dumps({"v": 99}))
    assert _load(d=tmp_path) == ""
    assert _load(agent="never-saved", d=tmp_path) == ""


# ---- the account, and not going backwards ------------------------------------------------------

def test_a_capture_for_another_account_replaces_the_file_whatever_its_expiry(tmp_path):
    said = []
    assert _save(_jwt(exp_in=7200, oid="user-a"), d=tmp_path)
    newer_for_b = _jwt(exp_in=3700, oid="user-b")
    assert _save(newer_for_b, d=tmp_path, log=said.append)
    assert _load(d=tmp_path) == newer_for_b
    assert any("account" in m for m in said)


def test_an_older_capture_of_the_same_account_does_not_replace_a_newer_one(tmp_path):
    later = _jwt(exp_in=7200)
    assert _save(later, d=tmp_path)
    assert _save(_jwt(exp_in=3700), d=tmp_path)
    assert _load(d=tmp_path) == later


def test_discard_all_forgets_every_agent(tmp_path):
    assert _save(_jwt(), agent="a1", d=tmp_path)
    assert _save(_jwt(), agent="a2", d=tmp_path)
    assert TC.discard_all(str(tmp_path)) == 2
    assert _load(agent="a1", d=tmp_path) == "" and _load(agent="a2", d=tmp_path) == ""


def test_the_write_goes_through_a_temporary_name_and_a_rename():
    import inspect
    assert "os.replace" in inspect.getsource(TC.save)


# ---- the coordinator takes the cached token instead of opening a page --------------------------

def _route():
    from relay.socket_route import SocketRoute
    return SocketRoute(capture_fn=lambda c, u: (None, None), connect_fn=lambda *a, **k: None,
                       enabled=True, log_path="", refresh_margin_s=1500)    # the fleet's margin


def _template(tmp_path, agent=AGENT):
    from relay.chathub import RequestTemplate
    t = RequestTemplate({"gptId": "T_x.y"}, {"threadLevelGptId": {"id": "T_x.y"}})
    assert PT.save_template(t, agent, str(tmp_path / "templates"))
    return t


@pytest.fixture
def dirs(tmp_path, monkeypatch, codec):
    monkeypatch.setattr(TC, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(PT, "TEMPLATE_DIR", str(tmp_path / "templates"))
    return tmp_path


def _manager():
    from relay.socket_capture_async import AsyncCaptureManager
    return AsyncCaptureManager(popen=lambda *a, **k: (_ for _ in ()).throw(AssertionError("helper started")),
                               log=lambda m: None)


def test_a_cold_route_takes_the_token_an_earlier_run_left(dirs):
    _template(dirs)
    tok = _jwt(exp_in=3600)
    assert TC.save(tok, AGENT)
    route, mgr = _route(), _manager()
    assert not route.ready(AGENT)
    # a token with 60 min left is far above the route's refresh margin: no page, no helper
    assert mgr.consider(route, AGENT) is False
    assert route.ready(AGENT) and route.token_for(AGENT) == tok


def test_a_short_cached_token_is_adopted_and_the_refresh_still_runs(dirs):
    """Inside the route's refresh margin the goals start at once and a helper is launched for a
    fresh token in the background."""
    _template(dirs)
    assert TC.save(_jwt(exp_in=int(PT.MIN_LIFE_S) + 300), AGENT)
    route = _route()
    started = []

    class P:
        returncode = 0

        def communicate(self, timeout=None):
            return b"", b""

    from relay.socket_capture_async import AsyncCaptureManager
    mgr = AsyncCaptureManager(popen=lambda *a, **k: started.append(1) or P(), log=lambda m: None)
    assert mgr.consider(route, AGENT) is True
    assert route.ready(AGENT)
    assert started == [1]


def test_without_a_cached_template_nothing_is_adopted(dirs):
    assert TC.save(_jwt(), AGENT)
    route = _route()
    assert _manager()._adopt_cached(route, AGENT) is False
    assert not route.ready(AGENT)


def test_a_route_that_already_has_a_token_keeps_it(dirs):
    _template(dirs)
    mine = _jwt(exp_in=5000, oid="user-a")
    route = _route()
    from relay.chathub import RequestTemplate
    route.install_capture(mine, RequestTemplate({"gptId": "T_x.y"}, {}), AGENT)
    assert TC.save(_jwt(exp_in=7000, oid="user-a"), AGENT)
    assert _manager()._adopt_cached(route, AGENT) is False
    assert route.token_for(AGENT) == mine


def test_a_capture_in_flight_is_not_pre_empted(dirs):
    """Installing here would change the route's generation under the helper and make its (newer)
    result be discarded as stale."""
    _template(dirs)
    assert TC.save(_jwt(), AGENT)
    mgr, route = _manager(), _route()
    mgr._inflight[AGENT] = object()
    assert mgr._adopt_cached(route, AGENT) is False
    assert not route.ready(AGENT)


def test_a_capture_that_succeeds_leaves_its_token_for_the_next_process(dirs):
    """_wait_one is where a helper's token is installed; it must also be saved."""
    import inspect

    from relay.socket_capture_async import AsyncCaptureManager
    src = inspect.getsource(AsyncCaptureManager._wait_one)
    assert "token_cache.save" in src


def test_a_browser_reset_forgets_the_cached_token(dirs):
    from relay import relay_fleet
    assert TC.save(_jwt(), AGENT)
    assert os.path.exists(TC.file_for(AGENT))
    relay_fleet.reset_socket_route()
    assert not os.path.exists(TC.file_for(AGENT))


def test_a_refused_token_forgets_the_cached_token_but_a_mere_mention_does_not(dirs):
    from relay import relay_fleet
    assert TC.save(_jwt(), AGENT)
    assert not relay_fleet._forget_cached_tokens("w1: connection reset while reading a frame")
    assert not relay_fleet._forget_cached_tokens("w1: no token available for the tool call")
    assert os.path.exists(TC.file_for(AGENT))
    assert relay_fleet._forget_cached_tokens("w1: HTTP 401 from the socket")
    assert not os.path.exists(TC.file_for(AGENT))


# ---- the page that holds a token with a few minutes left --------------------------------------

class _Req:
    def __init__(self, token):
        self.headers = {"authorization": "Bearer " + token}


class _Page:
    def __init__(self, tokens, url="https://m365.cloud.microsoft/chat/?x=1"):
        self._tokens, self.url, self.handler = tokens, url, None
        self.closed = False

    def on(self, event, fn):
        self.handler = fn

    def goto(self, url, **kw):
        for t in self._tokens:
            self.handler(_Req(t))

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000.0)

    def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(PT, "SHORT_TOKEN_GRACE_S", 0.3)


def test_a_token_in_the_last_minutes_of_its_life_is_used_instead_of_failing(quick):
    """The page keeps presenting the token it holds until it renews it. Rejecting that one and
    waiting out the full limit is the NoLightToken that kept goals waiting; 28 of the 48
    timeouts on record happened with the newest token in its last 10 minutes."""
    old = _jwt(exp_in=500)
    page = _Page([old])
    t0 = time.time()
    got = PT.token_via_light_page(_Ctx(page), AGENT, timeout_s=5, log=lambda m: None)
    assert got == old
    assert time.time() - t0 < 3, "waited for the page limit instead of the grace period"
    assert page.closed


def test_a_longer_lived_token_still_wins_when_it_arrives_in_time(quick):
    old, fresh = _jwt(exp_in=500), _jwt(exp_in=5000, oid="user-a")
    page = _Page([old, fresh])
    assert PT.token_via_light_page(_Ctx(page), AGENT, timeout_s=5, log=lambda m: None) == fresh


def test_a_token_too_short_to_serve_still_fails(quick):
    page = _Page([_jwt(exp_in=int(PT.SERVE_FLOOR_S) - 30)])
    with pytest.raises(PT.NoLightToken):
        PT.token_via_light_page(_Ctx(page), AGENT, timeout_s=1, log=lambda m: None)


def test_a_short_token_for_another_resource_does_not_count(quick):
    page = _Page([_jwt(exp_in=500, aud="https://graph.microsoft.com")])
    with pytest.raises(PT.NoLightToken):
        PT.token_via_light_page(_Ctx(page), AGENT, timeout_s=1, log=lambda m: None)


def test_the_failure_says_what_the_page_showed_and_never_shows_a_token(quick):
    tok = _jwt(exp_in=60, aud="https://graph.microsoft.com")
    page = _Page([tok], url="https://login.microsoftonline.com/common/oauth2?code=SECRET")
    with pytest.raises(PT.NoLightToken) as ei:
        PT.token_via_light_page(_Ctx(page), AGENT, timeout_s=1, log=lambda m: None)
    msg = str(ei.value)
    assert "no https://substrate.office.com/sydney token with" in msg
    assert "1 Bearer request" in msg and "graph.microsoft.com" in msg
    assert "login.microsoftonline.com" in msg
    for part in tok.split("."):
        assert part not in msg
    assert "SECRET" not in msg, "the page's query string must not be copied into the error"
