// Chrome Bridge — MV3 service worker.
//
// Attaches chrome.debugger to a tab in the user's REAL Chrome profile (shares
// cookies / login state) and relays raw CDP between Chrome and a Python
// Native Messaging host. See docs/design.md and docs/multi-profile.md.
//
// =====================================================================
//  WIRE PROTOCOL  (CONTRACT — nmh.py and chrome_bridge.bridge.client target this)
// =====================================================================
// Transport: Chrome Native Messaging. Each frame is one JSON object,
// length-prefixed with a 4-byte little-endian uint32 (handled by Chrome /
// nmh.py, never by this file). Two directions:
//
// Requests are processed one at a time (no pipelining): Python sends a
// request and awaits its reply before sending the next. The single-tab model
// below relies on this — do not interleave ops from the Python side.
//
// ---- Python -> extension (requests) ----------------------------------
// Every request carries a mandatory caller-chosen correlation id `id` (number
// or string) that is echoed back on exactly one reply. Five ops:
//
//   { "id": <id>, "op": "open",  "url": <string> }
//       Create a background tab (active:false — no focus steal) at `url`
//       in the current profile, then chrome.debugger.attach(..., "1.3").
//       The tab is BRIDGE-CREATED: "close" (and the onDetach reaper) will
//       remove it.
//
//   { "id": <id>, "op": "attach" }
//       Attach to the ACTIVE tab in the current window — the page the user
//       is looking at. Enforces the same single-tab invariant as "open"
//       (tears down any prior bridge tab first). Fails (ok:false) when
//       there is no active tab ("no active tab"), on attach contention
//       (DevTools open), or on a restricted page (chrome:// etc.); the
//       user's tab is never removed on failure. The tab is MERELY ATTACHED:
//       "close" detaches but leaves it open.
//
//   { "id": <id>, "op": "cdp",   "method": <string>, "params": <object?> }
//       chrome.debugger.sendCommand on the attached tab. `params` optional
//       (defaults to {}). `method` is a raw CDP method, e.g. "Page.navigate".
//
//   { "id": <id>, "op": "close" }
//       Detach the debugger. Remove the tab only if it was bridge-created
//       ("open"); a tab joined via "attach" is left open for the user.
//
//   { "id": <id>, "op": "detach" }
//       Detach the debugger and clear the tab reference, ALWAYS leaving the
//       tab open — even a bridge-created one. Idempotent: ok:true when no
//       tab is attached. The one-shot `nav` ends with this because its
//       no-active-tab fallback creates a bridge-owned tab that "close" would
//       remove: ending with "detach" leaves it open and clears the reference
//       so no stale attachment/ownership survives into the next session's
//       "attach" teardown. (Read one-shots that only ever "attach" an unowned
//       tab end with "close", which is equivalent for a tab it doesn't own.)
//
// ---- extension -> Python --------------------------------------------
// Replies (exactly one per request, correlated by `id`):
//
//   { "id": <id>, "ok": true,  "tabId": <number> }   // reply to "open"/"attach"
//   { "id": <id>, "ok": true,  "result": <object> }  // reply to "cdp"
//   { "id": <id>, "ok": true }                        // reply to "close"/"detach"
//   { "id": <id>, "ok": false, "error": <string> }   // any op failed
//
// Unsolicited events (no `id`; `event:true` discriminator):
//
//   { "event": true, "type": "cdp",     "method": <string>, "params": <object> }
//       A chrome.debugger.onEvent from the attached tab (raw CDP event).
//
//   { "event": true, "type": "detached", "reason": <string> }
//       chrome.debugger.onDetach fired (target closed, devtools opened,
//       SW evicted, etc.). After this the tab is no longer attached; Python
//       must treat the session as gone and re-"open" if it wants to resume.
//
// Contract notes:
//   * Single attached tab at a time (`dbgTabId`). A second "open" or "attach"
//     replaces the reference: it first tears down any existing bridge tab
//     (removing it only if bridge-created), so callers cannot leak tabs by
//     re-opening. Callers (chrome_bridge.bridge.client) drive one tab.
//   * `dbgTabOwned` records whether the current tab was bridge-created
//     ("open") or merely attached ("attach"). Teardown — the "close" op and
//     the onDetach reaper — removes the tab only when bridge-created; a user
//     tab the bridge only attached to is never removed.
//   * The extension keeps NO durable state across MV3 service-worker eviction
//     (aside from the profile name in chrome.storage.local, read once at each
//     spin-up). Replies and events flow only while the native port is up. A
//     reply CAN be lost: if the worker is evicted (or the port drops) while a
//     `cdp` op is awaiting `sendCommand`, no reply is delivered. Therefore
//     Python MUST enforce a per-`id` timeout and, on reconnect, treat the
//     session as possibly gone — re-"open" and re-`Page.enable` rather than
//     assume the prior tab is still attached. Events that occur while the port
//     is down are likewise dropped.
// =====================================================================
//
// =====================================================================
//  MULTI-PROFILE IDENTITY
// =====================================================================
// The same unpacked extension code is loaded into every Chrome profile you
// want to script. Each profile therefore needs its own native host name so
// Chrome's spawned host process (which gets no profile-identifying info from
// Chrome) can be launched with that profile's own port baked in — see
// docs/multi-profile.md. The profile name is set once per profile-load via
// this extension's options page (options.html/options.js), stored in
// chrome.storage.local under "bridgeProfileName", and read here at every
// service-worker spin-up to build the native host name
// "com.chromebridge.bridge.<profileName>".
// =====================================================================

