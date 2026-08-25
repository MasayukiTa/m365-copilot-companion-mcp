from pathlib import Path


SOURCE = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8")


def test_run_mode_is_not_mislabeled_as_operation_approval():
    assert 'if (k == "run_mode") return ja ? "実行方式" : "Run mode";' in SOURCE
    assert '_approvalLbl.Text = T("run_mode")' in SOURCE
    assert '"承認センター、未処理 "' in SOURCE


def test_approval_center_reads_durable_gates_even_when_idle():
    assert 'Directory.GetFiles(dir, "gate_*.json")' in SOURCE
    assert 'List<Dictionary<string, object>> gates = PendingGates(root);' in SOURCE
    assert 'UpdateGateBanner(root);' in SOURCE
    assert 'if (idle)' in SOURCE


def test_high_impact_approval_requires_a_second_confirmation():
    assert "GateNeedsSecondConfirmation" in SOURCE
    assert "MessageBoxButton.YesNo" in SOURCE
    assert "MessageBoxResult.No" in SOURCE
    assert "Approve all" not in SOURCE


def test_approval_center_has_accessibility_and_audit_history():
    assert 'AutomationProperties.SetName(_approvalCenterBtn' in SOURCE
    assert 'AutomationProperties.SetName(approve' in SOURCE
    assert 'gd["answered_at"] = NowUnix();' in SOURCE
    assert '"最近の判断"' in SOURCE


def test_actionable_notification_prompt_can_answer_without_opening_chat():
    assert 'args[0].Equals("--approval-gate"' in SOURCE
    assert "class ApprovalPromptWindow : Window" in SOURCE
    assert '_approve.Click += delegate { Answer("approved"); }' in SOURCE
    assert '_deny.Click += delegate { Answer("denied"); }' in SOURCE
    assert "M365CompanionApprovalPrompt" in SOURCE


def test_confirmation_auto_and_bypass_are_visible_and_persistent():
    assert '"default")' in SOURCE
    assert '"auto")' in SOURCE
    assert '"bypass")' in SOURCE
    assert "job_approval_mode=" in SOURCE
    assert "ApprovalPromptWindow.SavePolicy(next)" in SOURCE


def test_approval_prompt_uses_the_shared_theme_and_live_preferences():
    assert "Theme.Bg(_dark)" in SOURCE
    assert "Theme.Surface(_dark)" in SOURCE
    assert "Theme.Accent(_dark)" in SOURCE
    assert "Theme.UiFont" in SOURCE
    assert "UiPreferencesChanged()" in SOURCE


def test_unfinished_run_banner_has_persistent_close_button():
    assert 'IconButton("close", 14' in SOURCE
    assert "DismissResumeState(capturedSignature)" in SOURCE
    assert "cockpit_resume_dismissed.json" in SOURCE
    assert "ResumeStateSignature()" in SOURCE
    assert "ResumeStateDismissed(resumeSig)" in SOURCE
    assert 'rd["signature"] = resumeSig' in SOURCE


def test_health_repairs_have_immediate_busy_feedback():
    assert "HealthState.Checking" in SOURCE
    assert "BuildSpinner(10)" in SOURCE
    assert 'BuildFixPillContent(_fixRunning)' in SOURCE
    assert 'T(busy ? "hs_fixing_button" : "hs_fix")' in SOURCE
    assert "_fixTargetMask" in SOURCE
    assert "_healthWake.Set()" in SOURCE
    assert 'kind == "checking" || kind == "starting"' in SOURCE
    assert 'Path.GetDirectoryName(Path.GetFullPath(_statusPath))' in SOURCE


def test_tab_configuration_lives_in_settings_and_header_is_runtime_only():
    assert 'ctrls.Children.Add(AutoscaleControls())' not in SOURCE
    assert 'col.Children.Add(AutoscaleControls())' in SOURCE
    assert 'T("autoscale") + ": " + (_autoscale ? "ON" : "OFF")' in SOURCE
    assert '_workerChipBorder.Visibility = Visibility.Collapsed;' in SOURCE
    assert 'UpdateWorkerChip(openTabs, liveCap, runningNow);' in SOURCE
    assert '"タブ " + open + "/" + cap' in SOURCE


def test_gate_banner_cannot_starve_the_window_of_its_scroller():
    """The gate banner is docked, so it sits outside the card list's ScrollViewer --
    the only scroller in the cockpit. Left unbounded it took whatever height it
    wanted: measured with three pending Skill gates at 1.5x zoom, the health strip
    and counters had zero height, the list viewport collapsed to nothing, and
    controls landed at y=1199 on a 1080-tall screen. With no scroller left, an
    Approve/Deny row pushed past the bottom edge could not be reached at all.
    """
    # The banner owns a bounded scroller rather than hosting the cards directly.
    assert "ScrollViewer _gateScroll;" in SOURCE
    assert "_gateScroll.Content = _gateCardsPanel;" in SOURCE
    assert "_gateBanner.Child = _gateScroll;" in SOURCE
    # ...re-capped every tick, in the units the banner is actually measured in.
    assert "_gateScroll.MaxHeight = Math.Max(150, usable * 0.45);" in SOURCE
    assert "(ActualHeight > 0 ? ActualHeight : 760) / zoom" in SOURCE


