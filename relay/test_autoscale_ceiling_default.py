"""画面が見せている天井と、フリートが使う天井が一致すること。

観測(2026-08-25): cockpit の設定画面は「上限 100」を表示し、フリートは 3 で走っていた。
`autoscale_max` はステッパーを押したときだけ書かれるので、触っていない端末では鍵が無い。
この箱の settings.txt には autoscale_max も maxtabs も1行も無く、cockpit は自分の
メモリ上の既定 100(「high by design」と注記されている)を表示し、フリートは maxtabs
(こちらも既定 3)に落としていた。RAM が空いているのに3本しか走らない、の正体。
"""
import re
from pathlib import Path

from relay import fleet_runner as FR

ROOT = Path(__file__).resolve().parents[1]


def test_the_fleet_default_matches_what_the_cockpit_shows():
    """2箇所に別々の既定があると、片方だけが画面に出て、もう片方が実際に効く。"""
    ui = (ROOT / "ui" / "FleetCockpit.cs").read_text(encoding="utf-8-sig", errors="replace")
    m = re.search(r"int _autoMax = (\d+);", ui)
    assert m, "cockpit 側の既定が読めない"
    assert FR.AUTOSCALE_CEILING_DEFAULT == int(m.group(1)), (
        "画面の既定 %s とフリートの既定 %s が食い違っている"
        % (m.group(1), FR.AUTOSCALE_CEILING_DEFAULT))


def test_the_ceiling_is_not_silently_replaced_by_maxtabs_under_autoscale():
    """autoscale の天井は ram_target_cap の邪魔をしないためにある。
    maxtabs で代用すると、誰も選んでいない数字が RAM の判断を上書きする。"""
    import inspect
    src = inspect.getsource(FR.main) if hasattr(FR, "main") else ""
    if not src:
        src = (ROOT / "relay" / "fleet_runner.py").read_text(encoding="utf-8", errors="replace")
    i = src.index("asc_ceiling = AUTOSCALE_CEILING_DEFAULT")
    line = src[i:i + 240]
    assert "if autoscale" in line, "autoscale の有無で分けていない"
    assert "settings_maxtabs()" in line, "autoscale OFF のときの従来動作が消えている"


def test_a_configured_ceiling_still_wins():
    """運用者が実際に選んだ値は、どちらの既定よりも優先されること。"""
    import inspect
    src = (ROOT / "relay" / "fleet_runner.py").read_text(encoding="utf-8", errors="replace")
    i = src.index("if args.autoscale_max > 0:")
    block = src[i:i + 260]
    assert "elif set_ceiling > 0:" in block, "settings の値より既定が先に来ている"
