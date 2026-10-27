# -*- coding: utf-8 -*-
"""The running/waiting/stopped card carries the SAME "⋮" kebab as a completed card.

## 要件（オーナー 2026-10-27 16:22）

「完了タスクの右上に・・・てのがあるやん。あれを実行中のにもつけられればここで
優先度変えたりなにか細かいことしたりができるんじゃないの」

## このテストが固定すること

WPF のレンダーには生の GUI が要るので、他の cockpit ソーステストと同様、
ソースレベルの決定をここで固定する。

1. STATE -> ITEMS は 1 か所（``BuildCardMenu``）だけ。新しい操作はその表に 1 行足す。
2. 完了・実行中・待機・停止・ランタイム待ちの全カードが同じスロットで ``CardKebabBtn`` を得る。
3. 実行中の項目は実在する操作だけで組む（成物を開く・止める・履歴へ）。加えて新規に
   「最優先にする / 優先を解除」（本体は priority_boost、ここは呼び出し口のみ）と
   「進まない理由を表示」（status/reason/next_step を整形）がある。
4. メニューは押された時に作る（ContextMenu の中身を遅延生成）。

``FleetCockpit.*`` は tools.cockpit_source がパッケージを解決するので、Card の
姉弟テストと同じく、この作業ツリーの ui/ を名前で読む（utf-8-sig）。
"""
import os

UI = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8-sig") as fh:
        return fh.read()


def _build_card_menu(card_src):
    """The body of BuildCardMenu, from its signature to the brace that closes the method.

    A brace counter that skips strings/char-literals/comments, so a '{' inside a string in a
    menu label cannot end the slice early."""
    i = card_src.index("CardMenuModel BuildCardMenu")
    j, depth, seen = i, 0, False
    n = len(card_src)
    while j < n:
        c = card_src[j]
        d = card_src[j + 1] if j + 1 < n else ""
        if c == "/" and d == "/":
            k = card_src.find("\n", j)
            j = n if k < 0 else k
            continue
        if c == "/" and d == "*":
            k = card_src.find("*/", j + 2)
            j = n if k < 0 else k + 2
            continue
        if c == '"':
            j += 1
            while j < n and card_src[j] != '"' and card_src[j] != "\n":
                j += 2 if card_src[j] == "\\" else 1
            j += 1
            continue
        if c == "{":
            depth += 1
            seen = True
        elif c == "}":
            depth -= 1
            if seen and depth == 0:
                return card_src[i:j + 1]
        j += 1
    raise AssertionError("BuildCardMenu has no closing brace")


# ── (1) one table, not a branch per state ────────────────────────────────────

def test_state_to_items_lives_in_one_method():
    src = _read("FleetCockpit.Card.cs")
    assert src.count("CardMenuModel BuildCardMenu") == 1, \
        "state->items must be in exactly one place; a second builder splits the table"
    # Card() must not reintroduce a per-state menu branch: the old closed||terminal kebab branch
    # and the always-visible release button are gone.
    assert "else if (closed || terminal)" not in src
    assert "relBtn.Content = MakeReleaseContent();" not in src


# ── (2) every card gets the same kebab ─────────────────────────────────────

def test_the_kebab_is_added_for_every_card():
    src = _read("FleetCockpit.Card.cs")
    assert "var menu = BuildCardMenu(w, name, goal, conv, status, reason, closed, terminal);" in src
    assert "right.Children.Add(CardKebabBtn(menu.Labels.ToArray(), menu.Actions.ToArray(), w));" in src
    # The add is NOT nested under a closed/terminal or running-only guard: the block that adds it
    # sits at the same level as the runtime-wait Resume block (which only ADDS a button beside it).
    add_at = src.index("var menu = BuildCardMenu(")
    guard_region = src[src.index("if (isLocalRuntimeWait)"):add_at]
    # between the runtime-wait block and the kebab add there is no new state guard
    assert "else if (closed" not in guard_region
    assert "if (closed || terminal)" not in guard_region


