"""詳細設定 / Advanced -- the settings panel section added over アクセス範囲
(.fleet/folder_access.json) and 接続クライアント (.unlock_state.json via tools.security's
grant_ip/revoke_ip). These tests parse the C# source; they do not run WPF.

Run: pytest -q ui/test_fleet_cockpit_advanced_settings.py
"""
from pathlib import Path

NEWLINE = chr(10)


SOURCE = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8")


def _no_comments(text: str) -> str:
    """`//` 行を落とした本文。禁止したい呼び出しを説明するコメント自体に検査が当たり、
    守りたい不変条件が『通らない検査』として消される -- 今日それを3回やった。"""
    out = []
    for line in text.splitlines():
        if line.strip().startswith("//"):
            continue
        out.append(line.split("//")[0] if "//" in line else line)
    return NEWLINE.join(out)


def _body(start_marker: str, end_marker: str) -> str:
    start = SOURCE.index(start_marker)
    end = SOURCE.index(end_marker, start)
    return SOURCE[start:end]


def test_advanced_opens_as_a_submenu_from_the_settings_panel():
    """設定パネルの中に直接置かず、横に開く子パネルにする。

    インラインだと、フォルダ一覧と接続クライアント一覧で画面ほぼ1枚ぶん伸び、
    上にある日常の設定が押し出されて、そもそも見つけられなかった。
    """
    assert 'col.Children.Add(SectionHeader(L("詳細設定", "Advanced")));' in SOURCE
    assert "col.Children.Add(AdvancedSubmenuRow());" in SOURCE
    panel_body = _body("UIElement BuildSettingsPanel()", "\n    static string FmtFloor")
    assert "AdvancedSubmenuRow()" in panel_body
    # 本体を設定パネルへ直接足す行が復活していないこと。関数定義そのものにも同じ名前が
    # 出るので、探すのは「追加している行」に限定する。
    assert "col.Children.Add(BuildAdvancedSettingsSection());" not in SOURCE


def test_the_submenu_carries_the_advanced_section_and_can_scroll():
    body = _body("void ToggleAdvancedPopup(UIElement anchor)", "\n    UIElement BuildSettingsPanel()")
    assert "BuildAdvancedSettingsSection()" in body
    # 一覧が伸びても下端のボタンに手が届くこと
    assert "ScrollViewer" in body and "MaxHeight" in body


def test_the_submenu_does_not_dismiss_its_own_parent():
    """親は自分の視覚ツリー外のクリックで閉じる。子は別 HWND なので、
    開いた瞬間に親ごと消えるのを防ぐ必要がある。"""
    body = _body("void ToggleAdvancedPopup(UIElement anchor)", "\n    UIElement BuildSettingsPanel()")
    assert "_settingsPopup.StaysOpen = true;" in body
    assert "Closed +=" in body and "StaysOpen = false;" in body


def test_the_submenu_opens_toward_the_screen_not_off_it():
    """設定パネルは歯車に右寄せで左へ伸びる。右に出すと画面外か親の上に重なる。"""
    body = _body("void ToggleAdvancedPopup(UIElement anchor)", "\n    UIElement BuildSettingsPanel()")
    assert "PlacementMode.Left" in body


def test_the_row_is_findable_by_the_name_operators_look_for():
    """行のラベルは中身の説明なので、そのままでは 詳細設定 で引けない。"""
    body = _body("UIElement AdvancedSubmenuRow()", "\n    void ToggleAdvancedPopup")
    assert 'AutomationProperties.SetName(btn, L("詳細設定", "Advanced"))' in body


def test_access_scope_has_both_choices():
    assert 'L("アクセス範囲", "Access scope")' in SOURCE
    assert 'L("フルアクセス", "Full access")' in SOURCE
    assert 'L("指定フォルダのみ", "Selected folders only")' in SOURCE


