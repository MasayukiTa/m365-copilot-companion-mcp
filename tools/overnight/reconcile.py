"""Morning consistency check: does the report agree with what the fleet itself shows?

Two views are compared:
  * the cockpit's own view, read with submit_via_ui.ps1 -ReadOnly (a dry run: nothing is typed
    or submitted) -- kept verbatim in the report, or the reason it could not be read;
  * the fleet's status.json worker list, matched to our items by their [ID] tag.

Every disagreement becomes one plain sentence in the report. The check never raises and the
report always gets a result, even "could not be checked".
"""
from __future__ import annotations


from tools.overnight import state as S
from tools.overnight.detect import DONE, FAILED, QUEUED, RUNNING, UNSEEN

_WORKER_STATE_JA = {DONE: "完了", FAILED: "失敗", RUNNING: "実行中", QUEUED: "待機中", UNSEEN: "見当たらない"}


def compare(st: S.State, observations: dict) -> list:
    """Sentences for every item whose report status and worker status disagree."""
    out = []
    for iid in st.ids():
        rec = st.get(iid)
        ob = observations.get(iid)
        if ob is None:
            continue
        status, wstate = rec["status"], ob.state
        submitted = bool(rec.get("submitted_ts"))
        if not submitted and wstate != UNSEEN:
            out.append("%s: 投入していない扱いだが、フリートには「%s」の作業者がいる" % (iid, _WORKER_STATE_JA[wstate]))
        elif submitted and wstate == UNSEEN:
            out.append("%s: 投入済み(レポート上は%s)だが、フリートの作業者一覧に見当たらない" % (iid, status))
        elif status == S.DONE and wstate in (FAILED, RUNNING, QUEUED):
            out.append("%s: レポートでは成功だが、作業者は「%s」" % (iid, _WORKER_STATE_JA[wstate]))
        elif status in (S.FAILED, S.TIMEOUT) and wstate == DONE:
            out.append("%s: レポートでは%sだが、作業者はその後「完了」している(回答を確認してください)"
                       % (iid, "失敗" if status == S.FAILED else "時間切れ"))
        elif status in (S.FAILED, S.TIMEOUT) and wstate in (RUNNING, QUEUED):
            out.append("%s: レポートでは%sだが、作業者はまだ「%s」(止めていません)"
                       % (iid, "失敗" if status == S.FAILED else "時間切れ", _WORKER_STATE_JA[wstate]))
    return out


def run_check(st: S.State, reader, readonly_text_fn, now_ts: float) -> dict:
    """-> {"readonly": str, "mismatches": [str], "checked": int, "error": str}"""
    res = {"readonly": "", "mismatches": [], "checked": 0, "error": ""}
    try:
        res["readonly"] = str(readonly_text_fn())[:1500]
    except Exception as e:
        res["readonly"] = "取得できませんでした: %r" % (e,)
    try:
        ids = {i: "[%s]" % i for i in st.ids()}
        since = float(st.config.get("run_started_ts", 0)) - 3600
        obs = reader.observe(ids, since, now_ts)
        res["checked"] = len(ids)
        res["mismatches"] = compare(st, obs)
    except Exception as e:
        res["error"] = "作業者一覧との突合に失敗しました: %r" % (e,)
    return res


def describe(result) -> list:
    """Lines for the report. A missing result is stated, never left out."""
    if not result:
        return ["整合検査は実行されませんでした(途中終了の可能性があります)。"]
    lines = ["突合した項目数: %s" % result.get("checked", 0)]
    if result.get("error"):
        lines.append(result["error"])
    mm = result.get("mismatches") or []
    lines += (["ずれ: %d 件" % len(mm)] + mm) if mm else ["ずれ: なし" if not result.get("error") else "ずれ: 判定できず"]
    ro = (result.get("readonly") or "").strip()
    lines.append("コックピットの読み取り(-ReadOnly): " + (ro or "(出力なし)"))
    return lines


