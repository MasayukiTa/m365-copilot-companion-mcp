# Skill: screen-estimate-confirm (tracked copy)

> **Where this installs.** The runnable skill lives at `skills/screen-estimate-confirm/SKILL.md`
> in each agent's working tree. That `skills/` folder is gitignored by policy ("data, never
> code" -- see `.gitignore`), so this tracked copy under `docs/skills/` is the reviewable,
> version-controlled source of the procedure. When installing on an agent machine, copy this
> file to `skills/screen-estimate-confirm/SKILL.md` (same YAML front matter and body). The
> workflow it describes is `tools/screen_estimate_confirm.py`, which IS tracked and tested.

---

---
name: screen-estimate-confirm
description: "製造データの要因調査を、探索(スクリーニング)→推定→確認の順で行い、確認の主張を妥当性エンジンが正当に判定できる形にする手順。データを時間またはシードつき乱数でスクリーン集合と確認集合に分け、スクリーンで全要因を探索的に調べ、固定した規則で仮説を選び、確認データを書き出す前に主張を登録・ロックしてから確認する。『スクリーニングして確認したい』『要因を絞って因果を確かめる』『confirmatory な主張を判定できるようにする』等の依頼に使う。"
keywords: [screen estimate confirm, スクリーニング 確認, 事前登録 確認主張, hold-out 設計, 要因調査, screening confirmation, validity confirmatory, 交絡 分離, family size, 確認集合]
---

# screen-estimate-confirm: 探索→推定→確認で、確認主張を正当に判定できるようにする

## (a) これは何か・いつ使うか

製造データで「どの要因が効くか」を調べ、その中の少数を確認主張(confirmatory claim)として妥当性エンジンに判定させる手順。エンジンは、主張がデータより先に固定されたときだけ採用(GO)に達する。既にあるデータに後からスクリーニング・推定・確認をまとめてかけると、主張がデータより後に登録されるため判定は最大でも保留(HOLD)になる(理由コード `D_FAIL_LOCK_AFTER_DATA`、スクリーンで見た数値をそのまま確認すると `C_FAIL_STRADDLE`)。

この手順は、エンジンが既に受け入れる hold-out 設計に沿って、確認主張を正当に判定できるようにする。順序チェックを弱めたり偽装したりしない。

使う場面: 生の入力表があり、要因を絞って確認したいとき。
使わない場面: 既に事前登録済みの主張ID を判定するだけのとき(それは `validity-judge` を使う)。

## (b) 守る規則(固定。曲げない)

1. **生の入力表だけを使う。** 過去の結果ファイル(verdict_table.csv、runs/、以前の推定結果)を読み込んで使い回さない。この走行で数値を計算し直す。
2. **ロット単位で分割する。** スクリーン集合と確認集合は、行ではなくロットで分ける(行で分けるとロット効果が両側に漏れる)。分割は時間順、またはシードを manifest に記録したうえでの乱数で行う。
3. **確認データは後から書く。** 確認集合のファイルは、主張を登録・ロックした後にだけ書き出す。ロットの割り当てを決めた時点では確認集合の中身を見ない。
4. **スクリーンは探索モード。** スクリーン集合だけで、全候補要因を探索的(exploratory)に調べる。試した要因は全て数える(family の m に入れる)。交絡する群(例: 装置)で層別し、ロット単位で効果量と区間を出す。
5. **仮説の選択はコードで固定する。** 目で選ばない。規則は「区間が delta_min を外す要因のうち、標準化効果の絶対値が大きい順に最大 k 件」。同点は要因名で決める。
6. **登録順を偽らない。** 確認データの fetched_at は、事前登録コミットの時刻より後にする。順序を偽装したり、ロック前に確認ファイルを書いたりしない(ツールは拒否する)。
7. **family の m は全要因数。** 確認主張の family.m は、スクリーンで試した要因の総数にする。少数に絞っても多重性はそのまま持たせる。

## (c) 手順(正確に)

ツールは `tools/screen_estimate_confirm.py`。純 python(numpy のみ)で、確認は注入した judge 関数(本番はエンジンの `validity.engine.judge`)で行う。

1. 生の入力表を `Row`(lot, response, factors, group, time)の並びに読み込む。
2. `split_lots(rows, mode="time"|"random", screen_frac, seed)` でロットを分ける。乱数のときは seed を渡す(manifest に残る)。
3. `rows_for_lots(rows, manifest["screen_lots"])` でスクリーン集合を取り出す。
4. `screen(screen_rows, factors, delta_min=..., group_aware=True)` で全要因を調べる。戻り値は要因ごとの `FactorEffect`(標準化効果・区間・excludes_delta)。family の m は `len(factors)`。
5. `select_hypotheses(effects, k=...)` で確認する要因を選ぶ(固定規則)。
6. 選んだ要因ごとに `build_claim(factor, m=len(factors), delta_min, rationale, direction, ...)` で確認主張を組む(mode=confirmatory、delta_min は standardized・source=preregistration・rationale 必須)。
7. `lock_claims(claims, register_fn=...)` で、主張をハーネスの登録経路で登録・ロックする。`register_fn` は主張ファイルを書き、コミットし、ロックを作る本番の関数。戻り値の receipt に `locked_utc` が無ければ先に進めない。
8. `write_confirm_set(rows, manifest["confirm_lots"], path, lock_receipt=..., to_record=...)` で確認集合を書き出す。ロック receipt が無ければ拒否される。fetched_at は今の時刻で、事前登録より後になる。
9. `confirm(claims, confirm_path, judge_fn=engine.judge, lock_path, out_dir, root, screened=...)` で推定・確認する。戻り値は要因ごとの `ConfirmOutcome`(スクリーン効果・確認効果・verdict・理由コード)。
10. 報告する。要因ごとに: スクリーン効果、確認効果、判定(`verdict_label`: adopt / hold / not identifiable)、エンジンの理由コード、確認に至らなかったもの。

## (d) 判定の読み方

- adopt(GO): 確認集合で、事前登録した向き・大きさの効果が区間から言える。
- hold(HOLD): 情報不足、または順序・モードの不備(`D_FAIL_*`)で上限がかかった。`D_FAIL_LOCK_AFTER_DATA` が出たら、確認データが事前登録より先にあった(=手順の 3・6 を破った)。
- not identifiable(NA): 宣言した仮定のもとで量が識別されない。ロットを増やしても解けない。

GO を「証明された」「正しい」と書かない。スクリーンで効いた要因が確認で hold になるのは普通のこと(多重性と標本の分割による)で、失敗ではない。

## (e) 限界(隠さない)

- 交絡の分離は、群で層別できる交絡(例: 装置)に対してだけ効く。観測していない交絡は分離できない。
- 確認集合のロット数が少ないと、真の効果でも hold になりうる。これは標本の不足で、手順の誤りではない。
- エンジンを台帳の外で直接呼べば順序チェックは迂回できる。採用側が「ロック済みの主張を、確認集合で判定した結果であること」を採用条件にする運用が要る。
- family.m を全要因数にしても、部分集団探索(応答・曝露・ロット列・対象条件を変える探索)はこの m では数えられない。

## (f) エスカレーション

- 生の入力表が無い、または結果ファイルしか無いとき。
- delta_min の根拠を人が決めていないとき(rationale を自作しない)。
- ツールが `RegistrationOrderError` を返したとき(ロック前に確認ファイルを書こうとした)。原因を推測で回避しない。
