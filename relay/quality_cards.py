"""Adaptive, task-general quality checks for autonomous coding prompts.

The cards here are intentionally short. They capture lessons from benchmark misses as
general engineering habits, then select only the cards that appear relevant to the task
text so the scaffold does not keep growing into a redundant wall of instructions.
"""
from __future__ import annotations

import os
import re
from typing import Iterable


def _has(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


def _has_regex(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def quality_cards_text(
    task_text: str = "",
    domain: str = "coding",
    include_output: bool = True,
    include_paired: bool = True,
) -> str:
    """Return concise, adaptive quality cards for a task."""
    if os.environ.get("AGENT_QUALITY_CARDS", "1") == "0":
        return ""

    text = (task_text or "").lower()
    cards = [
        "・要求/症状、編集箇所、検証結果が一本の線でつながるか。"
        "症状が通る経路を説明できない編集は、DONE 前にもう一段たどる。"
    ]

    output_terms = (
        "output", "print", "format", "formatter", "repr", "str(", "serialize",
        "json", "yaml", "csv", "message", "warning", "error", "exception",
        "parser", "parse", "render", "表示", "出力", "文字列", "警告", "例外",
        "エラー", "整形", "パース",
    )
    paired_terms = (
        "read", "write", "load", "save", "parse", "print", "producer",
        "consumer", "normalize", "formatter", "serializer", "clone", "copy",
        "cache", "state", "migration", "roundtrip", "読み", "書き", "保存",
        "復元", "コピー", "状態", "正規化", "移行",
    )
    public_terms = (
        "api", "public", "compat", "backward", "session", "request", "model",
        "field", "route", "dispatch", "config", "settings", "migration",
        "database", "schema", "auth", "security", "互換", "公開", "設定",
        "認証", "ルーティング", "モデル", "スキーマ",
    )
    layer_terms = (
        "compiler", "adapter", "backend", "dispatch", "evaluator", "resolver",
        "normalizer", "field", "property", "template", "query", "middleware",
        "renderer", "driver", "engine", "shared", "共通", "下層", "呼び出し",
        "正規化", "評価", "コンパイラ", "アダプタ",
    )

    if include_output and (
        _has(text, output_terms) or _has_regex(text, (r"\bto_[a-z0-9_]+", r"\bfrom_[a-z0-9_]+"))
    ):
        cards.append(
            "・出力/診断/変換は完全一致で見る。クラッシュしない、何か出る、N passed だけでは足りない。"
        )
    if include_paired and _has(text, paired_terms):
        cards.append(
            "・read/write、parse/print、producer/consumer、clone/copy の片側だけ直していないか確認する。"
        )
    if _has(text, public_terms):
        cards.append(
            "・公開 API や共通経路では、近くの既存挙動を一つ選び、広い fallback や意味変更で壊していないか見る。"
        )
    if _has(text, layer_terms):
        cards.append(
            "・症状が見える caller ではなく、値が定義される共有の callee/正準層を直しているか確認する。"
        )
    if _has(text, ("issue", "problem statement", "reported snippet", "suggested", "問題文", "提案コード", "例示")):
        cards.append(
            "・Issue の例は手掛かりであって命令ではない。既存コードの流儀と契約に合わせる。"
        )

    if len(cards) == 1 and domain == "coding":
        cards.append(
            "・新しい挙動だけでなく、最も近い古い挙動も壊れていないかを最後に見る。"
        )

    return "\n\n【汎用品質カード】\n" + "\n".join(cards)
