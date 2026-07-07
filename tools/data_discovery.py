"""Natural-language DB discovery gateway -- "which tables/columns should I look at
for X?" answered from ACCUMULATED procedural memory FIRST (so an agent skips
re-exploration), falling back to live ODBC discovery only when memory is thin.

Gateway-only tool (reached via call_tool, not one of the always-registered 8): it
must stay self-describing and cheap, so it does exactly two reads -- a memory
search (tools/procedural_memory.py) and, only when memory is empty AND a
connection was given, a live `odbc_tables` listing (tools/odbc_ops.py) -- never
a write, never require_unlocked.
"""
import re

# --- keyword extraction (extract_keywords) ------------------------------------------
# Natural Japanese questions arrive unsegmented ("PAPの材料トレースで見るべきテーブルは？"
# -- no spaces), while procedural_memory_search scores by whitespace-split tokens. Left
# alone, an unsegmented question becomes ONE giant token and matches nothing even when
# the same content phrased with spaces ("PAP 材料トレース") would hit. This is a cheap
# heuristic segmenter, not a linguistics project -- it biases toward recall (more
# candidate keywords is fine; the search scorer below just counts token hits).
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
# Punctuation/whitespace treated as hard segment boundaries before particle-splitting.
_PUNCT_RE = re.compile(
    r"[?？。、!！,，.．:：;；()（）\[\]【】「」『』\"'\s]+"
)
# Japanese particles/verb-ish suffixes that separate content words in a natural question.
# Sorted longest-first below so multi-char particles split before their single-char
# substrings would (e.g. "ください" before "は" -- not that they overlap, but this keeps
# the iterative splitting order well-defined and avoids over-fragmenting).
_JP_PARTICLES = sorted(
    [
        "は", "が", "を", "に", "で", "の", "と", "へ", "から", "まで", "より",
        "や", "か", "も", "ね", "よ", "です", "ます", "ください", "べき",
        "する", "見る", "教え",
    ],
    key=len,
    reverse=True,
)
# A kept segment must consist only of these character classes (hiragana+katakana
# U+3040-30FF, kanji U+3400-9FFF, half-width katakana U+FF66-FF9F, ascii word chars) --
# rules out stray leftover punctuation making it through.
_JP_SEGMENT_RE = re.compile(r"^[぀-ヿ㐀-鿿ｦ-ﾟA-Za-z0-9_]+$")
# A kept segment must contain at least one kanji/katakana char (U+30A0-30FF katakana,
# U+3400-9FFF kanji) UNLESS it's pure ASCII -- plain hiragana-only leftovers (e.g. a
# stray verb stem) are function-word noise, not content.
_HAS_KANJI_KATAKANA_RE = re.compile(r"[゠-ヿ㐀-鿿]")


def extract_keywords(question: str, max_keywords: int = 8) -> list[str]:
    """Segment a free-text (possibly unsegmented Japanese) question into search keywords.

    ASCII word tokens are pulled out as-is. The Japanese remainder is split on
    punctuation/whitespace and then, per whitespace-delimited word, iteratively on a
    small particle list (は/が/を/に/で/の/と/へ/から/まで/より/... ) so that e.g.
    "材料トレースで見るべきテーブルは" yields ["材料トレース", "テーブル"] instead of one
    unmatchable blob. Order-preserving dedup, capped at `max_keywords`. Never raises;
    an empty/whitespace-only (or non-string) question returns [].
    """
    try:
        if not question or not isinstance(question, str):
            return []
        q = question.strip()
        if not q:
            return []

        ascii_tokens = _ASCII_TOKEN_RE.findall(q)

        stripped = _PUNCT_RE.sub(" ", q).strip()
        jp_keywords: list[str] = []
        for word in stripped.split():
            segments = [word]
            for particle in _JP_PARTICLES:
                next_segments: list[str] = []
                for seg in segments:
                    next_segments.extend(seg.split(particle))
                segments = next_segments
            for seg in segments:
                seg = seg.strip()
                if len(seg) < 2:
                    continue
                if not _JP_SEGMENT_RE.match(seg):
                    continue
                if seg.isascii() or _HAS_KANJI_KATAKANA_RE.search(seg):
                    jp_keywords.append(seg)

        out: list[str] = []
        seen: set[str] = set()
        for tok in ascii_tokens + jp_keywords:
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
            if len(out) >= max_keywords:
                break
        return out
    except Exception:
        return []


_TAG_RE_CACHE: dict[str, re.Pattern] = {}


def _tag_pattern(prefix: str) -> re.Pattern:
    pat = _TAG_RE_CACHE.get(prefix)
    if pat is None:
        pat = re.compile(re.escape(prefix) + r":([^\s,\]]+)")
        _TAG_RE_CACHE[prefix] = pat
    return pat


def _extract_tagged_tokens(text: str, prefix: str) -> list[str]:
    """Distinct `<prefix>:<value>` tokens anywhere in text, order-preserving.

    Prefix-based (not tied to the exact procedural_memory_search line layout), so
    it stays robust if that formatting drifts -- e.g. `table:foo,table:bar` inside
    a `tags=[...]` blob, or a standalone `src:memo.md` token.
    """
    seen: list[str] = []
    for m in _tag_pattern(prefix).finditer(text):
        v = m.group(1)
        if v not in seen:
            seen.append(v)
    return seen


