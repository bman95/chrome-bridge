// Chrome Bridge — MV3 service worker.
//
// Attaches chrome.debugger to tabs in the user's REAL Chrome profile (shares
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
// Request ordering (per-tab pipelining): requests that target ONE attached
// tab — "cdp"/"close"/"detach" carrying an explicit `tabId` — are processed
// FIFO per tab, in arrival order, and MAY be processed concurrently across
// different tabs; Python may therefore keep one request in flight per tab
// at a time (it still awaits a tab's reply before sending that tab's next
// request). Requests that mutate or read the session table as a whole —
// "open" (any `replace` shape), "attach", "tabs", any op WITHOUT a `tabId`,
// and any unrecognized op — are barriers: they run only after every
// in-flight request has settled, and every later request waits for them.
// This is what lets two concurrent "open"s never race the table while
// independent tabs still make progress in parallel.
//
// ---- Python -> extension (requests) ----------------------------------
// Every request carries a mandatory caller-chosen correlation id `id` (number
// or string) that is echoed back on exactly one reply. Six ops:
//
//   { "id": <id>, "op": "open",  "url": <string>, "replace": <bool?> }
//       Create a background tab (active:false — no focus steal) at `url`
//       in the current profile, then chrome.debugger.attach(..., "1.3").
//       The tab is BRIDGE-CREATED: "close" (and the onDetach reaper) will
//       remove it. `replace` is optional and defaults to true — the
//       single-tab mode: every currently attached tab is torn down first
//       (each removed only if bridge-created), so callers that never pass
//       `replace` keep the historical one-tab-at-a-time behavior. With
//       "replace": false the new tab is ADDITIONAL: existing attachments
//       are left untouched, and the caller addresses tabs explicitly via
//       `tabId` from there on. A non-boolean `replace` is rejected
//       (ok:false): a malformed flag must never pick the destructive
//       teardown-all default. The replace:true teardown runs BEFORE the
//       new tab is created, so a failed open (tab creation / attach
//       error) still leaves the prior tabs torn down — ok:false with
//       zero tabs attached.
//
//   { "id": <id>, "op": "attach" }
//       Attach to the ACTIVE tab — the page the user is looking at. A
//       service worker has no window of its own, so "current window"
//       resolves to this profile's LAST-FOCUSED Chrome window. Always
//       single-tab, like a replace:true "open": once the new attachment is
//       secured, every OTHER attached tab is torn down (each removed only
//       if bridge-created). Fails (ok:false) when there is no active tab
//       ("no active tab", errorCode "no_active_tab"), on attach contention
//       (DevTools open), or on a restricted page (chrome:// etc.); a failed
//       attach leaves the existing sessions fully intact and never removes
//       the user's tab. The tab is MERELY ATTACHED: "close" detaches but
//       leaves it open.
//
//   { "id": <id>, "op": "cdp",   "method": <string>, "params": <object?>, "tabId": <number?> }
//       chrome.debugger.sendCommand on an attached tab. `params` optional
//       (defaults to {}). `method` is a raw CDP method, e.g. "Page.navigate".
//       `tabId` optional: targets that attached tab (unknown/unattached
//       tabId -> ok:false with an actionable error). Without `tabId`:
//       exactly one tab attached -> targets it (legacy behavior); several
//       -> ok:false "ambiguous: multiple tabs attached; pass tabId"; none
//       -> ok:false "no attached tab".
//
//   { "id": <id>, "op": "close", "tabId": <number?> }
//       Detach the debugger from one attached tab. Remove the tab only if
//       it was bridge-created ("open"); a tab joined via "attach" is left
//       open for the user. `tabId` optional, resolved like "cdp" — except
//       that with no `tabId` and NO attached tab, "close" is a successful
//       no-op (ok:true) for ANY caller generation: there is no session
//       state a stale client could clobber.
//
//   { "id": <id>, "op": "detach", "tabId": <number?> }
//       Detach the debugger from one attached tab, ALWAYS leaving the tab
//       open — even a bridge-created one. `tabId` optional, resolved like
//       "close" (idempotent ok:true when no `tabId` is given and no tab is
//       attached). The one-shot `nav` ends with this because its
//       no-active-tab fallback creates a bridge-owned tab that "close" would
//       remove: ending with "detach" leaves it open and clears the entry
//       so no stale attachment/ownership survives into the next session's
//       "attach" teardown. (Read one-shots that only ever "attach" an unowned
//       tab end with "close", which is equivalent for a tab it doesn't own.)
//
//   { "id": <id>, "op": "tabs" }
//       List currently attached tabs, for client-side introspection and
//       post-reconnect recovery. Read-only — touches no session state, and
//       deliberately NOT generation-fenced: a reconnected client (whose
//       generation cannot match entries created by its predecessor) may
//       still list leftover tabs to decide whether to re-"open". Listing is
//       the full extent of cross-generation access: a predecessor's entries
//       cannot be driven or individually closed by a successor (see the
//       generation-fence note below) — a replace:true "open" is the reaper
//       that reclaims them.
//
// ---- extension -> Python --------------------------------------------
// Replies (exactly one per request, correlated by `id`):
//
//   { "id": <id>, "ok": true,  "tabId": <number> }   // reply to "open"/"attach"
//   { "id": <id>, "ok": true,  "result": <object> }  // reply to "cdp"
//   { "id": <id>, "ok": true }                        // reply to "close"/"detach"
//   { "id": <id>, "ok": true,  "tabs": [{"tabId": <number>}, ...] } // reply to "tabs"
//   { "id": <id>, "ok": false, "error": <string> }   // any op failed
//       Failure replies MAY additionally carry a machine-readable
//       "errorCode" where a caller must branch on the failure kind
//       (today: "no_active_tab" from "attach", which the CLI's nav
//       fallback keys on).
//
// Unsolicited events (no `id`; `event:true` discriminator). Every event
// carries the source `tabId` so a client holding several attached tabs can
// route it to the right tab handle, plus the `sessionGeneration` of the
// entry that produced it — nmh.py's event fence consumes and STRIPS that
// field (it never reaches chrome_bridge.bridge.client), dropping events
// left over from a predecessor client session:
//
//   { "event": true, "type": "cdp",     "method": <string>, "params": <object>,
//     "tabId": <number>, "sessionGeneration": <generation> }
//       A chrome.debugger.onEvent from an attached tab (raw CDP event).
//
//   { "event": true, "type": "detached", "reason": <string>,
//     "tabId": <number>, "sessionGeneration": <generation> }
//       chrome.debugger.onDetach fired for that tab (target closed, devtools
//       opened, SW evicted, etc.). After this that tab is no longer attached;
//       other attached tabs are unaffected. Python must treat that tab's
//       session as gone and re-"open" if it wants to resume.
//
// Contract notes:
//   * Session table: one entry per attached tab. The single-tab invariant is
//     now the DEFAULT (replace:true "open", and "attach") rather than the
//     only mode: those ops tear down every previously attached tab (each
//     removed only if bridge-created), so callers that never pass
//     "replace": false hold at most one tab and cannot leak tabs by
//     re-opening. "attach" secures the NEW attachment before that teardown,
//     so a failed attach leaves the existing sessions intact. Exception:
//     "attach" when the active tab is ALREADY attached performs an ownership
//     handover on that entry (bridgeCreated -> false, generation transfer)
//     instead of a re-attach; every other tab is still torn down. "open"
//     with "replace": false adds an entry without touching the rest; an op
//     without "tabId" while several tabs are attached fails as ambiguous
//     rather than guessing.
//   * Generation fence, per entry: a "cdp"/"close"/"detach" whose request
//     generation does not match the target entry's fails ("open"/"attach"
//     carry no target and are unfenced). An explicit "tabId" does NOT bypass
//     the fence — a tab from a dead session may since have been adopted by
//     a successor (the "attach" handover), and a queued stale frame must
//     not touch it. The mismatch reply for an explicit tabId says the tab
//     belongs to a different client session and points at replace:true
//     "open" as the reaper; implicit-target ops keep the historical "stale
//     client session" strings.
//   * Each entry records whether its tab was bridge-created ("open") or
//     merely attached ("attach"). Teardown — the "close" op and the onDetach
//     reaper — removes the tab only when bridge-created; a user tab the
//     bridge only attached to is never removed.
//   * The extension keeps NO durable state across MV3 service-worker eviction
//     (aside from the profile name in chrome.storage.local, read once at each
//     spin-up). Replies and events flow only while the native port is up. A
//     reply CAN be lost: if the worker is evicted (or the port drops) while a
//     `cdp` op is awaiting `sendCommand`, no reply is delivered. Therefore
//     Python MUST enforce a per-`id` timeout and, on reconnect, treat the
//     session as possibly gone — re-"open" and re-`Page.enable` rather than
//     assume the prior tabs are still attached ("tabs" lists whatever
//     survived). Events that occur while the port is down are likewise
//     dropped.
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
// Session table: one entry per attached tab (chrome.debugger natively keys
// every attach/sendCommand/onEvent/onDetach by tabId, so holding several
// concurrent debuggees is safe; requests are ordered per tab, with
// table-level ops serialized against everything — see the request chains
// below).
// Map<tabId, { sessionGeneration, owner, bridgeCreated }>:
//   sessionGeneration — generation injected into request ids by nmh.py.
//       Events have no correlation id, so each entry carries the generation
//       of the client session that opened/attached its tab, relayed on that
//       tab's events to prevent a late event from an old loopback client
//       session leaking into its successor. Per-request staleness checks
//       compare against the TARGET entry's generation.
//   owner — host name of the identity that opened/attached the tab. Lets the
//       profile-rename path detect a tab that predates the rename even when
//       the live port already matches the new identity — a reconnect alarm
//       can dial the new name in the gap between the storage write committing
//       and the onChanged handler running, while the old identity's tab is
//       still held.
//   bridgeCreated — true when the bridge created the tab ("open"); false when
//       it merely attached to the user's active tab ("attach"). Teardown
//       removes the tab only when true — see the WIRE PROTOCOL contract notes.
const sessions = new Map();

