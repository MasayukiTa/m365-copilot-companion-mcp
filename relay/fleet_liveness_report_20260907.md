# fleet-liveness 調査報告（生の実測値＋pytest出力） 2026-09-07

対象: `relay/task_router.py` `fleet_handoff()` / `fleet_is_live()`。ブランチ `fix/fleet-liveness-20260907`。
計測は本番の live state `C:/Users/M118A8586/resonac-mcp/.fleet/status.json` に対して実施。
実ワーカーは `Get-CimInstance Win32_Process` で CommandLine が `relay[\/](fleet_runner|relay_fleet)` に一致するプロセス。プロセスの起動/停止はしていない（観測のみ）。

## (1) fleet_is_live() と実プロセスの食い違い（複数回・生の値）

### 今セッション（20秒間隔・5サンプル）— false-positive
```
[
  {
    "sample": 1,
    "ts": "2026-09-07T22:45:36",
    "proc_count": 0,
    "pids": [],
    "fleet_is_live": true,
    "status_age_s": 1.0,
    "running": true
  },
  {
    "sample": 2,
    "ts": "2026-09-07T22:45:57",
    "proc_count": 0,
    "pids": [],
    "fleet_is_live": true,
    "status_age_s": 0.8,
    "running": true
  },
  {
    "sample": 3,
    "ts": "2026-09-07T22:46:18",
    "proc_count": 0,
    "pids": [],
    "fleet_is_live": true,
    "status_age_s": 0.6,
    "running": true
  },
  {
    "sample": 4,
    "ts": "2026-09-07T22:46:38",
    "proc_count": 0,
    "pids": [],
    "fleet_is_live": true,
    "status_age_s": 0.4,
    "running": true
  },
  {
    "sample": 5,
    "ts": "2026-09-07T22:46:59",
    "proc_count": 0,
    "pids": [],
    "fleet_is_live": true,
    "status_age_s": 1.1,
    "running": true
  }
]
```
- 実ワーカー数（`relay[\/](fleet_runner|relay_fleet)`）= 0（5サンプルとも proc_count=0）
- status.json を新鮮化していた実体 = `ui/FleetCockpit.exe`（実ワーカーではない）
- → ワーカー0でも `fleet_is_live()=True`。送信側の楽観判定は配送の証拠にならない。

### 前セッション（記録済）— false-negative
- 実ワーカー 2（pid 4880 / 21668、約57分稼働）に対し `fleet_is_live()=False` が5連続。

### 食い違う条件
- false-positive: status.json を更新する主体がワーカーと別（Cockpit）。
- false-negative: status.json の更新停止／走行終了直後。
- 原因: `fleet_is_live()` がプロセスを見ず status.json の mtime≤FLEET_LIVE_MAX_AGE_S(30s) と running フラグだけを読むため。

## (2) 受信側の着弾刻印
- 修正前: なし（`add_goal_to_live_fleet()` は生存確認せず `write_command()` を呼ぶだけ、送信側記録に `"delivered"`）。
- 修正後(c65f70c): 受信側 `fleet_runner.read_commands` が消費時に `<state_dir>/acked/<ack>.json` を stamp。送信側はこの刻印を待ち、一定時間内に付かなければ goal を `for_fleet/` に残置（awaiting_fleet）。

## (3) write_command() の行き先と読み前終了
- コマンドはコマンドチャネルに書かれ、`read_commands` が消費するまで刻印は付かない。読む前に終了すれば刻印なし→修正後は `for_fleet/` に parked（待ち状態で可視化）。

## テスト生出力（pytest -v）
```
============================= test session starts =============================
platform win32 -- Python 3.10.0, pytest-7.4.4, pluggy-1.6.0 -- C:\Users\M118A8586\resonac-mcp\.venv\Scripts\python.exe
cachedir: .pytest_cache
hypothesis profile 'default'
benchmark: 4.0.0 (defaults: timer=time.perf_counter disable_gc=False min_rounds=5 min_time=0.000005 max_time=1.0 calibration_precision=10 warmup=False warmup_iterations=100000)
PyQt5 5.15.11 -- Qt runtime 5.15.2 -- Qt compiled 5.15.2
rootdir: C:\Users\M118A8586\resonac-mcp-wt\fleet-liveness
plugins: anyio-4.13.0, hypothesis-6.155.6, arraydiff-0.7.0, astropy-header-0.2.2, asyncio-0.23.6, bdd-6.1.1, benchmark-4.0.0, cov-4.1.0, doctestplus-1.7.1, filter-subpackage-0.2.0, instafail-0.5.0, mock-3.15.1, qt-4.5.0, remotedata-0.4.1, rerunfailures-11.1.2, xvfb-3.1.1
asyncio: mode=strict
collecting ... collected 6 items

relay/test_fleet_delivery_ack.py::test_read_commands_stamps_ack_for_consumed_goal PASSED [ 16%]
relay/test_fleet_delivery_ack.py::test_ack_is_stamped_only_after_the_read_not_on_write PASSED [ 33%]
relay/test_fleet_delivery_ack.py::test_stamp_acks_skips_items_without_a_nonce PASSED [ 50%]
relay/test_fleet_delivery_ack.py::test_handoff_reports_dispatched_only_after_the_ack_lands PASSED [ 66%]
relay/test_fleet_delivery_ack.py::test_handoff_awaits_when_live_but_no_ack_arrives PASSED [ 83%]
relay/test_fleet_delivery_ack.py::test_handoff_awaits_when_not_live_and_parks_the_goal PASSED [100%]

============================== warnings summary ===============================
..\..\resonac-mcp\.venv\lib\site-packages\opentelemetry\util\_importlib_metadata.py:32
  C:\Users\M118A8586\resonac-mcp\.venv\lib\site-packages\opentelemetry\util\_importlib_metadata.py:32: DeprecationWarning: SelectableGroups dict interface is deprecated. Use select.
    return EntryPoints(ep for group_eps in eps.values() for ep in group_eps)

..\..\resonac-mcp\.venv\lib\site-packages\fastmcp\server\auth\providers\jwt.py:10
  C:\Users\M118A8586\resonac-mcp\.venv\lib\site-packages\fastmcp\server\auth\providers\jwt.py:10: AuthlibDeprecationWarning: authlib.jose module is deprecated, please use joserfc instead.
  It will be compatible before version 2.0.0.
    from authlib.jose import JsonWebKey, JsonWebToken

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================== 6 passed, 2 warnings in 3.96s ========================
```

## CI マニフェスト確認（python scripts/check_ci_test_manifest.py）
```
CI test manifest OK: 381 pytest files listed, 11 explicit exception(s).
```
