# Project review

Reviewed on 2026-07-11.

## Findings

### 1. High: `status` disrupts the bridge it checks

`chrome-bridge status` opens and immediately closes the native host's only
client connection (`src/chrome_bridge/cli.py:140`). The native host serves one
session and then exits (`src/chrome_bridge/bridge/nmh.py:219`), causing the
extension to lose its native port. The extension may not reconnect until its
alarm fires, which can take roughly a minute (`extension/sw.js:137`). One-shot
commands can consequently fail when run in quick succession.

Consider adding a non-destructive health-check request over a persistent host,
or making the extension reconnect immediately after a disconnect.

### 2. High: overlapping extension connections can corrupt global port state

`connect()` checks `port` before awaiting an asynchronous storage read
(`extension/sw.js:105`). Startup, install, alarm, and top-level calls can
therefore enter concurrently. Each connection's disconnect listener assigns
`port = null` through the shared global (`extension/sw.js:126`), so a failed
secondary connection can erase the reference to a healthy primary connection.

Track an in-flight connection promise and have each listener capture its own
port, clearing the global only when `port === connectedPort`.

### 3. Medium: configured ports are not validated

`init --port` accepts zero, negative values, and values above 65535
(`src/chrome_bridge/cli.py:51`), while profile registry entries accept any
integer (`src/chrome_bridge/profiles.py:110`). Invalid values are persisted and
baked into launchers, producing delayed socket errors instead of an actionable
setup error.

Validate ports against the range 1 through 65535 and reject duplicate ports
assigned to different profiles.

## Verification

`uv run pytest -q` passed. Three tests were skipped because their optional
runtime dependency was unavailable.
