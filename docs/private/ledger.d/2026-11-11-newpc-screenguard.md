## 2026-11-11 - Screen input is refused unless the named window is in front

- Change: `screen_click`, `screen_type` and `screen_press` (and the underlying
  `desktop_input.click`/`type_text`/`press`) now take an `expect` argument naming the
  application the action is meant for -- a process image name (e.g. `notepad.exe`) or a
  window-title pattern. Immediately before any synthetic input is sent, the live foreground
  is re-read (`window_probe.foreground_facts()`, which names the front window's title, class,
  owning process and rectangle) and the action is judged by `tools/foreground_guard.evaluate`.
  The input is sent only when the front window belongs to `expect` and -- for a click -- the
  target point lies inside that window's rectangle; otherwise `InputRefused` is raised and
  nothing is sent.
- Behaviour: a terminal/console in front is never accepted unless it is itself named by
  `expect`, which is exactly the class of window that intercepted the clicks on the new PC
  (the look computed a point for a mail/chat window, a terminal came to the front between the
  look and the click, and the click landed in the terminal / an unrelated window). With no
  `expect` the action is refused unless the operator opts out with
  `MCP_SCREEN_ALLOW_ANY_FOREGROUND=1` (off by default); the opt-out is exposed as a settings
  surface in `.env.example` only, deliberately NOT added as a live key in `settings_keys.KEYS`
  so the `test_a_setting_declares_when_it_takes_effect.py` follower-watch invariant is not
  tripped.
- Tests: `tools/test_a_click_lands_in_the_window_it_was_meant_for.py` (14 cases, hermetic --
  the judgement is a pure function over plain foreground facts, so no desktop is needed):
  match, process-name mismatch, title-pattern match, terminal in front, click point outside
  the matched window's rectangle, no `expect` given, the `MCP_SCREEN_ALLOW_ANY_FOREGROUND`
  opt-out, and no foreground window present. A red->green control confirmed a real bug where a
  generic `expect="terminal"` was wrongly accepted, now fixed. `test_a_setting_declares_when_
  it_takes_effect.py` still passes (66) -- no regression.
- Open: the guard covers the product's own screen tools. It does not police input sent by
  other processes, and the title-pattern match is a substring/glob on the caption, so a
  deliberately spoofed caption on a process that is ALSO named by `expect` would still match;
  process-name matching is the spoof-resistant path and is preferred where the owner is known.
