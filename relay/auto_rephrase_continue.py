# -*- coding: utf-8 -*-
"""作用を伴う目標が定型の無回答で止まったとき、言い換え+「ここまでの状況」を付けて
1回だけ再投入するための、状況収集と前置き文の組み立て。

なぜ別モジュールか。relay_fleet.py の定型無回答ハンドラは長く、git とフォルダ走査の
I/O をその場に書くと試験で実機を叩くことになる。ここに純関数として切り出し、I/O は
``_git_summary`` / ``_folder_listing`` の 2 つに閉じ込める。試験はこの 2 つを差し替えて
前置き文の組み立てだけを確かめられる。

设计の要点(relay_fleet.py の該当行は explain.md に記録):
  * 作用目標は同一会話での再送が重複副作用を生むため、従来は再送されず stuck だった。
    ここでは「元の会話を閉じてから新しい会話」を起こす経路に、前担当の進捗を機械的に
    前置きして渡す。作り直しを防ぐのは本文の明示指示(「すでにあるものを作り直さない。
    まず実ファイルと照合する」)である。
  * 1 回だけ。防ぐ事象=同じ副作用の二重実行。worker ごとに 1 回に限る。
"""
from __future__ import annotations

import os
import subprocess
import sys

# 前置きに載せる各断片の上限。長文の last / journal が文脈を食い潰さないよう切り詰める。
_MAX_LAST_CHARS = 600
_MAX_PARTIAL_CHARS = 400
_MAX_JOURNAL_CHARS = 400
_MAX_GIT_CHARS = 800
_MAX_FILES = 40


def _clip(text, cap):
    """末尾を … で切る。None/空は空文字。"""
    s = (text or "").strip()
    if len(s) <= cap:
        return s
    return s[:cap].rstrip() + "…"


def _run_git(args, cwd, timeout=15):
    """git をヘッドレスで 1 回走らせ、stdout を UTF-8(errors=replace)で読む。

    子プロセスの起動は creationflags を決める(Windows では CREATE_NO_WINDOW。
    relay/fleet_retention.py と同じ理由=オーナーのデスクトップにコンソール窓を出さない)。
    出力はエンコーディングを明示して読む(日本語ソース/コミットメッセージを cp932 で読むと
    落ちるため)。失敗・タイムアウトは空文字で返し、呼び出し側を止めない。
    """
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["git"] + list(args),
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
    return (proc.stdout or "").strip()


def _git_summary(cwd):
    """cwd が git 作業ツリーなら、ブランチ・直近コミット・変更ファイルの要約を返す。

    cwd が無い/git でない/読めない場合は空文字。I/O はここに閉じる(試験で差し替え可)。
    """
    if not cwd or not os.path.isdir(cwd):
        return ""
    inside = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    if inside.strip() != "true":
        return ""
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    log = _run_git(["log", "--oneline", "-5"], cwd)
    status = _run_git(["status", "--short"], cwd)
    parts = []
    if branch:
        parts.append("ブランチ: %s" % branch)
    if log:
        parts.append("直近のコミット:\n%s" % log)
    if status:
        parts.append("未コミットの変更:\n%s" % status)
    else:
        parts.append("未コミットの変更: なし")
    return _clip("\n".join(parts), _MAX_GIT_CHARS)


def _folder_listing(cwd):
    """cwd 直下のファイル/フォルダ名を列挙して返す。無ければ空文字。I/O はここに閉じる。"""
    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        names = sorted(os.listdir(cwd))
    except Exception:
        return ""
    if not names:
        return "(空)"
    shown = names[:_MAX_FILES]
    tail = "" if len(names) <= _MAX_FILES else "\n… 他 %d 件" % (len(names) - _MAX_FILES)
    return "\n".join(shown) + tail


