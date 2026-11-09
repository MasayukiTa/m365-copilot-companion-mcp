# -*- coding: utf-8 -*-
"""copilot_generate_image: ask M365 Copilot to generate an image and save it as a PNG file.

WHY THIS EXISTS. The connector-less bridge scrapes Copilot's answer as innerText only
(bridge/copilot_bridge.py, _CLEAN_ANSWER_JS -> clone.innerText), and copilot_chat returns
that text. A generated image is rendered in the DOM as an <img> whose src is a
`data:image/png;base64,...` (or blob:) URL, which carries NO text, so the image never
reaches the model: reply/adaptiveCards/attributions all come back empty. Measured
2026-11-04: a generation request succeeded in the UI (a 452x301 <img>, 2.96 MB PNG) while
the scraped answer held nothing about it.

WHAT THIS DOES. Drives the Edge instance already running with --remote-debugging-port=9222
over CDP (the same browser the bridge uses), opens a fresh Copilot chat page so it does not
disturb any in-progress tab, sends the prompt, waits for a large image to appear in an
assistant turn, fetches its bytes inside the page (the data:/blob: URL is same-origin and
already authenticated there), and writes them to out_path as a PNG.

NO image is drawn by this code. It only retrieves what Copilot generated.
"""
import base64
import os
import time

#: The CDP endpoint of the Edge the bridge drives. Fixed by setup (msedge --remote-debugging-port=9222).
CDP_URL = "http://127.0.0.1:9222"
#: Plain Copilot chat (image generation is enabled here; a custom agent may not have it).
CHAT_URL = "https://m365.cloud.microsoft/chat"
#: A generated image is large; icons/avatars are ~20px. This threshold tells them apart.
_MIN_SIDE = 200
#: How long to wait for the image after sending the prompt.
_DEFAULT_TIMEOUT_S = 180


_IMG_SCAN_JS = """() => {
    const out = [];
    document.querySelectorAll('img').forEach(im => {
        const s = im.currentSrc || im.src || '';
        if (!s) return;
        if (s.startsWith('blob:') || s.startsWith('data:image') || /^https?:/.test(s)) {
            const r = im.getBoundingClientRect();
            out.push({src: s, w: Math.round(r.width), h: Math.round(r.height), alt: im.alt || ''});
        }
    });
    return out;
}"""

_FETCH_JS = """async (needle) => {
    const im = [...document.querySelectorAll('img')].find(x => (x.currentSrc || x.src || '').startsWith(needle));
    if (!im) return {ok: false, err: 'img gone'};
    const s = im.currentSrc || im.src;
    try {
        const r = await fetch(s);
        const b = await r.blob();
        const buf = await b.arrayBuffer();
        const a = new Uint8Array(buf);
        let bin = '';
        const CH = 0x8000;
        for (let i = 0; i < a.length; i += CH) {
            bin += String.fromCharCode.apply(null, a.subarray(i, i + CH));
        }
        return {ok: true, mime: b.type, b64: btoa(bin)};
    } catch (e) {
        return {ok: false, err: String(e)};
    }
}"""


def _find_composer(page):
    for sel in ('div[contenteditable="true"]', 'textarea', '[role="textbox"]'):
        try:
            el = page.query_selector(sel)
            if el:
                return el
        except Exception:
            pass
    return None


def _save_png(b64, out_path):
    raw = base64.b64decode(b64)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(raw)
    return len(raw)


