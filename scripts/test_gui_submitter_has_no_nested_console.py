from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]
TARGETS = [
    REPO / 'scripts' / 'win' / 'run_bestofn.ps1',
    REPO / 'scripts' / 'win' / 'run_effort_ab.ps1',
    REPO / 'scripts' / 'win' / 'run_swe_via_ui.ps1',
    REPO / 'scripts' / 'win' / 'swe_supervisor.ps1',
]


def _active_lines(path: Path):
    for n, raw in enumerate(path.read_text(encoding='utf-8-sig', errors='replace').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        yield n, line


def test_gui_submitter_is_not_rewrapped_in_a_nested_powershell_process():
    offenders = []
    for path in TARGETS:
        for line_no, line in _active_lines(path):
            if 'submit_via_ui.ps1' not in line:
                continue
            if re.search(r'(?i)&\s*(?:powershell(?:\.exe)?)\b', line):
                offenders.append(f'{path.relative_to(REPO)}:{line_no}: {line}')
    assert not offenders, (
        'A PowerShell automation script must invoke submit_via_ui.ps1 in-process. '
        'Spawning another powershell.exe from a console-less/hidden parent can allocate or flash '
        'a new console window:\n' + '\n'.join(offenders)
    )


def test_each_driver_still_uses_the_visible_gui_submitter():
    for path in TARGETS:
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        assert 'submit_via_ui.ps1' in text, path