def test_full_access_is_the_default_and_stays_one_click_reachable():
    """フルアクセス must remain selectable and stay the default -- no flip, no warning dialog."""
    assert "bool AccessEnabled()" in SOURCE
    access_enabled_body = _body("bool AccessEnabled()", "\n    List<string> AccessGlobalFolders()")
    # Missing/unreadable policy file -> false (unrestricted), matching folder_policy.py's
    # own DEFAULT-OPEN behaviour.
    assert "return false;" in access_enabled_body
    assert "_accessFullBtn.Click += delegate { SetAccessScope(false); };" in SOURCE
    assert "_accessRestrictedBtn.Click += delegate { SetAccessScope(true); };" in SOURCE
    scope_controls_body = _body("UIElement BuildAccessScopeControls()", "\n    // ── 接続クライアント")
    assert "MessageBox" not in scope_controls_body
    assert "MessageBoxResult" not in scope_controls_body


def test_choosing_full_access_never_touches_the_saved_folder_list():
    """Choosing フルアクセス writes enabled:false but must preserve 'global' so re-enabling
    指定フォルダのみ restores it, and must leave any 'scopes' map untouched."""
    set_scope_body = _body("void SetAccessScope(bool restricted)", "\n    void PaintAccessScopeButtons")
    assert 'd["enabled"] = restricted;' in set_scope_body
    # Only ever conditionally *fills in* global/scopes when entirely absent -- never
    # unconditionally overwrites either key.
    assert 'd["global"] = new object[0];' not in set_scope_body.split("if (!d.ContainsKey(\"global\"))")[0]
    assert 'if (!d.ContainsKey("global")) d["global"] = new object[0];' in set_scope_body
    assert 'if (!d.ContainsKey("scopes")) d["scopes"] = new Dictionary<string, object>();' in set_scope_body
    # And never unconditionally sets "global" outside that guard.
    unconditional = set_scope_body.split('if (!d.ContainsKey("global"))')[0]
    assert '["global"] =' not in unconditional


def test_folder_add_and_remove_only_touch_the_global_list():
    assert "void AddAccessFolder()" in SOURCE
    assert "void RemoveAccessFolder(string folder)" in SOURCE
    add_body = _body("void AddAccessFolder()", "\n    void RemoveAccessFolder(string folder)")
    assert 'd["global"] = list.ToArray();' in add_body
    remove_body = _body("void RemoveAccessFolder(string folder)", "\n    UIElement BuildAccessScopeControls()")
    assert 'd["global"] = list.ToArray();' in remove_body
    # Neither ever writes "enabled" or "scopes" -- both are read back untouched via
    # ReadFolderAccessRaw()/WriteFolderAccessRaw() round-tripping the whole dict.
    assert '["enabled"] =' not in remove_body
    assert '["scopes"] =' not in remove_body


def test_folder_picker_reuses_the_existing_pick_any_file_pattern():
    add_body = _body("void AddAccessFolder()", "\n    void RemoveAccessFolder(string folder)")
    assert "Microsoft.Win32.OpenFileDialog" in add_body
    assert "Path.GetDirectoryName(ofd.FileName)" in add_body


def test_expired_folder_or_client_state_is_never_silently_reset():
    """AccessGlobalFolders() and RefreshFolderRows() only ever read/rebuild -- restricting or
    unrestricting must never truncate the saved list."""
    assert "void RefreshFolderRows(List<string> folders)" in SOURCE


def test_connected_clients_section_exists():
    assert 'L("接続クライアント", "Connected clients")' in SOURCE
    assert "void RefreshClientRows()" in SOURCE
    assert "List<Dictionary<string, object>> ListUnlockGrants()" in SOURCE
    assert "Dictionary<string, object> ReadRefusedClient()" in SOURCE


def test_client_admin_shells_out_to_tools_security_never_edits_unlock_state_directly():
    assert '"tools.security"' in SOURCE
    assert '"tools.lock_state"' in SOURCE
    # The cockpit never opens/writes the unlock state file itself -- every line naming it is a
    # comment explaining that tools/security.py (not this file) owns the atomic write.
    mentions = [ln for ln in SOURCE.splitlines() if ".unlock_state.json" in ln]
    assert mentions, "expected at least one explanatory comment naming .unlock_state.json"
    for ln in mentions:
        assert ln.strip().startswith("//"), "non-comment reference to .unlock_state.json: %r" % ln
    assert "File.WriteAllText" not in _body("void GrantClientIp(string ip)", "\n    void RefreshClientRows()")
    grant_body = _body("void GrantClientIp(string ip)", "\n    void RevokeClientIp(string ip)")
    assert 'RunPyModule("tools.security", "grant \\"" + ip.Replace("\\"", "") + "\\""' in grant_body
    revoke_body = _body("void RevokeClientIp(string ip)", "\n    void RefreshClientRows()")
    assert 'RunPyModule("tools.security", "revoke \\"" + ip.Replace("\\"", "") + "\\""' in revoke_body


