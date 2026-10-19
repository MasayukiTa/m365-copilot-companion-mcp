using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using System.Web.Script.Serialization;

partial class SelfImproveDashboardWindow
{
    // ── i18n ────────────────────────────────────────────────────────────────────
    string T(string k)
    {
        bool ja = _lang == 0;
        // window header
        if (k == "win_title")  return ja ? "自己改善 / Self-Improvement" : "Self-Improvement / 自己改善";
        if (k == "win_sub")    return ja
            ? "エージェントが自分の解決スキャフォルドをどう改善しているか（実タスクの完了率・A/Bテスト・採用履歴）"
            : "How the agent is improving its own solving scaffold — real-task completion, A/B tests, and what it kept.";

        // authority section -- what changed what the system may become
        if (k == "auth_sec") return ja ? "権限の履歴" : "Authority";
        if (k == "auth_exp") return ja
            ? "この系が「何になれるか」を変えた行為の記録。台帳は追記専用でハッシュ連鎖しているが、"
              + "何も許可しないし、止めもしない。actor は自己申告で検証されていない。"
            : "Acts that changed what this system may become. The ledger is append-only and "
              + "chained; it authorises nothing and prevents nothing, and actor is self-reported.";
        if (k == "auth_intact")   return ja ? "自己改善の安全確認" : "Self-improvement check";
        if (k == "auth_ok")       return ja ? "一致" : "matches";
        if (k == "auth_broken")   return ja ? "不一致" : "differs";
        if (k == "auth_anchor")   return ja ? "アンカー" : "Anchor";
        if (k == "auth_chain")    return ja ? "台帳の連結" : "Ledger links";
        if (k == "auth_chain_ok") return ja ? "連続" : "contiguous";
        if (k == "auth_verified_here") return ja
            ? "この2つはこの画面が自分で計算している（python の自己申告ではない）。"
              + "ただし各レコードの内容ハッシュまでは再計算していない — 連結と通し番号のみ。"
            : "Both computed by this window, not reported by python. Record CONTENTS are not "
              + "re-hashed here -- only the links and the sequence.";
        if (k == "auth_rate")     return ja ? "直近7日の再署名" : "Re-signings, last 7d";
        if (k == "auth_rate_hi")  return ja ? "平常より多い" : "above the usual";
        if (k == "auth_none")     return ja ? "記録なし" : "no records";
        if (k == "auth_revoke")   return ja ? "直前の再署名を取り消す" : "Revoke the last re-signing";
        if (k == "auth_revoke_q") return ja
            ? "直前の再署名を取り消します。\n\n戻すのは【承認】であって【コード】ではありません。"
              + "ファイルは変わったままなので、直後に自己改善の安全確認は不一致になり、走行は止まります。"
              + "それが狙いです。\n\n戻り先: "
            : "Withdraw the last re-signing.\n\nThis undoes the APPROVAL, not the code. The files "
              + "stay as they are, so the self-improvement check will show a mismatch immediately "
              + "afterwards and runs will stop. That is the intent.\n\nRestoring: ";
        if (k == "auth_revoke_t") return ja ? "最終確認" : "Final confirmation";
        if (k == "auth_revoke_ok") return ja
            ? "取り消しました。自己改善の安全確認が不一致になっているのは正常です。"
            : "Revoked. The self-improvement check showing a mismatch now is the expected state.";
        if (k == "auth_revoke_no") return ja ? "取り消せませんでした" : "Could not revoke";
        if (k == "auth_nothing")  return ja ? "取り消せる再署名がありません" : "No re-signing to withdraw";
        // THE ACT, not a command to go and run elsewhere. See BuildAuthority.
        if (k == "auth_resign")   return ja ? "ここで再署名する" : "Re-sign here";
        if (k == "auth_resign_t") return ja ? "自己改善の安全確認を今の内容で承認する"
                                            : "Approve the self-improvement check as it now stands";
        if (k == "auth_resign_why") return ja
            ? "自己改善の安全確認が不一致の間、自己改善ループは走れません。今の内容でよければ"
              + "ここで再署名できます。あなたが書いた文がそのまま台帳に残ります。"
              + "取り消しは下の「記録と取り消し」から。"
            : "While the self-improvement check shows a mismatch, the self-improvement loop "
              + "cannot run. If the files are right as they stand, re-sign here; what you write "
              + "is kept in the ledger word for word. The undo is under Records and undo, below.";
        if (k == "auth_r1")       return ja
            ? "この変更は意図したもの。今の内容で承認する。"
            : "This change was intended. Approve the set as it stands.";
        if (k == "auth_r2")       return ja
            ? "内容を確認した。判定器の守りは変わっていないので承認する。"
            : "I have read the change; it does not weaken the judge. Approve.";

        // no-data friendly message
        if (k == "nodata_title") return ja ? "まだデータがありません" : "No data yet";
        if (k == "nodata_body")  return ja
            ? "自己改善ループが走ると、ここに指標が表示されます。\n\npython -m relay.selfimprove.dashboard を実行するとデータが生成されます。"
            : "Metrics will appear here once the self-improvement loop runs.\n\nRun: python -m relay.selfimprove.dashboard";

        // section labels (shown in caps)
        if (k == "usage_sec")     return ja ? "実利用" : "Live usage";
        if (k == "scorecard_sec") return ja ? "スコアカード" : "Scorecard";
        if (k == "ab_sec")        return ja ? "A/B 履歴" : "A/B history";
        if (k == "burned_sec")    return ja ? "Burned 台帳" : "Burned ledger";
        if (k == "trend_sec")     return ja ? "Pass@1 推移" : "Pass@1 trend";
        if (k == "archive_sec")   return ja ? "ゲノム" : "Genomes";
        if (k == "pending_sec")   return ja ? "判断待ち" : "Awaiting a decision";
        if (k == "ev_rebless")    return ja ? "再署名" : "Re-signed";
        if (k == "ev_mismatch")   return ja ? "無承認の変更を検知" : "Unapproved change detected";
        if (k == "ev_revoke")     return ja ? "承認を取り消し" : "Approval withdrawn";
        if (k == "ev_apply")      return ja ? "ゲノム適用" : "Genome applied";
        if (k == "ev_revert")     return ja ? "ゲノム撤回" : "Genome reverted";
        if (k == "ev_branch_new") return ja ? "枝を命名" : "Branch named";
        if (k == "ev_branch_del") return ja ? "枝を削除" : "Branch deleted";
        if (k == "ev_unresolved") return ja ? "未解決" : "unresolved";
        if (k == "ev_closed_by")  return ja ? "検知 {0} → {1}後に再署名" : "detected {0}, re-signed {1} later";
        if (k == "ev_testrecords") return ja
            ? "テスト実行が本番台帳に書いたレコード {0} 件（一時ディレクトリのみを触るもの）"
            : "{0} records written into the live ledger by test runs (they touch only temp dirs)";
        if (k == "ev_by")         return ja ? "実行" : "by";
        if (k == "ev_summary")    return ja ? "要約" : "summary";
        if (k == "ev_churn")      return ja ? "再署名が集中しているファイル" : "Files re-signed most";
        if (k == "ev_summary_tip") return ja
            ? "モデルによる要約。台帳の記録ではない — 記録された原文はクリックで表示。"
            : "A model's summary, not the ledger's record -- click to show the recorded text.";
        if (k == "pending_exp")   return ja
            ? "恒久委任の外にある変更の提案。委任は「何を進化させてよいか」を定義するファイル自体には及ばないので、ここに溜まる。実行するには、あなたの指示をそのまま authorization に入れる。"
            : "Proposed changes outside the standing delegation. It does not extend to the files that define what may be evolved, so those queue here; running one takes your own words as its authorization.";
        if (k == "pending_copy")  return ja ? "コマンドをコピー" : "Copy command";
        if (k == "pd_approve")    return ja ? "承認する" : "Approve";
        if (k == "pd_a1")         return ja
            ? "承認する。この提案のまま実施してよい。"
            : "Approved. Carry it out as proposed.";
        if (k == "pd_a2")         return ja
            ? "承認する。実施したら結果を報告すること。"
            : "Approved. Report back once it is done.";
        if (k == "pd_r1")         return ja
            ? "却下する。この変更は不要。"
            : "Rejected. This change is not wanted.";
        if (k == "pd_r2")         return ja
            ? "却下する。今はやらない（後で見直す）。"
            : "Rejected for now; revisit later.";
        if (k == "pd_own")        return ja ? "自分で書く" : "Write my own";
        if (k == "pd_all_answered") return ja ? "すべて回答済み" : "all answered";
        if (k == "dh_sec")        return ja ? "判断の履歴" : "Decisions made";
        if (k == "dh_exp")        return ja
            ? "答えの済んだ提案。承認したものは実施されるまでここに残り、却下したものは理由とともに残る — 同じ提案が次に出たとき、既に判断したことが読める。"
            : "Proposals that have been answered. An approved one stays here until it is carried out; a rejected one stays with its reason, so the next time it comes up the earlier decision can be read.";
        if (k == "dh_done")       return ja ? "実施済み" : "done";
        if (k == "dh_dropped")    return ja ? "却下" : "rejected";
        if (k == "dh_none")       return ja ? "まだありません" : "none yet";
        if (k == "pd_recorded")   return ja
            ? "選んだ文がそのまま台帳に記録されます。"
            : "The phrase you pick is what goes into the ledger, word for word.";
        if (k == "pd_reject")     return ja ? "却下する" : "Reject";
        if (k == "pd_approved")   return ja ? "承認済み" : "approved";
        if (k == "pd_waiting")    return ja ? "エージェントの実行待ち" : "waiting on the agent";
        if (k == "pd_ask_t")      return ja ? "この提案を承認する" : "Approve this proposal";
        if (k == "pd_ask")        return ja
            ? "承認の理由を、あなたの言葉で書いてください。ここに書いた文はそのまま台帳に記録され、"
              + "この変更の根拠になります。要約も言い換えもされません。"
            : "Say why, in your own words. What you write is recorded verbatim in the ledger as "
              + "the authorisation for this change -- not summarised, not paraphrased.";
        if (k == "pd_ask_reject") return ja
            ? "却下の理由（任意）。書いておくと、同じ提案が次に出たときに読めます。"
            : "Why not (optional). Writing it means the next time this comes up, it can be read.";
        if (k == "pd_reject_t")   return ja ? "この提案を却下する" : "Reject this proposal";
        if (k == "pd_need_words") return ja
            ? "空のままでは承認できません。あとから誰も引用できない承認は、承認ではありません。"
            : "An empty authorisation is refused: an approval nobody can quote afterwards is not one.";
        if (k == "pd_failed")     return ja ? "記録できませんでした" : "Could not record it";
        if (k == "pd_ok")         return ja ? "OK" : "OK";
        if (k == "pd_cancel")     return ja ? "やめる" : "Cancel";
        if (k == "pending_copied") return ja ? "コピーしました" : "Copied";
        // The two outcomes of an approval that DOES something, rather than only records it.
        if (k == "pd_resign_done")   return ja
            ? "再署名しました。自己改善の安全確認は新しい内容で承認され、自己改善ループは再び走れます。"
            : "Re-signed. The self-improvement check is approved as it now stands and the loop can run again.";
        if (k == "pd_resign_failed") return ja
            ? "承認は記録しましたが、再署名は実行されませんでした。理由は下記のとおりです。"
            : "The approval was recorded, but the re-signing did not run. The reason follows.";
        if (k == "auth_detail")   return ja ? "記録と取り消し" : "Records and undo";

        // section explanations (one-liner below the label)
        if (k == "usage_exp")  return ja
            ? "あなたの実際のタスクから算出。ベンチマーク不要 — 普段の完了率と傾向を示します。"
            : "From your real runs, no benchmark — shows day-to-day completion rate and trend.";
        if (k == "scorecard_exp") return ja
            ? "最新世代の解決性能のスナップショット。A/B テストの結果と採用されたスキャフォルドの数。"
            : "Snapshot of the latest generation's solving performance, the latest A/B result, and how many scaffolds have been adopted.";
        if (k == "ab_exp")     return ja
            ? "新しいスキャフォルド案を旧版と比較した結果（新しい順）。採用=緑、示唆的=黄、却下=赤。"
            : "Results of testing a new scaffold idea against the current one, newest first. Keep=green, suggestive=yellow, rejected=red.";
        if (k == "burned_exp") return ja
            ? "評価に使われたため再利用できない問題の台帳。理由別の内訳。"
            : "Problems that can no longer be reused for evaluation because they were consumed during testing — broken down by reason.";
        if (k == "trend_exp")  return ja
            ? "各改善サイクル後の pass@1 の推移（新しい順・最大24件）。 薄い行は再測定で置き換えられた測定。"
            : "Pass@1 after each improvement cycle, newest first — up to 24 points shown. Dimmed rows were replaced by a re-measurement.";
        if (k == "archive_exp") return ja
            ? "採用されたゲノム（解決スクリプトの変種）とその測定値。QDセルは問題タイプごとのスロット数。"
            : "Adopted genomes (scaffold variants) and what each measured. QD cells = slots by problem type.";

        // metric labels
        if (k == "u_completion")  return ja ? "完了率(通常)" : "Completion (ordinary)";
        if (k == "u_recent")      return ja ? "直近完了率(通常)" : "Recent (ordinary)";
        if (k == "u_split")       return ja ? "ベンチは別母集団（基底率が約10倍違うため分けて表示）" : "Benchmarks are a separate population (their base rate differs about tenfold)";
        if (k == "u_turns")       return ja ? "中央ターン数" : "Median turns";
        if (k == "u_tasks")       return ja ? "タスク数" : "Tasks";
        if (k == "u_trend")       return ja ? "完了率の推移（古い→新しい）" : "Completion trend (old → new)";
        if (k == "u_persona")     return ja ? "自我/助言の混入率（一般利用）" : "Persona/advice leak rate (live usage)";
        if (k == "u_persona_exp") return ja
            ? "ベンチ不要・実際の利用から自動計測。出力に上から目線の助言/講釈/自我が混じった割合（低いほど良い）。"
            : "No benchmark — measured automatically from real usage. Share of outputs leaking coaching/lecturing/ego (lower is better).";
        if (k == "u_persona_pre") return ja ? "計測前" : "Not yet measured";
        if (k == "u_persona_n")   return ja ? "件採点" : "scored";
        if (k == "u_persona_eg")  return ja ? "検出例" : "Flagged examples";
        if (k == "latest_pass")   return ja ? "最新 pass@1" : "Latest pass@1";
        if (k == "latest_ab")     return ja ? "最新 A/B" : "Latest A/B";
        if (k == "burned_total")  return ja ? "Burned 合計" : "Burned total";
        if (k == "archive_count") return ja ? "採用ゲノム数" : "Archive count";
        if (k == "qd_cells")      return ja ? "QD セル" : "QD cells";
        if (k == "genomes")       return ja ? "ゲノム" : "Genomes";
        if (k == "g_id")          return ja ? "ゲノム" : "Genome";
        if (k == "g_pass")        return ja ? "pass@1" : "pass@1";
        if (k == "g_gate")        return ja ? "ゲート判定" : "Gate";
        if (k == "g_desc")        return ja ? "特性" : "Descriptors";
        if (k == "g_parent")      return ja ? "親" : "Parent";
        if (k == "g_best")        return ja ? "最良 pass@1" : "Best pass@1";
        if (k == "superseded")    return ja ? "(再測定で置換)" : "(replaced by a re-measurement)";
        if (k == "measured_n")    return ja ? "測定回数" : "Measurements";
        if (k == "g_root")        return ja ? "\u2014 (起点)" : "\u2014 (root)";
        if (k == "keep")          return ja ? "採用" : "Keep";
        if (k == "none")          return ja ? "なし" : "none";
        if (k == "total")         return ja ? "合計" : "Total";
        if (k == "by_reason")     return ja ? "理由別内訳" : "By reason";
        return k;
    }

}
