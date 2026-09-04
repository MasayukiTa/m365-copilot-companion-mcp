import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from ._untrusted import wrap_untrusted

USER_AGENT = "m365-copilot-companion-mcp/0.1 (+local agent)"
MAX_CHARS = 60_000


#: Japanese pages in the wild declare these, and every one of them should be read as cp932.
#: SHIFT_JIS PROPER IS THE WRONG CHOICE: real pages carry vendor extensions (circled digits,
#: full-width Roman numerals, "㈱") that shift_jis rejects and cp932 accepts, so decoding as the
#: name they declare fails on exactly the characters a Japanese cinema page is most likely to use.
_ENCODING_ALIASES = {
    "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "s-jis": "cp932",
    "x-sjis": "cp932", "ms_kanji": "cp932", "windows-31j": "cp932", "cp932": "cp932",
    "euc-jp": "euc_jp", "euc_jp": "euc_jp", "x-euc-jp": "euc_jp",
    "iso-2022-jp": "iso2022_jp",
}

#: A corporate web filter answers with a PAGE, not a network error, so a blocked site is
#: indistinguishable from a broken one unless something looks. Measured 2026-09-04: x.com and
#: every no-auth mirror of it answer 503 with this page, category "social-networking". Workers
#: surveying cinemas read that as an outage, retried, and recorded "no information" -- which is
#: why whole regions came back empty. "I was not allowed to look" is a different fact from
#: "I looked and found nothing", and only one of them is worth retrying.
_POLICY_BLOCK_MARKERS = (
    "web page blocked",
    "blocked in accordance with company policy",
    "access to the web page you were trying to visit has been blocked",
)


def _normalise_encoding(name):
    """Map a declared charset onto the codec that actually reads it."""
    key = (name or "").strip().strip('"\'').lower()
    return _ENCODING_ALIASES.get(key, key or None)


def _charset_from_meta(content: bytes) -> Optional[str]:
    """The charset a page declares in its own HTML, which is where Japanese sites put it.

    Sniffed from the raw BYTES, because deciding this from a string means having already
    guessed. Only the head is scanned: a charset declaration is required to appear early, and
    reading further would start matching text that merely talks about encodings.
    """
    head = content[:4096]
    for pattern in (rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_\-]+)""",
                    rb"""charset\s*=\s*["']?([A-Za-z0-9_\-]+)"""):
        m = re.search(pattern, head, re.I)
        if m:
            try:
                return m.group(1).decode("ascii", "ignore")
            except Exception:
                return None
    return None


def _decode_body(content: bytes, content_type: str, override=None):
    """(text, encoding_used, how_it_was_chosen). Never raises; never returns mojibake silently.

    ORDER MATTERS. An explicit argument beats the server, the server's header beats the page's
    own declaration, and the page beats a guess -- each step is more likely to be right than the
    next, and the one that was missing entirely is the page's own declaration. TOHO's sites are
    Shift_JIS and say so in a meta tag while sending no charset in the header, so the previous
    code decoded them as UTF-8 and handed workers mojibake. One worker out of nine worked around
    it by hand; the rest recorded "could not determine".
    """
    declared = None
    if "charset=" in (content_type or ""):
        declared = content_type.split("charset=", 1)[1].split(";")[0]

    for candidate, how in ((override, "argument"),
                           (declared, "HTTP header"),
                           (_charset_from_meta(content), "meta tag")):
        codec = _normalise_encoding(candidate)
        if not codec:
            continue
        try:
            return content.decode(codec), codec, how
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        return content.decode("utf-8"), "utf-8", "default"
    except UnicodeDecodeError:
        # LAST RESORT IS LABELLED. Replacement characters are a real answer here -- the bytes
        # are not valid in anything tried -- but the caller has to be told, or the U+FFFD it
        # reads will look like the site's own content.
        return content.decode("utf-8", "replace"), "utf-8", "default, with replacements"


def _blocked_by_policy(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _POLICY_BLOCK_MARKERS)


