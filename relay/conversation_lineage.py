"""Conversation transcript lineage helpers.

A single Copilot conversation can be continued by several fleet workers.  Each worker writes its
own transcript file, while the conversation identity stays the same.  The registry therefore needs
both a backwards-compatible latest pointer (``transcript``) and an ordered, non-destructive lineage
(``transcripts``).
"""


def transcript_chain(row):
    """Return ordered unique transcript paths carried by a registry row."""
    row = row or {}
    out = []
    raw = row.get("transcripts")
    if isinstance(raw, (list, tuple)):
        for value in raw:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
    latest = str(row.get("transcript") or "").strip()
    if latest and latest not in out:
        out.append(latest)
    return out


def merge_transcript_chains(*rows):
    """Stable ordered union of transcript lineages from rows/dicts."""
    out = []
    for row in rows:
        for text in transcript_chain(row):
            if text not in out:
                out.append(text)
    return out


def with_transcript_lineage(row):
    """Copy a row and materialize ``transcripts`` when it has any transcript pointer."""
    out = dict(row or {})
    chain = transcript_chain(out)
    if chain:
        out["transcripts"] = chain
    return out
