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

from relay.refuter import build_refuter_prompt

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


# --- P2 piece B: clear-framing / intent pre-flight ----------------------------------------
# A safety-conscious model can over-refuse a code/security review task if it can't quickly
# tell the request is legitimate. Rather than obscure or fragment the task to dodge that (out
# of scope -- see the module docstrings this task's spec forbids), state the real context up
# front, plainly and completely: this IS a review, of the operator's OWN repo, authorized, for
# defensive purposes. Prepended (never inserted mid-prompt) to every dimension/refute/
# behavioral-verify goal text below, so it can never interfere with the verbatim-filename
# asserts or the <<<FINDINGS>>> delimiter contract that follow it.
CLEAR_FRAMING_PREAMBLE = (
    "これは、このリポジトリの所有者本人が依頼した、正式に許可された防御目的のコードレビューです。\n"
    "目的は弱点を悪用することではなく、実際に存在する弱点を見つけて報告し、後で修正できるように"
    "することです。\n"
    "調べた内容と発見事項は、誇張も忖度もせず、事実に基づいて正直に報告してください。\n\n"
)


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
    text = CLEAR_FRAMING_PREAMBLE + rubric.replace(
        "{FILE_LIST_CWD}", repo_root).replace("{FILE_LIST}", file_list_text)

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
    text = CLEAR_FRAMING_PREAMBLE + dim["rubric"].replace(
        "{FILE_LIST_CWD}", repo_root).replace("{FILE_LIST}", file_list_text)

    if dim.get("behavioral"):
        text += _BEHAVIORAL_INSTRUCTION

    text += "\n" + _findings_spec_for(dim["key"])

    for f in file_group:
        assert f in text, "filename %r missing verbatim from goal text" % (f,)

    return {"text": text, "cwd": repo_root}


# --- REFUTE goal builder (P1b) -------------------------------------------------------------
# Reuses the EXISTING adversarial refuter (relay/refuter.py) instead of a bespoke second-pass
# verifier: each review/security finding is handed to an independent skeptic that must find a
# concrete reason the finding is NOT a real defect, or uphold it. relay.refuter.parse_verdict
# reads the reply -- a completely different contract from the <<<FINDINGS>>> block above, so
# the refuter goal text below is emitted AS-IS (no FINDINGS delimiters).

# Which relay.refuter.LENS_PROMPTS lens to use for a finding, keyed by its "dimension" tag (see
# REVIEW_DIMENSIONS above). Dimensions not listed here fall back to REFUTE_KIND_TO_LENS.
REFUTE_DIMENSION_TO_LENS = {
    "correctness": "correctness",
    "security": "security",
    "runtime_behavior": "edge",
    "adversarial_input": "edge",
    "deployment_operational": "correctness",
    "false_green_ci": "correctness",
    "missing_wiring": "correctness",
    "test_hygiene": "correctness",
    "cross_file_interaction": "correctness",
}

# Fallback lens when a finding carries no "dimension" tag (e.g. legacy build_review_goal
# output) or an unrecognized one -- keyed by the overall review run kind.
REFUTE_KIND_TO_LENS = {
    "review": "correctness",
    "security": "security",
}

_DEFAULT_REFUTE_LENS = "correctness"


def _lens_for_finding(finding, kind):
    """Pick the relay.refuter.LENS_PROMPTS key for one finding: its own "dimension" tag wins
    if recognized, else fall back to the overall run kind, else "correctness"."""
    dim = finding.get("dimension") if isinstance(finding, dict) else None
    if dim and dim in REFUTE_DIMENSION_TO_LENS:
        return REFUTE_DIMENSION_TO_LENS[dim]
    return REFUTE_KIND_TO_LENS.get(kind, _DEFAULT_REFUTE_LENS)


