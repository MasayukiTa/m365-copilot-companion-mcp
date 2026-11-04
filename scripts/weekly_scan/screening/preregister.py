"""強い候補の事前登録 — Candidate のうち strong なものを PreregRecord にして追記する。

順序の信頼時計は「登録済みのもの」を使う方針なので、事前登録レコードには
order_clock.state="initial" のみを置き、新規に順序を偽装しない。

出力は JSON Lines（1行1レコード）で追記する純関数的 I/O。同じ candidate_id は
二重登録しない（既存ファイルを読んで重複排除する）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def build_prereg_record(
    candidate: Dict[str, Any], now_iso: Optional[str] = None
) -> Dict[str, Any]:
    """Candidate -> PreregRecord（SCHEMA 6）。strong かどうかは呼び元が選別する。"""
    ts = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ri = candidate.get("result_item", "")
    pooled = candidate.get("pooled_delta", 0.0)
    claim = (
        "treated 群は control 群より %s が pooled_delta=%s だけ高い（品名層別）"
        % (ri, pooled)
    )
    return {
        "candidate_id": candidate.get("candidate_id", ""),
        "result_item": ri,
        "claim": claim,
        "pooled_delta": pooled,
        "preregistered_at": ts,
        # 順序の信頼時計は登録済みのものを使う。新規事前登録は initial 状態のみ。
        "order_clock": {"source": "registered", "state": "initial"},
    }


def _read_existing_ids(path: str) -> set:
    ids = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = rec.get("candidate_id")
                if cid:
                    ids.add(cid)
    return ids


def preregister_strong(
    candidates: List[Dict[str, Any]],
    prereg_path: str,
    now_iso: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """strong=True の候補のうち未登録のものを prereg_path(JSONL) に追記し、
    追記したレコードの一覧を返す。

    判定器・基準・Δmin・補正・順序の信頼時計は変更しない。
    """
    strong = [
        c for c in (candidates or []) if c.get("strong") and c.get("candidate_id")
    ]
    if not strong:
        return []
    os.makedirs(os.path.dirname(os.path.abspath(prereg_path)) or ".", exist_ok=True)
    existing = _read_existing_ids(prereg_path)
    written: List[Dict[str, Any]] = []
    with open(prereg_path, "a", encoding="utf-8") as fh:
        for c in strong:
            if c["candidate_id"] in existing:
                continue
            rec = build_prereg_record(c, now_iso=now_iso)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written.append(rec)
            existing.add(c["candidate_id"])
    return written


__all__ = ["build_prereg_record", "preregister_strong"]