// Request ordering (the WIRE PROTOCOL "per-tab pipelining" contract).
// Native Messaging listeners do not await async callbacks, so ordering is
// imposed here with promise chains: one chain per targeted tab, plus one
// GLOBAL chain for everything that mutates or reads the session table as a
// whole — "open" (its replace:true shape is the tear-everything-down
// reaper), "attach" (tears down every other tab on success), "tabs", any op
// without a tabId (implicit-target resolution reads the whole table), and
// unknown ops. A global op first drains every chain (in-flight per-tab ops
// included), and every later op — per-tab or global — queues behind it;
// per-tab ops on different tabs otherwise interleave freely while ops on
// one tab stay FIFO. Two overlapping `open` calls can therefore never both
// see no tab, create one each, and race to overwrite the shared session
// state.
//
// Lifetime: the chains order ONE port's requests. dial() resets them when it
// installs a new port, because an entry can legitimately never settle — a
// sendCommand awaiting a promise the page never resolves (Runtime.evaluate
// with awaitPromise) outlives the client's own timeout — and a session that
// FOLLOWS A PORT SWAP must not queue behind the dead port's wedge. In
// particular the replace:true reaper "open", the designated recovery op, is
// what CLEARS such a wedge (its teardown's debugger.detach settles the hung
// command); parked behind it, recovery would be impossible. (A successor
// loopback client on the SAME still-live port shares that port's chains, as
// it always shared the single pre-split chain; a wedge there ends with the
// worker's eviction dropping the port.) Already-queued old-port ops still
// run in their original order and bail at onPythonMessage's entry
// port-staleness gate; mid-flight ones re-check port currency at — and,
// for the multi-tab teardown loops, between — their await boundaries, so a
// resumed stale op can neither reply nor touch entries the successor
// session created or adopted.
let globalChain = Promise.resolve();
const tabChains = new Map();

