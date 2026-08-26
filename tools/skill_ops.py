"""Read-only model-facing access to already trusted Skills.

Creation, import, and approval are intentionally absent: only the local human console
may perform those administrative operations. Loading a Skill does not bypass the
normal unlock or autonomy-contract gates of any tool it later recommends.
"""
from __future__ import annotations

import json
import os
from typing import Any
from pathlib import Path

from relay.skills import SkillError, SkillStore


def _store() -> SkillStore:
    project = os.environ.get("MCP_SKILLS_PROJECT_ROOT") or str(Path(__file__).resolve().parent.parent)
    return SkillStore(project)


def skill_list() -> str:
    """List Skill metadata and trust state without loading instruction bodies.

    Also reports bundles that FAILED to load, under "invalid". A malformed SKILL.md
    used to be skipped in silence, so a Skill that had just been written simply never
    appeared and there was nothing to debug -- the commonest cause being an unquoted
    ':' in the description, which makes the YAML frontmatter invalid. The reason is
    surfaced here (folder name + parser message); the bundle's contents stay
    unexposed, so an untrusted body still cannot reach the model through this path.
    """
    try:
        store = _store()
        payload: Any = store.list_metadata(model_safe=True)
        try:
            invalid = store.invalid_bundles()
        except Exception:
            invalid = {}
        if invalid:
            payload = {
                "skills": payload,
                "invalid": [
                    {"folder": folder, "error": reason, "hint": _invalid_hint(reason)}
                    for folder, reason in sorted(invalid.items())
                ],
            }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"[skill_list error: {type(exc).__name__}: {exc}]"


def _invalid_hint(reason: str) -> str:
    """Turn a parser message into the concrete edit that fixes it."""
    text = (reason or "").lower()
    if "yaml" in text or "mapping values" in text:
        return ("SKILL.md の frontmatter が YAML として壊れています。"
                "description に ':' や '#' が含まれる場合は "
                'description: "..." のように引用符で囲んでください。')
    if "frontmatter" in text:
        return "SKILL.md の先頭を --- で開き、--- で閉じてください。"
    if "no SKILL.md" in reason:
        return "フォルダ直下に SKILL.md を置いてください。"
    return "SKILL.md を修正してから再度 skill_list を実行してください。"


def skill_match(text: str) -> str:
    """Find a confidently matching trusted Skill using metadata only; does not load it."""
    try:
        store = _store()
        result = store.match(text)
        if result:
            return json.dumps(result, ensure_ascii=False, indent=2)
        # 一致なしとだけ返していたとき、呼び出し側は「そんな手順は無い」と読み、
        # 自分でやり方を考え始めた。実際には手順はあって、束を1文字直したせいで
        # 再承認待ちになっていただけだった。照合は信頼済みしか見ないので、
        # 待っているものがあるならそれを言う。承認そのものは人の操作のまま。
        #
        # AND ASK FOR THE APPROVAL, rather than only mentioning that one is missing. Saying
        # "waiting for human re-approval" was true and useless: nothing in the system had
        # asked anybody for anything. request_approval is called from one place in the whole
        # codebase -- a command inside the chat CLI's REPL -- so the Approval Centre showed
        # nothing to approve, correctly, and six Skills sat unreadable for weeks. Raising the
        # request here puts the decision in front of the user at the moment it matters: when
        # a procedure they wrote would have been used and could not be.
        #
        # This does not trust anything. It writes a question, with the bundle's digest and a
        # preview, for a person to answer. Requests are de-duplicated by digest, so a Skill
        # that keeps almost-matching does not fill the queue with copies of one question.
        try:
            near = store.match_unapproved(text)
        except Exception:
            near = None
        if near:
            asked = ""
            try:
                review = store.request_approval(near["name"])
                asked = ("承認待ちとして登録しました（承認センターに表示されます）。"
                         if review.get("token") else "")
            except Exception:
                asked = ""
            # SAY WHICH KIND OF APPROVAL IS MISSING. `changed` means a human DID approve this
            # Skill and then its text moved, so the approval -- which is of a hash -- lapsed:
            # that is re-approval, and it tells the reader the procedure is one they trusted
            # before rather than something that arrived unvetted. `untrusted` has never been
            # approved at all. Flattening the two would lose the only part a person needs to
            # decide quickly.
            state = ("has changed since it was approved and is waiting for human "
                     "re-approval" if near["trust"] == "changed"
                     else "has never been approved by a human")
            return ("(no confident Skill match among TRUSTED Skills. "
                    "/%s looks like the right procedure but it %s, so it cannot be matched "
                    "or loaded. %sAsk the user to approve it, or proceed without it -- do not "
                    "invent your own procedure and present it as theirs.)"
                    % (near["name"], state, asked))
        try:
            waiting = [row["name"] for row in store.list_metadata(model_safe=True)
                       if row.get("trust") != "trusted"]
        except Exception:
            waiting = []
        if waiting:
            return ("(no confident Skill match among TRUSTED Skills. "
                    "These exist but are waiting for human approval, so they cannot be "
                    "matched or loaded yet: %s. Use skill_request_approval to put them in "
                    "front of the user, or proceed without them -- do not invent your own "
                    "procedure and present it as theirs.)" % ", ".join(waiting))
        return "(no confident Skill match)"
    except Exception as exc:
        return f"[skill_match error: {type(exc).__name__}: {exc}]"


