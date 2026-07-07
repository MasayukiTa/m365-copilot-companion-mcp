# Loop Engineering Spec — 過剰修正を起こさない完了/検証ループの設計規範

対象: Claude Code (agentic coding CLI) およびそれに準ずる LLM エージェント。
目的: `実装 → 検証 → 再修正 → 検証 → ... → 完了` の反復系を、
      発散(doom loop)・過剰修正(overcorrection)・偽完了(premature done)
      いずれにも陥らせずに収束させるための運用ルール。
著者スタンス: 妥協なし。曖昧な指示は棄却させる。verifier がない仕事は着手させない。

---

## 0. First Principle (これを外すと以下すべて無意味)

> **生成器と検証器を分離せよ。検証は外部の決定論的シグナルに帰着させよ。**

- 生成した LLM が自分の出力を「良さそう」と判定するのは、統計的に自己一致するだけであって、
  正しさの証拠ではない。Huang et al. (2023) と Stechly et al. (2024) の実証結論。
- したがって、以下の 4 種類のシグナルのうち **最低 1 つ** が pass/fail を返せる仕組みが
  存在しない仕事は、原則としてループを回してはならない。
  1. コンパイラ / 型チェッカ / リンタの exit code
  2. テストスイート (unit / integration / property-based / snapshot)
  3. スクリプト化された差分検査 (fixture diff, golden output, screenshot pixel diff)
  4. 明示的な受け入れ基準 (Given/When/Then 形式、または箇条書きの検収条件)
- 上記が用意できないタスクは、まず「検証器を書くタスク」に分解する。
  Anthropic 公式の Claude Code best practices もこれを "Give Claude a way to
  verify its work" として第一原則に据えている (code.claude.com/docs, 2026)。

---

## 1. ループの標準構造 (Canonical Loop)

```
[0] INTAKE     : 要求を acceptance criteria (AC) に翻訳
[1] PLAN       : AC を満たす最小変更のプランを作る
[2] IMPLEMENT  : プランの1ステップを実装
[3] VERIFY     : 外部シグナル (tests/build/lint/diff) を実行
[4] DECIDE     : 下記 4分岐に従って次を選ぶ
    (a) 全AC pass かつ副作用なし  -> [5] EXIT (完了)
    (b) 一部 AC fail、原因が明白    -> [2] へ (通常反復)
    (c) 一部 AC fail、原因が不明    -> [1] へ (再計画)
    (d) fail が振動 or 予算超過     -> [6] ESCALATE (人間に返す)
[5] EXIT       : 差分・テスト結果・未達 AC(あれば)を提示して停止
[6] ESCALATE   : 状態スナップショットと停止理由を提示して停止
```

**このループの成否を決めるのは [0] INTAKE と [3] VERIFY の質。** [2] の巧拙ではない。

---

## 2. 過剰修正(overcorrection)が生まれる 6 つの機序

過剰修正とは「テストが通ったにもかかわらず、あるいは通ったコードに対して、
LLM が主観的な `もっと良くできる` 判断でリファクタや追加変更を続け、
結果として本来の要件外の変更が混入し、テストが再び壊れる or 差分が肥大する」現象。
以下を認識しなければ対策できない。

1. **Verifier collapse**: 生成者=検証者による自己肯定バイアス
   → 対策: producer/critic を prompt レベルで分離、可能なら別モデル。
2. **Reward proxy hacking**: テストを通すこと自体が目的化し、
   テストの方を壊す/緩めることで通す (Krakovna et al. 2020 の specification gaming)。
   → 対策: テストファイルは編集禁止対象に指定、fixture の hash を pin。
3. **Sunk-cost drift**: 長い trajectory の後、要件から離れた方向に慣性で進む。
   → 対策: 各ループ先頭で AC を再読させる (later described as "AC reprint" rule)。
4. **Confirmatory rechecking**: 通ったテストを何度も verify し直して確信を稼ぐが、
   その間に無関係な "improvement" 変更が入り込む (2026年時点で複数論文が指摘)。
   → 対策: 一度 pass した AC は "frozen" とマークし、再検証は差分が触った範囲のみ。
5. **Context rot**: コンテキストが肥大化するにつれ、初期の制約や指示を忘れる
   (Anthropic Claude Code docs が明示的に警告している現象)。
   → 対策: 予算に応じて `/compact` 等で圧縮、または CLAUDE.md に不変制約を外部化。
6. **Over-eager refactor**: `while I'm here...` 症候群。触ってはいけない箇所を
   親切心で書き換え、テストされていない副作用を持ち込む。
   → 対策: 変更許可スコープ(allow-list of paths)を事前宣言、diff budget を課す。

