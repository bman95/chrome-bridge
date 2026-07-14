# chrome-bridge

Drive your real, everyday Chrome from Python scripts — no bundled browser, no
fresh untrusted profile, no per-connection permission dialog. Supports
scripting **multiple Chrome profiles** (e.g. personal vs. work accounts)
independently of one another.

See [`docs/design.md`](docs/design.md) for why this exists and how it works,
[`docs/multi-profile.md`](docs/multi-profile.md) for the multi-profile
design, and [`docs/multi-tab.md`](docs/multi-tab.md) for driving several
tabs over one connection.

## Quick start

```bash
uv sync --group dev
uv run pytest    # unit tests only — no Chrome required

# One-time setup for a Chrome profile you want to script:
uv run chrome-bridge init --profile personal

# Then, from anywhere:
uv run chrome-bridge status --profile personal
uv run chrome-bridge run my_script.py --profile personal
```

## Writing a script

```python
# my_script.py
from chrome_bridge.runner import get_bridge

cdp = get_bridge()
cdp.navigate("https://example.com")
cdp.wait_selector("#content")
print(cdp.outer_html("#content"))
```

Run it with:

```bash
chrome-bridge run my_script.py --profile personal
```

Or use the library directly, without the CLI:

```python
from chrome_bridge import ChromeBridge

with ChromeBridge(profile="personal") as cdp:
    cdp.navigate("https://example.com")
    cdp.click_selector("button.submit")
```

### Multiple tabs

One connection can drive several tabs: `new_tab` opens an additional tab and
returns a `Tab` handle with the same page primitives.

```python
from chrome_bridge import ChromeBridge

with ChromeBridge(profile="personal") as bridge:
    a = bridge.new_tab("https://example.com")
    b = bridge.new_tab("https://example.org")
    a.wait_selector("#content")
    print(b.outer_html("h1"))
    b.close()          # closes that tab; `a` is untouched
```

The legacy single-tab usage above is unchanged — the facade methods
(`cdp.navigate(...)` etc.) keep driving one tab at a time. Operations are
serialized per connection (tabs interleave, they don't run in parallel).
See [`docs/multi-tab.md`](docs/multi-tab.md).

## CLI commands

```
chrome-bridge init --profile <name>       # guided one-time profile setup
chrome-bridge profiles list
chrome-bridge profiles rm <name>
chrome-bridge status [--profile <name>]

chrome-bridge run <script.py> --profile <name> [args...]

chrome-bridge nav <url> --profile <name>          # one-shot convenience commands
chrome-bridge html [selector] --profile <name>
chrome-bridge click <selector> --profile <name>
chrome-bridge fill <selector> <text> --profile <name>
chrome-bridge eval "<js expression>" --profile <name>
```

The one-shot commands act on the **active tab** of that profile's last-focused
Chrome window — the page you are currently looking at. They attach, act,
print, and detach again, leaving the tab open. `nav` navigates the active tab
in place (opening a fresh tab only if there is no tab to attach to) and
likewise leaves it open on exit.

## Setting up a profile

`chrome-bridge init --profile <name>` will:

1. Allocate a loopback port for this profile.
2. Ask which Chrome profile directory this maps to (for display purposes).
3. Generate a native-messaging manifest + launcher batch file for this
   profile, with the port baked in.
4. Register the launcher in the Windows registry
   (`HKCU\Software\Google\Chrome\NativeMessagingHosts\com.chromebridge.bridge.<name>`).
5. Walk you through: opening `chrome://extensions` **in that profile's Chrome
   window**, enabling Developer mode, loading `extension/` unpacked, and
   setting the profile name on the extension's options page.
6. Save the extension ID it prints back into the profile registry.

Re-run it any time — every step is idempotent.

To script a second profile (e.g. "work"), just run `init` again with a
different `--profile` name; each profile gets its own port and native host, so
they never interfere with each other.

## Testing

```bash
uv run pytest
```

All tests run against a fake in-process bridge server — no real Chrome, no
native host, no registry writes required. A handful of tests that exercise the
actual in-page JS the click/fill primitives emit need `node` on PATH and skip
cleanly if it's absent.

The full end-to-end path (a real Chrome profile, the loaded extension, a real
script run) can only be verified manually — see the checklist in
`docs/multi-profile.md`.

## Configuration

Environment variables (all optional):

| Var | Default | Purpose |
|---|---|---|
| `CBR_BRIDGE_HOST` | `127.0.0.1` | Loopback host the native host binds / a script dials |
| `CBR_BRIDGE_PORT` | `8765` | Loopback port (each profile normally gets its own via `init`) |
| `CBR_BRIDGE_PROFILE` | *(unset)* | Profile name the native host writes its auth token under (baked into each launcher by `init`; without it the host falls back to the profile registered on its port, then to a port-derived token file) |
| `CBR_LOG_LEVEL` | `INFO` | Log level for the native-messaging host (writes to stderr) |

Profile-to-port mapping lives in `~/.config/chrome-bridge/profiles.json`,
managed by `chrome-bridge init`/`profiles rm` — you shouldn't need to edit it
by hand.

## Security

The bridge drives a **real, logged-in Chrome profile**, so its loopback socket
is a direct path to that profile's cookies and pages. Loopback alone is not a
trust boundary: any local process can dial `127.0.0.1:<port>` — and under
WSL2 that includes **Windows-side processes**, because WSL2 shares localhost
with the Windows host.

The socket is therefore authenticated as a baseline. On every launch the
native host writes a fresh random token (32 bytes from `os.urandom`,
hex-encoded) to a `0600` file in `~/.config/chrome-bridge/`
(`token-<profile>.txt`, or `token-port.<port>.txt` for `--host`/`--port`-only
setups), and a client must present it as its **first frame** —
`{"auth": "<hex>"}` — or the host drops that connection and keeps serving
the next one. `ChromeBridge` reads the token and sends the handshake
automatically; if the token file is missing or empty (e.g. the bridge was
never started for this profile), it raises an actionable error telling you to
reload the extension so the host regenerates it. `chrome-bridge status`
stays a pure reachability probe: it never needs the token, and its
unauthenticated connect-and-close cannot wedge the host.

The token gates processes that can reach the port but **not your files** —
other local users, and cross-boundary processes in the WSL2/Windows
shared-loopback topology. It does not (and cannot) protect against an
attacker who can already read your files: they could read the token, and the
Chrome profile itself. On Windows the file's protection comes from your user
profile directory's ACL rather than POSIX permissions (`chmod` only toggles
the read-only bit there). Two practical notes: scripts must run where they
can read `~/.config/chrome-bridge/` (the same account — and, under WSL, the
same side of the WSL/Windows boundary — as the host), and a *wrong* token is
answered with silence rather than an error (the host never replies to the
handshake), surfacing as the connection dying on first use; reconnecting
re-reads the fresh token. See `docs/design.md` (risk #3).