def web_fetch(
    url: str,
    timeout: int = 20,
    max_chars: int = MAX_CHARS,
    raw: bool = False,
    encoding: Optional[str] = None,
) -> str:
    """Fetch a URL and return readable text. HTML is stripped to plain text by default.

    Args:
        url: HTTP or HTTPS URL to fetch.
        timeout: Request timeout in seconds.
        max_chars: Truncate the response body to this many characters.
        raw: When true, return the raw response body (no HTML stripping).
        encoding: Force a character encoding (e.g. "cp932", "euc-jp") when a site declares the
            wrong one. Normally unnecessary -- the page's own declaration is honoured -- but it
            is here so a page that lies can still be read without reimplementing the fetch.
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
        body, used, how = _decode_body(r.content, content_type, encoding)
        header_lines = [
            f"URL: {r.url}",
            f"Status: {r.status_code}",
            f"Content-Type: {content_type or '(none)'}",
            f"Encoding: {used} (from {how})",
            f"Bytes: {len(r.content):,}",
        ]
        if _blocked_by_policy(body):
            # SAID FIRST AND SAID PLAINLY. This is not a page that failed to load; it is a page
            # the network refused to fetch, and the difference decides whether retrying is worth
            # anything. Left to infer it from a 503 and some HTML, callers retried and then
            # recorded the subject as having no information.
            header_lines.append(
                "BLOCKED: a filter returned a policy page instead of this site. Nothing here "
                "came from the requested URL. Do not retry -- access needs approval, not "
                "another attempt, and 'blocked' is not 'no information'."
            )
        header_lines.append("---")

        if not raw and ("html" in content_type or body.lstrip().startswith("<")):
            body = _html_to_text(body)
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n... truncated at {max_chars:,} characters"
        wrapped_body = wrap_untrusted(body, source="web_fetch", origin=str(r.url))
        return "\n".join(header_lines) + "\n" + wrapped_body
    except httpx.HTTPError as e:
        return f"[web_fetch HTTP error: {type(e).__name__}: {e}]"
    except Exception as e:
        return f"[web_fetch error: {type(e).__name__}: {e}]"


def render_page(
    url: str,
    timeout: int = 30,
    max_chars: int = MAX_CHARS,
    wait_for: Optional[str] = None,
    raw: bool = False,
) -> str:
    """Fetch a page AFTER its JavaScript has run, for sites that render themselves in the browser.

    Use this only when `web_fetch` comes back empty or as a bare skeleton. It costs a browser
    launch (a second or two) where web_fetch costs one request, so it is the second thing to
    try, not the first.

    MEASURED 2026-09-04, on a 290-cinema survey: Corona Cinema World, parts of Aeon Cinema, and
    Cinema Sunshine's newer portal are single-page apps whose markup contains none of their
    content. web_fetch returned skeletons and workers recorded the cinemas as undetermined. One
    worker got its group finished only by noticing that the site was Nuxt and calling its
    payload.js directly -- a rescue that depended on recognising one framework, which is luck
    rather than a capability.

    A CLEAN BROWSER, NEVER THE BRIDGE'S. The bridge drives a signed-in corporate Edge through a
    single page-owner thread. Borrowing it would put arbitrary sites inside an authenticated
    session and contend with the thread that serves real turns. This launches its own browser
    with no profile, so nothing it visits sees a logged-in anybody.

    It reaches exactly what the network reaches: it is the same machine on the same network, so
    a site blocked by a filter returns the filter's page here too, and says so.

    Args:
        url: HTTP or HTTPS URL to fetch.
        timeout: Seconds to allow for load and rendering.
        max_chars: Truncate the extracted text to this many characters.
        wait_for: Optional CSS selector to wait for before reading, when the interesting part
            of the page arrives after first paint.
        raw: When true, return the rendered HTML instead of extracted text.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "[render_page error: only http/https URLs are allowed]"
    if not parsed.netloc:
        return "[render_page error: invalid URL]"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "[render_page error: playwright is not installed]"

    ms = max(1000, int(timeout * 1000))
    browser = None
    try:
        with sync_playwright() as p:
            # channel="msedge" uses the Edge already on this machine. The bundled Chromium is
            # not downloaded here, and requiring `playwright install` would make this tool work
            # on the author's machine and nowhere else.
            browser = p.chromium.launch(headless=True, channel="msedge")
            context = browser.new_context(user_agent=USER_AGENT, locale="ja-JP")
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=ms)
            status = response.status if response is not None else 0
            try:
                # Settling, not just loaded: an SPA's first paint is usually empty.
                page.wait_for_load_state("networkidle", timeout=ms)
            except Exception:
                pass                        # a page that never goes idle is still worth reading
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=ms)
                except Exception:
                    pass                    # report what rendered rather than nothing at all
            html = page.content()
            final_url = page.url
            context.close()
    except Exception as e:
        return f"[render_page error: {type(e).__name__}: {str(e).splitlines()[0][:200]}]"
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass

    body = html if raw else _html_to_text(html)
    header_lines = [
        f"URL: {final_url}",
        f"Status: {status}",
        "Rendered: yes (JavaScript executed)",
        f"Bytes: {len(html):,}",
    ]
    if _blocked_by_policy(body):
        header_lines.append(
            "BLOCKED: a filter returned a policy page instead of this site. Rendering does not "
            "change what the network allows -- this is the same block web_fetch reports. Do not "
            "retry; 'blocked' is not 'no information'."
        )
    header_lines.append("---")
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n... truncated at {max_chars:,} characters"
    return "\n".join(header_lines) + "\n" + wrap_untrusted(
        body, source="render_page", origin=str(final_url))


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
