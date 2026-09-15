# -*- coding: utf-8 -*-
"""Did the Copilot agent make this deck.

ONE QUESTION, ANSWERED YES OR NO. Not "how many machine generations from a human original" --
that line does not exist here. The operator's own August and September material was drafted by
GPT or Claude and then corrected by hand, so "human original" is not a category this folder
contains. What the folder does contain is output from the Copilot agent fleet, which is at
present distinctly the weaker of the two, and telling it apart from everything else is the
distinction worth having.

WHY IT IS NEEDED. Measured 2026-09-15 on a deck a worker produced that morning from a September
2nd template: `python-pptx` carries the template's core properties across, so the file reports
the operator as its author, the operator as its last editor, and a creation time two weeks
before it existed. It does not merely fail to say a machine made it -- it says a person did.

WHAT THAT COST, in the operator's words: 「オリジナルを参照したのか自己作製をさらに模倣したのか
がわからなくなる」. A worker asked for "this month's report from the past material" finds last
week's agent-made report sitting in the same folder and treats it as past material. That
happened: the first of four runs reported the work complete by pointing at output an earlier run
had made, and the holdout that made the measurement mean anything had to be built by hand,
moving 94 files aside to decide which were source and which were product.

WHERE IT IS WRITTEN. `docProps/custom.xml` -- custom document properties. Invisible in normal
use (File > Info > Properties > Advanced is the only place PowerPoint shows them), preserved
across edit-and-save by PowerPoint itself, and already present in some of this operator's own
decks, so it is a mechanism the format supports rather than a place being squatted.

    CompanionMade    "1" -- the Copilot agent fleet wrote this file
    CompanionRunId   which run, so an output ties back to its transcript
    CompanionAt      when it was actually written, since `created` will lie
    CompanionSources which decks were open when it was written, for reading a bad output later

`CompanionSources` is a diagnostic, not part of the answer. The answer is `CompanionMade`, and
it is present or it is not.

THIS MODULE ONLY READS AND WRITES THE TAG. Making every worker-written deck carry one is a
separate problem -- see the injection into the child interpreter that runs worker code.

    python -m tools.pptx_provenance --scan <folder>
    python -m tools.pptx_provenance --read <file>
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import sys
import time
import zipfile
from xml.sax.saxutils import escape, unescape

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The custom-properties part, and the namespaces OOXML requires inside it.
CUSTOM_PART = "docProps/custom.xml"
_NS = ("xmlns=\"http://schemas.openxmlformats.org/officeDocument/2006/custom-properties\" "
       "xmlns:vt=\"http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes\"")
#: The fmtid every custom document property carries. Fixed by the format, not chosen here.
_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"

#: Property names. Prefixed so they cannot collide with a property the operator set, and so one
#: prefix test finds every one of ours.
PREFIX = "Companion"
MADE = PREFIX + "Made"
RUN_ID = PREFIX + "RunId"
AT = PREFIX + "At"
SOURCES = PREFIX + "Sources"

#: How much of a source file's digest is recorded. Enough to tell which file it was among the
#: few in a folder; not a claim that this is a cryptographic binding.
DIGEST_CHARS = 16
SOURCE_SEP = " | "


def digest(path) -> str:
    """A short content digest, or "" when the file cannot be read.

    Content rather than mtime: a deck copied to a new name is the same deck, and a deck
    rewritten in place is a different one. mtime says the opposite of both.
    """
    try:
        h = hashlib.sha256()
        with io.open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:DIGEST_CHARS]
    except OSError:
        return ""


def _parse_custom(xml: str) -> dict:
    """{name: value} from a custom.xml part. Tolerant: an unreadable part reads as empty.

    Deliberately not an XML parse. This part is read on every scan and written by several
    producers (PowerPoint, python-pptx, this module), and a strict parse that raises on one
    unusual deck would take the scan down for every other deck in the folder. The shape matched
    is fixed by the format: `<property ... name="X"><vt:lpwstr>Y</vt:lpwstr></property>`.
    """
    import re
    out = {}
    for m in re.finditer(r'<property\b[^>]*\bname="([^"]*)"[^>]*>(.*?)</property>', xml, re.S):
        body = m.group(2)
        v = re.search(r"<vt:[a-zA-Z0-9]+>(.*?)</vt:[a-zA-Z0-9]+>", body, re.S)
        out[unescape(m.group(1))] = unescape(v.group(1)) if v else ""
    return out


def _render_custom(props: dict) -> bytes:
    """A complete custom.xml for `props`. pids start at 2, as the format requires."""
    parts = ["<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n",
             "<Properties %s>" % _NS]
    for i, (name, value) in enumerate(sorted(props.items()), start=2):
        parts.append("<property fmtid=\"%s\" pid=\"%d\" name=\"%s\">"
                     "<vt:lpwstr>%s</vt:lpwstr></property>"
                     % (_FMTID, i, escape(str(name)), escape(str(value))))
    parts.append("</Properties>")
    return "".join(parts).encode("utf-8")


def read(path) -> dict:
    """This deck's tag, or {} if it has none. Never raises on a file it cannot read."""
    try:
        with zipfile.ZipFile(path) as z:
            if CUSTOM_PART not in z.namelist():
                return {}
            props = _parse_custom(z.read(CUSTOM_PART).decode("utf-8", "replace"))
    except (OSError, zipfile.BadZipFile, KeyError):
        return {}
    return {k: v for k, v in props.items() if k.startswith(PREFIX)}