def test_refused_client_is_surfaced_at_top_with_an_allow_button_no_typing_required():
    refresh_body = _body("void RefreshClientRows()", "\n    UIElement BuildPendingClientRow(string ip)")
    assert "ReadRefusedClient()" in refresh_body
    assert 'refused.TryGetValue("client_ip", out rv)' in refresh_body
    assert "BuildPendingClientRow(refusedIp)" in refresh_body
    pending_body = _body("UIElement BuildPendingClientRow(string ip)", "\n    UIElement BuildClientRow(")
    assert 'L("直近で拒否された接続", "Most recently refused")' in pending_body
    assert 'L("許可", "Allow")' in pending_body
    # UI スレッドを止めないよう RunClientAdmin 経由になった。押して効くことが要点で、
    # 呼ぶ関数名そのものではない。
    assert 'RunClientAdmin("grant", capturedIp)' in pending_body


def test_pending_row_only_shown_when_the_refusal_is_not_already_covered_by_a_live_grant():
    refresh_body = _body("void RefreshClientRows()", "\n    UIElement BuildPendingClientRow(string ip)")
    assert "bool covered = false;" in refresh_body
    assert "if (!covered) _clientRowsPanel.Children.Add(BuildPendingClientRow(refusedIp));" in refresh_body


def test_expired_grants_are_shown_plainly_never_hidden():
    client_row_body = _body("UIElement BuildClientRow(Dictionary<string, object> g)", "\n    UIElement BuildConnectedClientsControls()")
    assert "if (expired)" in client_row_body
    assert 'L("期限切れ", "Expired")' in client_row_body
    # Expired rows still render (not filtered out of the list before this point).
    assert "foreach (var g in grants) _clientRowsPanel.Children.Add(BuildClientRow(g));" in SOURCE


def test_each_client_row_has_allow_and_revoke():
    client_row_body = _body("UIElement BuildClientRow(Dictionary<string, object> g)", "\n    UIElement BuildConnectedClientsControls()")
    assert 'RunClientAdmin("revoke", capturedIp1)' in client_row_body
    assert 'RunClientAdmin("grant", capturedIp2)' in client_row_body
    assert 'L("取り消し", "Revoke")' in client_row_body
    assert 'L("許可", "Allow")' in client_row_body


def test_remaining_validity_is_shown_in_days():
    client_row_body = _body("UIElement BuildClientRow(Dictionary<string, object> g)", "\n    UIElement BuildConnectedClientsControls()")
    assert "double days = remainingSec / 86400.0;" in client_row_body


def test_new_advanced_settings_strings_are_bilingual_via_L():
    """Every new user-visible string in the 詳細設定/Advanced section goes through the shared
    L(ja, en) helper with both languages present, never a bare literal."""
    section_body = _body("UIElement BuildAdvancedSettingsSection()", "\n    UIElement BuildConnectedClientsControls()")
    section_body += _body("UIElement BuildConnectedClientsControls()", "\n    ")
    expected_pairs = [
        ("アクセス範囲", "Access scope"),
        ("フルアクセス", "Full access"),
        ("指定フォルダのみ", "Selected folders only"),
        ("削除", "Remove"),
        ("(フォルダ未設定)", "(no folders yet)"),
        ("＋ フォルダを追加", "+ Add folder"),
        ("接続クライアント", "Connected clients"),
        ("(接続履歴なし)", "(no clients yet)"),
        ("直近で拒否された接続", "Most recently refused"),
        ("許可", "Allow"),
        ("取り消し", "Revoke"),
        ("期限切れ", "Expired"),
    ]
    for ja, en in expected_pairs:
        needle = 'L("%s", "%s")' % (ja, en)
        assert needle in SOURCE, "missing bilingual pair: %r" % (needle,)


