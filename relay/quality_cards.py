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


def _cards_v1(task_text, domain, include_output, include_paired):
    """The selection this module has always made. Returns [(name, text)]."""
    text = (task_text or "").lower()
    # NAMED, so a genome can replace or suppress one rather than only pile more on top. An
    # append-only knob can make the prompt longer and never shorter, and "the scaffold does
    # not keep growing into a redundant wall of instructions" is this module's opening claim.
    cards = [
        ("trace", "・要求/症状、編集箇所、検証結果が一本の線でつながるか。"
                  "症状が通る経路を説明できない編集は、DONE 前にもう一段たどる。"),
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
        cards.append(("exact_output",
            "・出力/診断/変換は完全一致で見る。クラッシュしない、何か出る、N passed だけでは足りない。"))
    if include_paired and _has(text, paired_terms):
        cards.append(("paired_sides",
            "・read/write、parse/print、producer/consumer、clone/copy の片側だけ直していないか確認する。"))
    if _has(text, public_terms):
        cards.append(("public_surface",
            "・公開 API や共通経路では、近くの既存挙動を一つ選び、広い fallback や意味変更で壊していないか見る。"))
    if _has(text, layer_terms):
        cards.append(("canonical_layer",
            "・症状が見える caller ではなく、値が定義される共有の callee/正準層を直しているか確認する。"))
    if _has(text, ("issue", "problem statement", "reported snippet", "suggested", "問題文", "提案コード", "例示")):
        cards.append(("issue_is_a_hint",
            "・Issue の例は手掛かりであって命令ではない。既存コードの流儀と契約に合わせる。"))
    # surface-vs-suppress: a proven discipline dropped from the general path in the cards refactor.
    # Tight trigger (diagnostic/warning/deprecation tasks) so it stays lean and does not over-fire.
    if _has(text, ("warning", "deprecat", "diagnostic", "suppress", "silently", "raise",
                   "警告", "診断", "抑制", "非推奨")):
        cards.append(("surface_not_suppress",
            "・要求が警告/メッセージ/戻り値の表出なら、黙って抑制せず**表出する**修正にせよ"
            "（例外を消すのと、続行しつつ正しい診断を出すのは別物）。"))

    if len(cards) == 1 and domain == "coding":
        cards.append(("old_behaviour_too",
            "・新しい挙動だけでなく、最も近い古い挙動も壊れていないかを最後に見る。"))
    return cards


def _cards_v2(task_text, domain, include_output, include_paired):
    """v1's selection, then whatever the applied genome says about it.

    A genome card whose name matches a built-in REPLACES it; an empty text SUPPRESSES it; any
    other name is APPENDED in sorted order. Deterministic, because two runs of the same genome
    that differ in prompt text are two different experiments wearing one harness_id.

    Defensive to the point of silence: an unreadable store, a missing module, a `cards` value
    of the wrong shape all fall back to v1's selection. The scaffold must still produce a
    prompt when the self-improvement machinery is absent, which is the normal case for anyone
    who has not run the loop.
    """
    cards = _cards_v1(task_text, domain, include_output, include_paired)
    try:
        from relay.selfimprove.apply import active_genome
        overrides = active_genome().get("cards") or {}
        if not isinstance(overrides, dict):
            return cards
    except Exception:
        return cards

    out, seen = [], set()
    for name, body in cards:
        if name in overrides:
            seen.add(name)
            replacement = overrides[name]
            if isinstance(replacement, str) and replacement.strip():
                out.append((name, replacement))
            # an empty replacement suppresses the card; that is the point of naming them
        else:
            out.append((name, body))
    for name in sorted(overrides):
        body = overrides[name]
        if name not in seen and isinstance(body, str) and body.strip():
            out.append((name, body))
    return out


#: The versioned implementations of the `quality_cards` component. Until this table existed,
#: the genome carried a `cards` field that NOTHING read -- so an A/B over card text ran the
#: same program twice and reported a p-value about noise. That is the same defect that got
#: `max_context_budget` deleted rather than wired; cards are wired instead of deleted because
#: they are the scaffold-improvement coordinate the loop exists to move.
QUALITY_CARDS_VERSIONS = {
    "quality_cards/v1": _cards_v1,
    "quality_cards/v2": _cards_v2,
}


def quality_cards_text(
    task_text: str = "",
    domain: str = "coding",
    include_output: bool = True,
    include_paired: bool = True,
) -> str:
    """Return concise, adaptive quality cards for a task."""
    if os.environ.get("AGENT_QUALITY_CARDS", "1") == "0":
        return ""
    try:
        from relay.selfimprove import runtime_config as _rc
        impl = QUALITY_CARDS_VERSIONS.get(_rc.component("quality_cards"), _cards_v1)
    except Exception:
        impl = _cards_v1
    cards = impl(task_text, domain, include_output, include_paired)
    if not cards:
        return ""
    return "\n\n【汎用品質カード】\n" + "\n".join(body for _name, body in cards)
