"""refuter.py -- operator B: adversarial refutation of a claimed DONE (spec 4B).

A self-reported -- and even machine-checked -- DONE can still be semantically wrong in
ways no local check covers: the goal was misread, an edge case ignored, the "fix" merely
masks the bug, the test that "passes" doesn't actually exercise the requirement. The
refuter runs a SECOND, INDEPENDENT Copilot conversation as a skeptic: given the goal and
the implementer's claimed-done summary, it must find a concrete reason the goal is NOT
met, or uphold it. A genuine refutation is fed back to the implementer so it keeps
working; an upheld verdict lets the DONE stand.

Per spec 4B this doubles oracle cost, so callers keep it OFF by default and budget-capped
(use it for high-stakes / not-machine-verifiable goals). This module holds the pure
prompt/verdict logic (unit-tested) plus run_refuter(), which drives the live side page
the same way the research/analyst delegation does.
"""
from __future__ import annotations

import os
import re

from relay import settle as _settle

REFUTER_INSTRUCTION = (
    "あなたは厳格なレビュアーです。別のエージェントが下記のゴールに対して『完了(DONE)』と"
    "報告しました。あなたの仕事は、その完了を鵜呑みにせず、ゴールが本当には達成されていない"
    "具体的な理由を全力で探すことです（ゴールの読み違い、見落とされたエッジケース、症状だけ"
    "を隠す修正、要件を実際には検証していないテスト等）。必要ならツール"
    "(read_file / grep / run_python など)で実物のファイルやテストを今すぐ確認してください。\n"
    "次の観点を順に当ててください: ①正しさ(ゴールの全要件を満たすか) "
    "②境界値・エラー処理・例外ケース ③セキュリティ・安全性。\n"
    "重要: 『確認します』『調べます』等の前置きだけで終わらせないこと。**このターン内で確認まで"
    "済ませ、必ず回答の最後の行に判定を書く**こと。形式は次のどちらか:\n"
    "・本当に達成されていない具体的欠陥が一つでもあれば: REFUTED: <その欠陥を1〜2文で>\n"
    "・全力で探しても具体的欠陥が見つからなければ: UPHELD\n"
    "・証拠不足でどちらとも判定できなければ: INCONCLUSIVE: <不足している証拠>\n"
    "推測や些末な好みではなく、ゴール未達と言える具体的根拠のみを REFUTED の理由にしてください。"
)

# Nudge sent when the reviewer answered without a clear verdict (e.g. it only said
# "I'll check the files"). Asks it to finish and emit the marker now.
REFUTER_NUDGE = (
    "確認は済みましたか。前置きは不要です。今の判定を、最後の行に "
    "REFUTED: <理由> / UPHELD / INCONCLUSIVE: <不足証拠> のいずれかを必ず1行で書いてください。"
)

# Both retry loops below (run_refuter's blocking while-loop, RefuterSession._nudge's
# non-blocking equivalent) are ALREADY bounded by max_nudges (default 2) -- but they used
# to re-send this REFUTER_NUDGE constant byte-for-byte on every retry, into the SAME
# refuter conversation. That is the identical-nudge-repetition disease (confirmed on the
# implementer's own CONTINUE loop in relay_fleet.py) applied to the refuter's side chat.
# Vary the text across attempts so no two nudges in one refuter conversation are ever
# identical; the first variant is the original REFUTER_NUDGE for back-compat.
_REFUTER_NUDGE_VARIANTS = (
    REFUTER_NUDGE,
    "まだ判定が書かれていません。前置きや再確認は不要です。今すぐ最後の行に "
    "REFUTED: <理由> / UPHELD / INCONCLUSIVE: <不足証拠> のいずれか一言だけを書いてください。",  # 二択を強制すると、証拠不足のレビュアは従わず散文で答える
    # -- そしてそれが「マーカー無し」として記録される。
)


def _next_refuter_nudge(count):
    """Pure, testable nudge-text selector for the refuter's UNCLEAR-verdict retry (1-based
    `count` = which nudge attempt this is). Rotates through _REFUTER_NUDGE_VARIANTS so
    consecutive nudges in the same refuter conversation are never byte-identical."""
    return _REFUTER_NUDGE_VARIANTS[(count - 1) % len(_REFUTER_NUDGE_VARIANTS)]