// Chain `fn` after every queued op, global and per-tab, and make every later
// op wait for it. Stored tails never reject: `fn` is wrapped with a catch so
// one failed request cannot suppress every later one (a disconnect can make
// postMessage throw even inside the handler's error path).
function enqueueGlobal(fn) {
  const barrier = Promise.all([globalChain, ...tabChains.values()]);
  const run = barrier.then(fn).catch((error) => {
    console.warn("chrome-bridge: request failed:", error);
  });
  globalChain = run;
  // The old per-tab tails are absorbed into the barrier; later per-tab ops
  // must chain on the new global tail, not slip past it via a stale entry.
  tabChains.clear();
  return run;
}

// Chain `fn` after the target tab's last queued op (or the last global op,
// whichever is later — enqueueGlobal cleared the map, so a surviving entry
// already queues after the last global op). Same never-rejects wrapping as
// enqueueGlobal. The entry self-cleans once its chain drains, so closed
// tabs do not accumulate map entries.
function enqueueTab(tabId, fn) {
  const prev = tabChains.get(tabId) || globalChain;
  const run = prev.then(fn).catch((error) => {
    console.warn("chrome-bridge: request failed:", error);
  });
  tabChains.set(tabId, run);
  run.then(() => {
    if (tabChains.get(tabId) === run) tabChains.delete(tabId);
  });
  return run;
}

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
  // Fresh port, fresh ordering state (see the chains' Lifetime note): the
  // new session's requests must never queue behind a dead session's
  // never-settling entry.
  globalChain = Promise.resolve();
  tabChains.clear();
  // Route each request onto the ordering chains (see their comment block):
  // an explicit tabId on a tab-targeted op pins it to that tab's chain
  // (same-tab FIFO, cross-tab interleave); everything else — "open",
  // "attach", "tabs", implicit-target ops, unknown ops — is a session-table
  // op and takes the global barrier chain.
  p.onMessage.addListener((msg) => {
    const run = () => onPythonMessage(msg, p);
    if (
      msg.tabId !== undefined &&
      (msg.op === "cdp" || msg.op === "close" || msg.op === "detach")
    ) {
      enqueueTab(msg.tabId, run);
    } else {
      enqueueGlobal(run);
    }
  });
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
  // Staleness gate: ops carry side effects on shared session state (the
  // session table, tabs, the debugger attachments), not just replies. A
  // request from a port that has been replaced (profile rename) or dropped
  // must not touch the successor's session, so bail at entry — and, since a
  // replacement can land mid-op, re-check after the awaits below before
  // mutating shared state (or, in the attach path, roll back the op's own
  // freshly created tab).
  if (port !== fromPort) return;
  const requestGeneration = (
    Array.isArray(msg.id) &&
    msg.id.length === 3 &&
    msg.id[0] === RELAY_SESSION_ID_MARKER
  ) ? msg.id[1] : null;
  try {
    if (msg.op === "open") {
      if (msg.replace !== undefined && typeof msg.replace !== "boolean") {
        // Never let a malformed flag pick the DESTRUCTIVE default: a truthy
        // non-boolean (e.g. the string "false") must not tear every tab down.
        reply(msg.id, { ok: false, error: 'invalid "replace": expected a boolean' });
        return;
      }
      // Single-tab mode is the DEFAULT: unless the caller opts out with
      // "replace": false, tear down every attached tab so a re-open (e.g.
      // after a dropped detach) cannot orphan them. replace:false adds an
      // ADDITIONAL tab, leaving existing attachments untouched. Torn down
      // one tab at a time with a port-currency gate BETWEEN iterations: the
      // ordering chains reset when a new port is dialed, so a successor
      // session's ops can run while this loop is suspended in an await
      // (e.g. tabs.remove held up by a beforeunload dialog) — and closeTab
      // re-reads the table, so a stale iteration could otherwise destroy an
      // entry the successor just created or adopted.
      if (msg.replace !== false) {
        for (const priorTabId of [...sessions.keys()]) {
          if (port !== fromPort) return; // replaced mid-teardown: walk away
          await closeTab(priorTabId);
        }
      }
      if (port !== fromPort) return; // replaced while closing prior tabs
      // active:false opens the tab in the current window without focusing it,
      // sharing the real profile's cookies/login state. We deliberately use
      // tabs.create over windows.create: it satisfies "no focus steal" without
      // spawning a separate window to manage/teardown.
      const tab = await chrome.tabs.create({ url: msg.url, active: false });
      try {
        await chrome.debugger.attach({ tabId: tab.id }, CDP_PROTOCOL_VERSION);
      } catch (e) {
        // Attach failed (DevTools already open, debugger contention, crash).
        // Remove the orphaned tab and leave the table untouched before
        // surfacing.
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
        // the table — nothing was committed for this tab yet.
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
      sessions.set(tab.id, {
        sessionGeneration: requestGeneration,
        owner: fromPort.bridgeHostName,
        bridgeCreated: true,
      });
      reply(msg.id, { ok: true, tabId: tab.id });
    } else if (msg.op === "attach") {
      // Query FIRST so removing prior owned tabs cannot shift which tab is
      // active before we read it. A service worker has no window of its own,
      // so currentWindow resolves to this profile's LAST-FOCUSED window (see
      // the contract block).
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (port !== fromPort) return;
      if (!tab || tab.id === undefined) {
        // NOTE: the CLI's nav fallback keys on the errorCode below
        // (src/chrome_bridge/cli.py) — keep it stable.
        reply(msg.id, {
          ok: false,
          error: "no active tab",
          errorCode: "no_active_tab",
        });
        return;
      }
      if (!sessions.has(tab.id)) {
        // Acquire before release: attach the NEW tab first, so a failed
        // attach (contention, restricted chrome:// page, crash) leaves the
        // existing sessions fully intact — the outer catch replies ok:false
        // and no tab is removed. chrome.debugger supports concurrent
        // debuggees, so briefly holding the extra attachment is safe.
        await chrome.debugger.attach({ tabId: tab.id }, CDP_PROTOCOL_VERSION);
        if (port !== fromPort) {
          try {
            await chrome.debugger.detach({ tabId: tab.id });
          } catch (_) {
            /* already detached */
          }
          return;
        }
      }
      // Commit the new session BEFORE tearing down prior tabs: the teardown
      // awaits below run the event loop, and an onDetach for the
      // just-attached tab landing there must be recognized (it matches on
      // the table entry), relayed under the new generation, and drop the
      // entry — a held-but-uncommitted attachment would silently swallow it
      // and reply ok:true for a tab whose debugger is already gone. When the
      // active tab was ALREADY attached (e.g. the user focused a
      // bridge-created one) this commit is the ownership handover: it is the
      // user's tab now (bridgeCreated: false), so teardown must never
      // remove it. Keep a reference to OUR entry object: the post-teardown
      // checks below compare by identity, not by generation — generations
      // are per-host-process counters, so a successor client on a fresh
      // host after a port swap can carry the SAME number (both first
      // sessions are generation 1), and a value compare would let the
      // stale rollback destroy the successor's just-secured attachment.
      const ourEntry = {
        sessionGeneration: requestGeneration,
        owner: fromPort.bridgeHostName,
        bridgeCreated: false,
      };
      sessions.set(tab.id, ourEntry);
      // Single-tab invariant, same as a replace:true "open": tear down every
      // OTHER attached tab (each removed only if bridge-created). Same
      // per-iteration port gate as the "open" teardown loop — a stale
      // iteration after a port swap must not destroy entries the successor
      // session created or adopted; breaking falls into the rollback below,
      // which already handles the replaced-port case.
      for (const priorTabId of [...sessions.keys()]) {
        if (port !== fromPort) break;
        if (priorTabId !== tab.id) await closeTab(priorTabId);
      }
      if (port !== fromPort) {
        // This session died mid-teardown. Roll back only what is still OURS
        // — by OBJECT IDENTITY (see the ourEntry note above): a successor
        // may already have taken the entry over (its own "attach" to this
        // same active tab replaces the entry object), and its generation
        // number can collide with ours across a host swap.
        if (sessions.get(tab.id) === ourEntry) {
          sessions.delete(tab.id);
          try {
            await chrome.debugger.detach({ tabId: tab.id });
          } catch (_) {
            /* already detached */
          }
        }
        return;
      }
      if (sessions.get(tab.id) !== ourEntry) {
        // A mid-teardown onDetach for the just-attached tab already ended
        // this session (the detached event was relayed above) — replying
        // ok:true would claim a tab whose debugger is gone. Identity, not
        // generation, for the same collision-proofing as the rollback.
        reply(msg.id, { ok: false, error: "debugger detached during attach" });
        return;
      }
      reply(msg.id, { ok: true, tabId: tab.id });
    } else if (msg.op === "cdp") {
      if (msg.tabId === undefined && sessions.size === 0) {
        reply(msg.id, { ok: false, error: "no attached tab" });
        return;
      }
      const tabId = resolveTabId(msg, reply);
      if (tabId === null) return;
      if (requestGeneration !== sessions.get(tabId).sessionGeneration) {
        reply(msg.id, {
          ok: false,
          error: staleSessionError(msg, tabId, "stale client session; open a tab first"),
        });
        return;
      }
      const res = await chrome.debugger.sendCommand(
        { tabId },
        msg.method,
        msg.params || {}
      );
      reply(msg.id, { ok: true, result: res });
    } else if (msg.op === "close") {
      if (msg.tabId === undefined && sessions.size === 0) {
        // Nothing attached: successful no-op for ANY caller generation —
        // there is no session state a stale client could clobber.
        reply(msg.id, { ok: true });
        return;
      }
      const tabId = resolveTabId(msg, reply);
      if (tabId === null) return;
      if (requestGeneration !== sessions.get(tabId).sessionGeneration) {
        reply(msg.id, {
          ok: false,
          error: staleSessionError(msg, tabId, "stale client session"),
        });
        return;
      }
      await closeTab(tabId);
      reply(msg.id, { ok: true });
    } else if (msg.op === "detach") {
      // Release a tab WITHOUT removing it, whatever its ownership. Dropping
      // the table entry here matters as much as the detach itself: a stale
      // entry would make the NEXT session's "attach"/"open" teardown remove
      // a tab a prior session promised to leave open.
      if (msg.tabId === undefined && sessions.size === 0) {
        // Nothing attached: idempotent ok:true for ANY caller generation —
        // there is no session state a stale client could clobber, and the
        // contract promises this succeeds.
        reply(msg.id, { ok: true });
        return;
      }
      const tabId = resolveTabId(msg, reply);
      if (tabId === null) return;
      if (requestGeneration !== sessions.get(tabId).sessionGeneration) {
        reply(msg.id, {
          ok: false,
          error: staleSessionError(msg, tabId, "stale client session"),
        });
        return;
      }
      sessions.delete(tabId);
      try {
        await chrome.debugger.detach({ tabId });
      } catch (e) {
        /* already detached */
      }
      reply(msg.id, { ok: true });
    } else if (msg.op === "tabs") {
      // Read-only introspection; deliberately not generation-fenced (see the
      // contract block) so a reconnected client can discover leftover tabs.
      reply(msg.id, {
        ok: true,
        tabs: [...sessions.keys()].map((tabId) => ({ tabId })),
      });
    } else {
      reply(msg.id, { ok: false, error: "unknown op: " + String(msg.op) });
    }
  } catch (e) {
    reply(msg.id, { ok: false, error: String(e) });
  }
}

