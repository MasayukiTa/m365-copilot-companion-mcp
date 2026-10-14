"""Why did an item fail? Decided from the END of the worker's answer / transcript.

Categories (Japanese labels appear in the report):
  DB接続不可 / ロック(解錠不可) / タイムアウト / 依頼が曖昧で保留 / その他
"""
from __future__ import annotations

import re

DB_DOWN = "DB接続不可"
LOCKED = "ロック(解錠不可)"
TIMED_OUT = "タイムアウト"
AMBIGUOUS = "依頼が曖昧で保留"
OTHER = "その他"
#: The request never reached a worker (GUI route: locked screen / no cockpit; queue route:
#: the door refused or the queue is not drained). Set by the runner, not by answer text.
NOT_SUBMITTABLE = "投入不可(画面ロック等)"

TAIL_CHARS = 1500

# Order matters: the first group that matches wins.
_RULES = [
    (LOCKED, re.compile(
        r"解錠|アンロック|unlock|ロックされ|ロック中|承認が必要|承認待ち|"
        r"not\s+unlocked|gate\s+refus|refused\s+by\s+(?:the\s+)?gate|パスワード.{0,12}(?:必要|未入力|不明)",
        re.I)),
    (DB_DOWN, re.compile(
        r"接続できません|接続に失敗|接続エラー|接続不可|データベース.{0,10}(?:接続|応答)|"
        r"connection\s+(?:refused|failed|reset|error)|can'?t\s+connect|could\s+not\s+connect|"
        r"login\s+failed|ログインに失敗|ODBC.{0,40}(?:error|エラー|failed)|\b08001\b|\b08S01\b|"
        r"server\s+(?:not\s+found|does\s+not\s+exist)|サーバー?.{0,8}(?:見つかりません|応答しません)",
        re.I)),
    (TIMED_OUT, re.compile(
        r"タイムアウト|時間切れ|時間内に.{0,10}(?:終わ|完了|返)|timed?\s*out|timeout|\bHYT00\b|"
        r"(?:ターン|turn).{0,6}(?:上限|limit)",
        re.I)),
    (AMBIGUOUS, re.compile(
        r"曖昧|どちら(?:の|を|に)|いずれ(?:か|を)|ご指定ください|指定してください|"
        r"教えてください|確認させてください|確認が必要です|clarif|which\s+one|please\s+specify|"
        r"定義が(?:不明|異なり)",
        re.I)),
]

#: What a "DONE" answer must not end with for us to trust it blindly.
_STRICT = [LOCKED, DB_DOWN]


def tail_of(text: str, n: int = TAIL_CHARS) -> str:
    text = text or ""
    return text[-n:]


def classify_failure(answer: str = "", outcome: str = "", status: str = "", reason: str = "") -> str:
    """Category for a failed or stuck item. `status == 'timeout'` is decided by the caller's clock."""
    if status == "timeout":
        return TIMED_OUT
    blob = "\n".join([tail_of(answer), reason or ""])
    for label, rx in _RULES:
        if rx.search(blob):
            return label
    return OTHER


def suspicious_success(answer: str) -> str:
    """For an outcome=DONE answer: a failure-looking category found at its end, or ''.

    Only the two environment failures are checked; words like "ambiguous" appear in perfectly
    good answers, so they would only cry wolf.
    """
    tail = tail_of(answer, 600)
    for label, rx in _RULES:
        if label in _STRICT and rx.search(tail):
            return label
    return ""


def next_step_for(category: str) -> str:
    """What a person should do the next morning."""
    return {
        DB_DOWN: "接続設定とDBの稼働を確認してから、この項目だけ再実行してください(resume)。",
        LOCKED: "ツールの解錠状態を確認し、解錠してから再実行してください。",
        TIMED_OUT: "依頼を小さく分けるか、項目ごとの時間を延ばして再実行してください。",
        AMBIGUOUS: "依頼文の曖昧な語(期間・対象・指標)を具体的に書き直して再実行してください。",
        NOT_SUBMITTABLE: "PCの画面ロックやコックピットの状態、キューの詰まりを確認し、resume --retry-failed で再投入してください。",
        OTHER: "作業者の回答(項目シート)を読み、原因を確認してから再実行してください。",
    }.get(category, "作業者の回答を確認してください。")
