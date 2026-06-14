# Startup: Dev Tunnels sign-in (and the 2026-06-14 outage it caused)

**TL;DR for a new device:** run `devtunnel login` **once, interactively** (it persists across
reboots) **before** relying on the supervisor. `setup.ps1 -WithExternalTools` now does this for
you. The supervisor no longer interferes with an in-progress login.

---

## What went wrong (2026-06-14)

After a reboot the Dev Tunnels CLI was signed out (its token cache was empty). Every attempt to
`devtunnel login` **died after 36-108 s without saving a token** — the browser showed "signed in,"
but locally `devtunnel user show` still said *Not logged in* and the token file
(`%LOCALAPPDATA%\DevTunnels\devtunnels-tokens-microsoft`) stayed **0 bytes**. The Copilot cloud
agent reaches the local MCP server **through the Dev Tunnel relay**, so with no tunnel the entire
remote/fleet path was down.

## Root cause

`supervisor.ps1` had two flaws that combined into an unbreakable loop on any un-authenticated
machine:

1. **Unconditional, name-scoped kill.** `Start-TunnelHost` ran
   `Get-Process devtunnel | Stop-Process -Force` — killing **every** devtunnel process by name,
   including a user's interactive `devtunnel login` (the very command needed to authenticate) and
   any unrelated tunnel.
2. **No login gate.** When not signed in, `devtunnel show <tunnel>` reports 0 host connections, so
   the health loop incremented `tunnelMiss` every cycle and, after the debounce, called
   `Start-TunnelHost` — which killed the login and then failed to host (still not signed in). Next
   cycle: repeat. So a fresh device could **never** complete sign-in: the supervisor reaped the
   login every ~15-60 s, leaving the 0-byte token and an orphaned browser dialog (the account
   picker's "続行/Continue" button does nothing once its backing process is dead).

A background/non-interactive launch makes it worse: `devtunnel login -d` (device-code) only polls
for approval when attached to a real **TTY**; redirected or detached, it prints the code and exits
immediately. So both "the supervisor keeps killing it" and "it was launched in the background"
were true at once.

## The fix (in this repo)

**`supervisor.ps1`:**
- **Login gate** (`Test-DevtunnelLoggedIn`): if `devtunnel user show` is not signed in, the loop
  **pauses tunnel management and does not touch any devtunnel process** — so an interactive
  `devtunnel login` can run to completion. Logs once: *"tunnel management PAUSED. Run
  `devtunnel login`."*
- **Scoped kill**: `Start-TunnelHost` now stops only **our** stale host — processes whose command
  line is `devtunnel host <TunnelName>` — never a `devtunnel login` or an unrelated tunnel.

**`setup.ps1`:** after installing the Dev Tunnels CLI, it checks `devtunnel user show` and runs
`devtunnel login` interactively (one-time) during setup, while a real console exists. "Next steps"
now lists `devtunnel login` **before** the supervisor.

## Onboarding another device (the durable recipe)

1. `./setup.ps1 -WithExternalTools` — installs devtunnel **and** prompts the one-time sign-in.
   (Or manually: `devtunnel login` once, in a normal terminal.)
2. Start the MCP server / supervisor. The supervisor will only manage the tunnel **after** sign-in,
   and will never kill your login if the token later expires — it pauses and tells you to re-run
   `devtunnel login`.
3. For the Copilot **driver** (the fleet) you also need Edge with `--remote-debugging-port=9222`
   (see `docs/companion_setup.md`) — but that is the CDP side, separate from the tunnel/auth here.

## General principle (applies beyond devtunnel)

Interactive authentication — SSO, OAuth, device-code, consent dialogs — must run in the
**foreground, in a real console, and must not be killable by a watchdog**. Before launching such a
flow, check: (a) does it need a TTY / user interaction? (foreground + visible), and (b) is a
supervisor/watchdog running that manages the same process? (pause it first). Long-running
**non-interactive** work (evals, the fleet, dockerd, monitors) is what belongs in the background.

_2026-06-14._
