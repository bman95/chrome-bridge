# chrome-bridge

Drive your real, everyday Chrome from Python scripts — no bundled browser, no
fresh untrusted profile, no per-connection permission dialog. Supports
scripting **multiple Chrome profiles** (e.g. personal vs. work accounts)
independently of one another.

See [`docs/design.md`](docs/design.md) for why this exists and how it works,
and [`docs/multi-profile.md`](docs/multi-profile.md) for the multi-profile
design.

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

The one-shot commands act on the **active tab** of that profile's Chrome
window — the page you are currently looking at. They attach, act, print, and
detach again, leaving the tab open. `nav` navigates the active tab in place
(opening a fresh tab only if there is no tab to attach to) and likewise leaves
it open on exit.

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
| `CBR_LOG_LEVEL` | `INFO` | Log level for the native-messaging host (writes to stderr) |

Profile-to-port mapping lives in `~/.config/chrome-bridge/profiles.json`,
managed by `chrome-bridge init`/`profiles rm` — you shouldn't need to edit it
by hand.


