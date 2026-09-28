from pathlib import Path

RULE = "# lgtm[py/clear-text-storage-sensitive-data]"


def _lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines()


def test_env_writer_documents_the_dpapi_false_positive_at_the_exact_sink():
    lines = _lines("scripts/env_file.py")
    i = next(i for i, line in enumerate(lines) if "fh.write(data)" in line)
    assert lines[i - 1].strip() == RULE
    assert "_prepare_env_text_for_persistence(path, text)" in "\n".join(lines[max(0, i-20):i])


def test_rotate_writer_documents_the_dpapi_false_positive_at_the_exact_sink():
    lines = _lines("scripts/rotate_secrets.py")
    i = next(i for i, line in enumerate(lines) if "f.write(text)" in line)
    assert lines[i - 1].strip() == RULE
    block = "\n".join(lines[max(0, i-20):i])
    assert "refusing to persist legacy plaintext auth secret" in block
