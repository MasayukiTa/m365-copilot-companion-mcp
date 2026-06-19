"""relay_fleet.py -- run N AUTONOMOUS relays in parallel (spec §1 fleet x §3/§4 loop).

Where the official Cowork is one autonomous track per user, this drives MANY goals
at once: N Copilot conversations, each pursued to DONE by its own deterministic
relay loop, advanced from a single thread in a non-blocking round-robin. While the
client does one cheap poll, all N agents are thinking server-side in parallel, so
their (slow) turns overlap -- that's the throughput edge.

MEMORY DISCIPLINE (why this is not just "open N tabs"):
  Each M365 Copilot tab is a heavy SPA (~0.3-0.6 GB). On a 16 GB laptop already
  running other work, opening many at once exhausts RAM -- Edge then crashes, and
  when it auto-restarts WITHOUT --remote-debugging-port the CDP endpoint is gone and
  the whole run dies (observed). So this fleet:
    * never opens all N tabs up front -- it keeps at most `max_concurrent` open,
    * sizes `max_concurrent` to *available* physical memory (GlobalMemoryStatusEx),
    * CLOSES each conversation's tab the instant it reaches a terminal state, which
      frees that RAM and lets the next queued goal open. Resuming = just run again;
      a fresh tab is opened for each goal.

Each worker reuses the same loop policy as run_relay (PROTOCOL framing; decide
DONE / STUCK / no-progress / FAIL->fix / CONTINUE per turn) but as a non-blocking
state machine so the open ones interleave. No threads, no async.

  results = run_relay_fleet(context, [goalA, goalB, goalC], agent_url)
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import time

from .acceptance import Check, normalize_checks, run_all_blocking
from .copilot_autopilot_relay import (
    CONTINUE_JOB, COPILOT_SELECTORS, ConversationClosed, CopilotWebDriver, FIX_JOB,
    GenerationInProgress, PROTOCOL, REFUTE_FIX_JOB, RETRY_JOB, VERIFY_FIX_JOB,
    _is_processing, default_notify, extract_research, goal_not_seen, has_end_marker,
    reported_stuck, transient_backoff,
)
from .planner import PLAN_PROMPT, extract_plan, plan_ready

TERMINAL = ("done", "stuck", "maxturns", "error", "cancelled")
# non-terminal but not yet occupying a tab; counts as "still running" for the loop.
PENDING = "pending"

# Replies that mean the Copilot AGENT/PATH is down, NOT that the task failed. The error number /
# session-id / timestamp vary, but the prose is stable in both EN and JP. If one of these repeats,
# the agent is almost certainly wedged or STOPPED/DISABLED in Copilot Studio (a per-agent admin
# block -- seen when one agent died while the user's others kept working). The fix is to check
# Copilot Studio / switch agents, NOT to keep retrying. Stored lowercase; JP is unaffected by
# .lower(), so a single `marker in resp.lower()` covers both languages.
AGENT_DEAD_MARKERS = (
    "予期しないエラー", "システムエラー", "systemerror", "unexpected error",
    "something went wrong", "ページをもう一度読み込", "reload the page", "try reloading",
    "管理者に問い合わせ", "contact your administrator", "contact the administrator",
    "問題が解決しない場合", "if the problem persists",
)

# NETWORK-OUTAGE RESILIENCE (2026-06-17). A flaky corporate network / devtunnel can drop the path
# to the MCP backend for seconds-to-minutes. The retry budgets must be WALL-CLOCK windows, not tiny
# counts: a 10-count transient retry exhausted in ~55s and a 3-strike dead-agent detector STUCK in
# seconds, so a brief blip "ended everything". These windows let a worker RIDE OUT an outage (keep
# retrying with backoff) and give up only if the failure PERSISTS past the window -- at which point
# it really is a down/banned agent. Env-tunable.
NET_RETRY_WINDOW_S = float(os.environ.get("MCP_NET_RETRY_S", "1800"))      # send/CDP/network: 30 min
AGENT_ERR_WINDOW_S = float(os.environ.get("MCP_AGENT_ERR_S", "1200"))     # agent SystemError: 20 min

# TOOL-BACKEND-UNREACHABLE detector. When the MCP tool path (devtunnel) drops for even a moment, the
# agent's tool calls fail and it WRONGLY concludes its tools don't exist / aren't assigned and self-
# locks ("再試行では解消しません / won't respond without new input"). That is INFRA-FALSE (the tools
# DO exist; the network blipped), NOT a genuine STUCK -- but the agent's own STUCK was being accepted
# as a terminal miss. Detect it, RE-SEND THE GOAL (the "new input" it demands) to ride out the blip,
# and only give up (as a re-queueable infra stuck, NOT a miss) after the wall-clock window.
TOOL_UNREACHABLE_MARKERS = (
    "ツールが存在しない", "ツールが割り当てられ", "ツールがこのセッションに割り当て",
    "ツールが存在しないため", "再試行では解消", "再試行では解決しません",
    "恒常的制約", "構造的制約のため", "当環境へのツール有効化", "ツール有効化、または",
    "no tools are assigned", "tools are not available", "tool is not available to this session",
)

# CONNECTION-CONSENT detector. The FIRST time the agent calls an MCP tool, Copilot can show a
# connection-consent card ("この資格情報を 接続マネージャーを開く で検証してください ... 再試行")
# instead of executing -- the MCP connector's per-user connection is not authorized yet. This is
# NOT a dead agent and NOT a task failure: a HUMAN must authorize the connection IN THE DEDICATED
# EDGE (the consent is bound to that browser session/profile, not the account, so authorizing
# elsewhere does NOT count). The relay cannot click it (security gate). Detect it, SURFACE the
# headless Edge to the foreground so the user can authorize, and STUCK with an actionable reason
# rather than looping the card until MAXTURNS.
CONSENT_MARKERS = (
    "接続マネージャー", "この資格情報を", "接続の準備が整ったら",
    "open connection manager", "connection manager", "verify this credential",
    "verify your credential", "authorize the connection", "set up this connection",
)

# STUCK-ON-REDIRECT detector (2026-06-18, W4 xarray-3364). A worker tab can land on the M365
# SSO-redirect / landing page (e.g. https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2)
# instead of its agent conversation. That page has NO composer, so EVERY send fails with an empty
# composer (text_len:1, is_processing:False, phase:waiting_processing). The existing login-wall
# detector (edge_recover.looks_like_login) does NOT catch this -- it is a same-origin redirect, not
# a login.microsoftonline sign-in form -- so the send-retry loop kept hammering the wrong page for
# ~1h (~29/30 consecutive failures, 15:05-16:04) until the turn timed out -> STUCK. The fix: when a
# worker's send keeps failing AND its tab is on such a redirect/landing page (not the agent surface),
# RE-NAVIGATE the tab to the agent URL it was launched to drive (mirrors _open_fresh's about:blank
# re-nav) instead of retrying send forever. Bounded per turn so a persistently-wrong page still
# falls through to the existing terminal handling.
REDIRECT_URL_MARKERS = ("redirfrom=", "csrtossr", "auth=")

# A real conversation URL carries a UUID after /conversation/ OR /chat/ (the new agent uses the
# /chat/<guid> form). Used to capture conv_url regardless of which path the agent uses, without
# mistaking the agent BASE url (/chat/agent/T_xxx) for a conversation.
_CONV_GUID_RE = re.compile(
    r"/(?:conversation|chat)/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def looks_like_redirect_landing(url):
    """True if `url` looks like an M365 SSO-redirect / landing page rather than an agent
    conversation. Heuristic, deliberately conservative: it keys off the redirect query markers
    that appear on the CsrToSSR landing URL (W4) and the absence of a real conversation path.
    An agent conversation URL carries a `/conversation/<guid>` (or `/chat/<guid>`) segment; a
    bare landing/redirect page does not. Never raises -> False on odd input (safe: 'not a
    redirect', so the new re-nav branch simply does not fire and behaviour is unchanged)."""
    try:
        u = (url or "").lower()
    except Exception:
        return False
    if not u:
        return False
    # explicit redirect/SSO query markers (the W4 CsrToSSR landing URL)
    if any(m in u for m in REDIRECT_URL_MARKERS):
        return True
    return False


def on_agent_surface(url):
    """True if `url` looks like a real agent conversation surface (a non-empty
    /conversation/<id> or /chat/<id> path segment), i.e. NOT a bare redirect/landing page
    like '.../chat/?redirfrom=...'. Used as a CHEAP sanity check; the authoritative
    confirmation after a re-navigation is composer-present (a DOM probe), since the URL
    alone can lag. Strips the query string first so '/chat/?redirfrom=...' (empty id) does
    NOT count as a surface. Never raises -> False on odd input (conservative)."""
    try:
        path = (url or "").lower().split("?", 1)[0].split("#", 1)[0]
    except Exception:
        return False
    for marker in ("/conversation/", "/chat/"):
        if marker in path:
            tail = path.split(marker, 1)[1].strip("/")
            if tail:                       # there is an actual id after /conversation//chat/
                return True
    return False


def _reap_orphan_redirect_tabs(context, workers):
    """Close stray SSO-redirect / landing tabs that are NOT owned by any worker (a failed goto or
    an auth bounce leaves one behind). Never touches a worker's live page or a real conversation
    surface. Mirrors the bridge-side reaper -- keeps the fleet Edge from piling up dead tabs within
    a long chunk (the per-chunk hard-reset only clears them between chunks). Never raises."""
    try:
        owned = set(id(w.page) for w in workers if getattr(w, "page", None) is not None)
        for pg in list(getattr(context, "pages", []) or []):
            if id(pg) in owned:
                continue
            try:
                u = pg.url or ""
            except Exception:
                continue
            if looks_like_redirect_landing(u) and not on_agent_surface(u):
                try:
                    pg.close()
                except Exception:
                    pass
    except Exception:
        pass


# Statuses where the main thread is legitimately busy with a BOUNDED acceptance check
# (eval/verification), NOT a wedged Edge. The watchdog must not hard-reset while a worker
# is in one of these -- doing so throws away in-progress eval and resumes every goal at
# attempt 1 (the sphinx-8595 t7->t1 regression). See fleet_runner._watchdog.
VERIFY_STATUSES = ("verifying",)

# Upper bound (seconds) the watchdog will tolerate a frozen status.json while a worker
# claims to be in a blocking acceptance eval. The SWE-bench docker eval is capped at
# ~1300s (swe_check timeout) inside swe_check.py; we add generous margin so a legitimately
# slow eval is never killed, but a TRULY wedged Edge that merely happens to be mid-verify
# is still eventually recovered. Beyond this, a non-advancing status is treated as wedged.
EVAL_STALL_CEILING_S = 1500


class FleetContextLost(Exception):
    """Raised when the underlying Edge/CDP context died mid-run (wedged or hard-reset).
    Carries the goals that had not finished, so the runner can reconnect and resume."""
    def __init__(self, unfinished):
        super().__init__("fleet CDP context lost")
        self.unfinished = unfinished


class _Transcript:
    """Append-only full-text log of one worker's conversation, one JSON object per line.

    Each line is {"turn": n, "role": "user"|"assistant", "text": <full, untruncated>,
    "ts": epoch}. A first "meta" line records the worker name / goal / conv guid so a
    reader can match the file to a card even before any turn lands.

    The unique key is the KEY of the file, not the worker name: worker names (w0/w1) are
    reused across rounds/runs, so keying on the name alone would interleave two unrelated
    conversations into one file. The key is `<run_id>_<name>` where run_id is unique per
    run_relay_fleet() invocation (its start time, base36) -- so two runs that both have a
    'w0' write to different files. When the conversation's guid (conv_url tail) becomes
    known it is recorded in-line; we do NOT rename the open file (that races with appends).

    Completely exception-safe: any I/O failure is swallowed so the fleet never stalls on a
    logging hiccup. Each append is flushed so a crash leaves whole lines, not partial ones."""

    def __init__(self, directory, key, name, goal):
        self.dir = directory
        self.key = key
        self.path = os.path.join(directory, key + ".jsonl") if directory else None
        self._guid_logged = False
        if not self.path:
            return
        try:
            os.makedirs(directory, exist_ok=True)
            # fresh file for this run+worker; truncate any stale leftover with the same key
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"meta": True, "key": key, "name": name,
                                    "goal": goal, "ts": time.time()},
                                   ensure_ascii=False) + "\n")
                f.flush()
        except Exception:
            self.path = None

    def _append(self, obj):
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                f.flush()
        except Exception:
            pass

    def user(self, turn, text):
        self._append({"turn": turn, "role": "user", "text": text or "", "ts": time.time()})

    def assistant(self, turn, text):
        self._append({"turn": turn, "role": "assistant", "text": text or "", "ts": time.time()})

    def note_guid(self, guid):
        """Record the conversation guid once it's known (idempotent)."""
        if self._guid_logged or not guid:
            return
        self._guid_logged = True
        self._append({"guid": guid, "ts": time.time()})


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def avail_phys_mb() -> float:
    """Available physical memory in MB (Windows). Best-effort; ~4 GB on failure."""
    try:
        m = _MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / (1024.0 * 1024.0)
    except Exception:
        return 4096.0