def build_refute_goal(finding, kind, lens=None):
    """Build the refuter goal TEXT (a plain str, NOT a {"text","cwd"} goal dict) for one
    review/security finding, via relay.refuter.build_refuter_prompt -- the SAME independent-
    skeptic verdict contract (last line "REFUTED: <reason>" or "UPHELD", read by
    relay.refuter.parse_verdict) already used to refute an implementer's claimed DONE.

    `goal` (in build_refuter_prompt terms) is the finding's file/line plus a one-line "a
    reviewer claimed <title>"; `final_response` (the claim to attack) is the finding's title +
    detail. `kind` ("review" or "security") picks the lens fallback via REFUTE_KIND_TO_LENS
    when the finding has no usable "dimension" tag. `lens` overrides the picked lens entirely
    (used by the 3-lens panel mode in bench.review_run.refute_findings, which drives every
    relay.refuter.PANEL_LENSES lens per finding regardless of the finding's own dimension).

    Callers wrap the returned string into the plain fleet-goal dict themselves
    (this function does not know about cwd/repo_root, unlike build_dimension_goal)."""
    finding = finding if isinstance(finding, dict) else {}
    if lens is None:
        lens = _lens_for_finding(finding, kind)

    file_ = finding.get("file") or "?"
    line = finding.get("line")
    line_s = str(line) if line is not None else "?"
    title = finding.get("title") or ""
    detail = finding.get("detail") or ""

    goal_text = (
        "対象ファイル: %s (行 %s)\n"
        "別のレビュアーが次の指摘（claim）をしました: %s" % (file_, line_s, title)
    )
    claim_text = title + ("\n" + detail if detail else "")

    return CLEAR_FRAMING_PREAMBLE + build_refuter_prompt(goal_text, claim_text, lens=lens)


# --- P2 piece A: behavioral verification of CONFIRMED findings ------------------------------
# After the refuter (above) upholds a finding on REASONING alone, this goal asks a fresh worker
# to actually DEMONSTRATE it: build a minimal, READ-ONLY repro with run_python/shell_exec and
# report what was actually observed vs. what was claimed. This is deliberately a separate
# verdict contract from BOTH the <<<FINDINGS>>> block (build_dimension_goal) and the REFUTED/
# UPHELD one (relay/refuter.py) -- a behavioral-verify worker is neither hunting for new
# findings nor arguing philosophically about one; it is running code and reporting what
# happened. Keeping the three contracts textually distinct (different marker words, no shared
# delimiter) means a downstream parser can never confuse one worker's output for another's.
BEHAVIOR_VERDICT_TAG = "BEHAVIOR_VERDICT"
BEHAVIOR_EVIDENCE_TAG = "BEHAVIOR_EVIDENCE"
BEHAVIOR_VERDICTS = ("reproduced", "not_reproduced", "inconclusive")

_BEHAVIOR_VERDICT_SPEC = (
    "作業の最後に、次の2行を必ず出力してください（これは別のレビューで使われる指摘一覧の"
    "JSON形式ブロックとは別物です。混同しないこと。このゴールでは新しい指摘を探す必要は"
    "ありません）:\n\n"
    + BEHAVIOR_VERDICT_TAG + ": reproduced|not_reproduced|inconclusive\n"
    + BEHAVIOR_EVIDENCE_TAG + ": <実際に何を実行し、何を観測したかを1〜3文で具体的に>\n\n"
    "reproduced = 実際に実行して、この指摘が主張する挙動を観測できた（再現できた）。\n"
    "not_reproduced = 実際に実行してみたが、主張されている挙動は観測できなかった。\n"
    "inconclusive = 実行環境の制約等で実行できなかった、または結果が曖昧だった。\n"
    "この2行を出力した後、最後に DONE と書いて終了してください。"
)