// Resolve which attached tab a "cdp"/"close"/"detach" op targets. With
// msg.tabId, that exact entry must exist. Without it, a single attached tab
// is the implicit target (the historical single-tab behavior); several
// attached tabs are ambiguous — never guess. Returns the tabId, or null
// after emitting the error reply. The zero-tabs/no-tabId case is NOT handled
// here: its outcome is op-specific ("cdp" fails, "close"/"detach" are
// idempotent no-ops), so callers gate on it before calling.
function resolveTabId(msg, reply) {
  if (msg.tabId !== undefined) {
    if (!sessions.has(msg.tabId)) {
      reply(msg.id, {
        ok: false,
        error:
          "no attached tab with tabId " +
          String(msg.tabId) +
          '; use the "tabs" op to list attached tabs',
      });
      return null;
    }
    return msg.tabId;
  }
  if (sessions.size > 1) {
    reply(msg.id, {
      ok: false,
      error: "ambiguous: multiple tabs attached; pass tabId",
    });
    return null;
  }
  return sessions.keys().next().value;
}

// Message for a per-entry generation-fence failure. Implicit targets keep
// the historical strings byte-identical; an explicit tabId gets an accurate,
// actionable message instead — the caller is typically a FRESH client
// probing a predecessor's leftover entry (found via the un-fenced "tabs"
// op), not a stale one. The fence itself never yields to an explicit tabId:
// see the contract notes.
function staleSessionError(msg, tabId, legacyError) {
  if (msg.tabId === undefined) return legacyError;
  return (
    "tab " +
    String(tabId) +
    ' belongs to a different client session; a replace:true "open" reclaims leftover tabs'
  );
}