---

## 3. 完了 (Definition of Done) — 曖昧許容ゼロ

以下すべてを満たしたときにのみ [5] EXIT を許可する。

- [ ] すべての AC が対応する verifier で pass している。
- [ ] 変更差分が事前宣言した allow-list パス群に収まっている。
- [ ] 変更差分の行数が事前宣言した diff budget 内である
      (デフォルト目安: AC 1件あたり 50〜200 LoC、超過時は要理由)。
- [ ] 既存テストが 1 件も新規に fail していない (回帰なし)。
- [ ] リンタ / 型チェッカ / build が exit 0。
- [ ] 追加した依存関係が承認済み (新規追加は原則 escalate)。
- [ ] `TODO` `FIXME` `XXX` を新規に残していない、または残した理由を出力に明記。
- [ ] ランダム性が絡む場合、seed を固定して 3 回連続 pass を確認済み。

一つでも欠ければ **未完了として次ループへ回るか、[6] ESCALATE**。
「たぶん動いています」「多分大丈夫です」で完了宣言することは禁止。

---

## 4. 停止条件 (Termination Rules) — 発散を強制的に切る

過剰修正の逆の失敗、すなわち無限ループ / 発散を止めるためのハードストップ。
これらは verifier の質と独立に働く安全網である。

### 4.1 予算ベース (Budget-based)

| 予算 | デフォルト | 超過時 |
|------|-----------|--------|
| 反復回数 (loops) | 5 | [6] ESCALATE |
| 累積 tool call 数 | 60 | [6] ESCALATE |
| 総編集ファイル数 | 15 | 事前宣言時のみ許可 |
| 総 diff LoC | 800 | 事前宣言時のみ許可 |
| ウォールクロック時間 | 20 min | [6] ESCALATE |

数値はチームごとに調整可。**ただし着手前に必ず宣言し、変更は人間承認必須**。

### 4.2 パターンベース (Pattern-based, 振動検知)

以下のいずれかが検知されたら **即** [6] ESCALATE。「もう一度試す」は禁止。

- 同一ファイルの同一関数を 3 回以上編集している。
- 直近 2 回のループで失敗したテスト集合が完全一致している (進捗ゼロ)。
- 直近 2 回のループで edit の diff が意味的に打ち消し合っている (revert loop)。
- テストファイルを編集しようとした (テストが AC の写像である場合、原則禁止)。
- 「これは実装上の制約で不可能」など、AC を否定する説明が 2 回連続で出た
  (真に不可能なら人間の再定義が必要 = escalate 案件)。

### 4.3 収束ベース (Convergence-based)

pass 数が単調非減少で 2 ループ連続して不変、かつ diff が非空
→ 「テストが通ってるのに改変を続けている」= 過剰修正の典型。**即 EXIT** に切り替え。

---

## 5. 検証器の設計 (Verifier Design)

ここが全ての質を決める。手を抜くと 1〜4章 が絵に描いた餅になる。

### 5.1 検証器のヒエラルキー (信頼度高 → 低)

1. **決定論的な外部プログラム**: コンパイラ、型チェッカ、テストランナー、
   fixture の bit-exact diff、CIパイプラインの exit code。→ 最優先で用意。
2. **半決定論的検査**: property-based test (Hypothesis, QuickCheck),
   fuzzing, mutation testing, snapshot test。数値誤差は tolerance を明示。
3. **LLM-as-judge**: rubric を **事前に文字列で固定**、複数回サンプリングして
   多数決 (self-consistency)。生成モデルとは別モデル or 別 prompt 空間で。
   → 単独で使わず、必ず 1 or 2 と併用。
4. **人間レビュー**: escalate 時のみ発火。ループ内には置かない (レイテンシ破綻)。

### 5.2 検証器が満たすべき性質

- **決定論性 (determinism)**: 同じ入力に対して同じ結果を返す。乱数は seed 固定。
- **完全性 (completeness) の宣言**: 「これが pass すれば AC を満たす」ことを
  設計者が言い切れるか。言い切れないなら AC が未成熟、[0] INTAKE に戻る。
- **タイト性 (tightness)**: 過剰に緩いテストは reward hacking を許す。
  過剰に厳しいテストは実装非依存な内部表現に依存し、正しい変更をも壊す。
- **速度**: verify が 30 秒以内で回ることを目標。長い場合は smoke test 層を追加。

### 5.3 producer/critic の分離実装 (Reflexion 系, Producer-Critic)