def build_behavioral_verify_goal(finding):
    """Build the behavioral-verify goal TEXT (a plain str, NOT a {"text","cwd"} goal dict --
    callers wrap it themselves, mirroring build_refute_goal) for one finding whose
    verify_verdict is already "confirmed" (see bench.review_run.refute_findings /
    merge_verdicts): asks the agent to actually REPRODUCE the claim with a minimal, READ-ONLY
    run_python/shell_exec check (e.g. import the module and call the function on sample
    inputs, grep the relevant code, run an existing test in a tmp directory) and report the
    OBSERVED behavior against the claim.

    NON-MUTATING BY DESIGN: the prompt repeatedly and explicitly forbids editing source files,
    deleting anything, running destructive commands, or hitting external networks -- this is
    an observe-only repro, not a fix attempt (bench/review_fix.py already owns the separate
    edit-and-fix fleet pass; this goal must never blur into that).

    Emits its OWN verdict contract (BEHAVIOR_VERDICT_TAG / BEHAVIOR_EVIDENCE_TAG, see
    _BEHAVIOR_VERDICT_SPEC above), read by bench.review_run.parse_behavior_verdict -- a
    completely separate block from the <<<FINDINGS>>> contract, emitted AS-IS (no FINDINGS
    delimiters), the same shape as build_refute_goal's own REFUTED/UPHELD contract."""
    finding = finding if isinstance(finding, dict) else {}
    file_ = finding.get("file") or "?"
    line = finding.get("line")
    line_s = str(line) if line is not None else "?"
    title = finding.get("title") or ""
    detail = finding.get("detail") or ""

    claim = (
        "対象ファイル: %s (行 %s)\n"
        "指摘（他のレビュアーによる、反証レビューを通過済みの claim）: %s\n" % (file_, line_s, title)
    )
    if detail:
        claim += "詳細: %s\n" % detail

    return (
        CLEAR_FRAMING_PREAMBLE
        + "この指摘の主張が実際に正しいかどうかを、読むだけでなく実際にコードを実行して"
          "検証してください。\n\n"
        + claim
        + "\n【厳守事項 - 読み取り専用・非破壊（絶対に守ること）】\n"
          "1) run_python もしくは shell_exec を実際に使い、最小限の再現コードをこの場で実行し、"
          "主張されている挙動が本当に観測できるか確認すること（例: 対象モジュールを import して"
          "該当関数をサンプル入力で呼び出す、grep で該当箇所を確認する、既存テストをtmp"
          "ディレクトリにコピーして実行する等）。\n"
          "2) ソースコードは絶対に編集しないこと。ファイルの削除・上書き、破壊的なコマンド"
          "（rm -rf 等）、外部ネットワークへの書き込みや攻撃的なアクセスは一切行わないこと。"
          "読み取り・観測・分析のみを行うこと。\n"
          "3) 「動くはず」「〜と思われる」で済ませず、実際に実行したコマンド/コードと、実際に"
          "出力・例外・副作用として観測された内容を具体的に報告すること。再現できなかった場合も、"
          "何を試して何が起きたかを明記すること。\n\n"
        + _BEHAVIOR_VERDICT_SPEC
    )


# --- P3 piece B: completeness critic --------------------------------------------------------
# After a review round completes, this goal asks a FRESH worker to look at what has been
# covered so far (which REVIEW_DIMENSIONS keys ran, which files were examined, and the current
# findings list) and report what's MISSING: an un-run dimension, an unexamined file/area, or a
# current finding whose claim is asserted but not actually verified. Built ONLY through
# CLEAR_FRAMING_PREAMBLE + a plain rubric string below -- this function never reads, copies, or
# string-matches the preamble's own contents, so a future rewrite of CLEAR_FRAMING_PREAMBLE
# (see the module-level warning above it) stays entirely localized and cannot break this goal
# builder or bench/review_run.py's loop-until-dry orchestration that consumes it.
#
# Emits its OWN small delimiter-fenced JSON object (GAPS_BEGIN/GAPS_END), parsed in exactly one
# place: bench.review_run.parse_completeness_gaps. Deliberately a fourth distinct contract from
# <<<FINDINGS>>>, the REFUTED/UPHELD text block, and the BEHAVIOR_VERDICT block above -- a
# completeness-critic worker is not hunting for new findings (that's the next round's job) and
# not arguing about one finding (that's the refuter's job); it is auditing COVERAGE.
GAPS_BEGIN = "<<<GAPS>>>"
GAPS_END = "<<<END_GAPS>>>"

