# Chrome Bridge — Design

Drive your real, everyday Chrome from Python scripts, with **no per-connection
permission dialog** and **fully unattended** operation — replicating how
"Claude in Chrome" and similar tools work.

## Why this design

Some sites (heavy anti-bot protection, session-bound flows, or simply "I want
to script my own logged-in browser") need a **real, already-trusted browser
profile**, not a bundled/headless Chromium with a fresh, untrusted profile.
The simpler CDP paths were investigated and rejected for unattended,
dialog-free use:

- **Chrome's `chrome://inspect` "Allow remote debugging" toggle** — exposes a
  `ws://` endpoint on the real profile, but pops a **per-connection permission
  dialog that cannot be persisted**. Incompatible with unattended runs (a
  human must click "Allow" every new process).
- **Classic `--remote-debugging-port` + a dedicated `--user-data-dir`** — is
  dialog-free, but on modern Chrome the debug server **refuses to open while
  another instance of the same Chrome binary is already running** (your
  everyday Chrome). You don't want to close your everyday Chrome to script it.
- **Chrome for Testing** — a separate binary (no singleton conflict),
  dialog-free, but a **separate profile**: separate logins, separate trust.
  Discards the real profile's accumulated state (the whole reason to use it).
- **Copying the real profile** — broken by App-Bound Encryption (Chrome 127+):
  the cookie key is bound to the original user-data-dir; copies are
  undecryptable or go stale + fight the profile lock.

The only path that drives the **real profile**, **dialog-free**,
**unattended**, **alongside** your everyday Chrome is the **`chrome.debugger`
MV3 extension** — exactly what Claude-in-Chrome uses. Permission is granted
**once at extension install**; `chrome.debugger.attach()` then needs no
per-attach dialog (only a cosmetic, suppressible infobar).

## Three decisions

1. **Transport: Chrome Native Messaging** (not an outbound WebSocket). Same
   machine, no listening port, no firewall. Chrome **spawns and owns** the
   Python host via `chrome.runtime.connectNative()`, which also keeps the MV3
   service worker alive while the port is open.

2. **No Playwright; speak page-level raw CDP from Python.** `chrome.debugger`
   exposes **no browser-level target**, so `connect_over_cdp` (which needs
   `/json/version` → `webSocketDebuggerUrl` → `Target.setAutoAttach` …) cannot
   attach. A proxy can *synthesize* a browser target but it's a heavy second
   runtime for a handful of CDP ops, so this project speaks a small raw-CDP
   client directly.

3. **Install dialog-free via Developer-mode "Load unpacked" (one-time)** +
   suppress the infobar with `--silent-debugger-extension-api`. True off-store
   `ExtensionInstallForcelist` needs AD/Azure-AD/Chrome-Enterprise
   enrollment — not available on a standalone non-domain PC, so force-install
   is out here. Load-unpacked persists fine on a clean machine; the banner is
   cosmetic and does not block automation.

## Component diagram

```
your REAL everyday Chrome (running normally, one profile per script target)
  ┌────────────────────────┐   chrome.debugger.attach({tabId})
  │ MV3 extension          │ ─► NEW window/tab (same profile → same cookies/
  │ service worker (sw.js) │ ◄─  login state), focused:false (no focus steal)
  │  chrome.windows.create │
  │  chrome.debugger.*      │
  └────────┬───────────────┘
           │ Native Messaging (4-byte-LE length + JSON over stdin/stdout)
           │   ── Chrome spawns the host ──
  ┌────────▼───────────────┐
  │ Python host  nmh.py     │  read/write length-prefixed JSON frames
  └────────┬───────────────┘
           │ loopback TCP (a script dials 127.0.0.1:<profile's port>)
  ┌────────▼──────────────────────────────────────────────┐
  │ your Python script (via ChromeBridge)                  │
  │  navigate / wait_selector / outer_html / click / fill  │
  └────────────────────────────────────────────────────────┘
```

## The core primitives

| Need | CDP |
|---|---|
| Navigate | `Page.enable` → `Page.navigate {url}` → await `Page.loadEventFired` |
| Wait for a selector | poll `Runtime.evaluate {expression:"document.querySelector(...)!==null"}` |
| Read HTML | `Runtime.evaluate` reading a container subtree's `outerHTML` (replies flow extension→host→client under the relay's 64 MB read guard — see risk #1 below) |
| Click | element center via `getBoundingClientRect`, then real `Input.dispatchMouseEvent` (mousePressed/mouseReleased) — never synthetic `el.click()` |
| Fill | set `.value` + dispatch `input`/`change` events |
| Detect an element | `Runtime.evaluate {expression:"!!document.querySelector(...)"}` |

All in allowed domains `Page`/`Runtime`/`DOM`/`Input`.

## `host_permissions`: `<all_urls>`

The extension requests `<all_urls>` rather than a fixed site list or
`activeTab`:

- The whole point is scripting *any* site without editing/reloading the
  extension — only `<all_urls>` satisfies that.
- `chrome.debugger` already grants near-total control over an attached tab
  regardless of `host_permissions`; since this extension has no content
  scripts, `<all_urls>` adds little marginal risk on top of `debugger` itself.
- `activeTab` requires a user gesture on the active tab, which is
  incompatible with unattended background tabs (`active:false`) — a
  non-starter for this project.

This is a large install-time grant. It's acceptable here because install is a
deliberate manual dev-mode load (not a Web Store install a stranger might
click through), and it matches the trust level tools like Claude-in-Chrome
already carry.

## Top risks

1. **Native-messaging caps a single host→extension message at 1 MB — that
   direction only.** The cap applies to the command frames the host relays
   *to* the extension (which are small), not to extension replies:
   extension→host messages are allowed far above 1 MB by Chrome, and a large
   reply (e.g. `outer_html` of a heavy page) relays intact to the client,
   bounded only by the relay's 64 MB read guard. A client→extension frame
   over the cap is dropped by the relay without desyncing the stream (Chrome
   would kill the host on an oversize frame), surfacing as a per-op timeout;
   the bundled `ChromeBridge` client rejects such a frame at send time instead
   (fail-fast `BridgeError`), so the relay drop only covers raw clients. Keep
   command payloads (e.g. large `fill` values or injected scripts) under
   1 MB.
2. **MV3 service-worker eviction mid-job** (workers die ~30s idle).
   Mitigated by a `chrome.alarms` keepalive + `ChromeBridge.reconnect()`
   rebuilding the dead loopback connection and re-attaching idempotently
   (re-open the last URL, fresh `Page.enable`). `sw.js` re-dials on every
   worker spin-up plus a post-drop reconnect alarm. `Input.dispatchMouseEvent`
   keeps realistic coordinates (not synthetic `el.click()`).
3. **The host's script-facing channel is an unauthenticated loopback
   socket.** The host binds `127.0.0.1:<port>` and relays the first client's
   frames into raw CDP on the real profile, with no token. Loopback is not a
   hard trust boundary on a shared machine: any local process could race to
   connect. Acceptable for a single-user PC; add a per-launch shared secret
   (a `0600` token file the host writes, validated on the first frame) before
   using this on a shared machine.

## References

- chrome.debugger API — https://developer.chrome.com/docs/extensions/reference/api/debugger
- Native messaging — https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging
- Chrome 136 remote-debugging changes — https://developer.chrome.com/blog/remote-debugging-port
- Claude-in-Chrome teardown — https://medium.com/@buttercanbentley/how-claude-in-chrome-works-45b86b06f689
- ExtensionInstallForcelist (off-store needs enrollment) — https://support.google.com/chrome/a/answer/7532015
- App-Bound Encryption research — https://github.com/xaitax/Chrome-App-Bound-Encryption-Decryption/blob/main/docs/RESEARCH.md