def test_runtime_wait_keeps_resume_and_also_gets_the_kebab():
    src = _read("FleetCockpit.Card.cs")
    rt = src[src.index("if (isLocalRuntimeWait)"):src.index("var menu = BuildCardMenu(")]
    assert "ResumeLocalLoopRuntime(runtimeWaitWorker)" in rt, \
        "runtime-wait card keeps its explicit one-click Resume button"


# ── (3) the active-state item set: real actions + the two new items ──────────────────

def _active_branch(card_src):
    body = _build_card_menu(card_src)
    return body[body.index("// Active card"):]


def test_active_card_items_are_real_actions_only():
    active = _active_branch(_read("FleetCockpit.Card.cs"))
    # reveal artifacts -> RevealPath (exists); stop -> RequestClose (exists); history -> ArchiveAndHide
    assert 'T("reveal_artifacts")' in active
    assert "RevealPath(firstArtifact)" in active
    assert 'T("stop_task")' in active
    assert "RequestClose(nm);" in active
    assert 'T("to_history")' in active
    assert "ArchiveAndHide(wt);" in active


def test_priority_toggle_sends_the_fleet_command():
    src = _read("FleetCockpit.Card.cs")
    active = _active_branch(src)
    assert "_priorityBoostEnabled" in active
    assert 'on ? T("boost_clear") : T("boost_set")' in active
    assert "IsBoosted(wt)" in active
    assert "RequestSetPriorityBoost(nmb, !on)" in active
    assert 'ShowScaleToast(on ? T("boost_clear_done") : T("boost_set_done"))' in active


def test_why_waiting_formats_status_reason_next_step():
    src = _read("FleetCockpit.Card.cs")
    assert 'T("why_waiting")' in _active_branch(src)
    show = src[src.index("void ShowWhyWaiting"):]
    show = show[: show.index("\n    }")]
    for key in ('"status"', '"reason"', '"next_step"'):
        assert "S(w, %s)" % key in show, "ShowWhyWaiting must read %s from the worker" % key
    # when none of the three is present it shows the explicit none-text, not a guess
    assert 'T("why_waiting_none")' in show


def test_the_new_strings_are_translated_both_ways():
    s = _read("FleetCockpit.Strings.cs")
    for key in ("stop_task", "why_waiting", "why_waiting_title", "why_waiting_none",
                "make_priority", "clear_priority", "priority_pending"):
        assert ('k == "%s"' % key) in s, "missing translation for %s" % key


# ── (4) the menu body is built lazily, on click, not for 330 cards up front ─────────────

def test_the_context_menu_materializes_on_first_open_not_at_build():
    src = _read("FleetCockpit.CardToolbar.cs")
    kebab = src[src.index("UIElement CardKebabBtn"):]
    kebab = kebab[: kebab.index("\n    }\n}")]
    # the ContextMenu is opened from the button's Click handler -- it is not shown eagerly
    assert "btn.ContextMenu.IsOpen = true;" in kebab
    assert "btn.Click += delegate" in kebab
    # nothing opens the menu at construction time
    assert "cm.IsOpen = true" not in kebab
    # the ITEMS are built in the menu's Opened handler, guarded so re-opening does not rebuild --
    # so with ~330 cards on screen no card materializes its MenuItems until first opened.
    assert "cm.Opened += delegate" in kebab, \
        "menu items must be built on Opened, not eagerly at card build time"
    assert "if (menuBuilt) return;" in kebab
    assert "menuBuilt = true;" in kebab
    # the item loop (cm.Items.Add) sits AFTER the Opened handler begins, not before it
    assert kebab.index("cm.Opened += delegate") < kebab.index("cm.Items.Add")


def test_per_item_actions_fire_on_click_not_at_build():
    src = _read("FleetCockpit.CardToolbar.cs")
    kebab = src[src.index("UIElement CardKebabBtn"):]
    kebab = kebab[: kebab.index("\n    }\n}")]
    # each item's delegate runs inside its MenuItem.Click, so BuildCardMenu returning labels+
    # actions does not execute any action until the user clicks it.
    assert "mi.Click += delegate" in kebab
    assert "{ a(); }" in kebab