def made_by_agent(path) -> bool:
    """The whole question. No tag means no -- and means only that.

    ABSENCE IS NOT A CERTIFICATE. A deck the fleet wrote before this existed carries nothing,
    and so does one written by a path that never reaches the stamp. `False` says "nothing here
    claims the agent wrote it", which is weaker than "a person wrote it" and must not be read
    as the stronger thing.
    """
    return (read(path) or {}).get(MADE) == "1"


def stamp(path, sources=(), run_id="", now=None) -> dict:
    """Mark `path` as agent-written, preserving everything else in the file.

    REWRITES THE WHOLE ARCHIVE, because a zip cannot have a member replaced in place. Every
    other part is copied across byte-for-byte and the result is written beside the original then
    moved over it, so a failure mid-write leaves the original where it was rather than a
    half-archive -- the failure this repository spent a day on when an archive that stopped
    mid-write was mistaken for a backup.
    """
    tag = {
        MADE: "1",
        RUN_ID: str(run_id or os.environ.get("MCP_FLEET_RUN_ID") or ""),
        AT: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now or time.time())),
        SOURCES: SOURCE_SEP.join("%s@%s" % (os.path.basename(str(s)), digest(s) or "?")
                                 for s in (sources or ())),
    }

    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    existing = {}
    for i, data in items:
        if i.filename == CUSTOM_PART:
            existing = _parse_custom(data.decode("utf-8", "replace"))
    merged = dict(existing)
    merged.update(tag)
    payload = _render_custom(merged)

    tmp = str(path) + ".provtmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        wrote = False
        for i, data in items:
            if i.filename == CUSTOM_PART:
                out.writestr(i, payload)
                wrote = True
            elif i.filename == "[Content_Types].xml":
                out.writestr(i, _with_custom_declared(data))
            elif i.filename == "_rels/.rels":
                out.writestr(i, _with_custom_related(data))
            else:
                out.writestr(i, data)
        if not wrote:
            out.writestr(CUSTOM_PART, payload)
    shutil.move(tmp, str(path))
    return tag


def _with_custom_declared(content_types: bytes) -> bytes:
    """Declare custom.xml in [Content_Types].xml if it is not already declared.

    An undeclared part is one PowerPoint refuses to open the file over -- the tag would cost the
    deck rather than describe it.
    """
    text = content_types.decode("utf-8", "replace")
    if "/docProps/custom.xml" in text:
        return content_types
    decl = ('<Override PartName="/docProps/custom.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.custom-properties+xml"/>')
    return text.replace("</Types>", decl + "</Types>").encode("utf-8")


def _with_custom_related(rels: bytes) -> bytes:
    """Point the package at the part. An orphaned part is one nothing will read."""
    text = rels.decode("utf-8", "replace")
    if "docProps/custom.xml" in text:
        return rels
    rel = ('<Relationship Id="rIdCompanionProv" Type="http://schemas.openxmlformats.org/'
           'officeDocument/2006/relationships/custom-properties" '
           'Target="docProps/custom.xml"/>')
    return text.replace("</Relationships>", rel + "</Relationships>").encode("utf-8")


def scan(folder, suffix=".pptx"):
    """Every deck under `folder`, agent-made ones first."""
    rows = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if not name.lower().endswith(suffix):
                continue
            p = os.path.join(root, name)
            tag = read(p)
            rows.append({
                "path": os.path.relpath(p, folder),
                "agent": tag.get(MADE) == "1",
                "at": tag.get(AT, ""),
                "run": tag.get(RUN_ID, ""),
                "sources": tag.get(SOURCES, ""),
            })
    rows.sort(key=lambda r: (not r["agent"], r["path"]))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scan", metavar="FOLDER", help="list every deck, agent-made first")
    ap.add_argument("--read", metavar="FILE", help="print one deck's tag")
    args = ap.parse_args(argv)

    if args.read:
        tag = read(args.read)
        if not tag:
            print("no tag -- nothing in this file says the agent wrote it "
                  "(which is not the same as saying a person did)")
            return 1
        for k in sorted(tag):
            print("%-18s %s" % (k, tag[k]))
        return 0

    if args.scan:
        rows = scan(args.scan)
        agent = [r for r in rows if r["agent"]]
        print("%d deck(s); %d carry the agent tag" % (len(rows), len(agent)))
        print()
        for r in rows:
            print("%-6s %-19s %s" % ("AGENT" if r["agent"] else "-", r["at"] or "-", r["path"]))
            if r["sources"]:
                print("       from: %s" % r["sources"])
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