- Reflexion (Shinn et al. 2023) の枠組みは有効だが、critic が producer と
  同一モデル・同一 prompt の場合は confirmation bias を増幅する。
- 実務上は次のいずれかを取る:
  - critic を **別プロセス** として起動し、コード diff と test output だけを
    渡す (実装コンテキストを持たせない)。
  - critic には **rubric チェックリスト** のみを与え、自由記述の "improvement
    suggestion" は出させない (過剰修正の温床)。
  - critic の出力は **structured JSON** に強制し、`pass|fail|needs_info` の 3 値。

---

## 6. INTAKE (Acceptance Criteria の起こし方)

ここを曖昧にすると、後段のすべてが崩れる。過剰修正は多くの場合、AC の欠落を
LLM が「善意で埋めた」結果である。以下のテンプレートで受け取る:

```markdown
## Task
<1文で目的>

## Acceptance Criteria (must)
- [AC-1] <観測可能な条件>. Verifier: <どのコマンド/テストで pass 判定するか>
- [AC-2] ...

## Non-goals (must NOT do)
- <触ってはいけないもの、変えてはいけない挙動>

## Change scope
- allow_paths: [glob, ...]
- deny_paths:  [glob, ...] (default: tests/**, .github/**, migrations/**)
- diff_budget_loc: <int>
- max_new_deps: <int>  (default: 0)

## Budget
- max_loops: 5
- max_tool_calls: 60
- max_wall_time_min: 20
```

**入力に上記が欠けている場合、Claude Code は着手前に不足項目を列挙して停止すること。
推測で埋めない。**

---

## 7. ループ内で守るべき運用規則

### 7.1 各ループ先頭で必ず行う

1. AC を **原文のまま** 再出力する (context rot 対策)。
2. 直近ループで pass した AC を `[frozen]` としてマーク。
3. 残作業 (fail している AC のみ) をリスト化。
4. 今回のループで触るファイルを **事前宣言**。宣言外への編集は禁止。

### 7.2 各ループ末尾で必ず行う

1. verifier を実行し、raw output を保持。
2. pass/fail の差分を前ループと比較 (振動検知)。
3. 累積 diff LoC と修正ファイル数を予算と照合。
4. §4 の停止条件を評価。該当すれば EXIT または ESCALATE。

### 7.3 禁止事項 (violation は即 ESCALATE)

- verifier の実装を書き換える (テストを緩める、fixture を差し替える等)。
- allow_paths 外の編集。
- 「原則としては動くはず」で verifier 実行をスキップして完了宣言する。
- テスト失敗の原因調査なしに再実装する (最低 1 回はログ/スタックを読む)。
- 未使用コードを "cleanup" 名目で削除する (scope外変更)。
- コミットメッセージだけで挙動変更を報告し、実際の差分と一致させない。

---

## 8. 起動時のシステムプロンプト雛形 (Claude Code 向け)

CLAUDE.md または `-p` オプションに以下を含める。

```
You operate under the Loop Engineering Spec (docs/loop-engineering.md).
Before starting any non-trivial task:
1. Parse the task against the INTAKE template. If any of {AC, Non-goals,
   Change scope, Budget} is missing or ambiguous, STOP and ask.
2. Do not begin editing until you have identified at least one external
   verifier per AC (test, build, lint, or scripted diff). If no verifier
   exists, write the verifier first as a separate step.
3. At the top of every iteration: reprint AC verbatim, mark frozen ACs,
   declare which files you will touch this iteration.
4. At the end of every iteration: run verifiers, compare pass/fail set
   to previous iteration, check budgets, evaluate termination rules.
5. Never edit files under deny_paths. Never modify tests to make them pass.
   Never claim done without verifier evidence in the transcript.
6. If you detect oscillation, budget exhaustion, or an AC that appears
   unachievable, ESCALATE with a state snapshot. Do not "try one more time."
7. Prefer minimal diffs. `while I'm here` refactors are prohibited unless
   listed as an AC.
