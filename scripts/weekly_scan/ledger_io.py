"""台帳(台帳.json)の読み書き I/O。

週次走査の台帳は、取りこぼし棚卸しの結果(解析ごとの分類・次の契機・最終走査日)を
保持する。本モジュールは台帳の JSON スキーマの定義・検証・原子的な読み書きだけを
受け持ち、分類の判断や判定器の呼び出しはしない。

分類コード(「採用でない理由」の 5 種):
  A = 効果なしが確かめられた(区間が基準の内側で、ロット数も十分)。当面の対象外。
  B = 事後解析のため登録順序が成立しない(効果の傾向は出ているが保留止まり)。
  C = ロット数不足(あと何ロットで足りるかを next_trigger に書く)。
  D = 取得・入力の誤りの疑い(期間限定・結合失敗・品名不揃い等。再取得が必要)。
  E = 判定不能(構造の問題)。

B・C・D は「見込みのある取りこぼし候補」。A だけが「当面の対象外」。

スキーマ(1 エントリ):
  analysis_id   : str   解析 ID(claim_id 等、一意)。
  classification: str   'A'|'B'|'C'|'D'|'E' のいずれか。
  next_trigger  : str   次の契機(例: 確認用ロットが規定数に達する予定日、再取得)。
  last_scanned  : str   最後に走査した日(ISO 日付または日時。未走査は空文字)。
  evidence      : str   分類の根拠(ファイル・行・値)。任意。
  verdict       : str   直近の判定(HOLD 等)。任意。
  reasons       : list  判定の理由コード。任意。
  notes         : str   追跡メモ等。任意。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

#: 認められる分類コード。
VALID_CLASSIFICATIONS = ("A", "B", "C", "D", "E")

#: 「見込みのある取りこぼし候補」として走査を続ける分類。
CANDIDATE_CLASSIFICATIONS = ("B", "C", "D")

#: 台帳スキーマのバージョン。将来の移行判断に使う。
SCHEMA_VERSION = "1.0"


@dataclass
class LedgerEntry:
    """台帳の 1 解析分。"""

    analysis_id: str
    classification: str
    next_trigger: str = ""
    last_scanned: str = ""
    evidence: str = ""
    verdict: str = ""
    reasons: List[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.analysis_id:
            raise ValueError("analysis_id は必須です。")
        if self.classification not in VALID_CLASSIFICATIONS:
            raise ValueError(
                f"classification は {VALID_CLASSIFICATIONS} のいずれか必要: "
                f"{self.classification!r}"
            )
        if not isinstance(self.reasons, list):
            raise ValueError("reasons は配列である必要があります。")

    @property
    def is_candidate(self) -> bool:
        """見込みのある取りこぼし候補(B・C・D)か。"""
        return self.classification in CANDIDATE_CLASSIFICATIONS

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LedgerEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Ledger:
    """台帳全体(スキーマ版 + エントリ群)。"""

    entries: List[LedgerEntry] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def by_id(self, analysis_id: str) -> Optional[LedgerEntry]:
        for e in self.entries:
            if e.analysis_id == analysis_id:
                return e
        return None

    def candidates(self) -> List[LedgerEntry]:
        """B・C・D の見込み候補だけを返す。"""
        return [e for e in self.entries if e.is_candidate]

    def upsert(self, entry: LedgerEntry) -> None:
        """同じ analysis_id があれば置き換え、無ければ追加する。"""
        for i, e in enumerate(self.entries):
            if e.analysis_id == entry.analysis_id:
                self.entries[i] = entry
                return
        self.entries.append(entry)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Ledger":
        raw_entries = d.get("entries", [])
        if not isinstance(raw_entries, list):
            raise ValueError("entries は配列である必要があります。")
        entries = [LedgerEntry.from_dict(x) for x in raw_entries]
        ids = [e.analysis_id for e in entries]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"analysis_id が重複しています: {sorted(dup)}")
        return cls(
            entries=entries,
            schema_version=str(d.get("schema_version", SCHEMA_VERSION)),
        )


def load_ledger(path: str) -> Ledger:
    """台帳.json を読む。ファイルが無ければ空の台帳を返す。"""
    if not os.path.exists(path):
        return Ledger()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("台帳.json のトップレベルはオブジェクトである必要があります。")
    return Ledger.from_dict(data)


def save_ledger(path: str, ledger: Ledger) -> None:
    """台帳.json を原子的に書く(同一ディレクトリの一時ファイル経由で置換)。

    途中で落ちても元の台帳が壊れないよう、tempfile に書いてから os.replace する。
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    payload = json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".台帳_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def mark_scanned(ledger: Ledger, analysis_id: str, when: str) -> bool:
    """解析の last_scanned を更新する。見つかれば True。

    when はこの実行が与える日付/日時文字列(呼び出し側が now を渡す)。本モジュールは
    時計を生成しない(順序の信頼時計の偽装を避けるため、走査日の記録のみ)。
    """
    e = ledger.by_id(analysis_id)
    if e is None:
        return False
    e.last_scanned = when
    return True