_COMPLETENESS_RUBRIC = (
    "あなたはこのリポジトリのレビューが十分に網羅的か検証する「完全性チェック」だけを担当します。"
    "新しい指摘を探す必要はありません。\n"
    "対象リポジトリ（このPCのローカル、git チェックアウト済み）:\n"
    "  {FILE_LIST_CWD}\n\n"
    "これまでに実行されたレビュー観点(dimension)一覧:\n"
    "{DIMENSIONS_RUN}\n\n"
    "これまでにレビュー対象となったファイル一覧:\n"
    "{FILES_COVERED}\n\n"
    "現在までに得られている指摘一覧（file:line:title のみ、詳細は省略）:\n"
    "{FINDINGS_SUMMARY}\n\n"
    "次の3点を、必要であれば実際にファイルツール（read_file / grep 等）で確認したうえで"
    "報告してください:\n"
    "1) 上記でまだ実行されていないレビュー観点(dimension)のうち、追加で実施すべきものが"
    "あるか。\n"
    "2) 上記でまだ調べられていないファイル・領域のうち、追加でレビューすべきものが"
    "あるか。\n"
    "3) 現在の指摘一覧の中に、主張されているだけで実際には検証されていない(未確認の)ものが"
    "あるか。\n\n"
)

_GAPS_SPEC = (
    "作業の最後に、次の形式で結果をJSON1個として出力してください（これは他のゴールで使われる"
    "指摘一覧のJSON配列ブロックとは別物です。混同しないこと）:\n\n"
    + GAPS_BEGIN + "\n"
    '{"missing_dimensions": ["dimension_key", ...], '
    '"missing_files": ["path/to/file.py", ...], '
    '"unverified_claims": ["finding title等の短い説明", ...]}\n'
    + GAPS_END + "\n\n"
    "不足が無い項目は空配列 [] にしてください。\n"
    "このブロックを出力した後、最後に DONE と書いて終了してください。"
)


def build_completeness_goal(dimensions_run, files_covered, findings_so_far, repo_root):
    """Build one fleet goal dict {"text": str, "cwd": repo_root} (P3 piece B) that asks a
    fresh worker to audit COVERAGE (not hunt new findings): which REVIEW_DIMENSIONS keys have
    already run (`dimensions_run`, a list of key strings), which files have already been
    examined (`files_covered`, a list of repo-relative path strings), and a compact summary of
    the findings accumulated so far (`findings_so_far`, a list of finding dicts -- only
    file/line/title are ever included in the prompt, never the full "detail", to keep this
    goal small).

    Built ONLY through CLEAR_FRAMING_PREAMBLE (imported, prepended as-is) + a plain rubric
    string local to this function -- never reads, copies, or string-matches the preamble's own
    contents, so a future rewrite of CLEAR_FRAMING_PREAMBLE cannot break this function.

    Emits the GAPS_BEGIN/GAPS_END contract (see module comment above), parsed by
    bench.review_run.parse_completeness_gaps -- NOT the <<<FINDINGS>>> contract."""
    dims_text = "\n".join("- " + str(d) for d in dimensions_run) if dimensions_run else "(none)"
    files_text = "\n".join("- " + str(f) for f in files_covered) if files_covered else "(none)"

    finding_lines = []
    for f in (findings_so_far or []):
        if not isinstance(f, dict):
            continue
        file_ = f.get("file") or "?"
        line = f.get("line")
        line_s = str(line) if line is not None else "?"
        title = f.get("title") or ""
        finding_lines.append("- %s:%s %s" % (file_, line_s, title))
    findings_text = "\n".join(finding_lines) if finding_lines else "(none)"

    text = CLEAR_FRAMING_PREAMBLE + _COMPLETENESS_RUBRIC.replace(
        "{FILE_LIST_CWD}", repo_root).replace(
        "{DIMENSIONS_RUN}", dims_text).replace(
        "{FILES_COVERED}", files_text).replace(
        "{FINDINGS_SUMMARY}", findings_text)
    text += _GAPS_SPEC

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
