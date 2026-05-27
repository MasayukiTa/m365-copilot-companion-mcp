# Agent Memory

エージェント（Claude / Copilot）の疑似永続記憶ストア。

## 設計思想
- **ローカル完結**: Graph API / 外部認証不要。JSONファイルのみで成立
- **人間も読める**: 必要ならエディタで直接編集・確認できる
- **階層型**: 話題（topic）/ セッションログ / 恒久事実（facts）の3層

## ディレクトリ構造
```
agent_memory/
├── index.json              # 全体インデックス（最初に読む）
├── topics/                 # 話題ノード（プロジェクト・テーマ単位）
│   └── *.json
├── sessions/               # セッション単位の要約ログ
│   └── YYYY-MM-DD_*.json
└── facts/                  # 恒久的な事実（ユーザー情報・好み）
    └── *.json
```

## 運用ルール（エージェント向け）
### セッション開始時
1. `index.json` を読む
2. ユーザー発話のキーワードから関連 topic を特定
3. 該当 `topics/*.json` を読み込んで文脈復元
4. 「前回は〜でした」と続きから入る

### セッション中
- 重要な発見・決定・成果物（artifact）パスは即座に topic JSON に追記
- 大きな話題転換が起きたら新 topic を作成

### セッション終了時
1. このセッションで生じた `key_facts` / `decisions` / `next_actions` を抽出
2. 該当 topic JSON を更新
3. `sessions/YYYY-MM-DD_<topic>.json` に生ログ要約を保存
4. `index.json` の `last_updated` を更新

## スキーマ
### topics/*.json
- topic_id, title, status (active/paused/done/archived)
- created, updated
- tags[], keywords[]
- summary（2-4行）
- key_facts[]（事実とconfidence）
- artifacts[]（成果物のパス）
- decisions[]（判断と根拠）
- open_questions[]
- next_actions[]
- related_topics[]

### facts/*.json
- ユーザープロフィール、好み、嫌うこと、関係者情報など

### index.json
- 全 topic のサマリ一覧（高速検索用）

## バージョン管理
このディレクトリは `.gitignore` 対象**外**として管理してもよい（履歴が残る）。
ただし機微情報が混入する場合は除外検討。

## ⚠️ Git運用ポリシー（重要）

このディレクトリは **`.gitignore` で実データを除外** している。

### コミットされる（追跡される）もの
- `README.md` ← このファイル
- `templates/*.json` ← 空のスキーマテンプレート

### コミットされない（無視される）もの
- `index.json` ← 実インデックス
- `facts/*.json` ← ユーザー個人情報
- `topics/*.json` ← プロジェクト固有情報（社外秘含む可能性）
- `sessions/*.json` ← セッションログ（社外秘含む可能性）

### 理由
- カスレ分析等の topic には **社外秘の品質データ・社内ファイル名** が含まれる
- `user_profile.json` には **氏名・社内メール・社員ID** が含まれる
- メモリは頻繁に更新されるため、git管理してもノイズが多い

### 新しいトピック作成時
```bash
cp templates/topic_template.json topics/<new_topic_id>.json
# 編集する
```

### 万が一コミットしてしまったら
```bash
git rm --cached agent_memory/topics/xxx.json
git commit -m "remove sensitive memory file from tracking"
```

履歴からも完全に消したい場合は `git filter-repo` か `BFG Repo-Cleaner` が必要。