def ram_room_for_tab(floor_mb=2000.0) -> bool:
    """True iff there is enough free physical RAM to open ANOTHER browser tab without crowding
    the machine. Used to RAM-gate the SUB-AGENT side-pages (research / refuter) -- the fleet's
    worker-tab autoscale doesn't count those, so on a low-RAM box even a single task's ultra
    pipeline (main + research + refuter tabs) could overload the Edge until the sweep wedged and
    the watchdog hard-reset it. Each side-page opens lazily once this returns True, so the live
    tab count tracks free RAM at ALL granularities, not just at worker admission."""
    return avail_phys_mb() >= floor_mb


def auto_concurrency(n_goals, per_tab_mb=700, headroom_mb=2048, hard_cap=4):
    """How many heavy M365 tabs we can afford open at once, given free RAM right now.
    Keep `headroom_mb` for the user's other work; budget `per_tab_mb` per Copilot tab;
    never exceed `hard_cap` (Microsoft per-user fair-use also wants N modest)."""
    fit = int((avail_phys_mb() - headroom_mb) / per_tab_mb)
    return max(1, min(n_goals, fit, hard_cap))


# ── Disk-floor admission (capacity-aware continuous admission, 2026-06-14) ────────────────
# The fleet now admits jobs as fast as BOTH RAM and DISK allow, draining and re-admitting
# continuously (no batch barrier). The disk constraint matters because each SWE-bench eval
# pulls/builds Docker images and, on a timeout, can leave a detached container inflating the
# C: vhdx. So before opening a new tab we make sure C: free will stay above a reserved floor
# even after the new job's eval consumes its disk budget. The floor is USER-CONFIGURABLE
# (env SWE_DISK_FLOOR_GB, default 6; the cockpit can write it later) because "always keep N GB
# free" is a safety/usability win for normal use too, not just the bench.
DEFAULT_DISK_FLOOR_GB = float(os.environ.get("SWE_DISK_FLOOR_GB", "6"))
# Disk a single not-yet-started eval is assumed it MIGHT consume before its own per-instance
# cleanup reclaims it (image layers etc.). Used to look ahead so we never open a tab that would
# itself push C: under the floor. Env-tunable; conservative default. 0 disables look-ahead.
DEFAULT_EVAL_DISK_GB = float(os.environ.get("SWE_EVAL_DISK_GB", "0"))


def free_disk_gb(path=None):
    """Free space (GB) on the drive holding `path` (default: this repo's drive, i.e. C:).
    Best-effort; returns a large number on failure so a read error never WRONGLY blocks
    admission (RAM gate + per-instance disk guard in swe_check still protect the floor)."""
    try:
        import shutil
        if not path:
            path = os.path.splitdrive(os.path.abspath(__file__))[0] + os.sep
        return shutil.disk_usage(path).free / (1024.0 ** 3)
    except Exception:
        return 1e6


# Per-repo cold-build disk estimate (GB) -- the test's PER-UNIT WEIGHT in the disk gate. A 7GB
# matplotlib/sklearn build must reserve far more than a 2GB requests one, so the gate can safely
# PAIR light evals while keeping heavy ones solo, instead of one flat number that's wrong for both.
# Heavy values are tuned to ~(typical C: free - floor) so exactly ONE admits and a 2nd is blocked.
# Calibrated to MEASURED build footprints (2026-06-14), not guesses: matplotlib's cold build dips
# C: ~10GB (12->1.6GB observed); scikit-learn ~4GB (C: 13.7->7.3 with sklearn+requests, minus the
# 2.34GB requests image); requests image is 2.34GB. The earlier flat 7GB for sklearn was too high
# and wrongly blocked TWO sklearn from pairing -- at ~4-5GB each, two fit (2*5=10 <= C:free - min).
_REPO_EVAL_GB = {
    "matplotlib__matplotlib": 9.0, "astropy__astropy": 9.0,   # ~10GB build -> stays solo
    "scikit-learn__scikit-learn": 5.0,                          # ~4GB measured -> two can pair
    "django__django": 3.0, "sympy__sympy": 3.0, "sphinx-doc__sphinx": 3.0,
    "pydata__xarray": 3.0, "pytest-dev__pytest": 2.5, "pylint-dev__pylint": 2.5,
    "psf__requests": 2.5, "pallets__flask": 2.5,
}
DEFAULT_REPO_EVAL_GB = 5.0
EVAL_DISK_PERREPO = os.environ.get("SWE_EVAL_DISK_PERREPO") == "1"
# Crash-avoidance hard minimum for the per-repo gate (GB). A single heavy build (matplotlib ~7GB)
# legitimately dips C: BELOW the 6GB soft floor and recovers -- conc1 relies on that. So the
# per-repo gate reserves the SUM of all concurrent builds (in-flight + new) against this lower
# hard minimum, not the soft floor: heavy admits solo (12-7=5 >= 3), heavy+heavy or heavy+medium
# is blocked (would dip under 3GB, the level near which concurrent builds corrupted WSL), light
# evals still pair. Env-tunable.
PERREPO_HARD_MIN_GB = float(os.environ.get("SWE_EVAL_HARD_MIN_GB", "3"))


def repo_eval_gb(inst):
    """Per-instance cold-build disk estimate by repo (see _REPO_EVAL_GB). inst is '<owner>__<name>
    -<n>' or a worktree path embedding it."""
    inst = (inst or "").split("wt_")[-1]
    repo = inst.rsplit("-", 1)[0]
    return _REPO_EVAL_GB.get(repo, DEFAULT_REPO_EVAL_GB)


def disk_admission_ok(floor_gb=None, eval_gb=None, free_gb=None, building=0, reserve_gb=None):
    """Pure predicate: may we open ANOTHER eval-bearing tab without risking the disk floor?

    OK iff (current C: free) - (disk this new eval AND every already-admitted eval still in
    flight might use) >= floor. The `building` term is the count of eval-bearing tabs already
    open whose Docker builds have NOT yet been reclaimed: each will consume up to eval_gb, so we
    must reserve eval_gb*(building+1), not just eval_gb for the one we're about to open. Without
    this, admission-time free-space looks fine for tab #1, #2, #3... (no build has consumed disk
    YET), all get admitted, then their cold builds run CONCURRENTLY and blow past the floor --
    which is exactly how 5 concurrent heavy builds crashed C: and corrupted WSL (2026-06-14).

    Splitting the free-reading out (`free_gb`) keeps this unit-testable with a mocked disk. A
    non-positive floor disables the gate (always OK) -- normal (non-bench) use may not want a
    reserve. eval_gb=0 keeps legacy floor-only behavior (no per-build look-ahead)."""
    floor = DEFAULT_DISK_FLOOR_GB if floor_gb is None else float(floor_gb)
    if floor <= 0:
        return True
    free = free_disk_gb() if free_gb is None else float(free_gb)
    if reserve_gb is not None:
        # caller computed an exact reserve (e.g. per-repo sum of in-flight + new build sizes)
        return (free - float(reserve_gb)) >= floor
    eval_gb = DEFAULT_EVAL_DISK_GB if eval_gb is None else float(eval_gb)
    reserve = eval_gb * (1 + max(0, int(building)))
    return (free - reserve) >= floor