def test_cockpit_window_has_its_own_L_helper():
    """L() previously only existed on ApprovalPromptWindow; CockpitWindow (where the settings
    panel lives) needed its own instance-scoped copy since _lang is per-class state."""
    cockpit_window_start = SOURCE.index("class CockpitWindow : Window")
    approval_prompt_start = SOURCE.index("class ApprovalPromptWindow : Window")
    assert approval_prompt_start < cockpit_window_start
    cockpit_body = SOURCE[cockpit_window_start:]
    assert 'string L(string ja, string en) { return _lang == 0 ? ja : en; }' in cockpit_body


# ---- 開いても固まらないこと -------------------------------------------------------------------

def test_the_client_list_is_not_read_on_the_ui_thread():
    """詳細設定を開くと `python -m tools.security list` と `python -m tools.lock_state show`
    が走る。この端末での実測は2秒と4秒で、どちらもパッケージの import が理由。
    これをディスパッチャ上で回すと窓ごと止まり、パネルは閉じられず、
    閉じられない WPF ポップアップは他のどのウィンドウより上に描かれ続ける。
    報告された「固まる/他の設定が押せない/常時前面」は、この1本のスレッドの話。
    """
    body = _body("void RefreshClientRows()", "void RenderClientRows(")
    assert "ListUnlockGrants()" in body and "new Thread(" in body
    assert "Dispatcher.BeginInvoke" in body
    # 取得結果を受け取って描くだけの関数は、自分では取りに行かないこと。
    render = _body("void RenderClientRows(", "\n    UIElement BuildPendingClientRow")
    assert "ListUnlockGrants()" not in render
    assert "ReadRefusedClient()" not in render


def test_grant_and_revoke_do_not_block_the_ui_thread_either():
    """一覧の構築だけ直しても、許可ボタンから同じ関数に入れば同じ止まり方をする。"""
    for line in ('allow.Click += delegate { RunClientAdmin("grant", capturedIp); };',
                 'revoke.Click += delegate { RunClientAdmin("revoke", capturedIp1); };',
                 'allow.Click += delegate { RunClientAdmin("grant", capturedIp2); };'):
        assert line in SOURCE, line
    assert "GrantClientIp(capturedIp);" not in SOURCE
    assert "RevokeClientIp(capturedIp1);" not in SOURCE


def test_the_subprocess_timeout_actually_bounds_the_call():
    """`ReadToEnd()` には時間制限が無いので、その後ろの `WaitForExit(timeoutMs)` は
    子が終わらない限り到達しない。タイムアウト引数が飾りになっていた。
    加えて stdout を読み切ってから stderr を読む順序は、子が stderr の
    パイプを埋めた時点で相互待ちになる。python はここで毎回 stderr に書く。"""
    body = _no_comments(
        _body("string RunPyModule(", "List<Dictionary<string, object>> ListUnlockGrants()"))
    assert "ReadToEnd()" not in body, "同期読みが戻っている"
    assert "BeginOutputReadLine()" in body and "BeginErrorReadLine()" in body
    assert "p.Kill()" in body, "時間切れの子を放置している"
    # 時間切れは空文字で返す -- 途中まで読めた出力を完全な応答として描かせない。
    assert 'return "";' in body


# ---- 設定パネルの3つのトグルが同じ意味に見えること ------------------------------------------------

def test_the_three_status_toggles_share_one_on_treatment():
    """「RAM自動調整: ON」だけがアクセント(オレンジ)で、隣の2つの ON は緑だった。
    同じ意味のチップが3種類の見え方をし、浮いた1つは警告に読める。
    アクセント塗りは主要アクションの開始ボタン専用で、その規約は
    PaintAutoRetryBtn のコメントに既に書かれていた。"""
    for fn, end in (("void PaintAutoToggle()", "\n    void "),
                    ("void PaintAutoRetryBtn()", "\n    void "),
                    ("void PaintAutoArchiveBtn()", "\n    void ")):
        body = _no_comments(_body(fn, end))
        assert "Theme.Success(_dark)" in body, fn
        assert "Theme.SurfaceSubtle(_dark)" in body, fn
        assert "Theme.Accent(" not in body, fn + " がアクセント塗りに戻っている"
        assert "Theme.AccentSoft(" not in body, fn
