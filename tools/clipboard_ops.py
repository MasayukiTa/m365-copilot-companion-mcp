from .security import require_unlocked
from ._untrusted import wrap_if_content


def clipboard_get() -> str:
    """Return the current text content of the system clipboard.

    Useful for "I just copied something — analyse this" workflows.
    """
    try:
        try:
            import pyperclip
        except ImportError:
            return "[clipboard_get error: pyperclip not installed]"
        try:
            value = pyperclip.paste()
        except pyperclip.PyperclipException as e:
            return f"[clipboard_get error: {e}]"
        if value is None:
            return "(clipboard is empty or non-text)"
        return wrap_if_content(value, source="clipboard", origin="system clipboard")
    except Exception as e:
        return f"[clipboard_get error: {type(e).__name__}: {e}]"


def clipboard_set(text: str) -> str:
    """Write text to the system clipboard.

    Use this to hand a long result back to the user without forcing them to
    select-all in a chat window.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        try:
            import pyperclip
        except ImportError:
            return "[clipboard_set error: pyperclip not installed]"
        pyperclip.copy(text)
        return f"copied {len(text):,} characters to clipboard"
    except Exception as e:
        return f"[clipboard_set error: {type(e).__name__}: {e}]"