def ram_target_cap(open_now, current_cap, ceiling,
                   per_tab_mb=700, headroom_mb=1400, floor=1, up_margin_mb=0):
    """RAM-aware live concurrency target (autoscale). Recomputed each loop: given how many
    tabs are open right now (their RAM is already reflected in the free-RAM reading) and how
    much headroom we want to keep for the user, how many tabs can we SUSTAIN?

    Asymmetric on purpose, to never re-trigger the RAM-exhaustion crash:
      * scale UP by at most ONE tab per call (gentle ramp -- re-evaluated every loop), and
      * allow scale DOWN to the raw target immediately. A lower cap is SOFT: running tabs are
        not killed, we just stop opening new ones until some finish (natural drain).

    ANTI-THRASH HYSTERESIS (`up_margin_mb`): with up_margin_mb=0 (default, back-compat) the up-
    and down-thresholds coincide, so a fleet can oscillate 1<->3 every loop: open a tab -> RAM
    tightens just under the line -> drain target -> tab closes -> RAM frees just over the line
    -> ramp up -> repeat. A positive `up_margin_mb` makes the UP step require that much EXTRA
    free RAM beyond what merely holding the new tab needs, so once the fleet settles at a water
    level a small RAM jitter no longer pushes it back up -- it HOLDS. The DOWN side is unchanged
    (drains immediately on a real deficit), so the dead-band only damps needless growth.
    Clamped to [floor, ceiling] (ceiling = the user's configured maximum)."""
    avail = avail_phys_mb()
    # FLOOR division (not int(): truncates toward zero) so a RAM *deficit* yields a negative
    # term and the target actually drops below open_now -> drains. e.g. (-400)//700 == -1.
    raw = open_now + int((avail - headroom_mb) // per_tab_mb)
    target = max(floor, min(raw, ceiling))
    if target > current_cap:
        # hysteresis: only ramp UP if there is up_margin_mb of headroom ON TOP of the per-tab
        # budget the new tab needs (a dead-band so jitter around the line doesn't re-grow us).
        if (avail - headroom_mb - up_margin_mb) >= per_tab_mb:
            target = min(current_cap + 1, ceiling)   # ramp up one tab at a time
        else:
            target = current_cap                     # in the dead-band -> HOLD, don't grow
    return target


def _open_fresh(context, url):
    """Open a NEW tab on a fresh chat of the agent. Tolerant of slow navigation
    (a busy Edge can miss the 30s domcontentloaded) -- we proceed and wait for the
    composer to render either way. If a sign-in page appears, the background Edge is
    surfaced once so the user can authenticate."""
    from .edge_recover import surface, looks_like_login
    pg = context.new_page()
    surfaced = False
    # Up to 3 navigation attempts: a failed goto leaves the tab on about:blank, and
    # waiting 45s for a composer that will never come just leaves about:blank on screen.
    # Detect about:blank early (~4s) and RE-navigate instead of staring at it.
    for attempt in range(3):
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        for k in range(25):
            pg.wait_for_timeout(1000)
            if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                return pg
            try:
                u = pg.url or ""
                if not surfaced and looks_like_login(u):
                    surface(); surfaced = True
                elif u == "about:blank" and k >= 3:
                    break                      # stuck on about:blank -> re-navigate
            except Exception:
                pass
    return pg


def goal_fields(goal):
    """Normalize a goal into (text, checks, cwd). A goal is either a plain string
    (no acceptance check -- legacy/back-compat) or a dict
    {"text"|"goal": str, "check"|"checks": dict|list, "cwd": str}. This is how a
    goal carries machine-checkable acceptance criteria into the verification gate."""
    if isinstance(goal, dict):
        text = goal.get("text") or goal.get("goal") or ""
        checks = normalize_checks(goal.get("checks") or goal.get("check"))
        cwd = goal.get("cwd") or None
        return text, checks, cwd
    return str(goal), [], None


class RelayWorker:
    """One conversation running one goal to completion, as a non-blocking machine.
    Starts WITHOUT a tab (status 'pending'); attach() opens one, close() frees it.

    Acceptance gate (spec 3-3): if the goal carries `checks`, a Copilot "DONE" does
    NOT end the worker -- it moves to the 'verifying' state, where the frame runs the
    checks LOCALLY (acceptance.Check). Pass -> real DONE; fail -> the actual failure is
    re-injected and the agent keeps working, up to max_verify_attempts (then STUCK with
    outcome VERIFY_FAILED). No checks -> DONE is accepted as before (back-compat)."""

    def __init__(self, goal, name, max_turns=1000, dwell_s=4.0,
                 per_turn_timeout_s=240, max_no_progress=3, max_verify_attempts=3,
                 refuter=False, max_refute=2, plan_mode=False, review_lenses=None,
                 max_transient=10, transcript_dir=None, run_id="", busy_writer=None,
                 max_research=3):
        self.page = None
        self.drv = None
        text, checks, cwd = goal_fields(goal)
        self.goal = text
        self.checks = checks
        self.cwd = cwd
        self.max_verify_attempts = max_verify_attempts
        self.verify_attempts = 0
        # transient-failure retries (network/tool/send hiccups) -- the relay analog of
        # Claude Code retrying a failed request rather than giving up. Budget + backoff.
        self.max_transient = max_transient
        self.transient = 0
        self.first_transient_ts = 0.0   # wall-clock start of the current transient/outage streak
        # generation-wait reschedules: the PREVIOUS turn was still generating when we tried
        # to send (a slow django/sympy turn). This is NOT a failure -- send() waited and
        # then deferred -- so it does NOT consume the transient budget and does NOT count a
        # turn. A separate, very generous cap only catches a turn that LITERALLY never stops
        # generating (a wedged page). In the fleet path each send() only waits ~2s before
        # deferring (non-blocking). This was the W0 django__django-14730 STUCK:
        # send-into-generating burned the 10x transient budget into STUCK even though the turn
        # was merely slow.
        #
        # Patience is now WALL-CLOCK, not a fixed deferral COUNT: under load each defer cycle
        # can take well over the nominal ~4s (the round-robin sweep slows when many workers
        # are busy), so a count of 60 drifts to far LESS than the intended ~240s of realized
        # patience -- false-STUCKing a legitimately slow turn. We instead stamp the FIRST defer
        # (first_defer_ts) and cut off only when `now - first_defer_ts` exceeds
        # max_gen_wait_s, so the realized patience is the same wall-clock budget regardless of
        # load/sweep speed. (max_gen_waits is kept as a generous secondary guard against a
        # pathological tight-loop with no real elapsed time.) The single-relay path
        # (copilot_autopilot_relay.run_relay, GEN_WAIT_S=240) is synchronous and already
        # wall-clock equivalent, so it is unchanged.
        self.max_gen_wait_s = 360.0
        self.max_gen_waits = 90
        self.gen_waits = 0
        self.first_defer_ts = 0.0
        # goal-delivery recovery: when the agent reports it never received the task, RE-SEND
        # the goal verbatim instead of a generic retry nudge (bounded to avoid a resend loop).
        self.max_goal_resends = 3
        self._goal_resends = 0
        self._cooldown_until = 0.0
        self.verified = None          # None=not checked, True/False after a gate ran
        self.last_verify_detail = ""
        self._pending_checks = []     # acceptance.Check specs left to run this gate
        self._active_check = None     # the Check currently running (non-blocking)
        # When a BLOCKING acceptance eval (run_all_blocking) is about to run, this is set to
        # the time by which it must finish; surfaced into status.json so the watchdog can tell
        # "main thread legitimately busy with a bounded eval" from "Edge wedged". 0 = idle.
        # _busy_writer (if wired) flushes a status snapshot right BEFORE the blocking call so
        # the marker reaches disk even though on_tick can't fire during the blocked sweep.
        self.eval_busy_until = 0.0
        self._busy_writer = busy_writer
        # operator B refuter (spec 4B): an independent reviewer on a candidate DONE.
        self.refuter = refuter
        self.max_refute = max_refute
        self.refute_count = 0
        self._refuter_session = None
        # deep-research delegation (ported from the single-agent relay): a fleet worker can emit
        # `RESEARCH: <query>` and the relay spawns the Researcher sub-agent in a side page, feeds
        # its report back, and the worker continues -- the accuracy lever the single-agent relay
        # already has but the fleet previously lacked. ON by default, capped per worker.
        self.research_count = 0
        self.max_research = max_research
        self.research_model = "Claude"
        self._research_session = None   # non-blocking ResearchSession while status=='researching'
        self._copilot_err_streak = 0    # consecutive Copilot/tool 'SystemError' replies (path down)
        self._agent_err_ts = 0.0        # wall-clock start of the current agent-error (outage) streak
        self._toolerr_ts = 0.0          # wall-clock start of the tool-unreachable (devtunnel down) streak
        self._consent_streak = 0        # consecutive MCP connection-consent cards (auth needed)
        self._consent_auto_tried = False  # attempted the automatic click-through once
        self._consent_surfaced = False  # surfaced the Edge once (manual fallback)
        # review panel (operator B, perspective-diverse): a list of lenses runs one
        # independent reviewer each, aggregated by majority. Empty = single reviewer.
        self.review_lenses = list(review_lenses) if review_lenses else []
        self._panel_queue = []
        self._panel_results = []
        # OPT-IN adaptive refuter (MCP_ADAPTIVE_REFUTER=1): set only when the adaptive hook
        # fires; None/unset means the fixed-panel path runs unchanged (back-compat).
        self._adaptive_features = None
        self._adaptive_mem = None
        self._context = None          # stored at attach() so we can open the side page
        self._agent_url = ""          # bare agent URL -> a fresh independent chat
        # STUCK-ON-REDIRECT recovery (W4 xarray-3364): count consecutive send failures, and once
        # they pile up WHILE the tab is on an SSO-redirect/landing page (not the agent surface),
        # RE-NAVIGATE to _agent_url instead of retrying send into a page that has no composer.
        # Reset whenever a send goes through. Bounded re-navs per turn so a persistently-wrong
        # page still falls through to the existing transient/terminal handling.
        self._send_fail_streak = 0    # consecutive send() exceptions since the last good send
        self.redirect_renav_threshold = 3   # send failures before we suspect a stuck redirect page
        self.max_redirect_renavs = 3        # cap re-navs PER TURN so we never loop forever
        self._redirect_renavs = 0           # re-navs spent on the CURRENT turn
        self.name = name
        self.conv_url = ""         # filled once the conversation gets its /conversation/<id>
        self.conv_title = ""       # Copilot's auto-generated chat title (best-effort scrape)
        self.steer_msgs = []       # user steering messages to inject on the next turn(s)
        self._last_was_steer = False   # so the FOLLOWING continue bridges off the steer
        self.max_turns = max_turns
        self.dwell_s = dwell_s
        self.per_turn_timeout_s = per_turn_timeout_s
        self.max_no_progress = max_no_progress
        # plan-first: turn 1 proposes a plan and pauses for approval (a steer) before
        # executing. plan_steps is surfaced so the cockpit can show / let the user pick.
        self.plan_mode = plan_mode
        self.plan_steps = []
        self._plan_approved = False
        self.job = (PLAN_PROMPT + self.goal) if plan_mode else (PROTOCOL + self.goal)
        self.turn = 0
        self.no_progress = 0
        self.last_norm = None
        self.status = PENDING      # pending | ready | waiting | done | stuck | maxturns | error
        self.outcome = None
        self.reason = ""
        self.last_response = ""
        self.closed = False        # True once its tab has been released
        self._count_before = 0
        self._last_text = None
        self._stable_since = None
        self._t_send = 0.0
        # full-text transcript (each turn's send + Copilot reply, untruncated). The KEY
        # is run-unique (run_id includes the fleet start time) so reused worker names
        # (w0/w1) across rounds never share a file. Path is exposed via .transcript so
        # the snapshot can hand it to the UI. None when no dir was passed (back-compat).
        self._tx_key = ((run_id + "_") if run_id else "") + name
        self._tx = _Transcript(transcript_dir, self._tx_key, name, self.goal)
        self.transcript = self._tx.path or ""

    def attach(self, context, agent_url):
        """Open this worker's tab and make it ready to send. On failure -> error."""
        self._context = context
        self._agent_url = agent_url
        try:
            self.page = _open_fresh(context, agent_url)
            self.drv = CopilotWebDriver(self.page)
            self.status = "ready"
            return True
        except Exception as e:
            self.status, self.outcome = "error", "ERROR"
            self.reason = "open failed: " + type(e).__name__ + ": " + str(e)
            return False

    def close(self):
        """Release the tab (frees ~0.3-0.6 GB). Idempotent; never raises."""
        if self.closed:
            return
        self.closed = True
        try:
            if self.page is not None:
                if not self.conv_url:
                    self._capture_url()      # last chance: a guid that landed late, before we close
                self.page.close()
        except Exception:
            pass
        try:
            if self._refuter_session is not None:
                self._refuter_session.close()     # don't leak the side-page tab
        except Exception:
            pass
        try:
            if self._research_session is not None:
                self._research_session.close()    # don't leak the research side-page tab
        except Exception:
            pass
        self.page = None
        self.drv = None

    def cancel(self):
        """User asked to stop+release this one from the cockpit. Mark terminal so the
        loop won't reopen it, then free its tab."""
        if self.status in TERMINAL:
            self.close()
            return
        self.status, self.outcome = "cancelled", "CANCELLED"
        self.reason = "手動で停止・タブ解放しました"
        self.close()

    def _capture_url(self):
        try:
            if self.page is not None:
                u = self.page.url
                # Capture a real conversation guid (UUID) after EITHER /conversation/<guid> (the
                # old agent) OR /chat/<guid> (the new agent T_02140b8c, which never had a
                # /conversation/ segment -> conv_url stayed empty for every worker). The UUID gate
                # means the agent BASE url (/chat/agent/T_xxx, not a UUID) is never mistaken for a
                # conversation. Called every poll, so a guid that appears a beat late is still caught.
                m = _CONV_GUID_RE.search(u.split("?", 1)[0])
                if m:
                    self.conv_url = u
                    self._tx.note_guid(m.group(1))
        except Exception:
            pass
        # Best-effort: scrape Copilot's auto-generated chat title once it exists. M365
        # names a chat a beat after the first turn, so we keep trying (cheaply) until we
        # have one, then stop. The cockpit/chat use conv_title as the card headline when
        # present (else the goal text), so a miss is harmless. Fully isolated in try/except
        # -- a scrape failure must never affect the relay loop.
        try:
            if not self.conv_title and self.drv is not None:
                t = self.drv.conversation_title()
                if t:
                    self.conv_title = t
        except Exception:
            pass

    def steer(self, text):
        """Queue a user steering message; injected as the worker's next turn (Codex-
        style mid-task redirection). Takes priority over CONTINUE/FIX."""
        if text:
            self.steer_msgs.append(text)

    def _begin_send(self):
        # max_turns=0 (or falsy) means unlimited -- no turn-cap check at all.
        if self.max_turns and self.turn >= self.max_turns:
            # before reporting MAXTURNS, see if the workspace ALREADY satisfies the goal's
            # acceptance checks -- if so the result is proven-done and we finish DONE+verified
            # rather than labeling an already-correct artifact MAXTURNS.
            if self._salvage_via_checks():
                return
            self.status, self.outcome, self.reason = "maxturns", "MAXTURNS", "reached max_turns"
            return
        # a queued steering message preempts the normal CONTINUE/FIX job for this turn
        if self.steer_msgs:
            self.job = ("【ユーザーからの追加指示】" + self.steer_msgs.pop(0)
                        + "\n上記を最優先で踏まえて作業を続行してください。"
                        "完了なら DONE、無理なら FAIL と理由を書いてください。")
            self._last_was_steer = True
        else:
            self._last_was_steer = False
        try:
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            # FLEET PATH MUST NOT BLOCK: the round-robin advances every worker from one
            # thread, so send() may not sit in _wait_generation_idle for the full ~4min
            # (it would freeze the sweep -> status.json goes stale "フリート停止?" and, at
            # concurrency>1, starves the OTHER workers). Pass a SHORT gen-wait so send()
            # just checks "is the turn still generating?", waits ~2s, and if so raises
            # GenerationInProgress immediately -> we defer and the sweep moves on. The
            # generous total patience is realized across deferrals as a WALL-CLOCK budget
            # (max_gen_wait_s), not one blocking call. (run_relay's single-conversation path
            # keeps the full 240s.)
            self.drv.send(self.job, gen_wait_s=2.0)
        except ConversationClosed as e:
            # The target tab/composer is gone (conversation ended). Retrying a dead
            # target can never succeed -- terminal, skip the transient budget entirely
            # (prevents the 10x retry waste against TargetClosed pages seen in
            # send_failures.jsonl). Still let an already-satisfied workspace salvage to
            # DONE before declaring STUCK.
            if self._salvage_via_checks():
                return
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "conversation closed: %s" % (str(e),)
            return
        except GenerationInProgress as e:
            # The PREVIOUS turn was still generating when send() tried to submit (a slow
            # django/sympy turn). send() did a SHORT (~2s) non-blocking check and deferred
            # -- this is NOT a failure. Reschedule the SAME job WITHOUT consuming
            # a turn OR the transient budget (the W0 django__django-14730 fix: a slow turn
            # must never be counted into STUCK). A separate, very generous cap only catches
            # a turn that LITERALLY never stops generating.
            if self._defer_generation():
                elapsed = max(0.0, time.time() - self.first_defer_ts)
                self.reason = "previous turn still generating -> wait %ds/%ds (no budget)" % (
                    int(elapsed), int(self.max_gen_wait_s))
                return
            if self._salvage_via_checks():
                return
            self.status, self.outcome = "stuck", "STUCK"
            elapsed = max(0.0, time.time() - self.first_defer_ts) if self.first_defer_ts else 0.0
            self.reason = "previous turn never stopped generating (%ds, %d waits): %s" % (
                int(elapsed), self.gen_waits, str(e)[:120])
            return
        except Exception as e:
            # STUCK-ON-REDIRECT recovery (W4 xarray-3364): a tab parked on the M365 SSO-redirect
            # / landing page has no composer, so EVERY send fails identically and retrying send
            # there can never succeed. Detect "sends keep failing AND the tab is NOT on the agent
            # surface" and RE-NAVIGATE to the agent URL (mirrors _open_fresh's about:blank re-nav)
            # before spending the transient budget. Bounded per turn so a persistently-wrong page
            # still falls through to the normal transient/terminal handling below.
            self._send_fail_streak += 1
            if self._maybe_renav_off_redirect():
                self.reason = "stuck on redirect page -> re-navigated to agent (renav %d/%d)" % (
                    self._redirect_renavs, self.max_redirect_renavs)
                return
            # a send failure is a transient (CDP/Edge/network) hiccup -- retry the turn
            # rather than giving up, up to the budget. Don't consume a turn for a failed
            # send (turn is only counted once the send actually goes through).
            if self._retry_transient():
                self.reason = "send retry %d/%d (%s)" % (self.transient, self.max_transient,
                                                         type(e).__name__)
                return
            if self._salvage_via_checks():
                return
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "send failed after %d retries: %s: %s" % (
                self.transient, type(e).__name__, str(e))
            return
        self.turn += 1
        # a send actually went through -> reset BOTH the generation-wait count and the
        # wall-clock streak stamp so the next slow turn gets a fresh full patience budget.
        self.gen_waits = 0
        self.first_defer_ts = 0.0
        # a successful send proves the tab is on a live agent surface -> clear the
        # stuck-on-redirect state (streak + per-turn re-nav budget) for the next turn.
        self._send_fail_streak = 0
        self._redirect_renavs = 0
        self._tx.user(self.turn, self.job)     # persist the full sent prompt for this turn
        self._last_text, self._stable_since, self._t_send = None, None, time.time()
        self.status = "waiting"

    def _on_redirect_page(self):
        """True if the tab is currently parked on a non-agent / SSO-redirect / landing page
        (the W4 CsrToSSR symptom) rather than the agent conversation surface. Conservative and
        fully guarded: reads the live URL and probes for the composer. Returns False on any
        error or when there is no real page/agent URL to re-navigate to (so the new re-nav
        branch simply never fires in tests / before attach -- behaviour unchanged there).

        A page is judged 'on a redirect' when its URL carries the redirect markers, OR it is
        NOT on the agent surface AND the composer is absent (a landing page that lost the agent
        chat). The composer probe is the decisive signal -- the happy path (composer present)
        can never satisfy this, so a working agent tab is never re-navigated."""
        if self.page is None or not self._agent_url:
            return False
        try:
            url = self.page.url or ""
        except Exception:
            return False
        if looks_like_redirect_landing(url):
            return True
        # Not an explicit redirect URL: only treat as 'wrong page' if we are NOT on an agent
        # surface AND the composer is missing (so a transient send glitch on a real agent tab,
        # which still HAS a composer, is left to the normal transient retry).
        if on_agent_surface(url):
            return False
        try:
            has_composer = self.page.locator(COPILOT_SELECTORS["composer"]).count() > 0
        except Exception:
            has_composer = True            # unknown -> assume present (don't re-nav on a guess)
        return not has_composer

    def _maybe_renav_off_redirect(self):
        """If sends keep failing because the tab is stuck on a redirect/landing page, RE-NAVIGATE
        to the agent URL the worker was launched to drive (the same URL attach() opened) and reset
        the send-failure streak -- instead of retrying send into a page that has no composer (W4
        xarray-3364: ~29/30 consecutive empty-composer failures over ~1h until the turn timed out).

        Fires only when ALL of: (a) the consecutive send-failure streak has reached the threshold,
        (b) re-navs spent this turn are under the per-turn cap, and (c) the tab really is on a
        redirect/non-agent page (see _on_redirect_page -- the happy path with a live composer can
        never satisfy this). Re-arms the worker to 'ready' with a short cooldown so the next sweep
        re-sends the SAME job on the freshly-navigated agent surface. Returns True if it re-navigated
        (caller should return and let the loop continue), else False (caller falls through to the
        normal transient/terminal handling, so a persistently-wrong page still ends up STUCK)."""
        if self._send_fail_streak < self.redirect_renav_threshold:
            return False
        if self._redirect_renavs >= self.max_redirect_renavs:
            return False
        if not self._on_redirect_page():
            return False
        # Re-navigate this tab to the agent conversation URL (mirrors _open_fresh's about:blank
        # re-nav: goto + wait briefly for the composer to render). Fully guarded -- a failed goto
        # leaves the tab where it was and we just fall through to transient handling next time.
        self._redirect_renavs += 1
        landed = False
        try:
            try:
                self.page.goto(self._agent_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            # wait up to ~10s for the composer to appear (the agent surface is back)
            for _ in range(20):
                try:
                    if self.page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                        landed = True
                        break
                except Exception:
                    pass
                self.page.wait_for_timeout(500)
        except Exception:
            landed = False
        # Reset the send-failure streak: we have moved the tab, so the previous failures no
        # longer reflect the current page. Re-arm to 'ready' to re-send the SAME job. If the
        # composer never appeared, the streak will simply re-accumulate and, once the per-turn
        # re-nav cap is hit, fall through to the existing transient/terminal handling.
        self._send_fail_streak = 0
        self._cooldown_until = time.time() + 2.0
        self.status = "ready"
        return True

    def _defer_generation(self):
        """Schedule a non-failure RESCHEDULE because the previous turn is still generating.
        Unlike _retry_transient this does NOT touch self.transient (the transient/STUCK
        budget) -- waiting out a slow turn is not a failure. It re-arms the worker to 'ready'
        with a short cooldown and re-sends the SAME job. Bounded primarily by a WALL-CLOCK
        budget (max_gen_wait_s) so realized patience is load-independent, with max_gen_waits
        as a secondary guard. Returns True if rescheduled, else False (the budget was hit ->
        the caller should go terminal)."""
        now = time.time()
        # stamp the FIRST defer of this wait-streak; the streak's clock resets to 0 on a
        # successful send (see _begin_send, where gen_waits is also reset).
        if self.first_defer_ts <= 0.0:
            self.first_defer_ts = now
        # primary cutoff: total wall-clock spent deferring this turn exceeds the budget.
        if (now - self.first_defer_ts) > self.max_gen_wait_s:
            return False
        # secondary guard: a pathological tight-loop racking up deferrals with ~no elapsed
        # time (e.g. a clock that never advances) still terminates.
        if self.gen_waits >= self.max_gen_waits:
            return False
        self.gen_waits += 1
        # short, fixed cooldown before re-checking (send() itself does the long minutes-wait
        # for generation to finish; this is just a brief breather between deferrals).
        self._cooldown_until = now + 2.0
        self.status = "ready"
        return True

    def _retry_transient(self):
        """Schedule a retry for a TRANSIENT failure (send/timeout/likely-transient STUCK) with
        SDK-style exponential backoff (0.5->1->2->4->8s capped, -25% jitter). The budget is a
        WALL-CLOCK WINDOW (NET_RETRY_WINDOW_S), not a tiny count: a flaky network/devtunnel can be
        down for minutes, and a 10-count budget exhausted in ~55s -> a brief blip 'ended everything'.
        Now the worker keeps retrying (every ~8s) for up to the window, riding out a real outage, and
        gives up only if it PERSISTS past the window. Returns True if a retry was scheduled, else
        False (window exceeded -> caller goes terminal). first_transient_ts resets on a real reply."""
        now = time.time()
        if self.first_transient_ts <= 0.0:
            self.first_transient_ts = now
        if (now - self.first_transient_ts) > NET_RETRY_WINDOW_S:
            return False
        self.transient += 1
        self._cooldown_until = now + transient_backoff(self.transient)   # backoff caps at ~8s
        self.status = "ready"
        return True

    def _eval_ceiling_s(self):
        """The longest a single blocking acceptance eval should take = the max per-check
        timeout (the SWE-bench shell check carries timeout=1300), bounded by the global
        EVAL_STALL_CEILING_S so a mis-set huge timeout can't disable the failsafe."""
        try:
            mx = max((float(c.get("timeout", 0) or 0) for c in (self.checks or [])),
                     default=0.0)
        except Exception:
            mx = 0.0
        # use the larger of the configured check timeout and the global ceiling, so the
        # watchdog never kills an eval that is still within its own declared budget.
        return max(EVAL_STALL_CEILING_S, mx)

    def _mark_eval_busy(self):
        """Enter a blocking acceptance eval: set status 'verifying' + a busy deadline and
        flush a status snapshot so the watchdog sees the marker before the sweep freezes."""
        self.eval_busy_until = time.time() + self._eval_ceiling_s()
        # show 'verifying' on the card too (and so a status-only watchdog read also defers).
        if self.status not in TERMINAL:
            self.status = "verifying"
        if self._busy_writer is not None:
            try:
                self._busy_writer()
            except Exception:
                pass

    def _clear_eval_busy(self):
        """Leave a blocking acceptance eval (always, even on failure/exception)."""
        self.eval_busy_until = 0.0

    def _poll_research(self):
        """Drive the NON-BLOCKING deep-research (status=='researching'). None -> still researching,
        so the round-robin keeps stepping every OTHER worker; a report string -> inject it and
        continue; '' (failure/timeout) -> continue without it. Mirrors _poll_refute, so a worker's
        minutes-long deep-dive never freezes the fleet."""
        report = self._research_session.poll()
        if report is None:
            return False                     # still researching; the sweep keeps moving
        self._research_session = None
        if report:
            self.job = ("依頼された調査が完了しました。以下が結果です。これを踏まえて作業を続けて"
                        "ください。\n--- 調査結果 ---\n" + report + "\n--- 調査結果ここまで ---\n"
                        + CONTINUE_JOB)
            self.reason = "research %d/%d 反映して続行" % (self.research_count, self.max_research)
        else:
            self.job = ("調査結果を取得できませんでした。調査なしで可能な範囲で進めるか、無理なら"
                        "最後の行に STUCK: 理由 と書いてください。")
            self.reason = "research %d/%d 結果なし" % (self.research_count, self.max_research)
        self.status = "ready"
        return False

    def _salvage_via_checks(self):
        """Last-chance acceptance salvage for the EXHAUSTION paths (spec 3-3 verify gate,
        applied where the worker would otherwise go terminal NON-done). Before burning a
        turn it doesn't have (at max_turns) or giving up on a timeout/stuck, run the SAME
        acceptance checks the DONE gate uses against the current workspace. If they already
        PASS, the artifact is proven-done regardless of whether Copilot ever emitted a clean
        DONE -- so finish DONE+verified instead of MAXTURNS/STUCK. (Observed on HumanEval_56:
        solution.py passed the canonical test but per-turn timeout-retries ate the 10-turn
        budget before a clean DONE landed.) No checks -> nothing to prove against -> can't
        salvage. Runs blocking like the single-relay DONE gate (run_all_blocking); this is a
        once-per-worker terminal moment, not the hot round-robin path. Returns True iff
        salvaged (status is now terminal DONE)."""
        if not self.checks:
            return False
        # run_all_blocking is SYNCHRONOUS and can take the full eval timeout (SWE-bench docker
        # eval ~1300s). It freezes the single-thread round-robin -> status.json stops advancing.
        # Mark this worker "verifying" with a deadline and flush a snapshot BEFORE blocking, so
        # the watchdog sees a legitimate bounded eval (not a wedged Edge) and waits instead of
        # hard-resetting. Cleared in finally so the marker never sticks past the eval.
        self._mark_eval_busy()
        try:
            passed, detail = run_all_blocking(self.checks, cwd=self.cwd)
        finally:
            self._clear_eval_busy()
        self.last_verify_detail = detail
        if not passed:
            return False
        self.verified = True
        self.status, self.outcome = "done", "DONE"
        self.reason = "checks already pass at exhaustion -> salvaged DONE (%s)" % (
            (detail or "")[:160])
        return True

    def tab_load(self):
        """RAM footprint of this worker in OPEN browser tabs right now: the main agent tab plus
        any sub-agent side-pages currently open (research / refuter). The fleet's RAM admission
        counts THIS (sum over workers), not just the worker count -- so an auto/ultra task that
        fans out to 3 tabs is treated as ~3 tabs of RAM pressure, automatically, without a human
        hand-capping concurrency. 0 while the worker holds no tab (pending / closed)."""
        if self.page is None:
            return 0
        n = 1
        rs = self._research_session
        if rs is not None and getattr(rs, "page", None) is not None:
            n += 1
        fs = self._refuter_session
        if fs is not None and getattr(fs, "page", None) is not None:
            n += 1
        return n

    def tab_weight(self):
        """PEAK tabs this worker may hold: 1 main + 1 if it can delegate research + 1 if it runs a
        refuter. Admission RESERVES this many tab-slots, so N lean workers can't all be admitted at
        1 tab each and THEN fan out together to 3 tabs each (the balloon that crashed the Edge). For
        an auto/ultra task -- which nearly always researches AND refutes -- the peak IS the typical
        load, so this is accurate, not merely conservative. min-effort = 1 (no side-pages)."""
        return 1 + (1 if self.max_research > 0 else 0) + (1 if self.refuter else 0)

    def _auto_consent(self):
        """Click through the MCP connection-consent card AUTOMATICALLY. The card is NOT a
        credential entry -- the Bearer key is already configured on the connector; this is just a
        connection-SELECT confirm. Verified flow (2026-06-15): 接続マネージャーを開く opens the
        Copilot Studio connection manager in a popup; a stale connection exposes a レビュー link ->
        the 接続の作成または選択 dialog has the connection pre-selected -> 送信する commits it. Once
        committed, ANY later tool call works, so we do NOT click 再試行 here -- the caller sends
        RETRY_JOB and the agent re-invokes the tool on a now-valid connection.
        Returns True if 送信する was clicked, False otherwise (caller falls back to manual surface)."""
        pg = self.page
        if pg is None:
            return False
        try:
            ctx = pg.context
            link = pg.locator('a:has-text("接続マネージャーを開く"), a:has-text("connection manager")')
            if not link.count():
                return False
            try:
                with ctx.expect_page(timeout=15000) as pinfo:
                    link.first.click()
                cs = pinfo.value
            except Exception:
                return False            # opened in-place / no popup -> hand to manual
            try:
                cs.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
            for _ in range(15):         # let the CS /auth redirect settle
                try:
                    if "/auth" not in (cs.url or ""):
                        break
                except Exception:
                    break
                cs.wait_for_timeout(2000)
            try:                         # stale connection -> レビュー opens the select dialog
                rev = cs.locator('a:has-text("レビュー"), button:has-text("レビュー"), a:has-text("Review")')
                if rev.count():
                    rev.first.click()
                    cs.wait_for_timeout(3000)
            except Exception:
                pass
            submitted = False            # the connection is pre-selected -> just submit
            for label in ("送信する", "送信", "Submit"):
                try:
                    btn = cs.locator('button:has-text("%s")' % label)
                    if btn.count():
                        btn.first.click()
                        cs.wait_for_timeout(4000)
                        submitted = True
                        break
                except Exception:
                    continue
            try:
                cs.close()
            except Exception:
                pass
            try:
                pg.bring_to_front()
            except Exception:
                pass
            return submitted
        except Exception:
            return False

    def _decide(self, resp):
        self.last_response = resp
        self._tx.assistant(self.turn, resp)    # persist the full Copilot reply for this turn
        # DEAD-AGENT / DEAD-PATH detector. The Copilot agent surfaces a generic failure instead of
        # doing the task -- "予期しないエラー / SystemError" (e.g. the devtunnel to the MCP dropped on
        # a network switch) or "ページをもう一度読み込んで... / 管理者に問い合わせて" (the agent is
        # wedged, or has been ADMIN-BLOCKED -- observed when one agent was disabled while others kept
        # working). Each reply carries a fresh timestamp/session-id, so the exact-text no-progress
        # check never fires and the worker burns ALL its turns (50-turn MAXTURNS observed). Catch the
        # pattern, bail FAST after a few, and go STUCK so the goal can be re-submitted on a healthy
        # agent rather than wasting the whole turn budget on a dead endpoint.
        _low = resp.lower()
        # CONNECTION-CONSENT (interactive auth) -- a FOREGROUND-required event. The agent's tool
        # call returned the connector's consent card instead of a result. Surface the (headless)
        # Edge to the foreground on the FIRST occurrence so the user can authorize the connection
        # in that very Edge, then STUCK fast (do not loop the card to MAXTURNS).
        if any(m in resp for m in CONSENT_MARKERS) or any(m in _low for m in CONSENT_MARKERS):
            self._consent_streak += 1
            # AUTO-CONSENT first: the card is a connection-SELECT confirm, not a credential entry,
            # so the relay clicks through it (接続マネージャー -> レビュー -> 送信する) and re-invokes
            # the tool via RETRY_JOB. Manual surfacing is only a fallback when the click-through
            # can't complete (DOM changed / popup blocked).
            if not self._consent_auto_tried:
                self._consent_auto_tried = True
                if self._auto_consent():
                    self.job = RETRY_JOB
                    self.reason = "auto-consent: 接続を確定し再呼出"
                    return
            # fallback: surface the (headless) Edge ONCE so the user can authorize manually
            if not self._consent_surfaced:
                self._consent_surfaced = True
                try:
                    from .edge_recover import surface
                    surface()
                except Exception:
                    pass
                try:
                    default_notify("⚠ 接続の承認が必要",
                                   "自動承認に失敗。専用Edgeを前面に出しました。MCP接続を承認してください (%s)" % self.name)
                except Exception:
                    pass
            if self._consent_streak >= 2:
                self.status, self.outcome = "stuck", "STUCK"
                self.reason = ("⚠ MCPコネクタの**接続承認(consent)が未完**で自動承認も失敗。エージェントは"
                               "ツールを呼ぶ度に「接続マネージャーを開く」カードを返している。タスク失敗ではなく"
                               "**接続未確立**。→ 前面に出した**専用Edge**で接続を承認(consentは当該ブラウザ"
                               "専用・他で承認しても無効)してから再投入を。")
                return
            self.job = RETRY_JOB
            return
        # TOOL-BACKEND-UNREACHABLE: the agent's tool calls failed (devtunnel/network blip) and it
        # self-locked claiming its tools don't exist. INFRA-FALSE, not a miss. Re-send the GOAL (the
        # "new input" it demands) to ride out the blip; give up only past the window, as a re-queueable
        # infra stuck (NOT counted as a coding miss).
        if any(m in resp for m in TOOL_UNREACHABLE_MARKERS) or any(m in _low for m in TOOL_UNREACHABLE_MARKERS):
            now = time.time()
            if self._toolerr_ts <= 0.0:
                self._toolerr_ts = now
            if (now - self._toolerr_ts) > AGENT_ERR_WINDOW_S:
                self.status, self.outcome = "stuck", "INFRA_STUCK"
                self.reason = ("⚠ ツール経路(devtunnel/網)が%d分以上不通でツール呼び出し不可。エージェントは"
                               "『ツールが存在しない』と誤判定し自己ロックしている。**タスク失敗でなくインフラ起因**"
                               "(網/トンネル復旧後に再投入＝reverify対象)。" % int((now - self._toolerr_ts) / 60))
                return
            self.job = PROTOCOL + self.goal          # re-send the goal as 'new input' to unlock it
            self._cooldown_until = now + transient_backoff(2)
            self.status = "ready"
            self.reason = "tool path down (infra) -> re-send goal, riding out outage"
            return
        if any(m in _low for m in AGENT_DEAD_MARKERS):
            now = time.time()
            if self._agent_err_ts <= 0.0:
                self._agent_err_ts = now
            self._copilot_err_streak += 1
            # WALL-CLOCK window, not a 3-strike count: a devtunnel SSL flap / brief Copilot blip
            # surfaces as a SystemError too, and STUCKing after 3 quick errors (~seconds) meant a
            # momentary outage killed the worker. Keep retrying WITH BACKOFF for AGENT_ERR_WINDOW_S
            # so an outage is ridden out; only a failure that PERSISTS past the window is treated as
            # a genuinely down/banned agent (then the actionable Copilot-Studio message applies).
            if (now - self._agent_err_ts) > AGENT_ERR_WINDOW_S and self._copilot_err_streak >= 3:
                self.status, self.outcome = "stuck", "STUCK"
                self.reason = ("⚠ エージェント応答エラーが%d分以上継続(%d回連続)。一時的な網断ではなく"
                               "**エージェント自体が応答していない**。→ Copilot Studio でこのエージェントが"
                               "**停止/無効化(管理者ブロック)されていないか確認**してください"
                               "（他のエージェントが動くなら本エージェント固有の block の可能性大）。"
                               "健全なエージェントに切り替えて再投入を。"
                               % (int((now - self._agent_err_ts) / 60), self._copilot_err_streak))
                try:
                    default_notify("⚠ エージェント停止の疑い",
                                   "Copilot Studio で停止/無効化されていないか確認を (%s)" % self.name)
                except Exception:
                    pass
                return
            self.job = RETRY_JOB
            self._cooldown_until = now + transient_backoff(self._copilot_err_streak)  # back off, don't hammer
            return
        self._copilot_err_streak = 0
        self._agent_err_ts = 0.0
        norm = " ".join(resp.lower().split())[:300]
        self.no_progress = self.no_progress + 1 if norm and norm == self.last_norm else 0
        self.last_norm = norm
        up = resp.upper()
        last_line = (resp.strip().splitlines() or [""])[-1].upper()
        # GOAL-DELIVERY recovery (additive, exception-safe): if the agent says it never
        # received the task -- whether or not it dressed that up as STUCK -- the goal text
        # didn't land in the tab. Re-send the GOAL ITSELF (verbatim, via PROTOCOL) rather
        # than a generic retry nudge, which on round 5 spun 10 empty retries. Bounded by
        # max_goal_resends; only the plan-pending phase is exempted (it legitimately has no
        # task body yet for the executor).
        try:
            _gns = goal_not_seen(resp)
        except Exception:
            _gns = False
        if _gns and not (self.plan_mode and not self._plan_approved) \
                and self._goal_resends < self.max_goal_resends:
            self._goal_resends += 1
            self.job = (PROTOCOL + self.goal)
            self.status = "ready"
            self.reason = "goal not received by agent -> resend goal %d/%d" % (
                self._goal_resends, self.max_goal_resends)
            return
        if reported_stuck(resp):
            # Under load, an agent STUCK is usually a downstream symptom of a transient
            # tool/network failure (the agent couldn't write a file etc.). Retry the turn
            # (re-prompt to try the tools again) before giving up, up to the budget.
            if self._retry_transient():
                self.job = RETRY_JOB
                self.reason = "STUCK -> transient retry %d/%d" % (self.transient, self.max_transient)
                return
            if self._salvage_via_checks():
                return
            self.status, self.outcome, self.reason = "stuck", "STUCK", \
                "agent reported STUCK (after %d retries)" % self.transient
            return
        self.transient = 0   # a real (non-stuck) response -> the transient issue cleared
        self.first_transient_ts = 0.0   # reset the outage window on a healthy reply
        self._toolerr_ts = 0.0          # tool path is back -> clear the tool-unreachable window
        # deep-research delegation: the agent wrote `RESEARCH: <query>` asking for an external
        # deep-dive. Spawn the Researcher sub-agent (side page), feed its report back as the next
        # turn, and continue. Capped per worker (max_research); past the cap, tell it to proceed.
        rq = extract_research(resp)
        if rq and self._context is not None and self.max_research > 0:
            if self.research_count >= self.max_research:
                self.job = ("これ以上は調査を依頼できません（上限到達）。今ある情報で進めるか、"
                            "無理なら最後の行に STUCK: 理由 と書いてください。")
                self.status = "ready"
                return
            self.research_count += 1
            # NON-BLOCKING: kick off the Researcher in a side page and enter 'researching'. The
            # round-robin keeps stepping every OTHER worker while this one's deep-dive runs (see
            # _poll_research). A blocking wait here would freeze the whole fleet for minutes and
            # cause false turn-timeouts on the siblings -- the reason v1 was unusably slow.
            from .agent_profiles import ResearchSession
            self._research_session = ResearchSession(
                self._context, rq, model_name=self.research_model,
                tx_dir=getattr(self._tx, "dir", None), parent_key=self._tx_key,
                parent_turn=self.turn, sub_index=self.research_count).start()
            self.status = "researching"
            self.reason = "🔎 外部調査中 (%d/%d): %s" % (self.research_count, self.max_research, rq[:48])
            return
        # plan phase (plan_mode): capture the proposed plan and PAUSE for approval; a steer
        # (approve as-is, or an edit) resumes into execution. Don't run DONE/CONTINUE yet.
        if self.plan_mode and not self._plan_approved:
            if plan_ready(resp):
                self.plan_steps = extract_plan(resp)
                self.status = "awaiting"
                self.reason = "計画提示・承認待ち (%d ステップ)" % len(self.plan_steps)
                return
            self.job = ("実行計画を番号付きステップで完成させ、最後の行に PLAN_READY と"
                        "書いてください（まだ実装はしないこと）。")
            self.status = "ready"
            return
        if "DONE" in up and "FAIL" not in last_line:
            self._on_done_claimed()
            return
        if self.no_progress >= self.max_no_progress:
            if self._salvage_via_checks():
                return
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "no progress for %d turns" % (self.no_progress + 1)
            return
        if "FAIL" in last_line:
            self.job = FIX_JOB
        elif self._last_was_steer:
            # bridge off the steer instead of a raw CONTINUE so the redirection sticks
            self.job = ("先ほどの追加指示を踏まえて作業を続行してください。"
                        "完了なら DONE、無理なら FAIL と理由を書いてください。")
        else:
            self.job = CONTINUE_JOB
        self.status = "ready"

    def _on_done_claimed(self):
        """Copilot reported DONE. With no acceptance checks, go straight to the candidate-
        done step (back-compat trust, unless a refuter is on). With checks, run the
        verification gate first."""
        if not self.checks:
            self.verified = False
            self._candidate_done()
            return
        self._pending_checks = list(self.checks)
        self._active_check = None
        self.status = "verifying"
        self._advance_check()

    def _advance_check(self):
        """Start the next pending check, or go to the candidate-done step if all passed."""
        if not self._pending_checks:
            self.verified = True
            self.reason = "acceptance verified (%d check(s))" % len(self.checks)
            self._candidate_done()
            return
        self._active_check = Check(self._pending_checks[0], cwd=self.cwd).start()

    def _candidate_done(self):
        """Machine checks passed (or none) -> a CANDIDATE done. If the refuter is enabled
        and within budget, open an independent reviewer (non-blocking) before accepting;
        otherwise finish now."""
        if (self.refuter and self._context is not None
                and self.refute_count < self.max_refute):
            self.refute_count += 1
            if self.review_lenses:
                lenses = list(self.review_lenses)
                # OPT-IN adaptive lens selection (default OFF). When MCP_ADAPTIVE_REFUTER=1,
                # learn from past per-lens refutation rates and throw only the top-k most
                # likely-to-refute lenses for THIS candidate's features -- fewer oracle calls,
                # adaptive over time. Env unset => this branch is skipped and the full fixed
                # panel runs exactly as before (byte-for-byte the old behaviour).
                self._adaptive_features = None
                if os.environ.get("MCP_ADAPTIVE_REFUTER") == "1":
                    from .refuter_memory import RefuterMemory, extract_features
                    self._adaptive_mem = RefuterMemory()
                    self._adaptive_features = extract_features(self.goal, self.last_response)
                    try:
                        k = int(os.environ.get("MCP_ADAPTIVE_REFUTER_K", "2"))
                    except ValueError:
                        k = 2
                    lenses = self._adaptive_mem.select_lenses(
                        self._adaptive_features, lenses, k)
                self._panel_queue = lenses
                self._panel_results = []
                self._start_next_lens()
            else:
                from .refuter import RefuterSession
                self._refuter_session = RefuterSession(
                    self._context, self._agent_url or "", self.goal,
                    self.last_response).start()
            self.status = "refuting"
            return
        self.status, self.outcome = "done", "DONE"

    def _start_next_lens(self):
        from .refuter import RefuterSession
        lens = self._panel_queue.pop(0)
        self._refuter_session = RefuterSession(
            self._context, self._agent_url or "", self.goal,
            self.last_response, lens=lens).start()

    def _poll_refute(self):
        """Drive the non-blocking refuter / review panel. REFUTED -> feed the reason back
        and keep working; UPHELD/UNCLEAR -> accept. A panel runs one independent reviewer
        per lens in turn, then aggregates by majority."""
        r = self._refuter_session.poll()
        if r is None:
            return False
        kind, reason = r
        if self.review_lenses:
            lens = self._refuter_session.lens
            self._panel_results.append((lens, kind, reason))
            self._refuter_session = None
            # OPT-IN adaptive memory: after each lens's verdict is known, record whether it
            # refuted, keyed by this candidate's features. No-op unless the adaptive hook in
            # _candidate_done was taken (MCP_ADAPTIVE_REFUTER=1 => _adaptive_features is set).
            if getattr(self, "_adaptive_features", None) is not None:
                try:
                    self._adaptive_mem.record(
                        self._adaptive_features, lens, refuted=(kind == "REFUTED"))
                except Exception:
                    pass
            if self._panel_queue:                  # consult the remaining lenses first
                self._start_next_lens()
                return False
            from .refuter import aggregate_panel    # all in -> majority vote
            kind, reason = aggregate_panel(self._panel_results)
        else:
            self._refuter_session = None
        # surface the verdict in status.json (reason) so the run is observable live
        self.reason = ("refuter#%d: %s%s"
                       % (self.refute_count, kind,
                          (": " + reason) if reason else ""))[:300]
        if kind == "REFUTED":
            self.job = REFUTE_FIX_JOB % (reason or "(no reason)")
            self.status = "ready"
            return False
        self.status, self.outcome = "done", "DONE"
        return True

    def _poll_verify(self):
        """Drive the running acceptance check non-blockingly. On pass, advance to the
        next check (all pass -> DONE). On fail, re-inject the GROUND TRUTH and let the
        agent keep working, up to max_verify_attempts (then STUCK/VERIFY_FAILED)."""
        if self._active_check is None:
            self._advance_check()
            return self.status in TERMINAL
        r = self._active_check.poll()
        if r is None:
            return False                 # still running -- the other workers keep moving
        passed, detail = r
        self.last_verify_detail = detail
        if passed:
            self._pending_checks.pop(0)
            self._active_check = None
            self._advance_check()
            return self.status in TERMINAL
        self.verify_attempts += 1
        self.verified = False
        if self.verify_attempts >= self.max_verify_attempts:
            self.status, self.outcome = "stuck", "VERIFY_FAILED"
            self.reason = ("acceptance check failed %d time(s): %s"
                           % (self.verify_attempts, (detail or "")[:200]))
            return True
        self.job = VERIFY_FIX_JOB % (detail or "(no detail)")
        self._pending_checks = []
        self._active_check = None
        self.status = "ready"
        return False

    def poll(self):
        """Advance one non-blocking step. Returns True when terminal."""
        if self.status in TERMINAL:
            return True
        if self.status == PENDING:
            return False                 # not attached yet; the fleet attaches it
        if self.status == "awaiting":
            # plan proposed; paused for human approval. A steer (approval or an edit) is
            # the resume signal -- it becomes the next turn via _begin_send's steer path.
            if self.steer_msgs:
                self._plan_approved = True
                self.status = "ready"
            return False
        if self.status == "verifying":
            return self._poll_verify()
        if self.status == "researching":
            return self._poll_research()
        if self.status == "refuting":
            return self._poll_refute()
        if self.status == "ready":
            if time.time() < self._cooldown_until:
                return False             # waiting out a transient-retry backoff
            self._begin_send()
            self._capture_url()
            return self.status in TERMINAL
        if self.status == "waiting":
            self._capture_url()
            if time.time() - self._t_send > self.per_turn_timeout_s:
                # a turn that never finished is a transient stall -- retry before STUCK
                if self._retry_transient():
                    self.reason = "turn timeout -> retry %d/%d" % (self.transient, self.max_transient)
                    return False
                # retries exhausted: don't give up on an already-correct artifact -- if the
                # workspace already passes the acceptance checks, salvage it as DONE+verified.
                if self._salvage_via_checks():
                    return True
                self.status, self.outcome, self.reason = "stuck", "STUCK", \
                    "turn timeout (after %d retries)" % self.transient
                return True
            try:
                if self.drv._answers().count() <= self._count_before:
                    return False
            except Exception:
                return False
            # PRIMARY completion gate: never read/commit a turn while the agent is STILL
            # GENERATING (the live Stop/square button is showing). Reading mid-stream was
            # the root cause of partial capture (transcript turn5: 102 chars, mid-word
            # "...隠し", no end marker) -- a streaming pause longer than dwell_s made the
            # partial text look 'stable' and it was committed as the final answer, dropping
            # the rest of the reply AND its DONE/CONTINUE/STUCK tail marker. This reuses the
            # same Stop-button signal the SEND gate uses. Defensive getattr + guard so a
            # mock/stub driver without _is_generating degrades to pure text-stability
            # (back-compat with the test fakes). Reset the stability clock while generating
            # so a pre-pause partial never carries its stale stable_since across the resume.
            _isgen = getattr(self.drv, "_is_generating", None)
            if callable(_isgen):
                try:
                    if _isgen():
                        self._last_text, self._stable_since = None, None
                        return False
                except Exception:
                    pass
            t = self.drv.read_last_response()
            if _is_processing(t):
                self._last_text, self._stable_since = None, None
                return False
            if t == self._last_text:
                # A stable answer whose TAIL has no protocol marker may be a mid-stream
                # pause that briefly hid the Stop button; require an EXTENDED settle (2x
                # dwell) before committing it, vs the normal dwell for a marker-terminated
                # (DONE/CONTINUE/STUCK/...) tail. Bounded by per_turn_timeout_s, so this
                # cannot hang the round-robin -- a turn that genuinely never marks still
                # commits once it stays byte-identical for the extended window.
                need = self.dwell_s if has_end_marker(t) else self.dwell_s * 2.0
                if self._stable_since and (time.time() - self._stable_since) >= need:
                    self._decide(t)
                    return self.status in TERMINAL
                return False
            self._last_text, self._stable_since = t, time.time()
            return False
        return False


def run_relay_fleet(context, goals, agent_url, max_turns=1000, poll_s=1.0,
                    notify=default_notify, on_tick=None, max_concurrent=None,
                    mc_box=None, add_box=None, refuter=False, max_refute=2,
                    plan_mode=False, review_lenses=None, max_transient=10, max_research=3,
                    autoscale=False, autoscale_max=None, asc_box=None,
                    autoscale_per_tab_mb=700, autoscale_headroom_mb=1400,
                    autoscale_up_margin_mb=0,
                    disk_floor_gb=None, eval_disk_gb=None, disk_box=None,
                    transcript_dir=None, run_id="", busy_writer=None,
                    pause_box=None, stop_box=None):
    """Drive len(goals) autonomous relays in parallel to completion, but never with
    more than `max_concurrent` tabs open at once (defaults to what free RAM allows).
    A goal's tab is opened only when a slot frees and CLOSED the moment it finishes.

    CONTINUOUS CAPACITY-AWARE ADMISSION (2026-06-14): this is a single continuous flow --
    pass ALL goals at once and they are admitted as fast as capacity allows, NOT in batches.
    A job that finishes frees its slot (tab RAM + the eval's disk via swe_check cleanup) and
    the NEXT queued goal is admitted on the very next sweep -- there is no batch barrier (the
    orchestrator no longer waits for a chunk of K to all finish before launching the next K).
    Admission is gated on BOTH resources:
      * RAM -- the live cap (mc_box / autoscale ram_target_cap), and
      * DISK -- C: free must stay above a reserved floor (disk_floor_gb, user-configurable via
        env SWE_DISK_FLOOR_GB or disk_box) even after the new job's eval consumes its budget.
    Both must be satisfied to open a tab, so a job is never admitted in a way that would either
    exhaust RAM (the Edge-crash failure mode) or push C: under the floor.

    `mc_box`, if given, is a 1-element list whose value is read EACH loop -- so the
    cockpit can raise/lower the live concurrency cap mid-run (set_maxtabs command).
    `disk_box`, if given, is a 1-element list with the live disk floor in GB (cockpit-settable).

    Returns a list of {name, goal, outcome, turns, reason} in goal order. `on_tick`
    (workers) is called after each round-robin sweep -- use it to log live progress."""
    if max_concurrent is None:
        max_concurrent = auto_concurrency(len(goals))
    if mc_box is None:
        mc_box = [max_concurrent]
    # autoscale ceiling: never open more than this many tabs even if RAM is plentiful
    # (the user's configured maximum / fair-use bound). Defaults to the launch cap.
    if autoscale_max is None:
        autoscale_max = max(1, max_concurrent)
    # run_id keys the transcript files for THIS invocation; if the caller didn't supply
    # one, derive a run-unique id from the start time so resumes/rounds don't collide.
    if not run_id:
        run_id = "r%x" % int(time.time())
    # busy_writer: flush a status snapshot on demand. A worker calls this right before a
    # BLOCKING acceptance eval freezes the sweep, so its 'verifying'/eval-busy marker reaches
    # status.json BEFORE on_tick stops firing -- the watchdog then waits instead of resetting.
    # Caller may inject one; otherwise default to on_tick (which writes the snapshot).
    if busy_writer is None and on_tick is not None:
        def busy_writer():
            try:
                on_tick(workers)
            except Exception:
                pass

    # live disk floor (GB): cockpit can change it mid-run via disk_box; otherwise the launch
    # value (env SWE_DISK_FLOOR_GB default) applies. <=0 disables the disk gate.
    if disk_box is None:
        disk_box = [DEFAULT_DISK_FLOOR_GB if disk_floor_gb is None else float(disk_floor_gb)]
    # pause/stop control (1-element lists read EACH loop, set by the cockpit via commands.json):
    # pause_box[0] True  -> freeze the fleet in place (no new turns / no new tabs / no liveness
    #                       probe) so a deliberate network switch doesn't trip FleetContextLost.
    # stop_box[0]  True  -> graceful abort: cancel every running worker and end the run.
    if pause_box is None:
        pause_box = [False]
    if stop_box is None:
        stop_box = [False]

    workers = [RelayWorker(g, "w%d" % i, max_turns=max_turns,
                           refuter=refuter, max_refute=max_refute, plan_mode=plan_mode,
                           review_lenses=review_lenses, max_transient=max_transient,
                           transcript_dir=transcript_dir, run_id=run_id,
                           busy_writer=busy_writer, max_research=max_research)
               for i, g in enumerate(goals)]
    pending = list(workers)            # FIFO queue of not-yet-attached workers

    def _active_open():
        # Worker (MAIN-tab) count -- the DISK accounting unit: only main agent tabs run the
        # Docker eval, so the disk gate reserves per main tab, not per sub-agent side-page.
        # Every worker that still HOLDS a tab counts -- including ones in 'verifying'/'refuting'
        # (a bounded eval / review still occupies its tab + the disk its eval used).
        return sum(1 for w in workers
                   if w.page is not None and w.status not in TERMINAL)

    def _active_tabs():
        # ACTUAL open browser tabs across the fleet = main tabs + every open sub-agent side-page
        # (research / refuter). The RAM-pressure reading that drives the autoscale recompute and
        # the cockpit display -- "maxtabs" means TABS, so an auto worker fanned out to 3 shows as 3.
        return sum(w.tab_load() for w in workers)

    def _projected_peak():
        # WORST-CASE tabs if every active worker fans out fully (sum of tab_weight). Admission
        # reserves against THIS so N lean workers can't be admitted at 1 tab each and then balloon
        # to 3 tabs each at once (the overload that wedged the Edge). _active_tabs reacts AFTER a
        # fan-out; _projected_peak prevents the over-admission that makes the fan-out unaffordable.
        return sum(w.tab_weight() for w in workers
                   if w.page is not None and w.status not in TERMINAL)

    def _unfinished():
        # reconstruct the full goal (incl. acceptance checks/cwd) so a resume after a
        # wedged Edge keeps verifying -- returning bare text would drop the gate.
        return [{"text": w.goal, "checks": w.checks, "cwd": w.cwd}
                for w in workers if w.outcome not in ("DONE", "CANCELLED")]

    _reap_counter = 0
    while any(w.status not in TERMINAL for w in workers) or (add_box and len(add_box) > 0):
        # --- stop / pause control (cockpit -> commands.json -> *_box, read every loop) ---
        if stop_box[0]:
            # graceful abort: cancel every still-running worker, then fall through to the
            # cleanup below (which closes all tabs) and return the results normally.
            for w in workers:
                if w.status not in TERMINAL:
                    w.cancel()
            break
        if pause_box[0]:
            # freeze: take NO new turns, open NO tabs, and DON'T probe the context -- a
            # deliberate network switch must not trip FleetContextLost. Keep firing on_tick
            # so a later {"pause":false}/{"stop":true} is still drained and the cockpit shows
            # the paused state. Any cloud turn already in flight settles on its own; resume
            # picks up from the next poll. State is fully retained (nothing is lost).
            if on_tick:
                try:
                    on_tick(workers)
                except Exception:
                    pass
            time.sleep(poll_s)
            continue

        # auto-recovery: if the Edge/CDP context has died (wedged, or hard-reset by the
        # watchdog) a LIVE probe raises -> bail out with the unfinished goals so the
        # runner can reconnect to a fresh Edge and resume them. NB: context.pages is a
        # cached property and never raises -- cookies() actually round-trips to CDP.
        try:
            context.cookies()
        except Exception:
            raise FleetContextLost(_unfinished())

        # periodically reap orphan SSO-redirect tabs (every ~30 sweeps) so a long chunk does not
        # accumulate dead landing tabs; cheap and never touches a worker's own page.
        _reap_counter += 1
        if _reap_counter % 30 == 0:
            _reap_orphan_redirect_tabs(context, workers)

        # goals added mid-run (e.g. from the native chat while at capacity) join the
        # queue here -- priority items jump to the front, but still wait for a free slot
        # so the tab budget is never exceeded.
        if add_box:
            while add_box:
                item = add_box.pop(0)
                # item may carry checks/cwd too; goal_fields reads them (priority ignored)
                nw = RelayWorker(item, "w%d" % len(workers), max_turns=max_turns,
                                 refuter=refuter, max_refute=max_refute,
                                 plan_mode=plan_mode, review_lenses=review_lenses,
                                 max_transient=max_transient, busy_writer=busy_writer,
                                 max_research=max_research)
                workers.append(nw)
                if item.get("priority"):
                    pending.insert(0, nw)
                else:
                    pending.append(nw)

        # RAM-aware autoscale: recompute the live cap from free RAM each loop, ramping up
        # gently and draining down softly (see ram_target_cap). When on, this drives mc_box.
        # asc_box (if given) is the live [on, ceiling] control the cockpit can flip mid-run;
        # otherwise the launch-time `autoscale`/`autoscale_max` apply. The START cap is
        # whatever mc_box was initialized to (the user's DEFAULT) -- autoscale grows/shrinks
        # from there toward the ceiling.
        asc_on = autoscale
        ceiling = autoscale_max
        if asc_box:
            asc_on = bool(asc_box[0])
            ceiling = asc_box[1] or autoscale_max
        if asc_on:
            # Drive the cap off ACTUAL tabs (main + sub-agent side-pages), so the cap is a TABS
            # budget and an auto/ultra worker mid-fan-out (3 tabs) is felt as 3 tabs of pressure.
            mc_box[0] = ram_target_cap(_active_tabs(), mc_box[0], max(1, ceiling),
                                       per_tab_mb=autoscale_per_tab_mb,
                                       headroom_mb=autoscale_headroom_mb,
                                       up_margin_mb=autoscale_up_margin_mb)

        # fill free tab slots from the pending queue. ADMISSION is gated on BOTH (a) the live
        # RAM cap (mc_box / autoscale) and (b) the DISK floor: open a new eval-bearing tab only
        # if C: free will stay above the reserved floor after this job's eval (disk_admission_ok
        # looks ahead by eval_disk_gb). If disk is tight we STOP admitting this sweep and let
        # running jobs finish + release their disk (swe_check cleanup), then re-admit -- the
        # continuous-flow drain. A non-positive floor disables the disk gate (normal use).
        # ADMISSION gates on the TAB budget (mc_box[0] is in tabs), RESERVING each worker's PEAK
        # fan-out (tab_weight) so the fleet's worst-case tab count never exceeds the budget -- "an
        # auto task == ~3 tabs" is accounted for at admission, automatically, with no human cap.
        # EXCEPTION: when the fleet is empty always admit ONE worker even if its peak exceeds the
        # budget (a lone auto task needs 3 tabs while maxtabs=1) -- it runs solo and the per-tab
        # ram_room gate defers its side-pages if RAM is genuinely tight, rather than deadlocking.
        # Sub-agent tabs don't run evals, so the DISK gate below still counts main tabs only
        # (_active_open). With no side-pages tab_weight==1, reducing EXACTLY to the old worker cap.
        while pending and (_active_open() == 0
                           or _projected_peak() + pending[0].tab_weight() <= max(1, mc_box[0])):
            # reserve disk for THIS eval plus every already-open eval still in flight, so we never
            # admit N tabs that look fine individually but crash C: once their builds run at once.
            # PER-REPO mode sizes the reserve by each instance's actual build weight (matplotlib 7GB
            # vs requests 2GB) so light evals pair while heavy ones stay solo; flat mode uses one
            # eval_disk_gb for all. _active_open() counts just-attached tabs this sweep, so the
            # reserve grows as we admit -> same-sweep over-admission is prevented either way.
            if EVAL_DISK_PERREPO:
                # Reserve the SUM of all concurrent builds (in-flight + the one we're about to open)
                # against the crash-avoidance HARD MIN (not the soft floor): a lone heavy build may
                # dip under the soft floor and recover (admits solo: 13-7=6 >= 3), but a 2nd heavy
                # that would drag C: under the hard min is deferred; light evals pair.
                # SKIP-AHEAD: scan the queue for the FIRST job that fits alongside the in-flight
                # builds, instead of only testing the head -> a light eval (requests 2GB) behind a
                # heavy queue-head (sklearn 7GB) can still pair rather than waiting for the head.
                open_ws = [x for x in workers if x.page is not None and x.status not in TERMINAL]
                base = sum(repo_eval_gb(x.cwd) for x in open_ws)
                pick = -1
                for i, p in enumerate(pending):
                    if disk_admission_ok(floor_gb=PERREPO_HARD_MIN_GB,
                                         reserve_gb=base + repo_eval_gb(p.cwd)):
                        pick = i
                        break
                if pick < 0:
                    break              # nothing in the queue fits the remaining disk this sweep
                w = pending.pop(pick)
            else:
                if not disk_admission_ok(floor_gb=disk_box[0], eval_gb=eval_disk_gb,
                                         building=_active_open()):
                    break              # disk floor would be breached -> defer admission
                w = pending.pop(0)
            if w.status in TERMINAL:   # (shouldn't happen, but be safe)
                continue
            ok = w.attach(context, agent_url)
            if not ok:
                # attach failed. If the WHOLE Edge/context died mid-open (e.g. the
                # watchdog hard-reset it), don't burn this goal as a terminal ERROR --
                # probe the context, and if it's truly dead bail so the runner reconnects
                # and RESUMES every unfinished goal (this one included). A live context
                # means a one-off open failure -> leave the worker ERROR as before.
                try:
                    context.cookies()
                except Exception:
                    raise FleetContextLost(_unfinished())

        for w in workers:
            if w.status in TERMINAL or w.status == PENDING:
                continue
            try:
                w.poll()
            except Exception as e:
                w.status, w.outcome = "error", "ERROR"
                w.reason = type(e).__name__ + ": " + str(e)
            # the instant a worker is done, release its tab -> RAM for the next goal
            if w.status in TERMINAL and not w.closed:
                w.close()

        if on_tick:
            try:
                on_tick(workers)
            except Exception:
                pass
        time.sleep(poll_s)

    # make sure no tab is left behind
    for w in workers:
        if not w.closed:
            w.close()

    notify("🛰 並列自律フリート 完了",
           "%d ゴール: %s" % (len(workers), ", ".join(w.outcome or "?" for w in workers)))
    return [{"name": w.name, "goal": w.goal, "outcome": w.outcome,
             "turns": w.turn, "reason": w.reason,
             "verified": w.verified, "verify_attempts": w.verify_attempts,
             # carry the captured conversation identity into the FINAL snapshot so the
             # cockpit keeps the Copilot title/URL (and /history link) on finished cards
             # instead of reverting to the bare goal text.
             "conv_url": getattr(w, "conv_url", ""),
             "conv_title": getattr(w, "conv_title", ""),
             # full-text transcript path so finished cards can still show the WHOLE
             # conversation from disk (not just the truncated `last`).
             "transcript": getattr(w, "transcript", "") or "",
             # working dir of the goal -- orchestrators (bench/swe_run_until_done.py)
             # map workers back to instances via this in the FINAL snapshot.
             "cwd": getattr(w, "cwd", "") or ""}
            for w in workers]