# Review panel (operator B, perspective-diverse): N INDEPENDENT reviewers, each with a
# distinct lens, aggregated by majority. Catches failure modes a single redundant pass
# misses -- a quality mechanism Claude Code does not have built in.
LENS_PROMPTS = {
    "correctness": "このレビューでは特に『正しさ』に集中: ゴールの全要件を実際に満たしているか。",
    "edge": "このレビューでは特に『境界値・エラー処理・例外・想定外入力』に集中。",
    "security": "このレビューでは特に『セキュリティ・安全性』に集中: インジェクション、"
                "権限、破壊的操作、機微情報の漏えい等。",
    # The 'auto' effort gate. The dominant observed failure is OVER-ENGINEERING -- a 40+ line
    # diff (or an extra touched file) where the correct fix is 2-7 lines -- which the generic
    # reviewers UPHELD. This lens makes the reviewer hunt for exactly that, and only that, so a
    # cheap minimal solve is accepted but a bloated / wrong-target one is refuted (-> escalate).
    "minimality": "このレビューでは『最小性と正しさ』だけに集中して反証せよ。差分が"
                  "**必要以上に大きい/余計なファイルや関数を触っている/根本原因でない箇所を"
                  "変えている**なら、それは過剰修正＝欠陥として REFUTED にせよ。正しい修正は"
                  "通常ごく小さい（数行）。逆に、変更が最小で根本原因に的確に当たっていれば "
                  "UPHELD。『動くが過剰』も REFUTED 扱い（最小の正しい差分に絞らせる）。",
    # The 'auto' gate -- GENERAL (domain-agnostic) form. Checks the answer is minimal AND complete
    # AND actually satisfies the ORIGINAL request, for ANY task (research, summarization, M365, ...).
    # Coding tasks swap in 'rootcause_code' (below) via the domain flag. Keeping this lens general is
    # why effort modes (min/max/ultra/auto) stay orthogonal to task type -- a non-coding task must
    # not be reviewed with code-specific criteria.
    "rootcause": "このレビューでは『要求への最小かつ完全な回答か』を反証せよ。次のどれか一つでも"
                 "当たれば REFUTED:\n"
                 "(1) 過剰: 要求されていない事柄を足している/冗長/的外れな付加がある。\n"
                 "(2) 過小・的外れ: **元の要求(指示)を読み直し**、求められた事項のどれかが**実際には"
                 "満たされていない**。それらしく見えるだけで要件未達なら欠陥。\n"
                 "(3) 不完全: 要求が複数の部分（全項目・両方向・対象すべて・各観点）を含むのに一部しか"
                 "答えていない。\n"
                 "最小で、かつ要求の全要件を実際に満たしていれば UPHELD。",
    # The 'auto' gate -- CODING form (selected when the task domain is coding). From the 3-miss
    # analysis: the minimality lens alone is blind to UNDER-fixing (1 of 2 hunks; producer fixed but
    # consumers not; wrong root cause). Checks BOTH over- and under-fixing for code.
    "rootcause_code": "このレビューでは『根本原因への最小かつ完全な修正か』を反証せよ。次のどれか一つでも"
                 "当たれば REFUTED:\n"
                 "(1) 過剰: 差分が必要以上に大きい/余計なファイル・関数を触る/根本原因でない箇所を変える。\n"
                 "(2) 過小・的外れ: 問題文の症状を再現する独立ケースを頭の中で（または可能なら実際に）"
                 "走らせたとき**まだ症状が残る**。『既存テストが通った』は証拠にならない（バグ発生前から"
                 "通っていたはず）。\n"
                 "(3) 不完全（複数箇所）: 変更したデータ契約（キー名・戻り値・レコード形）を**書く側と"
                 "読む側の両方**、または同じメソッド族（例 `_eval_rewrite_as_X` と対の `_Y`、producer と"
                 "consumer）の**対になる箇所**、または mutate した状態を**コピー/clone する箇所**・"
                 "同じバグを持つ**兄弟メソッド**のどれかが未修正のまま。1箇所だけ直して対を放置していないか。\n"
                 "(4) 層違い: 症状が**現れる**呼び出し側（caller）を直しただけで、症状が**通過する共有の"
                 "定義**（callee＝設定プロパティ・正規化関数・dispatch/eval 経路）が未修正。テストは"
                 "通常 callee を直に叩くので、caller だけの修正は落ちる。\n"
                 "(5) 抑制 vs 表出: 要求が警告・メッセージ・戻り値の**表出**なのに、例外を握り潰す/黙って"
                 "抑制している（『続行しつつ正しい診断を出す』契約を満たさず、消すだけになっている）。\n"
                 # class 5 lens clause, gated by SWE_FIX_RADIUS (default ON) so an A/B can isolate it
                 + ("(6) 修正半径違い: 上流の dispatch/routing/consumer/便宜API で症状を消しただけで、契約が"
                 "定義された**最下層（プリミティブ/共有定義）が未修正**、または古い挙動をフォールバックで温存し、"
                 "退化・矛盾入力（空/ゼロ/負/None/未確定）での新しい契約を満たさない。\n"
                 if os.environ.get("SWE_FIX_RADIUS", "1") != "0" else "")
                 # class: producer fixed but a DOWNSTREAM consumer/re-emitter still carries the OLD shape.
                 # If you change WHAT a producer emits, every downstream function that consumes or
                 # RE-EMITS the old shape must be updated or be a no-op -- a producer fix that leaves a
                 # stale downstream emitter is incomplete (creates duplicates/contradictions).
                 + "(7) 下流取りこぼし: producer（生成側）が出す形を直したのに、その出力を**消費する/古い形のまま"
                 "再出力する下流関数**がそのまま残っている。生成側の出力を変えたなら、その値を消費・再出力する"
                 "下流関数を**すべて列挙**し、各々が更新済みか、もはや no-op になっているかを確認せよ。古い形を"
                 "まだ吐く下流エミッタを放置した producer 修正は不完全（重複・矛盾を生む）。\n"
                 + "上のいずれも無く、独立再現ケースが通り、関連する全ての箇所が直っていれば UPHELD。",
}
#: Extra settle windows granted to a reply that has not produced a verdict yet, before the
#: session decides the reviewer is refusing to commit and nudges it. Measured: the real
#: verdict arrived two seconds after the early accept, so one window is already enough; two
#: is for a reviewer running several tool calls.
MARKER_WAITS = 2

