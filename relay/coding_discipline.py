"""coding_discipline.py -- the shared production coding-discipline instruction block.

This is the single source of truth for the strengthened red->green / minimal-and-complete
diff discipline that gets appended to a self-verifying coding goal (one that carries a
verification `checks` gate). It was lifted INLINE out of code_task.build_goal so that the
several production entry points that emit such goals (code_task, folder_coder single mode)
share one verbatim block instead of drifting copies.

It is mirrored conceptually from bench/swe_batch_setup.py's SWE_STRONG_SELFTEST +
SWE_MINIMALITY blocks, but de-SWE-specialized for general project tasks: UNLIKE the
benchmark there is NO hidden held-out test here -- the acceptance gate IS the project's
own auto-detected tests (`checks`), so running the project's failing/relevant tests is
fully legitimate and is what we anchor red->green on. The benchmark scaffold text in
bench/ is intentionally NOT touched by this refactor (it owns its own A/B baseline).
"""
from __future__ import annotations


def coding_discipline_text() -> str:
    """Return the strengthened coding-discipline block (Japanese), verbatim.

    Caller appends this to a goal's text ONLY when the goal has a verification gate
    (`checks` non-empty): a no_verify task has nothing to run red->green against, so
    demanding it would be noise. The returned string begins with a leading "\\n\\n" so it
    concatenates cleanly onto the end of an existing goal body.
    """
    return ("\n\n【自己検証＝red→green を徹底】まず**編集する前に**、報告された問題を再現する小さな"
            "スクリプト/テストを書き、未修正コードで run_python か shell_exec で実行して**実際に失敗する**"
            "ことを確認せよ。その同じ再現が**通るまで**修正を続け、通ってから DONE。仕上げに、変更箇所を"
            "覆うこのプロジェクト自身のテストも実行し、回帰が無いことを確認する（ここでの受け入れ基準は"
            "隠しテストではなくプロジェクト自身のテストなので、それを実行するのが正しい）。\n"
            "再現は**報告に書かれた通りのオブジェクト・入力・シグネチャ（リテラルなクラス名・引数・呼び出し形）"
            "で組み立て**よ。似て非なる代用テストではなく、報告されたそのシナリオを動かすこと。さらに"
            "**期待される完全な出力をアサート**せよ——『例外が出ない』『N passed』では不十分で、正しい値・"
            "文字列・形まで突き合わせる。\n"
            "再現が通っても、データが流れる**全ての地点（生成→整形→消費 / producer→formatter→consumer）を"
            "漏れなく編集していなければ完全性は証明できていない**。報告されたシナリオを実際には動かさない既存の"
            "グリーンなテスト群は、何の証拠にもならない。\n"
            "**自分の再現テストを緩めて通すのは禁止**——通すためにアサーションを書き換えたくなったら、"
            "それは修正が間違っているサイン。テストではなく実装を直せ。\n"
            "\n【最小かつ完全な差分】正しい修正は通常ごく小さい（多くは数行）。**根本原因に的確に当たる"
            "最小の差分**だけを書け（余計なリファクタ・防御的コード・無関係な整形は過剰修正の元）。"
            "ただし**小さすぎ・的外れも同じく欠陥**——最小性と引き換えに**完全性**を落とすな:\n"
            "・データ契約（キー名・戻り値・レコード形）を変えたら**書く側と読む側の両方**を直す"
            "（producer だけ直して consumer を放置しない）。\n"
            "・あるメソッド/変換/分岐を足したら**対になる方向**（例 `X→Y` を足したら `Y→X` も）が"
            "同じ変更を要しないか確認する。状態を mutate する箇所を直したら、その状態を**コピー/clone "
            "する箇所**や同じバグを持つ**兄弟メソッド**も確認する。\n"
            "・同じ不具合パターンがファイル内の複数箇所にあれば grep で全て直す。\n"
            "\n【症状の出口でなく源を直す】症状が**現れる**呼び出し側（最初に症状が出る caller）ではなく、"
            "症状が**通過する共有の定義**（設定プロパティ・正規化関数・dispatch/eval 経路）を直せ。"
            "直す前に、その記号が本当に問題の経路で使われている方かを症状→源へ辿って確かめる"
            "（caller でなく callee を直す）。\n"
            "\n【握り潰す vs 正しく表出する】『例外を消す』のと『続行しつつ正しい診断を出す』は別物。"
            "要求が警告・メッセージ・戻り値の**表出**なら、それを**出す**修正にせよ（黙って抑制しない）。\n"
            "\n【最下層を直接叩く＝修正半径を合わせる】候補修正を、単一例が通っただけで採用するな。"
            "その修正が支配する**最も低レベルの操作を、高レベルの入口経由でなく直接呼ぶ**再現を自作し、"
            "さらに(a)退化・矛盾入力（空・ゼロ長・負・None・真偽値・未確定/簡約不能なケース）と、"
            "(b)『バグを直さず迂回・フォールバックで誤魔化しただけなら落ちる』対照を足し、これらも通すこと。"
            "最後にその挙動の**正準的な定義箇所（どの層）**を一行で述べ、編集がその層にあるか確認せよ。"
            "上流の dispatch が壊れた経路を避けているせいで再現が通っているだけなら、修正をプリミティブまで下げよ。"
            "\n【レビュー指摘は調査してから対応・検証済みの編集を捨てるな】レビュアーが反証(refute)してきたら、"
            "まず**調査してから**動け。すでに red→green で検証した編集を、それを**否定する具体的な失敗アサーションを"
            "名指しできない限り**、git checkout 等で破棄・巻き戻ししてはならない。反証は『最初からやり直せ』の合図"
            "ではなく、**さらに詰めろ**という指し示しである。")
