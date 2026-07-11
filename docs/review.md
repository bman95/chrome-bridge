# chrome-bridge — Code Review

**Reviewed by:** Claude Fable 5 (`claude-fable-5`)
**Date:** 2026-07-11
**Scope:** Full source tree — Python client/host/CLI, MV3 extension, docs, and test suite (113 passed, 4 skipped).

## Overall verdict

This is an unusually well-engineered small project. The docs are excellent (the
design doc's rejected-alternatives section and the wire-protocol contract block
in `sw.js` are exactly what this kind of multi-process system needs), the
reconnect/generation-guard logic in `client.py` shows real care about TOCTOU
races, and the test suite is substantial. That said, there is one genuine
transport bug, a set of CLI commands that can't work as documented, and a
security posture worth tightening before using it for anything logged-in.

## Significant findings

### 1. The 1 MB cap is enforced on the wrong direction, and the design doc misdiagnoses the risk

> **Resolved** in PR #10 (issue #1): the cap is now per-direction, and the docs were corrected. The description below is kept as the point-in-time finding.

Chrome's Native Messaging limits are asymmetric: messages *from* the host to the
extension are capped at 1 MB, but messages *to* the host are allowed up to 4 GB.
A large `outer_html` reply flows extension → host → client, which never crosses
the 1 MB-limited direction — yet `write_frame`
(`src/chrome_bridge/bridge/frames.py:112`) unconditionally enforces
`MAX_MESSAGE_BYTES`, and both relay pumps in `nmh.py` share it. So the
ext→client pump (`src/chrome_bridge/bridge/nmh.py:105-116`) silently drops any
reply over 1 MB that Chrome delivered just fine, and the client surfaces it as a
confusing 20-second `BridgeTimeoutError`. Meanwhile `docs/design.md` risk #1
describes the cap as applying to `outerHTML` reads, which is backwards — the
capped direction only carries your small command frames.

**Fix:** parametrize the cap per pump (enforce it only on the client→extension
direction, where sending an oversize frame really would make Chrome kill the
connection) and correct the doc. This turns the project's self-declared "#1
risk" into a non-issue for reads.

### 2. Four of the five one-shot CLI commands cannot succeed

A fresh `ChromeBridge` has `_tab_id = None` and `_current_url = None`, and the
wire protocol has no "attach to an existing tab" op — the bridge can only script
tabs it opened itself. Consequences:

- `chrome-bridge html/click/fill` raise `DetachedError("...call open/navigate
  first")` immediately (`src/chrome_bridge/bridge/client.py:1143`).
- `chrome-bridge eval` bypasses `_ensure_attached` (it calls the private
  `cdp._evaluate` directly, `src/chrome_bridge/cli.py:238`) and gets
  `ok:false, "no attached tab"` from `sw.js`.
- `chrome-bridge nav` does work — but its context-manager exit sends the `close`
  op, which detaches *and removes the tab it just opened*. So `nav` navigates
  and instantly destroys the result, and the implied workflow of `nav` then
  `html` is impossible.

**Fix:** either add an `attach` op targeting an existing/current tab (which would
make the one-shots genuinely useful against the page you're looking at), or drop
these commands from CLI and README — right now they're documented API that fails
on first use.

### 3. Every connection — including `status` — consumes the bridge for up to a minute

The host serves exactly one client per process and exits when that client
disconnects (`nmh.py:219-246`, no loop in `main`). The extension then can't
re-dial faster than the `chrome.alarms` 1-minute floor
(`extension/sw.js:141-147`). So `chrome-bridge status` reports "OK" while
*killing* the very endpoint it just probed, and consecutive CLI invocations or
scripts fail with connection-refused until the alarm fires. For a health check
that's actively misleading.

**Fix:** have the host loop back to `accept()` after a client disconnects (the
port is still bound; the extension port is still up), or at least document the
one-shot-session semantics prominently.

### 4. Security: unauthenticated loopback + `get_cookies` = local cookie exfiltration

`docs/design.md` already names the unauthenticated socket as risk #3 and calls
it acceptable, but `get_cookies` (`client.py:1075`) raises the stakes: any local
process — including anything running in this WSL VM, which shares localhost with
Windows — can connect first and read every cookie of your logged-in profile via
`Network.getCookies` with URLs of its choosing. That's session-token theft for
any site. The proposed `0600` token file is the right mitigation; move it from
"before using on a shared machine" to a baseline, given the WSL/Windows
shared-loopback reality this repo runs in.

## Minor

- **`profiles.py` `allocate_port` never reuses gaps.** `max(existing) + 1` means
  removing and re-adding profiles walks the port number upward forever. Harmless
  in practice, but a scan-for-free-port would be more honest.
- **`profiles rm` leaves the registry key and generated files orphaned.** The
  command's docstring admits it "does not touch the registry," but
  `unregister_native_host` and the manifest/launcher files exist right there in
  `install.py` — wiring them in (or at least printing what's left behind) would
  make cleanup complete.
- **`ProfileConfig.chrome_user_data_dir` is dead.** It's in the dataclass,
  persisted, and always `None` (`cli.py:113` passes `None`). Either populate it
  or drop it.
- **`--host`/`--port` fall back to `DEFAULT_BASE_PORT` (8765)** when only
  `--host` is given (`cli.py:125`), silently targeting the default profile's
  port rather than erroring.
- **README/design drift:** README shows `ChromeBridge(profile="personal")` as
  the primary library entry, which is nice, but the CLI's `run` path constructs
  `ChromeBridge(host=, port=)` and never uses the profile-name resolution the
  class supports — minor inconsistency in which path is exercised.

## What's genuinely good

The generation-counter guard against stale reader threads poisoning a fresh
reconnect (`client.py:387-439`) is correct and the atomicity reasoning in the
docstrings is sound. The deliberate choice *not* to set `SO_REUSEADDR` to
preserve the single-owner invariant (`nmh.py:219-233`) is subtle and right.
Using real `Input.dispatchMouseEvent` instead of `el.click()`, embedding all
page-side strings via `json.dumps` to prevent expression injection (`_js_string`),
and the innermost-match click walk are all thoughtful. Test coverage of the
reconnect state machine is impressive for a project this age.

## Recommended priorities

1. **#1** — silent data loss on large reads, mislabeled in the design doc.
2. **#2** — documented CLI commands that fail immediately.
3. **#4** — baseline the loopback token before any logged-in use.