def _extract_match_blocks(text: str) -> list[str]:
    """Split a procedural_memory_search result into per-match blocks (each block
    starts at a "- [" line and runs to the next one, or end of text)."""
    blocks: list[str] = []
    current: list[str] = []
    for ln in text.splitlines():
        if ln.startswith("- ["):
            if current:
                blocks.append("\n".join(current))
            current = [ln]
        elif current:
            current.append(ln)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _intent_and_snippet_from_block(block: str) -> tuple[str, str]:
    """Pull (intent, snippet-preview) out of one match block. `intent=` is a
    Python repr() of the original string, so outer quotes (either kind) are
    stripped if present; anything after the first line is the snippet preview."""
    lines = block.splitlines()
    first_line, rest = (lines[0], lines[1:]) if lines else ("", [])
    intent = ""
    idx = first_line.find("intent=")
    if idx != -1:
        raw = first_line[idx + len("intent="):].strip()
        if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
            raw = raw[1:-1]
        intent = raw
    snippet = " ".join(ln.strip() for ln in rest if ln.strip())
    return intent, snippet


def _compose_answer(
    question: str,
    connection: str,
    tables: list[str],
    intents: list[tuple[str, str]],
    srcs: list[str],
    live_fallback: str | None = None,
    empty_reason: str | None = None,
) -> str:
    """Assemble the final answer string from already-parsed pieces. Kept separate
    from find_db_objects so it (and the parsers above) are unit-testable without
    any DB or memory store."""
    lines = [f"question: {question}"]
    if connection:
        lines.append(f"connection: {connection}")
    if tables:
        lines.append("候補テーブル (memory):")
        for t in tables:
            lines.append(f"  - {t}")
    if intents:
        lines.append("根拠 (memory intents/snippets):")
        for intent, snippet in intents[:5]:
            snip = snippet[:200]
            if intent and snip:
                lines.append(f"  - {intent}: {snip}")
            elif intent:
                lines.append(f"  - {intent}")
            elif snip:
                lines.append(f"  - {snip}")
    if srcs:
        lines.append("出典: " + ", ".join(srcs))
    if live_fallback is not None:
        lines.append("memoryに該当なし。ライブ探索します:")
        lines.append(live_fallback)
    if empty_reason:
        lines.append(empty_reason)
    return "\n".join(lines)


def find_db_objects(question: str, connection: str = "", limit: int = 8) -> str:
    """Answer "which tables/columns should I look at for X?" from procedural
    memory first, falling back to a live ODBC table listing only when memory has
    nothing AND a connection was given. Read-only; no unlock required.

    Args:
        question: Free-text question about what DB objects are relevant.
        connection: Optional named ODBC connection to bias the memory search
            toward and to fall back to live-list if memory is empty.
        limit: Max procedural_memory_search matches to consider (default 8).
    """
    try:
        if not question or not isinstance(question, str):
            return "[find_db_objects error: question must be a non-empty string]"

        # Segment the (possibly unsegmented Japanese) question into search keywords --
        # procedural_memory_search scores by whitespace-split tokens, so a raw
        # unsegmented question would collapse into one giant unmatchable token. Falls
        # back to the raw question if extraction yields nothing (e.g. pure ASCII with
        # no word-ish tokens, or an edge case the heuristic doesn't cover).
        keywords = extract_keywords(question)
        base_query = " ".join(keywords) if keywords else question
        query = f"{base_query} connection:{connection}" if connection else base_query
        try:
            from .procedural_memory import procedural_memory_search, SQL_STOPWORDS
        except Exception as e:
            return f"[find_db_objects error: {type(e).__name__}: {e}]"
        result = procedural_memory_search(query, limit)

        has_memory = bool(result) and not result.startswith("[") and not result.startswith("(no matches")
        tables: list[str] = []
        intents: list[tuple[str, str]] = []
        srcs: list[str] = []
        if has_memory:
            # Display-side stopword filter: also cleans candidates for entries that
            # were imported BEFORE the importer-side SQL_STOPWORDS fix, since the store
            # may still hold old noisy table:group / table:having / ... tags.
            tables = [
                t for t in _extract_tagged_tokens(result, "table")
                if t.upper() not in SQL_STOPWORDS
            ]
            srcs = _extract_tagged_tokens(result, "src")
            intents = [_intent_and_snippet_from_block(b) for b in _extract_match_blocks(result)]

        if tables or intents:
            return _compose_answer(question, connection, tables, intents, srcs)

        # Memory had nothing usable.
        if connection:
            try:
                from .odbc_ops import odbc_tables
                live = odbc_tables(connection)
            except Exception as e:
                live = f"[odbc_tables error: {type(e).__name__}: {e}]"
            if isinstance(live, str) and live.startswith("["):
                return _compose_answer(
                    question, connection, [], [], [],
                    empty_reason=f"memoryに該当なし。ライブ探索も失敗しました: {live}",
                )
            live_capped = "\n".join(live.splitlines()[:30])
            return _compose_answer(question, connection, [], [], [], live_fallback=live_capped)

        guidance = (
            "memoryに該当なし (connection未指定のためライブ探索は行いません)。"
            " procedural_memory_import_markdown でDBメモを取り込むか、"
            " MCP_DATA_MEMORY_AUTO を有効にしてください。"
        )
        return _compose_answer(question, connection, [], [], [], empty_reason=guidance)
    except Exception as e:
        return f"[find_db_objects error: {type(e).__name__}: {e}]"
