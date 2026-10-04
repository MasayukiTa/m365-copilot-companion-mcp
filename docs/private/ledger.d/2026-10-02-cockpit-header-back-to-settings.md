## 2026-10-02 - Cockpit header back to its earlier controls; fan-out settings move to the gear popup

- Change: the header had grown to sixteen controls because each fan-out slice added its own.
  It composes the earlier nine again (tab chip, effort, run mode, approvals, pause/stop, gear,
  language, theme, overflow). The effort policy and every fan-out control (on/off, depth,
  hierarchical merge, write scope, the four budget limits) are now rows in the settings popup:
  an "Effort" section holding the effort policy, and one "Fan-out" section holding the rest,
  the four limits in a single four-column row. Settings keys, SaveKey-only persistence,
  no-refire paint guards, validation, tooltips and the "in effect" lines are unchanged (the
  lines are the row's secondary text). The tick that polls settings.txt by mtime now repaints
  these controls too; every paint is null-safe because the popup is built on first open.
- Tests: `relay/test_cockpit_header_controls.py` pins the header control list (new settings
  go in the popup); the placement assertions of the fan-out tests now name the popup.
- Open: none.
