"""genome と skills のデータが、公開リポジトリへ由来ごと漏れないこと。

これは「いま中身が危ないか」を人が見て判断する運用の代わりに置くもの。判断する人が
居ないときに破れるので、機械が見る述語にしてある。

境界は**パスではなく由来**に引いた。最初はパスで引こうとして archive を丸ごと追跡から
外したが、それは間違いだった: 今の archive の中身はノブ名・カード名と真偽値・公開
SWE-bench の instance ID で、要するに genome テンプレートであり、公開している pass@1 の
根拠そのものである。まだ来ていないリスクのために、実在する公開記録を捨てる判断だった。

危ないのは中身の形ではなく出どころなので、そこを見る。archive の各エントリが引用する
slice が公開ベンチの instance ID である限り、そのエントリは公開してよい。業務エピソードが
1件でも入った瞬間にここが落ち、エントリを分離するまで公開できなくなる。そのころには
genome.cards は学習したプロンプト本文を、descriptors と slice_ids は学習元の課題名を
持っているはずで、それらは会社の系統名・様式・ワークフローそのものになる。

`.claude/skills/` は以前から ignore されていた。genome 側は丸ごと無防備だった --
片側にだけ適用された規律という、このリポジトリが何度も見つけている形。
"""
import io
import json
import os
import re
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 生まれた瞬間に staging できてはいけないパス。存在しないものも含む -- 存在しないから
#: 安全なのではなく、ループが最初の genome を適用した瞬間に生まれる。
MUST_BE_IGNORED = (
    "relay/selfimprove/active_genome.json",
    "relay/selfimprove/episode_records.jsonl",
    "relay/selfimprove/skills.sqlite3",
    "skills.sqlite3",
    ".claude/skills/anything/SKILL.md",
)

#: 公開ベンチの instance ID の形。実測で2系統ある:
#:   SWE-bench Verified : astropy__astropy-13453
#:   SWE-bench Pro      : instance_NodeBB__NodeBB-<sha>-v<sha|nan>
#: 業務エピソードの ID はどちらの形にもならないので、これが由来の判別になる。
#:
#: 定義は archive.py 側に1つだけ置き、ここは import する。ここで別に持っていた頃、
#: この判定は「公開済みファイルを監査する」ためだけのもので、書き込み経路は何も見て
#: いなかった。監査はコミットが存在した後に走るので、最初に気づくのは pull した人に
#: なる。同じ規則で書き込み時に拒否するようにした以上、二重定義は必ずずれる。
from relay.selfimprove.archive import PUBLIC_BENCH_ID  # noqa: E402


def _git(*args):
    return subprocess.run(("git",) + args, cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _tracked():
    return set(_git("ls-files").stdout.splitlines())


def _rows(rel):
    path = os.path.join(REPO, rel)
    if not os.path.isfile(path):
        return []
    out = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_git_is_available():
    assert _git("rev-parse", "--git-dir").returncode == 0, "git が無いと何も確かめられない"


# ---- パスで守るもの -----------------------------------------------------------------------

def test_the_runtime_learned_state_is_not_tracked():
    """適用中の genome、エピソード記録、skills の信頼 DB。公開する理由が無く、
    業務フェーズでは学習内容そのものになる。"""
    tracked = _tracked()
    leaked = [rel for rel in MUST_BE_IGNORED if rel in tracked]
    leaked += [rel for rel in tracked if rel.startswith(".claude/skills/")]
    assert not leaked, (
        "学習データが公開リポジトリに追跡されている: %s" % sorted(set(leaked)))


def test_those_paths_are_ignored_even_where_no_file_exists_yet():
    not_ignored = [rel for rel in MUST_BE_IGNORED
                   if _git("check-ignore", "-q", rel).returncode != 0]
    assert not not_ignored, ("生まれた瞬間に staging できてしまうパス: %s" % not_ignored)


def test_the_code_beside_the_data_is_still_tracked():
    """データを外した拍子にモジュールごと外すのが、この種の変更でいちばん起きる事故。"""
    tracked = _tracked()
    for rel in ("relay/selfimprove/archive.py", "relay/selfimprove/apply.py",
                "relay/selfimprove/manifest.py", "relay/skills.py"):
        assert rel in tracked, "%s が追跡から外れている" % rel


# ---- 由来で守るもの -----------------------------------------------------------------------

def test_the_tracked_archive_cites_public_benchmark_slices_only():
    """archive を追跡し続ける条件そのもの。業務エピソードが1件入れば落ちる。"""
    if "relay/selfimprove/archive/entries.jsonl" not in _tracked():
        return                          # 追跡していないなら守る対象でもない
    odd = []
    for entry in _rows("relay/selfimprove/archive/entries.jsonl"):
        for sid in entry.get("slice_ids") or []:
            if not PUBLIC_BENCH_ID.match(str(sid)):
                odd.append((entry.get("id"), str(sid)))
    assert not odd, (
        "公開ベンチの形をしていない slice が、追跡中の archive に入っている: %s。\n"
        "業務由来ならこのエントリは公開できない -- 追跡外の台帳へ分離すること"
        % odd[:5])


def test_the_burned_registry_holds_public_benchmark_ids_only():
    """追跡し続けるもう一方のデータ。焼いた記録は改竄されないことに意味があるので
    追跡が正しいが、業務由来の ID が混ざれば同じ判断の見直しになる。"""
    if "relay/selfimprove/burned.jsonl" not in _tracked():
        return
    odd = [str(r.get("instance_id", "")) for r in _rows("relay/selfimprove/burned.jsonl")
           if not PUBLIC_BENCH_ID.match(str(r.get("instance_id", "")))]
    assert not odd, (
        "公開ベンチの形をしていない ID が追跡ファイルに入っている: %s" % odd[:5])


def test_the_shape_actually_rejects_a_business_episode_id():
    """述語が何も弾かないのでは、守っているつもりになるだけ。"""
    assert PUBLIC_BENCH_ID.match("astropy__astropy-13453")
    assert PUBLIC_BENCH_ID.match("instance_NodeBB__NodeBB-70b4a0e2aebe-vnan")
    for business in ("monthly_close_journal_entry", "approval_route_finance_2026",
                     "expense-form-v3", "顧客請求_月次"):
        assert not PUBLIC_BENCH_ID.match(business), business
