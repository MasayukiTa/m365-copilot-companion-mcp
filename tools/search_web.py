from typing import Optional


def web_search(
    query: str,
    max_results: int = 10,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    timelimit: Optional[str] = None,
) -> str:
    """Search the web via DuckDuckGo (no API key required).

    Returns a compact list of result snippets with titles, URLs, and short
    descriptions. Pair with web_fetch to read a specific result in full.

    Args:
        query: Search query.
        max_results: Maximum number of results to return (1..30).
        region: Region code, e.g. "jp-jp" for Japan or "wt-wt" for worldwide.
        safesearch: "off", "moderate", or "strict".
        timelimit: Optional time filter: "d" (day), "w" (week), "m" (month), "y" (year).
    """
    try:
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore
            except ImportError:
                return "[web_search error: install `ddgs` or `duckduckgo-search`]"

        n = max(1, min(int(max_results), 30))
        with DDGS() as ddgs:
            results = list(
                ddgs.text(
                    query,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                    max_results=n,
                )
            )
        if not results:
            return f"(no results for {query!r})"
        lines = [f"# {len(results)} result(s) for: {query}"]
        for i, r in enumerate(results, 1):
            title = r.get("title") or r.get("heading") or "(no title)"
            url = r.get("href") or r.get("url") or ""
            body = (r.get("body") or r.get("snippet") or "").strip().replace("\n", " ")
            if len(body) > 280:
                body = body[:280] + "…"
            lines.append(f"\n[{i}] {title}\n    {url}\n    {body}")
        return "\n".join(lines)
    except Exception as e:
        return f"[web_search error: {type(e).__name__}: {e}]"


def web_search_news(
    query: str,
    max_results: int = 10,
    region: str = "jp-jp",
    timelimit: str = "w",
) -> str:
    """Search recent news via DuckDuckGo News.

    Args:
        query: Search query.
        max_results: 1..30.
        region: Region code, defaults to Japan.
        timelimit: "d" / "w" / "m". Defaults to past week.
    """
    try:
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore

        n = max(1, min(int(max_results), 30))
        with DDGS() as ddgs:
            results = list(
                ddgs.news(query, region=region, timelimit=timelimit, max_results=n)
            )
        if not results:
            return f"(no news for {query!r})"
        lines = [f"# {len(results)} news item(s) for: {query}"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "(no title)")
            url = r.get("url", "")
            source = r.get("source", "")
            date = r.get("date", "")
            body = (r.get("body") or "").strip().replace("\n", " ")
            if len(body) > 240:
                body = body[:240] + "…"
            lines.append(f"\n[{i}] {title}\n    {source}  {date}\n    {url}\n    {body}")
        return "\n".join(lines)
    except Exception as e:
        return f"[web_search_news error: {type(e).__name__}: {e}]"