def test_gate_banner_shows_a_context_preview_not_the_whole_manifest():
    """A Skill gate's context is a multi-line file/hash manifest. Printed in full it
    pushed the Approve/Deny row below the fold; the complete text stays available
    in the Approval Center (which scrolls) and as a tooltip."""
    assert "static string GateContextPreview(string context)" in SOURCE
    assert "ctxTb.Text = GateContextPreview(context2);" in SOURCE
    assert "ctxTb.ToolTip = context2;" in SOURCE
    assert "if (kept.Count == 2) break;" in SOURCE


def test_approval_windows_are_sized_against_the_work_area_and_the_zoom():
    """Both approval surfaces are measured in unscaled units while the cockpit runs
    at a zoom, so the work area must be divided by that zoom before clamping --
    otherwise the footer holding Approve/Deny lands past the bottom of the screen."""
    assert "double ReadUiScale()" in SOURCE
    assert "Width = Math.Min(600, Math.Max(360, workW / zoom - 60));" in SOURCE
    assert "Height = Math.Min(620, Math.Max(360, workH / zoom - 60));" in SOURCE
    assert "w.Width = Math.Min(760, Math.Max(480, waW / scale - 60));" in SOURCE
    assert "w.Height = Math.Min(720, Math.Max(380, waH / scale - 60));" in SOURCE
    assert "w.MaxHeight = waH;" in SOURCE


def test_cached_history_search_box_is_detached_before_re_parenting():
    """HistoryHeader() runs again whenever the list re-realizes the header row, but
    _histSearchBox is cached so focus and caret survive a re-filter. Wrapping a
    still-parented element in a fresh Border throws InvalidOperationException, and
    because the call sits inside a binding converter nothing catches it -- WPF
    aborted the process and the cockpit died mid-run (three times in one session).
    """
    # BOTH parent kinds. The box moved from a bare Border into a Grid (it shares a cell with its
    # placeholder now), and a Decorator cast returns null for a Panel -- so checking only the
    # Decorator path would have gone on passing while the crash came back.
    assert "LogicalTreeHelper.GetParent(_histSearchBox) as Decorator" in SOURCE
    assert "LogicalTreeHelper.GetParent(_histSearchBox) as Panel" in SOURCE
    assert "prevDec.Child = null;" in SOURCE
    assert "prevPanel.Children.Remove(_histSearchBox);" in SOURCE
    # The detach must come before the new wrapper is built, not after.
    detach = max(SOURCE.index("prevDec.Child = null;"),
                 SOURCE.index("prevPanel.Children.Remove(_histSearchBox);"))
    rewrap = SOURCE.index("searchStack.Children.Add(_histSearchBox);")
    assert detach < rewrap


def test_autoscale_toggle_is_painted_when_it_is_built():
    """The settings panel rebuilds AutoscaleControls() on every open, but only the Click
    handler used to call PaintAutoToggle -- the control that sets the button's label and
    colours. So the RAM auto-adjust toggle first rendered as a blank pill, and the only
    way to see its state was to click it, which flipped the setting."""
    body = SOURCE[SOURCE.index("UIElement AutoscaleControls()"):]
    body = body[:body.index("\n    }")]
    assert "group.Children.Add(_autoToggle);" in body
    paint = body.index("PaintAutoToggle();", body.index("group.Children.Add(_autoToggle);"))
    ret = body.index("return group;")
    assert paint < ret, "must paint before returning, not only from the Click handler"
    assert body.index("UpdateAutoEnabled();", paint) < ret


def test_gate_banner_lives_in_the_run_column_not_across_the_window():
    """Docked on the root DockPanel the banner spanned the whole window, so once the
    timeline spine column opened the banner's box overhung it -- crossing the spine's
    frame instead of lining up with the cards it refers to."""
    assert "col1.Children.Add(BuildGateBanner());" in SOURCE
    assert "root.Children.Add(BuildGateBanner());" not in SOURCE
    # ...on the SAME gutter the card list uses -- compared against each other rather than against
    # a number typed here, because the number is what went stale when the spacing scale landed
    # while the property the test exists to protect (they line up) was never in danger.
    import re as _re
    gutter = _re.search(r"_list\.Padding = new Thickness\((\d+),", SOURCE).group(1)
    assert ("_gateBanner.Margin = new Thickness(%s, 6, %s, 6);" % (gutter, gutter)) in SOURCE
    assert ("_pinnedToolbarHost.Padding = new Thickness(%s, 6, %s, 0);" % (gutter, gutter)) in SOURCE