def copilot_generate_image(prompt: str, out_path: str, timeout_s: int = _DEFAULT_TIMEOUT_S,
                           cdp_url: str = CDP_URL, chat_url: str = CHAT_URL) -> str:
    """Generate an image with M365 Copilot and save it as a PNG file.

    Drives the already-running Edge (CDP :9222) the bridge uses, opens a fresh Copilot chat,
    sends `prompt`, waits up to `timeout_s` for the generated image, fetches its bytes and
    writes them to `out_path`. Returns a one-line status string (saved path + byte count, or
    the reason it failed). Does NOT draw anything itself -- it only retrieves Copilot's image.

    Args:
        prompt: The image description to send to Copilot.
        out_path: Absolute path to write the PNG to (parent dirs are created).
        timeout_s: Seconds to wait for the image to appear (default 180).
        cdp_url: CDP endpoint of the running Edge (default http://127.0.0.1:9222).
        chat_url: Copilot chat URL to open (default the plain chat).
    """
    if not prompt or not str(prompt).strip():
        return "[copilot_generate_image] prompt is empty"
    if not out_path:
        return "[copilot_generate_image] out_path is empty"
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return "[copilot_generate_image] playwright not available: %s" % exc

    page = None
    _opened = False
    try:
        with sync_playwright() as pw:
            try:
                # Edge on :9222 is shared: other sessions may have dozens of tabs open,
                # which slows CDP target enumeration. Give the connect a generous timeout
                # (default is 180s) so a busy browser does not spuriously fail the connect.
                browser = pw.chromium.connect_over_cdp(cdp_url, timeout=300000)
            except Exception as exc:
                return ("[copilot_generate_image] could not reach Edge over CDP at %s "
                        "(is Edge running with --remote-debugging-port=9222?): %s" % (cdp_url, exc))
            if not browser.contexts:
                return "[copilot_generate_image] no browser context on the CDP endpoint"
            ctx = browser.contexts[0]
            # Reuse an already-open, authenticated Copilot chat tab if present; opening a
            # fresh tab and navigating can time out when the surface is slow to load.
            page = None
            for p in ctx.pages:
                try:
                    if "m365.cloud.microsoft" in (p.url or "") and "/chat" in (p.url or ""):
                        page = p
                        break
                except Exception:
                    pass
            _opened = False
            if page is None:
                page = ctx.new_page()
                _opened = True
                last_exc = None
                for attempt in range(3):
                    try:
                        page.goto(chat_url, wait_until="commit", timeout=60000)
                        last_exc = None
                        break
                    except Exception as exc:
                        last_exc = exc
                        time.sleep(3)
                if last_exc is not None:
                    return "[copilot_generate_image] could not open chat page %s: %s" % (
                        chat_url, str(last_exc)[:200])
            else:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            # Let the chat surface and composer mount.
            time.sleep(8)
            box = _find_composer(page)
            if not box:
                return "[copilot_generate_image] composer textbox not found on %s" % page.url
            # When reusing an existing tab, images from earlier turns are already in the DOM.
            # Record their srcs now so we only accept a NEW large image that appears after we
            # send this prompt.
            try:
                pre = page.evaluate(_IMG_SCAN_JS)
            except Exception:
                pre = []
            seen = set(i["src"] for i in pre if i["w"] >= _MIN_SIDE and i["h"] >= _MIN_SIDE)
            box.click()
            page.keyboard.type(str(prompt), delay=4)
            page.keyboard.press("Enter")

            deadline = time.time() + max(10, int(timeout_s))
            target = None
            while time.time() < deadline:
                time.sleep(4)
                try:
                    imgs = page.evaluate(_IMG_SCAN_JS)
                except Exception:
                    imgs = []
                big = [i for i in imgs if i["w"] >= _MIN_SIDE and i["h"] >= _MIN_SIDE
                       and i["src"] not in seen]
                if big:
                    target = big[0]
                    break
            if not target:
                return "[copilot_generate_image] no image appeared within %ds" % int(timeout_s)

            needle = target["src"][:40]
            got = page.evaluate(_FETCH_JS, needle)
            if not got or not got.get("ok"):
                return "[copilot_generate_image] could not fetch image bytes: %s" % (
                    (got or {}).get("err", "unknown"))
            n = _save_png(got["b64"], out_path)
            return "[copilot_generate_image] saved %s (%d bytes, %s)" % (
                out_path, n, got.get("mime", "image/png"))
    except Exception as exc:
        return "[copilot_generate_image] %s: %s" % (type(exc).__name__, str(exc)[:300])
    finally:
        try:
            if page is not None and _opened:
                page.close()
        except Exception:
            pass
