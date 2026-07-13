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
    appear verbatim in the resulting text.

    LEGACY / superseded by REVIEW_DIMENSIONS + build_dimension_goal below (one monolithic
    all-in-one rubric per file-group, instead of one goal per (dimension, file-group)).
    Kept as-is -- unchanged signature and behavior -- for any caller still relying on the
    single-rubric-per-kind shape (e.g. this module's own `main()` CLI)."""
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


# --- Multi-dimensional review fan-out ------------------------------------------------------
# REVIEW_DIMENSIONS replaces "one all-in-one rubric per file-group" with one goal per
# (dimension, file-group): each dimension is a narrow lens that targets ONE specific failure
# class, including classes the monolithic REVIEW_RUBRIC/SECURITY_RUBRIC above never asked
# about (deployed-state identity leaks, multi-device/operational hazards, test side effects,
# false-green CI, dormant/unwired features, adversarial inputs, and ACTUALLY RUNNING code to
# observe behavior instead of only reading it).
#
# Each entry: {"key": str, "title": str (JP label), "rubric": str (JP prompt body, still
# carrying the {FILE_LIST_CWD}/{FILE_LIST} placeholders build_dimension_goal fills in),
# "applies_to": frozenset of {"review","security"} subset, "behavioral": bool}.

_LENS_PREAMBLE = (
    "あなたはこのリポジトリのレビューを行います。担当する観点（レンズ）は「{LENS_LABEL}」だけです。"
    "他の観点は別のゴールが並行して担当するので、ここで深追いしすぎないこと（見つけたら報告し、"
    "無関係な観点の指摘は無理に広げない）。\n"
    "対象リポジトリ（このPCのローカル、git チェックアウト済み）:\n"
    "  {FILE_LIST_CWD}\n\n"
    "以下のファイルを、ファイルツール（read_file / grep 等）で実際に開いて読んでください:\n"
    "{FILE_LIST}\n\n"
    "観点（このレンズで具体的に探すもの）:\n"
)


def _lens_rubric(label, focus_text):
    return _LENS_PREAMBLE.replace("{LENS_LABEL}", label) + focus_text


# The run-it-and-observe instruction appended (in addition to the dimension's own focus
# text) for dimensions marked behavioral=True. Deliberately distinct from
# relay/coding_discipline.py's coding_discipline_text() -- that block assumes an
# edit-until-green fix workflow ("まず編集する前に...修正を続け"); a review goal never edits
# anything, it only runs code to OBSERVE, so this is a review-tailored variant instead of a
# verbatim reuse.
_BEHAVIORAL_INSTRUCTION = (
    "\n【このゴールは実際に実行して観測すること】読むだけのレビューでは不十分です。"
    "run_python もしくは shell_exec を実際に使って、該当するコードパスをこの場で実行し、"
    "observed（実際に起きたこと）を報告してください。「動くはず」「〜と思われる」で済ませず、"
    "実行結果（標準出力・例外・実際に発生した副作用）を detail に具体的に書くこと。"
    "実行できなかった/再現できなかった場合も、その旨と試したコマンド・入力を detail に明記する"
    "こと。\n"
)


def _findings_spec_for(dimension_key):
    """Same FINDINGS/DONE contract as _FINDINGS_SPEC, plus an optional "dimension" field in
    the example schema so a dimension-scoped worker tags each finding with which lens found
    it (aggregation tolerates the extra key -- see bench/review_aggregate.aggregate, which
    copies every key of the parsed finding dict verbatim)."""
    example = (
        '  {"file": "path/to/file.py", "line": 123, "severity": "high", '
        '"title": "...", "detail": "...", "dimension": "' + dimension_key + '"}'
    )
    return (
        "作業の最後に、次の形式で発見事項をJSON配列として出力してください（見つからなければ空配列 [] ）。\n"
        "この行より前にも後にも文章を書いて構いませんが、必ず以下の2行のデリミタで囲んでください:\n\n"
        + FINDINGS_BEGIN + "\n"
        "[\n" + example + "\n"
        "]\n"
        + FINDINGS_END + "\n\n"
        "severity は \"low\" / \"medium\" / \"high\" のいずれか。line は不明なら null。\n"
        "dimension には必ず \"" + dimension_key + "\" を入れること"
        "（このゴールが担当する観点のキー。他の値を書かないこと）。\n"
        "このブロックを出力した後、最後に DONE と書いて終了してください。"
    )


REVIEW_DIMENSIONS = [
    {
        "key": "correctness",
        "title": "正しさ・バグ",
        "applies_to": frozenset({"review"}),
        "behavioral": False,
        "rubric": _lens_rubric(
            "正しさ・バグ",
            "1) ロジック誤り・仕様との不一致（意図した動作と実装のズレ）\n"
            "2) 境界条件（空/0件/1件、上限値、None、範囲外インデックス、off-by-one）\n"
            "3) エラーハンドリング漏れ（例外の握りつぶし、失敗時に不整合な状態を残す、"
            "リトライ無し）\n"
            "4) リソースリーク（ファイル/ソケット/プロセスのclose漏れ、withブロック未使用）\n"
            "見つけたら、実際にその行のロジックを追い、具体的な入力でどう壊れるかをdetailに"
            "書くこと。\n",
        ),
    },
    {
        "key": "security",
        "title": "セキュリティ",
        "applies_to": frozenset({"security"}),
        "behavioral": False,
        "rubric": _lens_rubric(
            "セキュリティ",
            "1) インジェクション（SQL/コマンド/テンプレート/パス）\n"
            "2) ハードコードされた秘密情報・認証情報（APIキー、パスワード、トークン）\n"
            "3) 認可・認証の抜け（権限チェック漏れ、権限昇格）\n"
            "4) 安全でないデシリアライズ・evalの使用\n"
            "5) パストラバーサル\n"
            "6) SSRF（外部URLをそのまま取得する処理）\n"
            "7) 依存ライブラリのCVE・既知脆弱性の懸念（必要ならWeb検索で確認）\n"
            "8) デプロイ済み状態での識別情報漏洩: コードのデフォルト値が安全でも、"
            "コミットされた設定ファイル・ドキュメント・サンプルの中に、特定の組織/ユーザーを"
            "特定できるtunnel名・ホスト名・.envに記録された識別子が残っていないか。コードの"
            "ロジックだけでなく、config/ドキュメント/exampleファイルの中身も実際に開いて"
            "確認すること。\n",
        ),
    },
    {
        "key": "runtime_behavior",
        "title": "実行時挙動（実際に動かして観測する）",
        "applies_to": frozenset({"review", "security"}),
        "behavioral": True,
        "rubric": _lens_rubric(
            "実行時挙動（実際に動かして観測する）",
            "1) このコードパスを実際に実行し、主張されている動作(claimed)と観測された"
            "動作(observed)が一致するか確認する。\n"
            "2) 副作用: テストや関数を実行した時に、本物のデスクトップ通知・ファイル書き込み・"
            "ネットワーク送信が実際に発火しないか。\n"
            "3) 自己参照入力の罠: テキスト中の特定マーカーを検出するコードが、そのマーカーを"
            "含むテキスト自体（このレビュー指摘文やログなど）をスキャンした時に誤発火"
            "(false trigger)しないか。\n"
            "4) ハッピーパスが本当に実行できるか（importエラー・未定義変数・型不一致等で"
            "「動くはず」なだけで実際には動かないコードでないか）。\n",
        ),
    },
    {
        "key": "deployment_operational",
        "title": "デプロイ・運用（複数端末・共有設定）",
        "applies_to": frozenset({"review", "security"}),
        "behavioral": False,
        "rubric": _lens_rubric(
            "デプロイ・運用（複数端末・共有設定）",
            "1) 端末固有の値（URL、tunnel名、ホスト名、個人アカウント名、絶対パス）が、"
            "複数人/複数端末で共有・コピーされる想定のファイルにハードコードされていないか。\n"
            "2) 設定ファイルがフレッシュチェックアウト/別端末にコピーされた時にそのまま動くか"
            "（存在しないローカル状態を前提にしていて壊れないか）。\n"
            "3) 所有権・スコープの前提（アカウントAが作成したリソースをアカウントBが参照して"
            "いる等）が崩れていないか。\n"
            "4) 設定のドリフト（あるファイルが宣言している値と、別のファイルが実際に使って"
            "いる値がズレていないか）。\n",
        ),
    },
    {
        "key": "test_hygiene",
        "title": "テストの副作用・衛生",
        "applies_to": frozenset({"review"}),
        "behavioral": False,
        "rubric": _lens_rubric(
            "テストの副作用・衛生",
            "1) テストがモックではなく本物の副作用（OSのデスクトップ通知、テンポラリ外への"
            "実ファイル書き込み、実ネットワーク通信、実サブプロセス起動）を発生させていないか。\n"
            "2) テストがダーティなローカル作業ツリーや特定の事前状態（フレッシュチェックアウトでは"
            "存在しない未追跡ファイル等）に依存していないか。\n"
            "3) テストスイート全体を実行した時、開発者にトーストやポップアップが出たり、実データを"
            "変更したりしないか。\n",
        ),
    },
    {
        "key": "false_green_ci",
        "title": "CIの偽陽性(false-green)",
        "applies_to": frozenset({"review", "security"}),
        "behavioral": False,
        "rubric": _lens_rubric(
            "CIの偽陽性(false-green)",
            "1) CI上でピン留めされたバージョンが requirements や実際にユーザーがインストールする"
            "ものと異なっていないか（CIが緑でも出荷物を検証していない）。\n"
            "2) ローカルではpassするが、gitで追跡されていない依存（未コミットのファイル/"
            "パッケージ）に依存していて、フレッシュチェックアウトでは壊れないか。\n"
            "3) 「CIが緑」であることが、実際にその機能が動くことの証明になっているか、それとも"
            "見かけ上緑なだけか（本質的なアサーションを迂回していないか）。\n",
        ),
    },
    {
        "key": "missing_wiring",
        "title": "配線漏れ・死んだ機能(dormant)",
        "applies_to": frozenset({"review"}),
        "behavioral": False,
        "rubric": _lens_rubric(
            "配線漏れ・死んだ機能(dormant)",
            "1) 実装済みだが、どこからも呼ばれていない機能・ストア・ツール（設計されただけで"
            "発火するトリガーが無いもの）が無いか。\n"
            "2) ドキュメントに書かれた「規約」だけで、それを強制・実行するコードが実際には"
            "存在しないケースが無いか。\n"
            "3) 各capability（関数/クラス/ツール定義）について、実際にそれを呼び出すcallerが"
            "存在するかgrepで確認すること（存在しなければ死んだ機能として報告）。\n",
        ),
    },
    {
        "key": "adversarial_input",
        "title": "敵対的入力（実際に投入して観測する）",
        "applies_to": frozenset({"review", "security"}),
        "behavioral": True,
        "rubric": _lens_rubric(
            "敵対的入力（実際に投入して観測する）",
            "1) 非常に大きい入力を与えた時の挙動。\n"
            "2) 複数行/改行を含む入力（行指向のパーサーがそれによって壊れないか）。\n"
            "3) 不正な形式・ギリギリ近い(near-miss)入力。\n"
            "4) 自己参照的な内容（マーカーやデリミタそのものを含む入力）。\n"
            "可能な限り、実際に小さな再現コードをrun_pythonで組み立てて実行し、結果を観測する"
            "こと。\n",
        ),
    },
    {
        "key": "cross_file_interaction",
        "title": "ファイル横断の契約・不変条件",
        "applies_to": frozenset({"review"}),
        "behavioral": False,
        "rubric": _lens_rubric(
            "ファイル横断の契約・不変条件",
            "1) あるモジュールが書き出すフォーマットを、別のモジュールがパースするような契約"
            "（区切り文字、JSON形状など）が、両側で一致しているか。\n"
            "2) 複数ファイルで共有される定数・マーカー文字列が、片方だけ変更されてズレて"
            "いないか。\n"
            "3) 順序・ライフサイクルの前提（初期化→使用の順番、状態遷移）が複数ファイルに"
            "またがって成立しているか。\n"
            "担当ファイル一覧に無いファイルでも、grep等で関連する呼び出し元・パース側を"
            "見つけたら実際に開いて確認すること。\n",
        ),
    },
]

_DIMENSIONS_BY_KEY = {d["key"]: d for d in REVIEW_DIMENSIONS}


def dimensions_for_kind(kind):
    """Return the REVIEW_DIMENSIONS entries applicable to `kind` ("review" or "security"),
    in registry order. Raises ValueError for any other kind."""
    if kind not in ("review", "security"):
        raise ValueError("kind must be 'review' or 'security', got %r" % (kind,))
    return [d for d in REVIEW_DIMENSIONS if kind in d["applies_to"]]


def _resolve_dimension(dimension):
    if isinstance(dimension, dict):
        if "key" not in dimension or "rubric" not in dimension:
            raise ValueError("dimension dict missing required keys: %r" % (dimension,))
        return dimension
    dim = _DIMENSIONS_BY_KEY.get(dimension)
    if dim is None:
        raise ValueError("unknown dimension %r" % (dimension,))
    return dim


def build_dimension_goal(dimension, file_group, repo_root):
    """Build one fleet goal dict {"text": str, "cwd": repo_root} (no "checks" key) that asks
    the agent to review exactly `file_group` through ONE dimension's lens and end with the
    same FINDINGS/DONE contract as build_review_goal (with an added "dimension" tag in the
    example schema). `dimension` may be a REVIEW_DIMENSIONS entry (dict) or its "key" string.

    Every filename in file_group is guaranteed to appear verbatim in the resulting text
    (mirrors build_review_goal's own verbatim-filename assert)."""
    dim = _resolve_dimension(dimension)

    file_list_text = "\n".join("- " + f for f in file_group)
    text = dim["rubric"].replace("{FILE_LIST_CWD}", repo_root).replace(
        "{FILE_LIST}", file_list_text)

    if dim.get("behavioral"):
        text += _BEHAVIORAL_INSTRUCTION

    text += "\n" + _findings_spec_for(dim["key"])

    for f in file_group:
        assert f in text, "filename %r missing verbatim from goal text" % (f,)

    return {"text": text, "cwd": repo_root}


# --- FIX goal builder ---------------------------------------------------------------------
# Takes findings already produced by a review run (bench/review_aggregate.aggregate) and asks
# a fresh fleet worker to FIX them -- edit-with-file-tools, mirroring bench/swe_build_goals.py's
# tool guidance (grep / glob / read_file / replace_in_file / write_file), but scoped to exactly
# the listed findings instead of an open-ended issue. Reuses FINDINGS_BEGIN/FINDINGS_END so the
# "what I changed" block is parsed by the SAME bench.review_aggregate.parse_findings_block --
# no second parser (see bench/review_fix.py, which reuses it verbatim).
FIX_RUBRIC = (
    "あなたはこのリポジトリのコードレビュー指摘を修正します。\n"
    "対象リポジトリ（このPCのローカル、git チェックアウト済み）:\n"
    "  {FILE_LIST_CWD}\n\n"
    "以下のファイルを、ファイルツール（read_file / grep / replace_in_file / write_file）で"
    "実際に開いて読み、下記の指摘事項を修正してください。\n\n"
    "{FINDINGS_TEXT}\n\n"
    "厳守事項（違反しないこと）:\n"
    "1) 上記の指摘一覧に無いファイル・箇所は一切変更しないこと。無関係なリファクタや"
    "再フォーマットは禁止。\n"
    "2) 修正は指摘を解決するのに必要な最小限の差分にすること。\n"
    "3) 既存のテストを壊さないこと。\n"
    "4) 指摘の内容が誤り、あるいは対応不要と判断した場合は、無理に修正を加えないこと。"
    "その場合は理由を記録すること。\n\n"
    "作業の最後に、次の形式で「各指摘に対して何をしたか」をJSON配列として出力してください"
    "（この行より前にも後にも文章を書いて構いませんが、必ず以下の2行のデリミタで囲んでください）:\n\n"
    + FINDINGS_BEGIN + "\n"
    "[\n"
    '  {"file": "path/to/file.py", "line": 123, "title": "...", '
    '"applied": true, "summary": "..."}\n'
    "]\n"
    + FINDINGS_END + "\n\n"
    "applied は実際に修正を適用したなら true、指摘が誤りだった等の理由で修正しなかったなら "
    "false（summary にその理由を書くこと）。\n"
    "このブロックを出力した後、最後に DONE と書いて終了してください。"
)


def build_fix_goal(finding_group, repo_root):
    """Build one fleet goal dict {"text": str, "cwd": repo_root} (no "checks" key) that asks
    the agent to fix exactly the findings in `finding_group` (a list of finding dicts as
    produced by bench.review_aggregate.aggregate()["findings"]) and end with a FINDINGS-shaped
    "what I changed" block.

    Every finding's "file" AND "title" is guaranteed to appear verbatim in the resulting text
    (mirrors build_review_goal's own verbatim-filename assert)."""
    files = []
    for f in finding_group:
        fp = f.get("file", "")
        if fp and fp not in files:
            files.append(fp)
    file_list_text = "\n".join("- " + f for f in files)

    detail_lines = []
    for i, f in enumerate(finding_group):
        file_ = f.get("file", "?")
        line = f.get("line")
        line_s = str(line) if line is not None else "?"
        sev = f.get("severity", "?")
        title = f.get("title", "")
        detail = f.get("detail", "")
        detail_lines.append(
            "%d. %s:%s [%s] %s\n   detail: %s" % (i + 1, file_, line_s, sev, title, detail)
        )

    findings_text = (
        "対象ファイル:\n" + file_list_text + "\n\n指摘一覧:\n" + "\n".join(detail_lines)
    )

    text = FIX_RUBRIC.replace("{FILE_LIST_CWD}", repo_root).replace(
        "{FINDINGS_TEXT}", findings_text)

    for f in finding_group:
        file_ = f.get("file", "")
        title_ = f.get("title", "")
        assert file_ in text, "finding file %r missing verbatim from goal text" % (file_,)
        assert title_ in text, "finding title %r missing verbatim from goal text" % (title_,)

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