// Tear down ONE attached tab: drop its table entry first (so nothing else
// double-handles the tab while the teardown awaits run), then detach and —
// bridge-created tabs only — remove it. No-op for a tab without an entry.
async function closeTab(tabId) {
  const entry = sessions.get(tabId);
  if (entry === undefined) return;
  sessions.delete(tabId);
  await teardownTab(tabId, entry.bridgeCreated);
}

// The teardown-everything path — replace:true "open", profile rename /
// identity change: closes every table entry.
async function closeAllTabs() {
  for (const tabId of [...sessions.keys()]) {
    await closeTab(tabId);
  }
}

// Detach first, then remove (bridge-created tabs only — a user tab the
// bridge merely attached to stays open). Swallow per-step errors so a
// half-gone tab (e.g. already closed by the user) still fully tears down.
// Parameterized (not reading the table) so callers tear down by values
// captured before the entry was dropped or replaced.
async function teardownTab(tabId, bridgeCreated) {
  try {
    await chrome.debugger.detach({ tabId });
  } catch (e) {
    /* already detached */
  }
  if (bridgeCreated) {
    try {
      await chrome.tabs.remove(tabId);
    } catch (e) {
      /* already removed */
    }
  }
}

// Relay raw CDP events from attached tabs back to Python, tagged with the
// source tabId so the client can route them to the right tab handle.
chrome.debugger.onEvent.addListener((source, method, params) => {
  const entry = sessions.get(source.tabId);
  if (port && entry !== undefined) {
    port.postMessage({
      event: true,
      type: "cdp",
      method,
      params,
      tabId: source.tabId,
      sessionGeneration: entry.sessionGeneration,
    });
  }
});

