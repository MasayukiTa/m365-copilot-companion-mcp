import re
from typing import Optional
from urllib.parse import urlparse

import httpx

USER_AGENT = "m365-copilot-companion-mcp/0.1 (+local agent)"
MAX_CHARS = 60_000


def web_fetch(
    url: str,
    timeout: int = 20,
    max_chars: int = MAX_CHARS,
    raw: bool = False,
) -> str:
    """Fetch a URL and return readable text. HTML is stripped to plain text by default.

    Args:
        url: HTTP or HTTPS URL to fetch.
        timeout: Request timeout in seconds.
        max_chars: Truncate the response body to this many characters.
        raw: When true, return the raw response body (no HTML stripping).
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return "[web_fetch error: only http/https URLs are allowed]"
        if not parsed.netloc:
            return "[web_fetch error: invalid URL]"

        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
        ) as client:
            r = client.get(url)

        content_type = r.headers.get("content-type", "").lower()
        header_lines = [
            f"URL: {r.url}",
            f"Status: {r.status_code}",
            f"Content-Type: {content_type or '(none)'}",
            f"Bytes: {len(r.content):,}",
            "---",
        ]

        body = r.text
        if not raw and ("html" in content_type or body.lstrip().startswith("<")):
            body = _html_to_text(body)
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n... truncated at {max_chars:,} characters"
        return "\n".join(header_lines) + "\n" + body
    except httpx.HTTPError as e:
        return f"[web_fetch HTTP error: {type(e).__name__}: {e}]"
    except Exception as e:
        return f"[web_fetch error: {type(e).__name__}: {e}]"


def _html_to_text(html: str) -> str:
    # Strip script/style, then tags, collapse whitespace. Lightweight by design.
    no_script = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    no_tags = re.sub(r"<[^>]+>", " ", no_script)
    unescaped = (
        no_tags.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    collapsed = re.sub(r"[ \t]+", " ", unescaped)
    collapsed = re.sub(r"\n\s*\n\s*\n+", "\n\n", collapsed)
    return collapsed.strip()


def github_file(
    repo: str,
    file_path: str,
    ref: str = "main",
    max_chars: int = MAX_CHARS,
) -> str:
    """Fetch a file from a public GitHub repository (raw content).

    Args:
        repo: owner/repo, for example "anthropics/claude-code".
        file_path: Path within the repository.
        ref: Branch, tag, or commit. Defaults to main.
        max_chars: Truncate to this many characters.
    """
    safe_repo = repo.strip("/")
    safe_path = file_path.lstrip("/")
    url = f"https://raw.githubusercontent.com/{safe_repo}/{ref}/{safe_path}"
    return web_fetch(url, raw=True, max_chars=max_chars)