const NATIVE_HOST_PREFIX = "com.chromebridge.bridge";
const CDP_PROTOCOL_VERSION = "1.3";
const KEEPALIVE_ALARM = "ka";
// One-shot alarm to re-dial soon after the port drops while idle. Alarms are
// the ONLY timer that survives a service-worker eviction (a setTimeout dies
// with the worker), and chrome.alarms clamps packed extensions to a 1-minute
// minimum — so this cannot beat the ~30 s idle eviction, but it shortens the
// gap before the next reconnect attempt versus waiting on the periodic alarm
// alone. The client side (ChromeBridge.reconnect) re-dials the loopback and
// re-attaches the moment this brings the host back, so resume needs no human.
const RECONNECT_ALARM = "reconnect";

let port = null;
let dbgTabId = null;
// True when the bridge created the current tab ("open"); false when it merely
// attached to the user's active tab ("attach"). Teardown removes the tab only
// when true — see the WIRE PROTOCOL contract notes.
let dbgTabOwned = false;

async function nativeHostName() {
  const { bridgeProfileName } = await chrome.storage.local.get("bridgeProfileName");
  if (!bridgeProfileName) {
    throw new Error(
      "no profile name configured — open this extension's options page and set one"
    );
  }
  return `${NATIVE_HOST_PREFIX}.${bridgeProfileName}`;
}

async function connect() {
  if (port) return;
  let hostName;
  try {
    hostName = await nativeHostName();
  } catch (e) {
    // No profile name set yet (first load, before the user visited options).
    // Nothing to dial; the user must configure it once. Do not spam retries.
    console.warn("chrome-bridge:", e.message);
    return;
  }
  try {
    port = chrome.runtime.connectNative(hostName);
  } catch (e) {
    // Host not registered / not launchable; nothing to relay to. Schedule a
    // retry so a transient launch failure (e.g. uv still warming the venv)
    // self-heals rather than wedging until the periodic keepalive alarm.
    port = null;
    scheduleReconnect();
    return;
  }
  port.onMessage.addListener(onPythonMessage);
  port.onDisconnect.addListener(() => {
    // chrome.runtime.lastError is set here when the host died/was missing
    // (e.g. the worker was evicted and the native host Chrome spawned exited).
    // Drop the reference and schedule a near-term re-dial so the bridge comes
    // back without waiting for the next periodic keepalive tick.
    port = null;
    scheduleReconnect();
  });
}

// Schedule a single delayed reconnect. chrome.alarms enforces a 1-minute floor
// for packed extensions, so this is best-effort "soon", not instant — but it
// survives worker eviction, which is what matters. Creating it again before it
// fires just resets the same single alarm (no pile-up).
function scheduleReconnect() {
  try {
    chrome.alarms.create(RECONNECT_ALARM, { delayInMinutes: 0.5 });
  } catch (e) {
    /* alarms unavailable (should not happen with the alarms permission) */
  }
}

