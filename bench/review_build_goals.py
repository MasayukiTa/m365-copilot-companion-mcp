"""Build fleet goals (one JSON line per goal) that ask the M365 Copilot agent to review or
security-audit a group of repo files, and to emit a machine-parseable FINDINGS block before
its DONE marker.

Mirrors bench/swe_build_goals.py's goals-file writer: one JSON object per line, fields
{"text": str, "cwd": str} (no "checks" key -- reviews have no automated acceptance gate,
the verdict comes from the refuter + the FINDINGS block in the worker's own final text).
See relay/fleet_runner.py:_read_goals for the reader contract this must match.

This module is PURE aside from the `git` subprocess calls in enumerate_files (the only
required impurity -- tests drive them against a real tmp `git init` fixture, no network).

  python -m bench.review_build_goals --repo-root <path> --mode all --kind review --out out.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess

# --- extensions we never ask the agent to open (binary/asset noise, not reviewable text) ---
BINARY_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".tiff", ".tif",
    ".pdf", ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
    ".xlsx", ".xls", ".xlsm", ".docx", ".doc", ".pptx", ".ppt",
    ".parquet", ".db", ".sqlite", ".sqlite3",
    ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyo", ".class", ".o", ".a",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".flac",
    ".bin", ".dat", ".pkl", ".pickle", ".npy", ".npz", ".h5",
    ".jar", ".whl", ".pdb", ".lock",
})


def enumerate_files(mode, repo_root, base_ref=None, cached=False, max_bytes=200_000,
                     exclude_ext=BINARY_EXTS):
    """List candidate review files from git, filtered to small, non-binary, existing files.

    mode == "all"  -> `git -C repo_root ls-files`
    mode == "diff" -> `git -C repo_root diff --name-only [--cached] [base_ref]`

    Drops: empty lines, excluded extensions (case-insensitive), files missing on disk
    (e.g. a diff entry for a file that was deleted), and files whose on-disk size exceeds
    max_bytes. Tolerates git failure (non-zero exit, missing repo, git not on PATH) by
    returning [] rather than raising. Returns a sorted, de-duplicated list of paths as
    reported by git (repo-root-relative, forward-slashed)."""
    if mode == "all":
        cmd = ["git", "-C", repo_root, "ls-files"]
    elif mode == "diff":
        cmd = ["git", "-C", repo_root, "diff", "--name-only"]
        if cached:
            cmd.append("--cached")
        if base_ref:
            cmd.append(base_ref)
    else:
        raise ValueError("mode must be 'all' or 'diff', got %r" % (mode,))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    out = set()
    for line in (proc.stdout or "").splitlines():
        f = line.strip()
        if not f:
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in exclude_ext:
            continue
        full = os.path.join(repo_root, f)
        try:
            if not os.path.isfile(full):
                continue  # deleted/missing (e.g. a diff entry for a removed file)
            if os.path.getsize(full) > max_bytes:
                continue
        except OSError:
            continue
        out.add(f)
    return sorted(out)


def group_files(files, group_size):
    """Chunk `files` into a list of lists of at most `group_size`, preserving order.
    Never drops or duplicates entries; the last group may be shorter. group_size < 1
    is treated as 1."""
    n = max(1, int(group_size))
    return [files[i:i + n] for i in range(0, len(files), n)]


# --- FINDINGS block contract -------------------------------------------------------------
# Every review/security worker must end its final reply with this delimiter-fenced JSON
# array (empty array if nothing found) immediately before its DONE marker. Parsed by
# bench/review_aggregate.py:parse_findings_block -- import these constants from here so
# the two modules can never drift apart.
FINDINGS_BEGIN = "<<<FINDINGS>>>"
FINDINGS_END = "<<<END_FINDINGS>>>"

_FINDINGS_SPEC = (
    "作業の最後に、次の形式で発見事項をJSON配列として出力してください（見つからなければ空配列 [] ）。\n"
    "この行より前にも後にも文章を書いて構いませんが、必ず以下の2行のデリミタで囲んでください:\n\n"
    + FINDINGS_BEGIN + "\n"
    "[\n"
    '  {"file": "path/to/file.py", "line": 123, "severity": "high", '
    '"title": "...", "detail": "..."}\n'
    "]\n"
    + FINDINGS_END + "\n\n"
    "severity は \"low\" / \"medium\" / \"high\" のいずれか。line は不明なら null。\n"
    "このブロックを出力した後、最後に DONE と書いて終了してください。"
)

REVIEW_RUBRIC = (
    "あなたはこのリポジトリのコードレビューを行います。\n"
    "対象リポジトリ（このPCのローカル、git チェックアウト済み）:\n"
    "  {FILE_LIST_CWD}\n\n"
    "以下のファイルを、ファイルツール（read_file / grep 等）で実際に開いて読んでください:\n"
    "{FILE_LIST}\n\n"
    "観点:\n"
    "1) 正しさ・バグ（ロジック誤り、境界条件、エラーハンドリング漏れ）\n"
    "2) 重複・簡潔化（コピペ、統合できる分岐、不要な複雑さ）\n"
    "3) パフォーマンス（無駄なループ・I/O、非効率なアルゴリズム）\n"
    "4) 保守性（命名、責務分割、テスト容易性）\n"
    "必要に応じてWeb検索で仕様やベストプラクティスを確認して構いません。\n\n"
    + _FINDINGS_SPEC
)

SECURITY_RUBRIC = (
    "あなたはこのリポジトリのセキュリティレビューを行います。\n"
    "対象リポジトリ（このPCのローカル、git チェックアウト済み）:\n"
    "  {FILE_LIST_CWD}\n\n"
    "以下のファイルを、ファイルツール（read_file / grep 等）で実際に開いて読んでください:\n"
    "{FILE_LIST}\n\n"
    "観点:\n"
    "1) インジェクション（SQL/コマンド/テンプレート/パス）\n"
    "2) ハードコードされた秘密情報・認証情報（APIキー、パスワード、トークン）\n"
    "3) 認可・認証の抜け（権限チェック漏れ、権限昇格）\n"
    "4) 安全でないデシリアライズ・eval の使用\n"
    "5) パストラバーサル\n"
    "6) SSRF（外部URLをそのまま取得する処理）\n"
    "7) 依存ライブラリのCVE・既知脆弱性の懸念（必要ならWeb検索で確認）\n\n"
    + _FINDINGS_SPEC
)


def build_review_goal(file_group, repo_root, kind):
    """Build one fleet goal dict {"text": str, "cwd": repo_root} (no "checks" key) that asks
    the agent to review/security-audit exactly `file_group` and end with a FINDINGS block.
    kind must be "review" or "security". Every filename in file_group is guaranteed to
    appear verbatim in the resulting text."""
    if kind == "review":
        rubric = REVIEW_RUBRIC
    elif kind == "security":
        rubric = SECURITY_RUBRIC
    else:
        raise ValueError("kind must be 'review' or 'security', got %r" % (kind,))

    file_list_text = "\n".join("- " + f for f in file_group)
    text = rubric.replace("{FILE_LIST_CWD}", repo_root).replace("{FILE_LIST}", file_list_text)

    for f in file_group:
        assert f in text, "filename %r missing verbatim from goal text" % (f,)

    return {"text": text, "cwd": repo_root}


def write_goals_jsonl(goals, out_path):
    """Write `goals` (list of dicts) as one JSON object per line (fleet_runner's
    goals-file format). Creates the parent directory if needed. Returns the count written."""
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    n = 0
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for g in goals:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
            n += 1
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=os.getcwd())
    ap.add_argument("--mode", choices=["all", "diff"], default="all")
    ap.add_argument("--base-ref", default=None)
    ap.add_argument("--cached", action="store_true")
    ap.add_argument("--kind", choices=["review", "security"], default="review")
    ap.add_argument("--group-size", type=int, default=20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    repo_root = os.path.abspath(args.repo_root)
    files = enumerate_files(args.mode, repo_root, base_ref=args.base_ref, cached=args.cached)
    groups = group_files(files, args.group_size)
    goals = [build_review_goal(g, repo_root, args.kind) for g in groups]
    n = write_goals_jsonl(goals, args.out)
    print("wrote %s with %d goals (%d files, kind=%s, mode=%s)" %
          (args.out, n, len(files), args.kind, args.mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
