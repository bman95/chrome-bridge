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
// or string) that is echoed back on exactly one reply. Three ops:
//
//   { "id": <id>, "op": "open",  "url": <string> }
//       Create a background tab (active:false — no focus steal) at `url`
//       in the current profile, then chrome.debugger.attach(..., "1.3").
//
//   { "id": <id>, "op": "cdp",   "method": <string>, "params": <object?> }
//       chrome.debugger.sendCommand on the attached tab. `params` optional
//       (defaults to {}). `method` is a raw CDP method, e.g. "Page.navigate".
//
//   { "id": <id>, "op": "close" }
//       Detach the debugger and remove the tab.
//
// ---- extension -> Python --------------------------------------------
// Replies (exactly one per request, correlated by `id`):
//
//   { "id": <id>, "ok": true,  "tabId": <number> }   // reply to "open"
//   { "id": <id>, "ok": true,  "result": <object> }  // reply to "cdp"
//   { "id": <id>, "ok": true }                        // reply to "close"
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
//   * Single attached tab at a time (`dbgTabId`). A second "open" replaces the
//     reference: it first tears down any existing tab, so callers cannot leak
//     tabs by re-opening. Callers (chrome_bridge.bridge.client) open one tab.
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
const RELAY_SESSION_ID_MARKER = "chrome-bridge-session";

let port = null;
// Single-flight guard for connect(): the promise of the dial currently in
// progress, or null when none is. Multiple spin-up triggers (top-level call,
// onInstalled, onStartup, alarms) routinely overlap; they must share one dial
// rather than each calling connectNative — a second connectNative would spawn
// a second native host process that immediately dies on EADDRINUSE, and its
// onDisconnect used to null the healthy connection's global `port`.
let connectPromise = null;
let dbgTabId = null;
// Generation injected into request ids by nmh.py. Events have no correlation
// id, so carry this explicitly to prevent a late event from an old loopback
// client session leaking into its successor.
let relaySessionGeneration = null;
// Host name of the identity that opened the current tab (set alongside
// dbgTabId on a successful "open"; meaningful only while dbgTabId !== null).
// Lets the profile-rename path detect a tab that predates the rename even
// when the live port already matches the new identity — a reconnect alarm can
// dial the new name in the gap between the storage write committing and the
// onChanged handler running, while the old identity's tab is still held.
let dbgTabOwner = null;

async function nativeHostName() {
  const { bridgeProfileName } = await chrome.storage.local.get("bridgeProfileName");
  if (!bridgeProfileName) {
    throw new Error(
      "no profile name configured — open this extension's options page and set one"
    );
  }
  return `${NATIVE_HOST_PREFIX}.${bridgeProfileName}`;
}

// Single-flight: concurrent calls while a connection is up return immediately;
// concurrent calls while a dial is in flight all share that dial's promise. A
// second connectNative is never issued while a connection is up or being
// established.
function connect() {
  if (port) return Promise.resolve();
  if (!connectPromise) {
    connectPromise = dial().finally(() => {
      connectPromise = null;
    });
  }
  return connectPromise;
}