async function onPythonMessage(msg) {
  try {
    if (msg.op === "open") {
      // Enforce the single-tab invariant: tear down any prior tab so a re-open
      // (e.g. after a dropped detach) cannot orphan it.
      if (dbgTabId !== null) await closeTab();
      // active:false opens the tab in the current window without focusing it,
      // sharing the real profile's cookies/login state. We deliberately use
      // tabs.create over windows.create: it satisfies "no focus steal" without
      // spawning a separate window to manage/teardown.
      const tab = await chrome.tabs.create({ url: msg.url, active: false });
      try {
        await chrome.debugger.attach({ tabId: tab.id }, CDP_PROTOCOL_VERSION);
      } catch (e) {
        // Attach failed (DevTools already open, debugger contention, crash).
        // Remove the orphaned tab and leave dbgTabId null before surfacing.
        try {
          await chrome.tabs.remove(tab.id);
        } catch (_) {
          /* already gone */
        }
        throw e;
      }
      dbgTabId = tab.id;
      dbgTabOwned = true;
      reply(msg.id, { ok: true, tabId: dbgTabId });
    } else if (msg.op === "attach") {
      // Query FIRST so a failed attach ("no active tab") does not destroy the
      // prior session, and so removing a prior owned tab cannot shift which
      // tab is active before we read it.
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab || tab.id === undefined) {
        // NOTE: the CLI's nav fallback matches this exact string
        // (src/chrome_bridge/cli.py) — keep it stable.
        reply(msg.id, { ok: false, error: "no active tab" });
        return;
      }
      if (tab.id === dbgTabId) {
        // Already attached to this very tab (e.g. the user focused a
        // bridge-created one). Hand it over: it is the user's tab now, so
        // teardown must never remove it.
        dbgTabOwned = false;
        reply(msg.id, { ok: true, tabId: dbgTabId });
        return;
      }
      // Single-tab invariant, same as "open": tear down any prior bridge tab
      // (removed only if bridge-created) before attaching to the new one.
      if (dbgTabId !== null) await closeTab();
      // If attach fails (contention, restricted chrome:// page, crash) the
      // outer catch replies ok:false. The tab is the USER's — never remove it.
      await chrome.debugger.attach({ tabId: tab.id }, CDP_PROTOCOL_VERSION);
      dbgTabId = tab.id;
      dbgTabOwned = false;
      reply(msg.id, { ok: true, tabId: dbgTabId });
    } else if (msg.op === "cdp") {
      if (dbgTabId === null) {
        reply(msg.id, { ok: false, error: "no attached tab" });
        return;
      }
      const res = await chrome.debugger.sendCommand(
        { tabId: dbgTabId },
        msg.method,
        msg.params || {}
      );
      reply(msg.id, { ok: true, result: res });
    } else if (msg.op === "close") {
      await closeTab();
      reply(msg.id, { ok: true });
    } else if (msg.op === "detach") {
      // Release the tab WITHOUT removing it, whatever its ownership. Clearing
      // the reference here matters as much as the detach itself: stale
      // dbgTabId/dbgTabOwned state would make the NEXT session's "attach"
      // teardown remove a tab a prior session promised to leave open.
      const tabId = dbgTabId;
      dbgTabId = null;
      dbgTabOwned = false;
      if (tabId !== null) {
        try {
          await chrome.debugger.detach({ tabId });
        } catch (e) {
          /* already detached */
        }
      }
      reply(msg.id, { ok: true });
    } else {
      reply(msg.id, { ok: false, error: "unknown op: " + String(msg.op) });
    }
  } catch (e) {
    reply(msg.id, { ok: false, error: String(e) });
  }
}

async function closeTab() {
  const tabId = dbgTabId;
  const owned = dbgTabOwned;
  dbgTabId = null;
  dbgTabOwned = false;
  if (tabId === null) return;
  // Detach first, then remove (bridge-created tabs only — a user tab the
  // bridge merely attached to stays open). Swallow per-step errors so a
  // half-gone tab (e.g. already closed by the user) still fully tears down.
  try {
    await chrome.debugger.detach({ tabId });
  } catch (e) {
    /* already detached */
  }
  if (owned) {
    try {
      await chrome.tabs.remove(tabId);
    } catch (e) {
      /* already removed */
    }
  }
}

// Relay raw CDP events from the attached tab back to Python.
chrome.debugger.onEvent.addListener((source, method, params) => {
  if (port && source.tabId === dbgTabId) {
    port.postMessage({ event: true, type: "cdp", method, params });
  }
});

// Relay debugger detach (target gone, devtools opened, SW evicted, ...).
chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId === dbgTabId) {
    const tabId = dbgTabId;
    const owned = dbgTabOwned;
    dbgTabId = null;
    dbgTabOwned = false;
    if (port) port.postMessage({ event: true, type: "detached", reason });
    // If a BRIDGE-CREATED tab still exists (detach was NOT caused by tab
    // closure — e.g. the user opened DevTools), reap it so it doesn't linger
    // as an orphan. A user tab the bridge only attached to is never removed.
    if (owned) chrome.tabs.remove(tabId).catch(() => {});
  }
});

function reply(id, payload) {
  if (port) port.postMessage({ id, ...payload });
}

// Keepalive. MV3 service workers are evicted after ~30 s idle. The PRIMARY
// keepalive is the open native-messaging port: while `connectNative` is
// connected, message traffic on it resets the idle timer, so an active job
// (Python <-> extension chatter) keeps the worker alive on its own.
//
// The alarm is only a coarse RECONNECT poke for when the port has dropped while
// idle. NOTE: chrome.alarms enforces a 1-minute minimum period for packed
// extensions (values < 1 are clamped up; unpacked/dev builds allow shorter but
// warn), so this fires at most ~once a minute — it cannot by itself beat the
// ~30 s idle eviction. That is fine: the port (not the alarm) is what holds the
// worker during work; the alarm just re-establishes the port after an idle gap.
chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  // Both the periodic keepalive and the one-shot post-drop reconnect just
  // re-dial if the port is down (connect() no-ops when it is already up). The
  // alarm handler firing is also what re-spins an evicted worker, so this is
  // the path that brings the bridge back unattended after eviction.
  if (alarm.name === KEEPALIVE_ALARM || alarm.name === RECONNECT_ALARM) {
    connect();
  }
});

// Re-dial on every worker spin-up trigger: browser launch (onStartup), the
// extension being (re)loaded/updated (onInstalled), and the top-level call for
// a plain eviction-respawn. connect() is idempotent, so overlapping triggers
// are harmless.
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

// If the user (re)configures the profile name from the options page while
// this worker is alive, reconnect under the new identity immediately rather
// than waiting for the next spin-up trigger.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && "bridgeProfileName" in changes) {
    if (port) {
      port.disconnect();
      port = null;
    }
    connect();
  }
});