def progress_preamble(worker_last="", partial_text="", journal="", cwd="",
                      git_summary=None, folder_listing=None):
    """前担当の「ここまでにできていること」を数行にまとめた前置き文を返す。

    (a) 前担当の最後の返答の要点(last / partial_text / journal)
    (b) 作業ツリー・ブランチの状態(git status と git log の要約。cwd がある場合)
    (c) 出力フォルダ(= cwd)にあるファイルの一覧
    を載せ、最後に「続きから進める。作り直さない。まず実ファイルと照合する」を書く。

    git_summary / folder_listing を渡せば I/O を差し替えられる(試験用)。省略時は cwd から集める。
    """
    gs = _git_summary(cwd) if git_summary is None else (git_summary or "")
    fl = _folder_listing(cwd) if folder_listing is None else (folder_listing or "")
    lines = ["--- 前の担当がここまでに進めた状況(機械的に収集。要確認) ---"]

    last = _clip(worker_last, _MAX_LAST_CHARS)
    partial = _clip(partial_text, _MAX_PARTIAL_CHARS)
    jrnl = _clip(journal, _MAX_JOURNAL_CHARS)
    if last:
        lines.append("[最後の返答の要点]\n%s" % last)
    if partial:
        lines.append("[送信途中で途切れた本文]\n%s" % partial)
    if jrnl:
        lines.append("[ジャーナル(実行済みの記録)]\n%s" % jrnl)
    if not (last or partial or jrnl):
        lines.append("[最後の返答] 記録なし。")

    if gs:
        lines.append("[作業ツリー・ブランチ]\n%s" % gs)
    else:
        lines.append("[作業ツリー・ブランチ] 不明(cwd なし、または git ではない)。")

    if fl:
        lines.append("[出力フォルダのファイル一覧]\n%s" % fl)
    else:
        lines.append("[出力フォルダのファイル一覧] 取得できず。")

    lines.append(
        "次の続きから進める。すでにあるものを作り直さない。"
        "まず、上の状況が実際のファイルと合っているかを 1 回確認する。")
    return "\n\n".join(lines)


#: 言い換えの前置き。定型の無回答で止まった前回と同じ文面をそのまま送らないための、短い
#: 枠組み文。本文(元のゴール)は後ろに続く。
REPHRASE_LEAD = (
    "前回この作業は、依頼の本文に対して定型の無回答で止まりました。"
    "同じ本文を繰り返すのではなく、下記の状況を踏まえて続きから着手してください。\n")


def build_resend_text(goal_body, worker_last="", partial_text="", journal="", cwd="",
                      git_summary=None, folder_listing=None):
    """再投入する 1 通の本文。言い換えの前置き + 進捗の前置き + 元のゴール。

    先頭に進捗(「ここまでの状況」)を機械的に付ける、という要件に従い、進捗前置きを
    ゴール本文より前に置く。
    """
    preamble = progress_preamble(
        worker_last=worker_last, partial_text=partial_text, journal=journal,
        cwd=cwd, git_summary=git_summary, folder_listing=folder_listing)
    return REPHRASE_LEAD + "\n" + preamble + "\n\n--- 元の依頼 ---\n" + (goal_body or "")


#: 設定キー名。既定オン。tools/settings_keys.py に宣言、コックピットがミラーする。
#: 読み方は relay/continue_recovery.py の socket_continue_recovery と同じ(each_gate: 変更が
#: 次の判断に restart なしで届く)。
KEY = "auto_rephrase_continue"
DEFAULT = "on"
SETTING_KEY = KEY            # 後方互換の別名
SETTING_DEFAULT = DEFAULT

_mode_cache = {}


def continue_enabled(value):
    """設定値(文字列)から、作用目標の言い換え再投入が有効かを返す。既定(None/空)はオン。

    "off" / "0" / "false" / "no" を無効とみなし、それ以外(既定 "on" を含む)は有効。
    """
    if value is None:
        return True
    s = str(value).strip().lower()
    if s == "":
        return True
    return s not in ("off", "0", "false", "no")


def _settings_path(path=None):
    if path:
        return str(path)
    from tools.settings_path import settings_file
    return settings_file()


def mode(path=None):
    """settings.txt の auto_rephrase_continue を 'on'/'off' で返す。既定 on。never raises。

    os.stat 1 回で mtime を見て、変わったときだけ読み直す。コックピットでの変更が restart
    なしに次の判断へ届く(each_gate)。ファイルは utf-8-sig で開く(continue_recovery と同じ)。
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
                    val = "on" if v == "on" else "off"
                    break
        _mode_cache[p] = (key, val)
        return val
    except Exception:
        return DEFAULT


def enabled(path=None):
    """settings.txt を読み、作用目標の言い換え再投入が有効なら True。既定 on。"""
    return mode(path) == "on"
