# -*- coding: utf-8 -*-
"""An acting repo goal whose turn was cut with delivery UNKNOWN is resumed in a NEW conversation
that first READS ITS REAL STATE, instead of being left STUCK or re-sent blindly.

THE PROBLEM, MEASURED (coordinator_20261109_074008, run r6af0fa8c_a0). A shared socket dropped with
``no close frame received or sent`` (a single ConnectionClosedError on the route silences every
in-flight turn at once). Two workers on that socket -- w41 and w43 -- were each sent a turn at
10:59:44, got NO reply at all, and timed out together at 11:07:29. The turn produced no output, so
delivery is the honest ``unknown``; the same-conversation continue (relay/continue_recovery.py)
refuses because the backend completed no exchange in the conversation (``conversation_unknown``) and
nothing was executed to reconcile (``no_executed_calls``). With no continue available the timeout
path called ``_refuse_resend`` and both workers ended STUCK, never resumed.

WHY A NEW CONVERSATION IS SAFE HERE WHEN A VERBATIM RE-SEND IS NOT. Re-sending the identical turn
asks the model to perform the act a second time, blindly. This path instead opens a FRESH
conversation whose FIRST instruction is to read what actually happened -- ``git status``,
``git log``, ``gh pr view`` of the goal's branch, and the files the goal was writing -- and to
continue from there, explicitly NOT redoing anything already present. The duplicate-effect risk is
controlled by the INSTRUCTION (read-then-continue, verify against the real tree) rather than by a
guess, the same way relay/auto_rephrase_continue.py handles the canned-non-answer case. It reuses
that module's ``progress_preamble`` and adds the PR state of the branch.

WHAT IT WILL NOT DO. It fires only for repository work done through tools (git/tests/files) and
NEVER for a goal that names the model's own native acts (mail, calendar, chat): those cannot be
reconciled from a git/file read, so re-opening blindly could still send a second mail. For them the
careful refuse-to-STUCK is unchanged -- a person who can check whether the mail went re-queues it.
The gate (``goal_class_ok`` from relay/continue_recovery.py) is the same one the same-conversation
continue uses, so the two paths agree on what is safe.

BOUNDED. One resume per goal by default (RESUME_FROM_STATE_PER_GOAL), mirroring
SOCKET_RECONNECTS_IF_DELIVERED: a dropped answer to an un-landed turn is worth one fresh start, not
a loop. A resume that is itself cut does not start another.

DEFAULT ON (``resume_from_state``). With it off nothing here runs: a delivery-unknown acting cut
with no same-conversation continue ends the worker exactly as before (``_refuse_resend`` -> STUCK).
"""
from __future__ import annotations

import os
import subprocess
import sys

from relay import auto_rephrase_continue as _arc

KEY = "resume_from_state"
DEFAULT = "on"

#: Fresh-conversation resumes allowed for one goal. One, like SOCKET_RECONNECTS_IF_DELIVERED: a
#: dropped answer to an un-landed turn is worth one fresh start, not a loop. Overridable for tests
#: and operations through the environment, read once at import like the fleet's other caps.
RESUME_FROM_STATE_PER_GOAL = int(
    os.environ.get("MCP_FLEET_RESUME_FROM_STATE_MAX", "1"))

#: Upper bound on the `gh pr view` fragment carried into the preamble.
_MAX_PR_CHARS = 800


# ---- the setting (mtime-cached, never raises; same shape as the sibling gates) ---------------

_mode_cache = {}


def _settings_path(path=None):
    if path:
        return str(path)
    from tools.settings_path import settings_file
    return settings_file()


