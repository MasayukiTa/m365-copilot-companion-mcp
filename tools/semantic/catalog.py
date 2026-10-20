# -*- coding: utf-8 -*-
"""Search and describe the table catalogue, and find join paths between two tables.

* ``catalog_search``  -- keyword search across table names, descriptions, column names and
  meanings, value meanings, notes and pitfalls; ranked, with the matching places shown.
* ``catalog_describe`` -- everything known about one table, including joins declared by OTHER
  tables that point at it, pitfalls and performance notes.
* ``catalog_joins``   -- the known joins between two tables and, breadth first, the multi-hop
  paths through intermediate tables.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from ._text import bigrams, content_runs, latin_words, norm
from .store import Store, get_store

# field weights: where a hit counts for most
W_TABLE_NAME = 6.0
W_COLUMN_NAME = 5.0
W_TABLE_DESC = 3.0
W_COLUMN_MEANING = 3.0
W_VALUE_MEANING = 2.5
W_VALUE = 2.0
W_PITFALL = 2.0
W_NOTES = 1.5
W_KEYS = 1.0
W_PERF = 0.5


def _query_tokens(query: str) -> List[Tuple[str, str]]:
    """[(kind, token)] from the query: latin words (stemmed) and kanji / katakana runs."""
    q = norm(query)
    toks: List[Tuple[str, str]] = []
    for w in latin_words(q):
        if len(w.text) >= 2:
            toks.append(("latin", w.text))
    for r in content_runs(q):
        if r.kind in ("han", "kata"):
            toks.append((r.kind, r.text))
    if not toks and q:
        toks.append(("raw", q))
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _field_score(kind: str, tok: str, field_norm: str, field_words: List[str]) -> float:
    """0..1: how well one query token matches one normalised field."""
    if not field_norm:
        return 0.0
    if kind == "latin":
        if tok in field_words:
            return 1.0
        if len(tok) >= 3 and tok in field_norm:
            return 0.6
        return 0.0
    if tok in field_norm:
        return 1.0
    if len(tok) >= 3:
        bg = bigrams(tok)
        hit = sum(1 for g in bg if g in field_norm)
        frac = hit / len(bg)
        if frac >= 0.5:
            return 0.5 * frac
    return 0.0


def _words_of(s: str) -> List[str]:
    return [w.text for w in latin_words(s.replace("_", " "))]    # already stemmed


def _prep(text: str) -> Tuple[str, List[str]]:
    n = norm(text)
    return n, _words_of(n)


def _fields(t: Dict[str, Any]) -> List[Tuple[str, float, str, str]]:
    """(label, weight, column or "", text) for every searchable piece of a table."""
    out: List[Tuple[str, float, str, str]] = [
        ("table name", W_TABLE_NAME, "", t["name"]),
        ("table description", W_TABLE_DESC, "", t.get("description", "")),
        ("performance", W_PERF, "", t.get("performance", "")),
    ]
    for k in t.get("keys", []):
        out.append(("key", W_KEYS, "", k))
    for p in t.get("pitfalls", []):
        out.append(("pitfall", W_PITFALL, "", p))
    for c in t.get("columns", []):
        cn = c["name"]
        out.append(("column name", W_COLUMN_NAME, cn, cn))
        out.append(("column meaning", W_COLUMN_MEANING, cn, c.get("meaning", "")))
        out.append(("column notes", W_NOTES, cn, c.get("notes", "")))
        for v, mean in c.get("values", {}).items():
            out.append(("value", W_VALUE, cn, v))
            out.append(("value meaning", W_VALUE_MEANING, cn, "%s %s" % (v, mean)))
    return out


_STUB_NOTE = ("named by the dictionary but has no catalogue file: columns and joins are unknown "
              "here. Read its columns with odbc_columns before using it; do not conclude that "
              "it does not exist.")


def _stub_table(u: Dict[str, Any]) -> Dict[str, Any]:
    """A searchable stand-in for a table that is only known from references to it."""
    terms = [r.split(": ", 1)[-1] for r in u["referenced_by"]]
    return {
        "name": u["name"], "kind": "table", "_stub": True, "_referenced_by": u["referenced_by"],
        "description": ("Referenced by %s. %s" % ("; ".join(u["referenced_by"]),
                                                   u.get("meaning", ""))).strip(),
        "columns": [{"name": c, "meaning": " ".join(terms)} for c in u.get("columns", [])],
        "status": "draft",
    }


def catalog_search(query: str, limit: int = 10, store: Optional[Store] = None) -> Dict[str, Any]:
    """Rank catalogue tables by how well they match `query`."""
    st = store if store is not None else get_store()
    err = st.error()
    if err:
        return {"ok": False, "configured": st.configured, "error": err, "results": []}
    toks = _query_tokens(query)
    if not toks:
        return {"ok": False, "error": "empty query", "results": []}
    limit = max(1, min(int(limit), 50))

    results = []
    for t in list(st.tables.values()) + [_stub_table(u) for u in st.uncatalogued.values()]:
        prepared = [(lab, w, col, text) + _prep(text) for lab, w, col, text in _fields(t)]
        total = 0.0
        matched_tokens = 0
        hits: List[Dict[str, Any]] = []
        for kind, tok in toks:
            scored = []
            for lab, w, col, text, fn, fw in prepared:
                s = _field_score(kind, tok, fn, fw)
                if s > 0:
                    scored.append((s * w, lab, col, text))
            if not scored:
                continue
            matched_tokens += 1
            scored.sort(key=lambda x: -x[0])
            best = scored[0][0]
            rest = sum(x[0] for x in scored[1:])
            total += best + 0.2 * min(rest, best)
            for sc, lab, col, text in scored[:3]:
                h = {"field": lab, "text": text if len(text) <= 120 else text[:117] + "..."}
                if col:
                    h["column"] = col
                if h not in hits:
                    hits.append(h)
        if matched_tokens == 0:
            continue
        coverage = matched_tokens / len(toks)
        score = total * (0.5 + 0.5 * coverage)
        row = {
            "table": t["name"], "kind": t["kind"], "score": round(score, 3),
            "coverage": round(coverage, 2), "description": t.get("description", ""),
            "status": t.get("status", "draft"), "matched": hits[:6],
            "in_catalogue": not t.get("_stub"),
        }
        if t.get("_stub"):
            row["referenced_by"] = t["_referenced_by"]
            row["note"] = _STUB_NOTE
        results.append(row)
    results.sort(key=lambda r: (-r["score"], r["table"]))
    return {"ok": True, "query": query, "count": len(results), "results": results[:limit],
            "store": st.status()}


# ---------------------------------------------------------------------------- joins graph

def _conjuncts(on: str) -> frozenset:
    """The AND-separated conditions of a join, each normalised so that side order, case and
    spacing do not matter."""
    out = set()
    for part in re.split(r"\bAND\b", on or "", flags=re.IGNORECASE):
        sides = part.split("=")
        if len(sides) == 2:
            out.add(tuple(sorted(norm(s).replace(" ", "") for s in sides)))
        elif part.strip():
            out.add((norm(part).replace(" ", ""),))
    return frozenset(out)


def _declarations(st: Store) -> List[Dict[str, Any]]:
    """Every declared join, with ``demoted`` set when it only ADDS conditions to another
    declaration between the same two tables that is at least as trusted. The extra condition is
    exactly what a wrong two-column key looks like (measured: it returned no rows), so such a
    declaration is not offered as a join; the simpler, trusted one is."""
    decls: List[Dict[str, Any]] = []
    for t in st.tables.values():
        for j in t.get("joins", []):
            decls.append({"declared_in": t["name"], "from": t["name"], "to": j["to"],
                          "on": j.get("on", ""), "note": j.get("note", ""),
                          "confidence": j.get("confidence"), "demoted": False, "reason": "",
                          "_pair": frozenset((norm(t["name"]), norm(j["to"]))),
                          "_conj": _conjuncts(j.get("on", ""))})
    for d in decls:
        for o in decls:
            if o is d or o["_pair"] != d["_pair"] or o["confidence"] is None:
                continue
            if o["_conj"] and o["_conj"] < d["_conj"] and \
                    (d["confidence"] is None or o["confidence"] >= d["confidence"]):
                extra = sorted(" = ".join(c) for c in d["_conj"] - o["_conj"])
                d["demoted"] = True
                d["reason"] = ("adds condition(s) %s to the declaration %r (confidence %s); the "
                               "extra condition is unverified, so the simpler join is offered "
                               "instead" % (", ".join(extra), o["on"], o["confidence"]))
                break
    return decls


def _edges(st: Store) -> Dict[str, List[Dict[str, Any]]]:
    """Undirected adjacency: norm(table) -> [edge]. Each declared join appears both ways.
    Demoted declarations (see _declarations) are left out."""
    adj: Dict[str, List[Dict[str, Any]]] = {}
    for d in _declarations(st):
        if d["demoted"]:
            continue
        a, b = d["from"], d["to"]
        decl = {"declared_in": a, "on": d["on"], "note": d["note"],
                "confidence": d["confidence"]}
        adj.setdefault(norm(a), []).append(dict(decl, **{"from": a, "to": b}))
        adj.setdefault(norm(b), []).append(dict(decl, **{"from": b, "to": a}))
    return adj


def _display(st: Store, name: str) -> str:
    t = st.find_table(name)
    return t["name"] if t else name


def _suggest(st: Store, name: str, n: int = 5) -> List[str]:
    q = norm(name)
    scored = []
    for t in st.tables.values():
        k = norm(t["name"])
        s = 1.0 if q and (q in k or k in q) else 0.0
        if not s and bigrams(q) and bigrams(k):
            s = len(bigrams(q) & bigrams(k)) / len(bigrams(q) | bigrams(k))
        if s > 0.2:
            scored.append((s, t["name"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [x[1] for x in scored[:n]]


def catalog_describe(table: str, store: Optional[Store] = None) -> Dict[str, Any]:
    """Full description of one table or view."""
    st = store if store is not None else get_store()
    err = st.error()
    if err:
        return {"ok": False, "configured": st.configured, "error": err}
    t = st.find_table(table)
    if t is None:
        u = st.uncatalogued.get(norm(table))
        if u is not None:
            return {"ok": False, "in_catalogue": False, "referenced_by": u["referenced_by"],
                    "error": "table %r is referenced (%s) but has no catalogue file; %s"
                             % (u["name"], "; ".join(u["referenced_by"]), _STUB_NOTE)}
        return {"ok": False, "error": "no table %r in the catalogue" % table,
                "did_you_mean": _suggest(st, table)}
    adj = _edges(st)
    dem = {(norm(d["from"]), d["to"], d["on"]): d["reason"]
           for d in _declarations(st) if d["demoted"]}
    own = []
    for j in t.get("joins", []):
        row = dict(j, direction="declared here")
        reason = dem.get((norm(t["name"]), j["to"], j.get("on", "")))
        if reason:
            row["demoted"] = True
            row["reason"] = reason
        own.append(row)
    incoming = []
    for e in adj.get(norm(t["name"]), []):
        if norm(e["declared_in"]) != norm(t["name"]):
            incoming.append({"to": e["to"], "on": e["on"], "note": e["note"],
                             "confidence": e["confidence"],
                             "direction": "declared by %s" % e["declared_in"]})
    return {
        "ok": True, "name": t["name"], "kind": t["kind"],
        "description": t.get("description", ""), "status": t.get("status", "draft"),
        "row_estimate": t.get("row_estimate"), "keys": t.get("keys", []),
        "columns": t.get("columns", []),
        "joins": own + incoming,
        "pitfalls": t.get("pitfalls", []),
        "performance": t.get("performance", ""),
        "source_file": t.get("_file"),
    }


def catalog_joins(a: str, b: str, max_hops: int = 3, limit: int = 8,
                  store: Optional[Store] = None) -> Dict[str, Any]:
    """Known joins between `a` and `b`, plus breadth-first multi-hop paths (<= max_hops)."""
    st = store if store is not None else get_store()
    err = st.error()
    if err:
        return {"ok": False, "configured": st.configured, "error": err}
    adj = _edges(st)
    ka, kb = norm(a), norm(b)
    known = set(adj) | set(st.tables)
    missing = [x for x, k in ((a, ka), (b, kb)) if k not in known]
    if missing:
        return {"ok": False, "error": "not in the catalogue: %s" % ", ".join(map(repr, missing)),
                "did_you_mean": {m: _suggest(st, m) for m in missing}}
    if ka == kb:
        return {"ok": False, "error": "a and b are the same table"}
    max_hops = max(1, min(int(max_hops), 6))
    limit = max(1, min(int(limit), 30))

    direct = [{"from": e["from"], "to": e["to"], "on": e["on"], "note": e["note"],
               "confidence": e["confidence"], "declared_in": e["declared_in"]}
              for e in adj.get(ka, []) if norm(e["to"]) == kb]

    # breadth first over simple paths: shorter paths come out first
    paths: List[List[Dict[str, Any]]] = []
    queue = deque([(ka, [], {ka})])
    while queue and len(paths) < 200:
        node, path, seen = queue.popleft()
        if len(path) >= max_hops:
            continue
        # one edge per neighbour (the most trusted), so parallel declarations do not multiply
        best: Dict[str, Dict[str, Any]] = {}
        for e in adj.get(node, []):
            k = norm(e["to"])
            c = e["confidence"] if e["confidence"] is not None else -1.0
            if k not in best or c > (best[k]["confidence"]
                                     if best[k]["confidence"] is not None else -1.0):
                best[k] = e
        for nxt, e in best.items():
            if nxt in seen:
                continue
            if nxt == kb:
                paths.append(path + [e])
                continue
            queue.append((nxt, path + [e], seen | {nxt}))

    def conf(path):
        known_c = [e["confidence"] for e in path if e["confidence"] is not None]
        p = 1.0
        for c in known_c:
            p *= c
        return (round(p, 4) if known_c else None, len(path) - len(known_c))

    ranked = []
    for p in paths:
        c, unrated = conf(p)
        ranked.append((len(p), -(c if c is not None else 0.0), p, c, unrated))
    ranked.sort(key=lambda x: (x[0], x[1]))
    out_paths = []
    for hops, _, p, c, unrated in ranked[:limit]:
        out_paths.append({
            "hops": hops,
            "tables": [p[0]["from"]] + [e["to"] for e in p],
            "confidence": c, "unrated_hops": unrated,
            "steps": [{"from": e["from"], "to": e["to"], "on": e["on"], "note": e["note"],
                       "confidence": e["confidence"],
                       "to_in_catalogue": norm(e["to"]) in st.tables} for e in p],
        })
    notes = []
    if not direct and not out_paths:
        notes.append("no known join path within %d hop(s); the catalogue may be missing a join, "
                     "so absence here is not proof the tables are unrelated" % max_hops)
    if any(p["unrated_hops"] for p in out_paths):
        notes.append("some hops have no confidence value; treat them as unverified")
    demoted = [{"from": d["from"], "to": d["to"], "on": d["on"], "note": d["note"],
                "confidence": d["confidence"], "declared_in": d["declared_in"],
                "reason": d["reason"]}
               for d in _declarations(st) if d["demoted"] and d["_pair"] == frozenset((ka, kb))]
    if demoted:
        notes.append("%d declared join(s) not offered: they only add unverified conditions to a "
                     "trusted join (see 'demoted')" % len(demoted))
    return {"ok": True, "a": _display(st, a), "b": _display(st, b), "direct": direct,
            "demoted": demoted, "paths": out_paths, "max_hops": max_hops, "notes": notes}