```

---

## 9. アンチパターン早見表

| 症状 | 根本原因 | 直し方 |
|------|---------|--------|
| テストは通るが要件と違う | AC が観測可能条件で書かれていない | INTAKE を書き直す |
| 何ターンも同じテストを直している | verifier が implementation-dependent | テストを behavior-based に |
| 差分が肥大化する | scope 宣言なし、diff budget なし | §6 のテンプレを強制 |
| self-refine で性能が下がる | producer=critic の自己肯定 | critic を分離 (§5.3) |
| 「多分動きます」で終わる | verifier 未実行のまま完了判定 | §3 の DoD 強制 |
| ループが振動する | AC 同士が矛盾している | escalate、AC を再交渉 |
| 依存パッケージが勝手に増える | max_new_deps 未設定 | INTAKE で 0 デフォルト |
| tests/ を書き換えて通した | reward hacking | deny_paths に tests/** |
| 完了後にリファクタで壊れる | over-eager refactor | 完了宣言後の追加編集を禁止 |

---

## 10. 参考文献 (実在確認済み)

以下は 2026-07 時点で arXiv / 公式ページで内容を確認したものだけを載せている。
番号や著者名は原本と一致することを検証済み。

### 反復ループの基盤
- Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., Cao, Y.
  (2022). *ReAct: Synergizing Reasoning and Acting in Language Models*.
  arXiv:2210.03629. ICLR 2023. https://arxiv.org/abs/2210.03629
- Shinn, N., Cassano, F., Berman, E., Gopinath, A., Narasimhan, K., Yao, S.
  (2023). *Reflexion: Language Agents with Verbal Reinforcement Learning*.
  arXiv:2303.11366. NeurIPS 2023. https://arxiv.org/abs/2303.11366
- Madaan, A., Tandon, N., Gupta, P., et al. (2023). *Self-Refine: Iterative
  Refinement with Self-Feedback*. arXiv:2303.17651. NeurIPS 2023.
  https://arxiv.org/abs/2303.17651
- Wang, G., Xie, Y., Jiang, Y., et al. (2023). *Voyager: An Open-Ended
  Embodied Agent with Large Language Models*. arXiv:2305.16291.
  https://arxiv.org/abs/2305.16291
  (skill library + iterative self-verification prompting の実例)

### 自己修正の限界 (過剰修正/verifier collapse の根拠)
- Huang, J., Chen, X., Mishra, S., Zheng, H. S., Yu, A. W., Song, X.,
  Zhou, D. (2023). *Large Language Models Cannot Self-Correct Reasoning Yet*.
  arXiv:2310.01798. ICLR 2024. https://arxiv.org/abs/2310.01798
- Stechly, K., Valmeekam, K., Kambhampati, S. (2024). *On the Self-Verification
  Limitations of Large Language Models on Reasoning and Planning Tasks*.
  arXiv:2402.08115. https://arxiv.org/abs/2402.08115

### 検証器 gaming / reward hacking
- Krakovna, V., Uesato, J., Mikulik, V., Rahtz, M., Everitt, T., Kumar, R.,
  Kenton, Z., Leike, J., Legg, S. (2020). *Specification gaming: the flip
  side of AI ingenuity*. Google DeepMind Blog.
  https://deepmind.google/blog/specification-gaming-the-flip-side-of-ai-ingenuity/

### コーディングエージェントの実務基盤
- Jimenez, C. E., Yang, J., Wettig, A., Yao, S., Pei, K., Press, O.,
  Narasimhan, K. (2023). *SWE-bench: Can Language Models Resolve Real-World
  GitHub Issues?* arXiv:2310.06770. ICLR 2024.
  https://arxiv.org/abs/2310.06770
- Yang, J., Jimenez, C. E., Wettig, A., Lieret, K., Yao, S., Narasimhan, K.,
  Press, O. (2024). *SWE-agent: Agent-Computer Interfaces Enable Automated
  Software Engineering*. arXiv:2405.15793. NeurIPS 2024.
  https://arxiv.org/abs/2405.15793
- Anthropic. *Best practices for Claude Code*.
  https://code.claude.com/docs/en/best-practices
  ("Give Claude a way to verify its work" 節が本 spec の §0 First Principle と
  対応する)

---

## 11. 実装チェックリスト (テンプレとして copy して使う)

タスク着手時:
- [ ] AC を観測可能な条件で列挙した
- [ ] 各 AC に対応する verifier コマンドがある
- [ ] allow_paths / deny_paths を宣言した
- [ ] diff_budget_loc と max_loops を宣言した
- [ ] Non-goals を明記した

各ループで:
- [ ] AC を先頭で再出力した
- [ ] frozen AC をマークした
- [ ] 触るファイルを事前宣言した
- [ ] 末尾で verifier を実行した
- [ ] pass/fail 差分を前回と比較した
- [ ] §4 の停止条件を評価した

完了宣言時:
- [ ] §3 の DoD すべて満たした
- [ ] verifier の raw output を提示した
- [ ] 差分ファイルリストと LoC を提示した
- [ ] 予算内で完了したことを数値で示した

以上。