def mode(path=None) -> str:
    """`on` or `off`, from settings.txt. Default on; anything but an explicit `off` is `on`. Never raises.

    One os.stat per read and a re-read only when the file changed, so a change made in the cockpit
    reaches the next decision with no restart (declared each_gate in tools/settings_keys.py).
    """
    try:
        p = _settings_path(path)
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
        hit = _mode_cache.get(p)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = DEFAULT
        with open(p, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = "off" if v == "off" else "on"
                    break
        _mode_cache[p] = (key, val)
        return val
    except Exception:
        return DEFAULT


def enabled(path=None) -> bool:
    return mode(path) == "on"


# ---- is this goal eligible --------------------------------------------------------------------

def goal_is_resumable(goal) -> bool:
    """Repository work done through tools, and nothing the model could do with its own tools.

    Delegates to relay/continue_recovery.goal_class_ok so this path and the same-conversation
    continue agree on what is safe to re-open. A goal that names mail/calendar/chat returns False:
    its act cannot be reconciled from a git/file read, so it keeps the careful refuse-to-STUCK.
    """
    try:
        from relay.continue_recovery import goal_class_ok
        return bool(goal_class_ok(goal or ""))
    except Exception:
        return False


# ---- reading the PR state (best-effort; I/O lives here so the text builder stays pure) --------

def _run_gh(args, cwd, timeout=20):
    """Run `gh` headless once and read stdout as UTF-8 (errors=replace). Empty on any failure.

    Same creationflags reasoning as relay/auto_rephrase_continue._run_git: on Windows a console
    window must not appear on the owner's desktop. `gh` may be absent (returns empty) -- the
    instruction text still tells the new conversation to run `gh pr view` itself.
    """
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["gh"] + list(args),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _pr_summary(cwd):
    """`gh pr view` for the branch checked out in `cwd`, summarised. Empty when unavailable.

    Read-only: `gh pr view` shows the PR the current branch opened without changing anything, so
    asking the question has no effect of its own. No branch/PR, no `gh`, or no network -> empty, and
    the caller's preamble still carries the explicit instruction to look.
    """
    if not cwd or not os.path.isdir(cwd):
        return ""
    out = _run_gh(
        ["pr", "view", "--json", "number,state,title,url",
         "--template",
         "PR #{{.number}} [{{.state}}] {{.title}}\n{{.url}}"],
        cwd)
    return _arc._clip(out, _MAX_PR_CHARS)


# ---- the opening text for the fresh conversation ---------------------------------------------

#: Prepended so the new conversation knows why it exists and what to do FIRST.
RESUME_LEAD = (
    "前の担当のターンは、応答が返る前に接続が切れて途切れました(配送状況=不明)。"
    "作業がどこまで実際に進んだかは、この新しい会話からは分かりません。\n"
    "同じ依頼をそのまま実行し直すと、すでに済んだ操作を二重に行う恐れがあります。"
    "そのため、まず下の手順で『実際の状態』を読み取り、その結果に基づいて続きだけを行ってください。\n"
    "1) 作業ツリーの状態を git status と git log で確認する。\n"
    "2) 自分のブランチの PR を gh pr view で確認する(無ければ未作成)。\n"
    "3) 書きかけだったファイルを実際に開いて内容を確かめる。\n"
    "4) 上で分かった実状と、下の『機械的に収集した状況』が食い違う場合は、実状を優先する。\n"
    "5) すでに存在する成果物(コミット・PR・ファイル)は作り直さず、不足している所だけを進める。\n")


def build_resume_text(goal_body, worker_last="", partial_text="", journal="", cwd="",
                      git_summary=None, folder_listing=None, pr_summary=None):
    """The single opening message for the fresh conversation.

    RESUME_LEAD (read-your-real-state-first) + the machine-collected progress preamble (reused from
    relay/auto_rephrase_continue, extended with the PR state) + the original goal. The preamble is
    advisory ("要確認"); the lead tells the model to verify it against the real tree before acting.

    git_summary / folder_listing / pr_summary may be passed to substitute the I/O (for tests). When
    omitted they are collected from `cwd`.
    """
    preamble = _arc.progress_preamble(
        worker_last=worker_last, partial_text=partial_text, journal=journal,
        cwd=cwd, git_summary=git_summary, folder_listing=folder_listing)
    pr = _pr_summary(cwd) if pr_summary is None else (pr_summary or "")
    pr_block = ("\n\n[ブランチの PR 状態]\n%s" % pr) if pr else (
        "\n\n[ブランチの PR 状態] 取得できず(gh 不在/PR 未作成の可能性)。上の手順2で自分で確認する。")
    return (RESUME_LEAD + "\n" + preamble + pr_block
            + "\n\n--- 元の依頼 ---\n" + (goal_body or ""))
