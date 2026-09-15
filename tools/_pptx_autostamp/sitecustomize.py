# -*- coding: utf-8 -*-
"""Mark every deck a worker's own code writes, without the code having to know.

WHY IT IS HERE AND NOT IN `tools/pptx_ops`. The fleet has write tools for decks -- create_pptx,
pptx_add_slide, pptx_add_image -- and stamping inside them would have been the obvious place.
Measured over the four OGF runs of 2026-09-14/15: not one of them used those tools. Every deck
was written by python-pptx code the worker composed itself and handed to `run_python`. A stamp
in the tool surface would have covered none of the output it exists to describe.

WHAT IS ACTUALLY A CHOKE POINT. `tools/code_exec.run_python` writes the worker's code to a temp
file and runs it in a CHILD interpreter whose environment `sanitized_child_env` builds. Python
imports `sitecustomize` automatically at interpreter start if it is on the path, so putting this
directory on the child's PYTHONPATH reaches every script that runs there -- including code
nobody wrote with stamping in mind, which is all of it.

WHAT IT PATCHES. `pptx.Presentation.save`, and `pptx.Presentation()` so a deck opened for
reading is recorded as a source of whatever is saved afterwards. Both are wrapped, never
replaced: the original is always called, its return value is always returned, and any failure
inside the stamp is swallowed. A tag is worth nothing if adding it can cost the deck.

IT DOES NOT CLAIM TO BE A BOUNDARY. A worker that writes a .pptx by hand with `zipfile`, or
shells out, or runs a different interpreter, produces an unstamped deck. `made_by_agent` is
therefore a positive signal only -- "this says the agent wrote it" -- and its absence says
nothing, which is written into that function too.
"""
import os
import sys


def _install():
    try:
        import pptx
    except Exception:
        return                      # no python-pptx in this child: nothing to wrap

    if getattr(pptx, "_companion_stamped", False):
        return
    pptx._companion_stamped = True

    repo = os.environ.get("MCP_COMPANION_REPO") or ""
    if repo and repo not in sys.path:
        sys.path.insert(0, repo)
    try:
        from tools import pptx_provenance as prov
    except Exception:
        return

    opened = []                     # decks this interpreter read, in order

    real_open = pptx.Presentation

    def _presentation(pptx_file=None, *a, **k):
        prs = real_open(pptx_file, *a, **k)
        try:
            if isinstance(pptx_file, str) and pptx_file and os.path.isfile(pptx_file):
                if pptx_file not in opened:
                    opened.append(pptx_file)
        except Exception:
            pass
        return prs

    pptx.Presentation = _presentation

    try:
        from pptx.presentation import Presentation as _Cls
    except Exception:
        return
    real_save = _Cls.save

    def _save(self, file, *a, **k):
        result = real_save(self, file, *a, **k)
        try:
            if isinstance(file, str) and file.lower().endswith(".pptx") \
                    and os.path.isfile(file):
                # A deck is not its own source, however it got opened.
                srcs = [p for p in opened if os.path.abspath(p) != os.path.abspath(file)]
                prov.stamp(file, sources=srcs,
                           run_id=os.environ.get("MCP_FLEET_RUN_ID", ""))
        except Exception:
            pass                    # never let the tag cost the save
        return result

    _Cls.save = _save


_install()
