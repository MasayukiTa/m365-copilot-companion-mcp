# ファンアウト木の予算: total と wall_min を「実測」に直す

ジョブ fanbudget。origin/main の worktree (fix/fanout-budget-measurement)。
対象は relay/fanout_budget.py の usage_from_status()。実測の根拠は本番の
`.fleet/`(読み取りのみ。コーディネータ/コックピットは停止していない)。

## 症状(実測 .fleet/status.json の tree_budget)

- スクリーンショットの root c234d1e0a6436: total 28(上限 24, 赤)、wall_min 525.11(上限 120, 赤)、
  active 0・turns 0・known true。すなわち「終了済みの木」なのに wall が 525 分。
- 終了済みなのに時計が暴走: c219044f97381 wall_min 41426.92(約28.8日)、c30fcc13deb6a 41616.77、
  c573b542742de 21814.1、c1c6347ac010b 8697.16。いずれも active 0・turns 0。
- c7e01b58b1956: total 128(known false)。
- 生きている木 cd1d08b153c03: wall_min 417.13・active 6(稼働中に 120 を超過)。

## 原因(修正前のコード)

1. total が「過去の全 split の campaign ヘッダ n の累積和」だった。n は子が終わっても減算されない
   ため、年を重ねた終了済みの木が total 28 や 128 と、もう存在しない worker 数を報告していた。
2. wall_min = (now - first_header_ts)/60 に 終端(end bound)が無かった。木が終了しても now との差を
   数え続けるので、最初のヘッダから経過した実時間(最大 28.8 日)をそのまま分で出していた。

両者とも「木が終わってもゼロに戻らない/止まらない」ことが本質で、終了済みの木が赤く出続けて
ネストした split を不当に拒否していた。

## 実測した分布(根拠データ)

.fleet/campaigns.jsonl(24MB)をストリーム解析。campaign ヘッダ 653 件: n は min 2 / max 12 /
mean 4.83 / median 4 / p90 8 / p95 9 / p99 12。既定 total=24・MAX_CHILDREN=12 は p99=12 の
1 split 分 + マージ予約 + 余裕を満たし、第1 split を一切絞らない(tests が全 split サイズで証明)。

## 修正

### total — 実際に走った worker を数える

ran(その木に属する worker 行の数)を基本とし、campaign ヘッダ n は FLOOR として次の 2 例だけに
効かせる: (a) 木が live でまだ worker になっていない子がキューにある、(b) worker 行がまだ 1 件も
無い(ran==0 — 台帳だけで split を判断する段階)。終了済みで実際に走った木(ran>0・live でない)は
ヘッダ和で底上げしない。これで終了済みの木の total が過去 split 分だけ膨らむことが無くなる。

### wall_min — アクティブ期間を測り、終了で凍結

phase_events から各 worker の active span(started=最初に tab を持った=入場待ち以外の最初のイベント、
ended=最後の TERMINAL)を取り、木の wall_min を「最初の worker の start → 最後の worker の end」とする。
全 worker が終端に達したら時計は最後の end で凍結し、now に対して増え続けない(525 分 /41183 分の木)。
いずれかが live の間は end=now。

per-worker の時刻が無い場合のフォールバック:
- live、または worker 行がまだ無い木(ran==0, 台帳だけで split 判断)→ header start → now。
  これはアジアした header-only の木に wall 上限を効かせ、古い root へのネスト split を fail-safe に
  拒否するために必要(0.0 にすると上限をすり抜ける)。
- worker が走って終わった木(ran>0・live でない)→ 0.0(not running)。

known は「数えた worker の turn が読めない」または「live な木の start が読めない」ときだけ False。

## 実データ形状での検証

- 終了済みで実走した木(ヘッダ n=28、worker 2 件が各60秒で終了): 修正後 total 2 / wall_min 3.33
  (旧: total 28 / wall_min 41417)。時計は凍結。
- header-only の木(worker 行なし、ran==0、ヘッダ ts が 28.8 日前): total 28 / wall_min 41417 を
  維持 — これは「これから split する木」を台帳だけで評価している状態で、wall 上限が働いて古い root の
  ネスト split を拒否する(意図した非対称)。

## テスト

tests/test_fanout_budget.py の 2 アサーションを新しい実測セマンティクスに更新:
- rows_from_workers が started/ended を持つこと(phase_events からの span)。
- 終了済みで実走した木(b)の wall_min は 0.0(凍結)。

tests/test_fanout_budget.py + test_fanout_budget_large_ledger.py は 70 passed、
関連する relay/tests のファンアウト群は 112 passed。origin/main に対する差分は
relay/fanout_budget.py と tests/test_fanout_budget.py の 2 ファイルのみ。