PANEL_LENSES = ("correctness", "edge", "security")


def build_refuter_prompt(goal: str, final_response: str, lens: str = "") -> str:
    """Compose the adversarial reviewer prompt from the goal and the implementer's
    claimed-done summary. `lens` (one of LENS_PROMPTS) focuses a panel reviewer."""
    base = REFUTER_INSTRUCTION
    if lens and lens in LENS_PROMPTS:
        base = LENS_PROMPTS[lens] + "\n" + base
    return (
        base
        + "\n\n--- ゴール ---\n" + (goal or "").strip()
        + "\n\n--- 実装エージェントの最終報告 ---\n" + (final_response or "").strip()
        + "\n--- ここまで ---"
    )


def aggregate_panel(results, min_refute=None):
    """Aggregate a panel of (lens, kind, reason) verdicts into one (kind, reason).

    REFUTED only when at least `min_refute` reviewers refute (default: strict majority),
    so a lone over-eager reviewer can't block, but a real defect that several lenses see
    does. The combined reason names which lenses objected. Anything short of the threshold
    is UPHELD (we never trap the loop on a minority/ambiguous objection).
    """
    n = len(results)
    if n == 0:
        return ("UNCLEAR", "harness: the panel produced no verdicts to aggregate")
    refuted = [(l, r) for (l, k, r) in results if k == "REFUTED"]
    if min_refute is None:
        min_refute = (n // 2) + 1
    if len(refuted) >= min_refute:
        reason = " / ".join("[%s] %s" % (l, r) for (l, r) in refuted)
        return ("REFUTED", reason)
    return ("UPHELD", "")


#: A reason that starts with this prefix means the review could not be CONDUCTED -- no page,
#: no context, an exception -- as opposed to a reviewer that looked and could not decide. The
#: fleet ignores the distinction and is right to: UNCLEAR means "do not block" either way.
#: Anything MEASURING the reviewers must not, because scoring a policy against a lens that was
#: never asked credits it with a clean result it did not obtain.
HARNESS_FAULT_PREFIX = "harness: "


def unclear_is_harness_fault(reason) -> bool:
    """True when an UNCLEAR came from the harness rather than from the reviewer."""
    return str(reason or "").startswith(HARNESS_FAULT_PREFIX)


def parse_verdict(text: str):
    """Parse a refuter reply into (kind, reason).

    kind is "REFUTED" (with a non-empty reason), "UPHELD", or "UNCLEAR". We look for an
    explicit REFUTED:/UPHELD marker (the prompt demands one on the last line); a bare
    "REFUTED" with no reason, or no marker at all, is UNCLEAR -- callers treat UNCLEAR as
    "do not block" so ambiguous output can never trap the loop forever.
    """
    if not text:
        return ("UNCLEAR", "the reviewer's reply was empty")
    # an explicit "REFUTED: <reason>" anywhere wins
    for line in text.splitlines():
        m = re.search(r"REFUTED\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m and m.group(1).strip():
            return ("REFUTED", m.group(1).strip())
    for line in text.splitlines():
        m = re.search(r"INCONCLUSIVE(?:\s*[:：]\s*(.*))?", line, re.IGNORECASE)
        if m:
            return ("INCONCLUSIVE", (m.group(1) or "").strip())
    up = text.upper()
    if "UPHELD" in up:
        return ("UPHELD", "")
    if "REFUTED" in up:               # marker present but no concrete reason given
        return ("UNCLEAR", "the reply says REFUTED but gives no reason to act on")
    return ("UNCLEAR", "the reply carries no verdict marker")


def agent_base_url(conversation_url: str) -> str:
    """The bare agent URL (a fresh chat) from a conversation URL -- navigating here starts
    an INDEPENDENT conversation, which is what makes the refuter a separate skeptic rather
    than a continuation of the implementer's own chat."""
    if not conversation_url:
        return ""
    return conversation_url.split("/conversation/", 1)[0]


def run_refuter(context, conversation_url: str, goal: str, final_response: str,
                notify=None, runlog=None, run_id: str = "relay", turn: int = 0,
                timeout_s: int = 600, max_nudges: int = 2, lens: str = ""):
    """Open an independent side-page Copilot chat and ask it to refute the claimed DONE.
    Returns (kind, reason). Never raises into the control loop -- any failure yields
    ("UNCLEAR", "") so the loop falls back to accepting the DONE. Mirrors the research/
    analyst side-page delegation."""
    from .copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver
    base = agent_base_url(conversation_url)
    if context is None or not base:
        return ("UNCLEAR", "harness: no browser context or no agent url")
    page = None
    try:
        page = context.new_page()
        page.goto(base, wait_until="domcontentloaded", timeout=45000)
        appeared = False
        for _ in range(40):
            page.wait_for_timeout(1000)
            if page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                appeared = True
                break
        if not appeared:
            return ("UNCLEAR",
                    "harness: the composer never rendered within 40s of loading the page")
        drv = CopilotWebDriver(page)
        drv.send(build_refuter_prompt(goal, final_response, lens=lens))
        ok = drv.wait_for_idle(timeout_s=timeout_s)
        verdict = (parse_verdict(drv.read_last_response()) if ok else
                   ("UNCLEAR", "harness: the reviewer did not settle within %ds" % timeout_s))
        # the reviewer often answers a preamble first ("I'll check the files") -- nudge it
        # to actually emit the verdict, like the implementer needs a CONTINUE.
        nudges = 0
        while verdict[0] == "UNCLEAR" and nudges < max_nudges:
            nudges += 1
            drv.send(_next_refuter_nudge(nudges))
            if not drv.wait_for_idle(timeout_s=timeout_s):
                break
            verdict = parse_verdict(drv.read_last_response())
    except Exception as exc:
        verdict = ("UNCLEAR", "harness: the review raised %s" % type(exc).__name__)
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
    if runlog is not None:
        try:
            runlog(run_id, {"turn": turn, "event": "refute",
                            "verdict": verdict[0], "reason": (verdict[1] or "")[:200]})
        except Exception:
            pass
    return verdict


#: Prefix on the reasons where UNCLEAR means "this session could not ask the question",
#: as opposed to "the reviewer was asked and did not produce a verdict". Both are UNCLEAR to
#: the fleet, which is right -- neither blocks. They are opposites to anything measuring the
#: reviewers: the second is an observation about a lens, and the first is a hole where an
#: observation should be. A prefix rather than a lookup table, so a new exit that forgets to
#: classify itself reads as a reviewer outcome only if its author wrote it that way.
HARNESS_REASON_PREFIX = "harness: "


def unclear_is_harness_fault(reason) -> bool:
    """True when an UNCLEAR came from the session, not from the reviewer."""
    return str(reason or "").startswith(HARNESS_REASON_PREFIX)


class RefuterSession:
    """Non-blocking refuter for the FLEET (single-thread round-robin): a blocking
    side-page wait would freeze every other worker for minutes. start() opens the side
    chat and sends the refuter prompt (a brief one-off, like opening any tab); poll()
    then checks for the verdict without blocking -- returning None while the reviewer is
    still thinking, or (kind, reason) when its answer stabilises. Mirrors the worker's own
    send/wait state machine. Never raises; any failure yields ("UNCLEAR", "")."""

    def __init__(self, context, base_url, goal, final_response,
                 dwell_s=4.0, timeout_s=600, max_nudges=2, lens="",
                 max_network_reopens=2):
        self.context = context
        self.base_url = base_url
        self.goal = goal
        self.final = final_response
        self.lens = lens               # panel reviewer focus (correctness/edge/security)
        self.dwell_s = dwell_s
        self.timeout_s = timeout_s
        self.max_nudges = max_nudges
        self.page = None
        self.drv = None
        self._count_before = 0
        self._t_send = None
        self._last = None
        self._stable_since = None
        self._settle_state = _settle.SettleState()
        self._nudges_used = 0
        self._pending_open = True  # side-page not opened yet -- deferred until there's free RAM
        self._done = None          # verdict tuple once finished
        self._network_reopens = 0
        self.max_network_reopens = max_network_reopens

    def start(self):
        # Defer the side-page open until poll() sees enough free RAM (ram_room_for_tab) -- the
        # same RAM-gating ResearchSession uses, so the ultra pipeline's sub-agent tabs never crowd
        # a low-RAM box into the sweep-wedge + watchdog hard-reset failure mode.
        import time
        self._pending_open = True
        self._t_send = time.time()
        return self

    def _do_open(self):
        import time
        from .copilot_autopilot_relay import (
            COPILOT_SELECTORS, CopilotWebDriver, _is_network_failure,
            _page_network_available,
        )
        try:
            if self.context is None or not self.base_url:
                self._finish(("UNCLEAR", HARNESS_REASON_PREFIX + "no browser context or no agent url: the session was constructed without a target"))
                return
            self.page = self.context.new_page()
            self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=45000)
            appeared = False
            for _ in range(40):
                self.page.wait_for_timeout(1000)
                if self.page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                    appeared = True
                    break
            if not appeared:
                self._finish(("UNCLEAR", HARNESS_REASON_PREFIX + "the composer never rendered within 40s of loading the agent page"))
                return
            self.drv = CopilotWebDriver(self.page)
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            self.drv.send(build_refuter_prompt(self.goal, self.final, lens=self.lens))
            self._pending_open = False
            self._t_send = time.time()
        except Exception as exc:
            if (_is_network_failure(exc)
                    or (self.page is not None and not _page_network_available(self.page))):
                self._schedule_network_reopen("open failed: %s" % type(exc).__name__)
            else:
                self._finish(("UNCLEAR", HARNESS_REASON_PREFIX + "opening the side page failed: %s: %s" % (type(exc).__name__, str(exc)[:160].replace(chr(10), " | "))))

    def _schedule_network_reopen(self, reason):
        """Close the stale side page and retry the same read-only refuter prompt later."""
        import sys
        import time
        if self._network_reopens >= self.max_network_reopens:
            self._finish(("UNCLEAR", HARNESS_REASON_PREFIX + "the network-reopen budget ran out while the browser reported offline"))
            return
        self._network_reopens += 1
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        self.page = None
        self.drv = None
        self._count_before = 0
        self._last = None
        self._stable_since = None
        self._settle_state = _settle.SettleState()
        self._pending_open = True
        # Reset the timeout window: time spent disconnected is not reviewer latency.
        self._t_send = time.time()
        sys.stderr.write("[refuter] NETWORK_RETRY %d/%d: %s\n" %
                         (self._network_reopens, self.max_network_reopens, reason))

    def poll(self):
        """None while the reviewer is still answering; else (kind, reason)."""
        import time
        from .copilot_autopilot_relay import _is_processing, _page_network_available
        if self._done is not None:
            return self._done
        # RAM-gated lazy open (bounded by timeout); returns quickly so the sweep stays responsive.
        if self._pending_open:
            from .relay_fleet import ram_room_for_tab
            if self._t_send and time.time() - self._t_send > self.timeout_s:
                # RAM-STARVED SKIP: the side-page never got a tab within timeout_s, so this
                # review did NOT run -- the candidate is accepted unreviewed (effective effort
                # downgrade for THIS instance). Emit a distinct marker so a benchmark can COUNT
                # how many instances were degraded by tab pressure (vs a genuine reviewer UNCLEAR)
                # and re-run just those at a lower concurrency. Only fires after a full timeout_s
                # (default 600s) of never clearing the ram_room floor -- i.e. RAM jammed for 10min.
                import sys
                sys.stderr.write("[refuter] RAM_SKIP: no tab within %ds, review SKIPPED (instance "
                                 "solved without refutation)\n" % int(self.timeout_s))
                self._finish(("UNCLEAR", HARNESS_REASON_PREFIX + "no tab within %ds: the RAM floor for another page never cleared" % int(self.timeout_s)))
                return self._done
            if not ram_room_for_tab():
                return None
            self._do_open()
            return None
        if self.drv is None:
            return self._done
        try:
            if self.page is not None and not _page_network_available(self.page):
                self._schedule_network_reopen("browser reported offline during review")
                return self._done
            if self._t_send and time.time() - self._t_send > self.timeout_s:
                self._finish(("UNCLEAR", HARNESS_REASON_PREFIX + "the reviewer did not answer within %ds" % int(self.timeout_s)))
                return self._done
            if self.drv._answers().count() <= self._count_before:
                return None
            t = self.drv.read_last_response()
            if _settle.unified():
                # THE WEAKEST OF THE FOUR, and the one with the most to gain. This loop has
                # no sample requirement and no marker concept at all: it waits one dwell and
                # commits. A refutation still being written can therefore be parsed as a
                # verdict, and this session is the SELECTOR in best-of-N -- the component
                # this project's own notes call the ceiling on output quality.
                #
                # The marker is "a verdict can be extracted". That is the right analogue of
                # a protocol tail here: a reply whose verdict does not parse is either
                # preamble or unfinished, and both want the longer settle. The existing
                # UNCLEAR-then-nudge path is unchanged and still runs after acceptance --
                # it answers a DIFFERENT question ("the model would not commit") from the
                # one the marker asks ("has the text stopped moving").
                state = getattr(self, "_settle_state", None) or _settle.SettleState()
                state, outcome = _settle.settle_step(
                    state, t, now=time.time(), dwell_s=self.dwell_s,
                    # THE STOP BUTTON IS NOT A RELIABLE "FINISHED" SIGNAL HERE, measured: it
                    # disappears BETWEEN tool calls, so a reviewer that is reading files looks
                    # idle for a second or two with a tool-status line as its whole reply.
                    # Passing the real value would not have prevented the early accept that
                    # motivated this change -- the page genuinely was not generating.
                    generating=False,
                    is_processing=_is_processing(t),
                    has_marker=lambda text: parse_verdict(text)[0] != "UNCLEAR")
                self._settle_state = state
                self._last, self._stable_since = state.last, state.stable_since
                if outcome != _settle.ACCEPT:
                    return None
                if getattr(self.drv, "_is_stale_repeat", lambda _t: False)(t):
                    return None
                verdict = parse_verdict(t)
                if verdict[0] == "UNCLEAR" and self._nudges_used < self.max_nudges:
                    # WAIT BEFORE NUDGING, because the measured failure was committing two
                    # seconds early and then asking again. `has_marker` doubles the settle
                    # requirement for a markerless reply, but doubling still ACCEPTS, so a
                    # reviewer mid-way through its tool calls gets nudged instead of finished.
                    # A nudge is for a model that will not commit; it is the wrong answer to a
                    # model that simply has not finished, and it discards the real verdict --
                    # the completed reply is never read, only the answer to the nudge is.
                    #
                    # So: give the page one more settle window and re-read first. Bounded by
                    # the same clock as everything else, so a genuinely silent reviewer still
                    # reaches the nudge, and then the timeout.
                    waited = getattr(self, "_marker_waits", 0)
                    if waited < MARKER_WAITS:
                        self._marker_waits = waited + 1
                        self._settle_state = _settle.SettleState()
                        self._last, self._stable_since = None, None
                        return None
                    self._nudge()
                    return None
                accept = getattr(self.drv, "_accept_new_reply", None)
                if callable(accept):
                    accept(t)
                self._finish(verdict)
                return self._done
            if _is_processing(t):
                self._last, self._stable_since = None, None
                return None
            if t == self._last:
                # LEGACY: one dwell, no samples, no marker doubling. Left exactly as it was
                # -- the gated path above is the change under test.
                if self._stable_since and (time.time() - self._stable_since) >= self.dwell_s:
                    # This poll loop bypasses CopilotWebDriver.wait_for_idle -- apply the
                    # same cross-turn correspondence guard directly (see its docstring): a
                    # settled text byte-identical to the PREVIOUS turn's accepted answer on
                    # this driver is the stale-capture signature, not a fresh verdict. Keep
                    # waiting; still bounded by self.timeout_s above.
                    if getattr(self.drv, "_is_stale_repeat", lambda _t: False)(t):
                        return None
                    verdict = parse_verdict(t)
                    # preamble-only answer ("I'll check...") -> nudge for the verdict
                    if verdict[0] == "UNCLEAR" and self._nudges_used < self.max_nudges:
                        self._nudge()
                        return None
                    accept = getattr(self.drv, "_accept_new_reply", None)
                    if callable(accept):
                        accept(t)
                    self._finish(verdict)
                    return self._done
                return None
            self._last, self._stable_since = t, time.time()
            return None
        except Exception:
            self._finish(("UNCLEAR", HARNESS_REASON_PREFIX + "reading the reviewer's reply raised"))
            return self._done

    def _nudge(self):
        import time
        self._nudges_used += 1
        try:
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            self.drv.send(_next_refuter_nudge(self._nudges_used))
            self._t_send = time.time()
            self._last, self._stable_since = None, None
            # A nudge is a NEW turn. Carrying stability across it would let the settle
            # accumulated on the preamble count toward accepting the answer to a question
            # that had not been asked yet.
            self._settle_state = _settle.SettleState()
        except Exception:
            self._finish(("UNCLEAR", "the nudge budget ran out without a parseable verdict"))

    # EVERY EXIT SAYS WHY. UNCLEAR is returned both by a reviewer that looked and could not
    # decide and by a page that never opened, and for a long time those were the same
    # ("UNCLEAR", "") from eight places. The fleet reads only the verdict, so it cannot tell
    # and does not need to -- it treats UNCLEAR as "do not block" either way. Anything
    # MEASURING the reviewers does need to: three consecutive corpus collections came back
    # empty, and the reason they took three runs to explain rather than one is that the
    # sessions were silent about which of the eight had happened.
    def _finish(self, verdict):
        self._done = verdict
        self.close()

    def close(self):
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        self.page = None
