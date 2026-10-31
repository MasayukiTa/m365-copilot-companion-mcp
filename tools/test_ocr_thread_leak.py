"""Thread-leak regression for the OCR path (2026-10-31).

Root cause: EasyOCR runs on PyTorch, whose CPU backend creates a native OpenMP/MKL
intra-op thread pool the first time a torch op runs ON A GIVEN OS THREAD, and that pool
is tied to that thread and is not released when the thread returns. The MCP server
dispatches every tool on a fresh anyio worker thread, so running OCR on the calling
thread leaked ~9-11 native (py-spy-invisible, Wait-state) threads per distinct worker
thread -- the ~500 leaked OS threads measured on the live server after 22h.

The fix funnels every EasyOCR/torch call through one dedicated worker thread
(ocr_ops._ocr_executor), so the native pool is built once and reused regardless of
which anyio worker thread called in.

These tests do not require easyocr/torch to be installed: they assert the structural
property that makes the leak impossible -- that _easyocr_image_text executes its work
on the single dedicated executor thread, never on the calling thread.
"""
import threading

from tools import ocr_ops


class _FakeImage:
    def convert(self, mode):
        return self


def test_easyocr_work_runs_on_the_dedicated_thread_not_the_caller(monkeypatch):
    """The Reader build + readtext must run on ocr_ops' single OCR worker thread,
    not on whatever (anyio worker) thread called _easyocr_image_text. Running on the
    caller is exactly what leaked a fresh torch native pool per worker thread."""
    # Make numpy import inside the module succeed without the real dependency.
    import sys, types
    fake_np = types.ModuleType("numpy")
    fake_np.array = lambda x: x
    monkeypatch.setitem(sys.modules, "numpy", fake_np)

    seen = {}

    class _Reader:
        def readtext(self, arr, detail=0):
            seen["work_thread"] = threading.current_thread().name
            return ["hello"]

    # Pre-seed the reader cache so no real easyocr import is needed.
    ocr_ops._EASYOCR_READERS["en"] = _Reader()

    caller_thread = threading.current_thread().name
    out = ocr_ops._easyocr_image_text(_FakeImage(), "eng")

    assert out == "hello"
    assert "work_thread" in seen, "readtext never ran"
    # The work must NOT have run on the calling thread.
    assert seen["work_thread"] != caller_thread
    # And it must have run on the dedicated OCR worker.
    assert seen["work_thread"].startswith("ocr-easyocr")


def test_all_ocr_calls_share_one_executor_thread(monkeypatch):
    """Calls arriving on many different threads must all land on the SAME single OCR
    worker thread -- that is what keeps the torch native pool to one copy."""
    import sys, types
    fake_np = types.ModuleType("numpy")
    fake_np.array = lambda x: x
    monkeypatch.setitem(sys.modules, "numpy", fake_np)

    work_threads = set()

    class _Reader:
        def readtext(self, arr, detail=0):
            work_threads.add(threading.current_thread().name)
            return ["x"]

    ocr_ops._EASYOCR_READERS["en"] = _Reader()

    def call():
        ocr_ops._easyocr_image_text(_FakeImage(), "eng")

    threads = [threading.Thread(target=call) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(work_threads) == 1, f"OCR ran on multiple worker threads: {work_threads}"
    assert next(iter(work_threads)).startswith("ocr-easyocr")


def test_executor_is_single_worker_and_lazy():
    """The executor is created lazily and has exactly one worker (so one torch pool)."""
    ex = ocr_ops._ocr_executor()
    assert ex is ocr_ops._ocr_executor(), "executor must be a singleton"
    assert ex._max_workers == 1