// Relay debugger detach (target gone, devtools opened, SW evicted, ...).
// Removes only the detached tab's entry; other attached tabs are unaffected.
chrome.debugger.onDetach.addListener((source, reason) => {
  const entry = sessions.get(source.tabId);
  if (entry === undefined) return;
  sessions.delete(source.tabId);
  if (port)
    port.postMessage({
      event: true,
      type: "detached",
      reason,
      tabId: source.tabId,
      sessionGeneration: entry.sessionGeneration,
    });
  // If a BRIDGE-CREATED tab still exists (detach was NOT caused by tab
  // closure — e.g. the user opened DevTools), reap it so it doesn't linger
  // as an orphan. A user tab the bridge only attached to is never removed.
  if (entry.bridgeCreated) chrome.tabs.remove(source.tabId).catch(() => {});
});

// Keepalive. MV3 service workers are evicted after ~30 s idle. The PRIMARY
// keepalive is the open native-messaging port: while `connectNative` is
// connected, message traffic on it resets the idle timer, so an active job
// (Python <-> extension chatter) keeps the worker alive on its own.
//
chrome.alarms.onAlarm.addListener((alarm) => {
  // The one-shot alarm is created only after a real dial failure or disconnect.
  // It survives worker eviction and brings the bridge back unattended without
  // waking an extension that has never been configured.
  if (alarm.name === RECONNECT_ALARM) {
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
//
// Deliberately NOT queued on the global chain, even though it mutates the
// session table: this teardown is the recovery valve for a WEDGED session —
// its closeAllTabs() detaches every debuggee, which is what settles a hung
// sendCommand — so parking it behind the wedge would make a rename
// permanently un-runnable (and, chained, a single never-settling op would
// survive even the port swap). Its safety against in-flight requests is
// what it has always been: single-threaded JS, the per-op port-staleness
// gates (ops re-check `port !== fromPort` after every await), and the
// mid-op rollback paths. The chains reset when the new identity's port is
// dialed (see dial()).
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
    // Identity already matches: keep the healthy connection. But held tabs
    // may still predate the rename (port dropped, tabs kept for
    // same-identity resume, then a reconnect-alarm dial landed the NEW name
    // before this handler ran) — only tabs opened under the wanted identity
    // may survive.
    if (wanted !== null && port.bridgeHostName === wanted) {
      for (const [tabId, entry] of [...sessions]) {
        if (entry.owner !== wanted) await closeTab(tabId);
      }
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
  // deliberately keeps the tabs for same-identity resume). Reap every tab so
  // the new identity does not inherit them: otherwise the onEvent/onDetach
  // relays (keyed on the global port + the session table) would feed the old
  // tabs' CDP traffic to the new host, and a premature `cdp` op from the new
  // client would silently target the old session's tabs.
  await closeAllTabs();
  await connect();
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && "bridgeProfileName" in changes) {
    reconnectWithNewIdentity();
  }
});