// The actual dial. Never called directly — only via connect(), which
// guarantees at most one invocation is in flight at a time. Never rejects:
// every failure path is handled (warn or schedule a retry) so callers can
// fire-and-forget.
async function dial() {
  let hostName;
  try {
    hostName = await nativeHostName();
  } catch (e) {
    // No profile name set yet (first load, before the user visited options).
    // Nothing to dial; the user must configure it once. Do not spam retries.
    console.warn("chrome-bridge:", e.message);
    return;
  }
  let p;
  try {
    p = chrome.runtime.connectNative(hostName);
  } catch (e) {
    // Host not registered / not launchable; nothing to relay to. Schedule a
    // retry so a transient launch failure (e.g. uv still warming the venv)
    // self-heals rather than wedging until the periodic keepalive alarm.
    scheduleReconnect();
    return;
  }
  // Each listener captures ITS port (p) and acts on the global only when the
  // global still refers to it — a stale port's callbacks must never touch a
  // healthy successor connection.
  // Remember which host this port dialed so the profile-rename path can tell
  // whether an in-flight dial already landed on the wanted identity.
  p.bridgeHostName = hostName;
  p.onMessage.addListener((msg) => onPythonMessage(msg, p));
  p.onDisconnect.addListener(() => {
    // chrome.runtime.lastError is set here when the host died/was missing
    // (e.g. the worker was evicted and the native host Chrome spawned exited).
    // Drop the reference and schedule a near-term re-dial so the bridge comes
    // back without waiting for the next periodic keepalive tick. Only this
    // port's owner may clear the global: if `port` already points elsewhere
    // (we were deliberately replaced, e.g. profile rename), leave it alone.
    if (port === p) {
      port = null;
      scheduleReconnect();
    }
  });
  port = p;
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

async function onPythonMessage(msg, fromPort) {
  // Route replies to the port the request arrived on — and only while that
  // port is still the live one. A request that was in flight when its port got
  // replaced (profile rename) or dropped must not leak a reply to a successor
  // connection, where it would corrupt the new host's id space.
  const reply = (id, payload) => {
    if (port === fromPort) fromPort.postMessage({ id, ...payload });
  };
  // Staleness gate: ops carry side effects on shared session state (dbgTabId,
  // tabs, the debugger attachment), not just replies. A request from a port
  // that has been replaced (profile rename) or dropped must not touch the
  // successor's session, so bail at entry — and, since a replacement can land
  // mid-op, re-check after the awaits below before mutating shared state (or,
  // in the attach path, roll back the op's own freshly created tab).
  if (port !== fromPort) return;
  if (
    Array.isArray(msg.id) &&
    msg.id.length === 3 &&
    msg.id[0] === RELAY_SESSION_ID_MARKER
  ) {
    relaySessionGeneration = msg.id[1];
  }
  try {
    if (msg.op === "open") {
      // Enforce the single-tab invariant: tear down any prior tab so a re-open
      // (e.g. after a dropped detach) cannot orphan it.
      if (dbgTabId !== null) await closeTab();
      if (port !== fromPort) return; // replaced while closing the old tab
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
      if (port !== fromPort) {
        // Port was replaced while we were creating/attaching: this session is
        // dead. Tear down the just-created tab and walk away without touching
        // dbgTabId — it may already belong to the successor's session.
        try {
          await chrome.debugger.detach({ tabId: tab.id });
        } catch (_) {
          /* already detached */
        }
        try {
          await chrome.tabs.remove(tab.id);
        } catch (_) {
          /* already removed */
        }
        return;
      }
      dbgTabId = tab.id;
      dbgTabOwner = fromPort.bridgeHostName;
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
    } else {
      reply(msg.id, { ok: false, error: "unknown op: " + String(msg.op) });
    }
  } catch (e) {
    reply(msg.id, { ok: false, error: String(e) });
  }
}

async function closeTab() {
  const tabId = dbgTabId;
  dbgTabId = null;
  if (tabId === null) return;
  // Detach first, then remove. Swallow per-step errors so a half-gone tab
  // (e.g. already closed by the user) still fully tears down.
  try {
    await chrome.debugger.detach({ tabId });
  } catch (e) {
    /* already detached */
  }
  try {
    await chrome.tabs.remove(tabId);
  } catch (e) {
    /* already removed */
  }
}

// Relay raw CDP events from the attached tab back to Python.
chrome.debugger.onEvent.addListener((source, method, params) => {
  if (port && source.tabId === dbgTabId) {
    port.postMessage({
      event: true,
      type: "cdp",
      method,
      params,
      sessionGeneration: relaySessionGeneration,
    });
  }
});

// Relay debugger detach (target gone, devtools opened, SW evicted, ...).
chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId === dbgTabId) {
    const tabId = dbgTabId;
    dbgTabId = null;
    if (port)
      port.postMessage({
        event: true,
        type: "detached",
        reason,
        sessionGeneration: relaySessionGeneration,
      });
    // If the tab still exists (detach was NOT caused by tab closure — e.g. the
    // user opened DevTools), reap it so it doesn't linger as an orphan.
    chrome.tabs.remove(tabId).catch(() => {});
  }
});

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
// a plain eviction-respawn. These routinely overlap (e.g. onInstalled and the
// top-level call both fire on an extension load); connect()'s single-flight
// guard collapses them onto one dial, so exactly one native host is spawned.
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

// If the user (re)configures the profile name from the options page while
// this worker is alive, reconnect under the new identity immediately rather
// than waiting for the next spin-up trigger.
async function reconnectWithNewIdentity() {
  // Let any in-flight dial settle first: it may have read the OLD profile name
  // before the storage write landed, so whatever it produced must be torn down
  // and re-dialed under the now-current name. (dial() never rejects, and the
  // single-flight guard means a fresh connect() below cannot piggyback on the
  // stale attempt — connectPromise is cleared once it settles.)
  if (connectPromise) await connectPromise;
  // The settled dial may instead have read the NEW name (the storage write
  // commits before onChanged fires). Tearing that healthy connection down and
  // immediately re-dialing the SAME host name would race the dying host for
  // its loopback port (EADDRINUSE, then up to a minute on the reconnect
  // alarm) — so skip the teardown when the identity already matches.
  let wanted = null;
  try {
    wanted = await nativeHostName();
  } catch (e) {
    // Name was cleared: tear down below; connect() will warn and stand down.
  }
  if (port) {
    // Identity already matches: keep the healthy connection. But the held tab
    // may still predate the rename (port dropped, tab kept for same-identity
    // resume, then a reconnect-alarm dial landed the NEW name before this
    // handler ran) — only a tab opened under the wanted identity may survive.
    if (wanted !== null && port.bridgeHostName === wanted) {
      if (dbgTabId !== null && dbgTabOwner !== wanted) await closeTab();
      return;
    }
    // Deliberate replacement: disconnect() does not fire our own onDisconnect
    // listener, and even if the host's exit raced us here, that listener only
    // acts when the global still points at its port.
    port.disconnect();
    port = null;
  }
  // The old identity's session is dead on any actual identity change — whether
  // we just disconnected its port or the port was already down (host died and
  // the rename landed inside the reconnect-alarm window; a mere port drop
  // deliberately keeps the tab for same-identity resume). Reap the tab so the
  // new identity does not inherit it: otherwise the onEvent/onDetach relays
  // (keyed on the global port + dbgTabId) would feed the old tab's CDP traffic
  // to the new host, and a premature `cdp` op from the new client would
  // silently target the old session's tab.
  await closeTab();
  await connect();
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && "bridgeProfileName" in changes) {
    reconnectWithNewIdentity();
  }
});