def skill_request_approval(name: str = "") -> str:
    """Put unapproved Skills in front of the human, so they can be approved.

    A Skill is readable only while a person has approved THIS version of its text, and the
    approval is of a hash -- so editing one character correctly drops it back to `changed`.
    What was missing is the other half: nothing ever asked for the approval again. The
    request path existed and was reachable from exactly one command inside the chat CLI's
    REPL, so the Approval Centre sat empty and truthful while six Skills were unreadable,
    two of them approved once and invalidated by an edit weeks ago.

    With no name, asks for every unapproved Skill at once -- the "why is nothing in my
    approval queue" case. With a name, asks for that one.

    This grants nothing. It writes a question carrying the bundle's digest, its file list,
    its scripts, and a preview of its text, for a person to answer in the Approval Centre.
    Approving remains entirely a human action.

    Args:
        name: A single Skill to request, or empty for every unapproved one.
    """
    try:
        store = _store()
        targets = [name] if name else [row["name"] for row in store.unapproved()]
        if not targets:
            return "(every Skill is already approved -- nothing to request)"
        asked, already, failed = [], [], []
        for target in targets:
            try:
                review = store.request_approval(target)
            except Exception as exc:
                failed.append("%s (%s)" % (target, type(exc).__name__))
                continue
            if review.get("status") == "already-trusted":
                already.append(target)
            else:
                asked.append(target)
        lines = []
        if asked:
            lines.append("承認待ちとして登録しました（承認センターに表示されます）: "
                         + ", ".join("/" + n for n in asked))
        if already:
            lines.append("すでに承認済み: " + ", ".join("/" + n for n in already))
        if failed:
            lines.append("要求できませんでした: " + ", ".join(failed))
        lines.append("承認は人の操作です。承認センターで内容(digest とプレビュー)を"
                     "確認して承認してください。")
        return "\n".join(lines)
    except Exception as exc:
        return f"[skill_request_approval error: {type(exc).__name__}: {exc}]"


def skill_load(name: str, arguments: str = "") -> str:
    """Load and render one trusted Skill. Untrusted or changed bundles are refused."""
    try:
        return _store().render(name, arguments)
    except SkillError as exc:
        return f"[skill_load refused: {exc}]"
    except Exception as exc:
        return f"[skill_load error: {type(exc).__name__}: {exc}]"


def skill_read_resource(name: str, path: str) -> str:
    """Read a bounded UTF-8 resource from a trusted Skill's resources directories."""
    try:
        return _store().read_resource(name, path)
    except SkillError as exc:
        return f"[skill_read_resource refused: {exc}]"
    except Exception as exc:
        return f"[skill_read_resource error: {type(exc).__name__}: {exc}]"
