"""Regenerate copilot_message_dom.json from a LIVE M365 Copilot agent conversation.

Run this against a fleet/bridge Edge that already has an agent chat open:

    python relay/testdata/capture_copilot_dom.py 9222

It attaches over CDP read-only (evaluate only -- no clicks, no navigation) and records,
for every `.fai-CopilotMessage` on the page, the block's outerHTML plus the innerText of
the block and of each named sub-element. Nothing here is hand-written: the fixture is
whatever the tenant actually rendered, which is the point -- test_copilot_dom_reader.py
asserts against a capture, so a DOM change on Microsoft's side breaks the test with the
real markup in hand instead of silently drifting from a fixture we invented.
"""
from __future__ import annotations

import json
import os
import sys

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "copilot_message_dom.json")

_JS = r"""
() => Array.from(document.querySelectorAll('.fai-CopilotMessage')).map((b, i) => {
  const q = (s) => { const e = b.querySelector(s); return e ? e.innerText : null; };
  const img = b.querySelector('.fai-CopilotMessage__avatar img');
  return {
    i,
    outerHTML: b.outerHTML,
    block_innerText:   b.innerText,
    content_innerText: q('.fai-CopilotMessage__content'),
    name_innerText:    q('.fai-CopilotMessage__name'),
    heading_innerText: q('.fai-CopilotMessage__accessibleHeading'),
    avatar_img_alt:    img ? img.getAttribute('alt') : null,
  };
})
"""


def capture(port: int):
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        for ctx in browser.contexts:
            for page in ctx.pages:
                if "m365.cloud.microsoft" not in (page.url or ""):
                    continue
                try:
                    out.extend(page.evaluate(_JS))
                except Exception as exc:      # a page mid-navigation, etc.
                    print("skip %s: %r" % (page.url[:60], exc))
    return out


def main(argv):
    port = int(argv[1]) if len(argv) > 1 else 9222
    blocks = capture(port)
    if not blocks:
        print("no .fai-CopilotMessage found on port %d -- open an agent chat first" % port)
        return 1
    print("captured %d block(s); fixture NOT overwritten automatically." % len(blocks))
    print("Review, then write the ones you want into %s" % FIXTURE)
    for b in blocks:
        print("--- block %d: %r" % (b["i"], (b["block_innerText"] or "")[:120]))
    json.dump(blocks, open(FIXTURE + ".new", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("raw capture -> %s.new" % FIXTURE)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
