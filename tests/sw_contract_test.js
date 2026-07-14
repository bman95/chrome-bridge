const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const debuggerEvents = [];
const debuggerDetaches = [];
const storageChanges = [];
const alarmEvents = [];
const removed = [];
const detached = [];
const sent = [];
let activeTab = null;
let nextTabId = 1;
// When set, the next chrome.debugger.attach throws this error once —
// simulates a restricted page (chrome://) or DevTools contention.
let attachFailure = null;

global.chrome = {
  storage: {
    local: { get: async () => ({}) },
    onChanged: { addListener: (fn) => storageChanges.push(fn) },
  },
  runtime: {
    connectNative: () => { throw new Error("unused in test"); },
    onStartup: { addListener: () => {} },
    onInstalled: { addListener: () => {} },
  },
  alarms: {
    create: () => {},
    onAlarm: { addListener: (fn) => alarmEvents.push(fn) },
  },
  tabs: {
    create: async () => ({ id: nextTabId++ }),
    query: async () => activeTab ? [activeTab] : [],
    remove: async (id) => { removed.push(id); },
  },
  debugger: {
    attach: async () => {
      if (attachFailure) {
        const error = attachFailure;
        attachFailure = null;
        throw error;
      }
    },
    detach: async ({ tabId }) => { detached.push(tabId); },
    sendCommand: async ({ tabId }) => { sent.push(tabId); return {}; },
    onEvent: { addListener: (fn) => debuggerEvents.push(fn) },
    onDetach: { addListener: (fn) => debuggerDetaches.push(fn) },
  },
};

const source = fs.readFileSync(
  path.join(__dirname, "..", "extension", "sw.js"),
  "utf8"
);
const tests = String.raw`
(async () => {
  const replies = [];
  const fakePort = {
    bridgeHostName: "com.chromebridge.bridge.personal",
    postMessage: (msg) => replies.push(msg),
  };
  port = fakePort;

  const id = (generation, original) => [RELAY_SESSION_ID_MARKER, generation, original];

  // A fresh worker has no attached tab: implicit-target cdp fails with the
  // legacy error.
  await onPythonMessage({ id: id(1, 0), op: "cdp", method: "Page.enable" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.equal(replies.at(-1).error, "no attached tab");

  await onPythonMessage({ id: id(1, 1), op: "open", url: "https://example.com" }, fakePort);
  assert.deepEqual([...sessions.keys()], [1]);
  assert.equal(sessions.get(1).bridgeCreated, true);
  assert.equal(sessions.get(1).sessionGeneration, 1);

  // Attaching the already-focused bridge tab hands ownership to the user and
  // transfers the session generation. Closing must detach but never remove it.
  activeTab = { id: 1 };
  await onPythonMessage({ id: id(2, 1), op: "attach" }, fakePort);
  assert.equal(sessions.get(1).bridgeCreated, false);
  assert.equal(sessions.get(1).sessionGeneration, 2);
  await onPythonMessage({ id: id(2, 2), op: "close" }, fakePort);
  assert.deepEqual(detached, [1]);
  assert.deepEqual(removed, []);

  // An owned tab is reaped by onDetach and its event retains the tab-bound
  // generation used by nmh.py's event fence — and carries the source tabId.
  await onPythonMessage({ id: id(3, 1), op: "open", url: "https://example.org" }, fakePort);
  assert.ok(sessions.has(2));
  debuggerDetaches[0]({ tabId: 2 }, "target_closed");
  await Promise.resolve();
  assert.deepEqual(removed, [2]);
  const event = replies.find((msg) => msg.event && msg.type === "detached");
  assert.equal(event.sessionGeneration, 3);
  assert.equal(event.tabId, 2);

  // attach with NO active tab replies ok:false with the structured errorCode
  // the CLI's nav fallback keys on (cli.py branches on "no_active_tab").
  activeTab = null;
  await onPythonMessage({ id: id(4, 1), op: "attach" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.equal(replies.at(-1).errorCode, "no_active_tab");
  assert.equal(sessions.size, 0);

  // "detach" with no tab attached is idempotent ok:true even for a fenced
  // caller, whose generation cannot match any table entry.
  await onPythonMessage({ id: id(4, 2), op: "detach" }, fakePort);
  assert.equal(replies.at(-1).ok, true);

  // Attaching a DIFFERENT active tab while an owned bridge tab is held: the
  // single-tab teardown removes the owned tab; the user tab attaches unowned.
  await onPythonMessage({ id: id(5, 1), op: "open", url: "https://example.net" }, fakePort);
  const ownedTab = replies.at(-1).tabId;
  activeTab = { id: 90 };
  await onPythonMessage({ id: id(6, 1), op: "attach" }, fakePort);
  assert.deepEqual([...sessions.keys()], [90]);
  assert.equal(sessions.get(90).bridgeCreated, false);
  assert.ok(removed.includes(ownedTab));
  assert.ok(!removed.includes(90));

  // A failed attach (restricted page, contention) leaves the prior session
  // fully intact — acquire-before-release. Hold an OWNED tab, then make the
  // attach to a different user tab throw.
  await onPythonMessage(
    { id: id(7, 1), op: "open", url: "https://example.com/keep" },
    fakePort
  );
  const keptTab = replies.at(-1).tabId;
  assert.ok(!removed.includes(90)); // the unowned tab 90 was detached, not removed
  activeTab = { id: 91 };
  attachFailure = new Error("Cannot attach to this target");
  await onPythonMessage({ id: id(8, 1), op: "attach" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.deepEqual([...sessions.keys()], [keptTab]); // prior session intact
  assert.equal(sessions.get(keptTab).bridgeCreated, true);
  assert.equal(sessions.get(keptTab).sessionGeneration, 7);
  assert.ok(!removed.includes(keptTab));
  assert.ok(!removed.includes(91)); // the user's tab is never removed on failure

  // The onDetach reaper must NEVER remove a merely-attached user tab; it
  // still emits the detached event and clears only that tab's entry.
  await onPythonMessage({ id: id(9, 1), op: "attach" }, fakePort);
  assert.ok(sessions.has(91));
  debuggerDetaches[0]({ tabId: 91 }, "target_closed");
  await Promise.resolve();
  assert.ok(!removed.includes(91));
  const userDetach = replies.filter((m) => m.event && m.type === "detached").at(-1);
  assert.equal(userDetach.reason, "target_closed");
  assert.equal(userDetach.sessionGeneration, 9);
  assert.equal(userDetach.tabId, 91);
  assert.equal(sessions.size, 0);

  // An onDetach for the JUST-ATTACHED tab landing during the prior tab's
  // teardown must be seen: the new session is committed before the teardown,
  // so the event is relayed under the new generation and the entry is
  // dropped — never a silent ok:true for an already-detached tab.
  await onPythonMessage(
    { id: id(10, 1), op: "open", url: "https://example.com/prior" },
    fakePort
  );
  const priorTab = replies.at(-1).tabId;
  activeTab = { id: 92 };
  const originalRemove = chrome.tabs.remove;
  chrome.tabs.remove = async (tabId) => {
    await originalRemove(tabId);
    // The new tab's debugger detaches while the prior tab is being removed.
    debuggerDetaches[0]({ tabId: 92 }, "target_closed");
  };
  await onPythonMessage({ id: id(11, 1), op: "attach" }, fakePort);
  chrome.tabs.remove = originalRemove;
  assert.ok(removed.includes(priorTab)); // prior owned tab torn down
  assert.ok(!removed.includes(92)); // the user's tab is never removed
  assert.equal(sessions.size, 0); // the mid-teardown detach was recognized
  const midTeardown = replies.filter((m) => m.event && m.type === "detached").at(-1);
  assert.equal(midTeardown.reason, "target_closed");
  assert.equal(midTeardown.sessionGeneration, 11);
  assert.equal(midTeardown.tabId, 92);
  assert.equal(replies.at(-1).ok, false); // never ok:true for a dead session

  // --- Multi-tab: "open" with replace:false adds an ADDITIONAL tab without
  // touching the existing one; ops route by tabId; closing one tab leaves
  // the other attached and responsive.
  activeTab = null;
  const removedBefore = removed.length;
  await onPythonMessage({ id: id(12, 1), op: "open", url: "https://a.example" }, fakePort);
  const tabA = replies.at(-1).tabId;
  await onPythonMessage(
    { id: id(12, 2), op: "open", url: "https://b.example", replace: false },
    fakePort
  );
  const tabB = replies.at(-1).tabId;
  assert.notEqual(tabA, tabB);
  assert.equal(sessions.size, 2);
  assert.equal(removed.length, removedBefore); // tabA untouched by the second open
  assert.equal(sessions.get(tabA).bridgeCreated, true);
  assert.equal(sessions.get(tabB).bridgeCreated, true);

  // "tabs" lists every attached tab.
  await onPythonMessage({ id: id(12, 3), op: "tabs" }, fakePort);
  assert.deepEqual(replies.at(-1), {
    id: id(12, 3),
    ok: true,
    tabs: [{ tabId: tabA }, { tabId: tabB }],
  });

  // cdp routes by tabId to the requested tab.
  await onPythonMessage(
    { id: id(12, 4), op: "cdp", method: "Page.enable", tabId: tabB },
    fakePort
  );
  assert.equal(replies.at(-1).ok, true);
  assert.equal(sent.at(-1), tabB);
  await onPythonMessage(
    { id: id(12, 5), op: "cdp", method: "Page.enable", tabId: tabA },
    fakePort
  );
  assert.equal(sent.at(-1), tabA);

  // Without tabId and several tabs attached, cdp/close refuse to guess.
  await onPythonMessage({ id: id(12, 6), op: "cdp", method: "Page.enable" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.match(replies.at(-1).error, /ambiguous/);
  await onPythonMessage({ id: id(12, 7), op: "close" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.match(replies.at(-1).error, /ambiguous/);

  // Unknown/unattached tabId -> actionable error.
  await onPythonMessage(
    { id: id(12, 8), op: "cdp", method: "Page.enable", tabId: 999 },
    fakePort
  );
  assert.equal(replies.at(-1).ok, false);
  assert.match(replies.at(-1).error, /999/);

  // CDP events carry the source tabId and that entry's own generation.
  debuggerEvents[0]({ tabId: tabB }, "Page.loadEventFired", {});
  const routed = replies.at(-1);
  assert.equal(routed.type, "cdp");
  assert.equal(routed.tabId, tabB);
  assert.equal(routed.sessionGeneration, 12);

  // Closing ONE tab by tabId tears down only that tab; the other stays
  // attached and responsive (single tab again -> implicit target).
  await onPythonMessage({ id: id(12, 9), op: "close", tabId: tabA }, fakePort);
  assert.ok(removed.includes(tabA));
  assert.ok(!removed.includes(tabB));
  assert.deepEqual([...sessions.keys()], [tabB]);
  await onPythonMessage({ id: id(12, 10), op: "cdp", method: "Page.enable" }, fakePort);
  assert.equal(replies.at(-1).ok, true);
  assert.equal(sent.at(-1), tabB);

  // Default "open" (replace:true) restores the single-tab mode: every
  // attached tab is torn down first, bridge-created ones removed.
  await onPythonMessage(
    { id: id(13, 1), op: "open", url: "https://c.example", replace: false },
    fakePort
  );
  const tabC = replies.at(-1).tabId;
  assert.equal(sessions.size, 2); // tabB + tabC
  await onPythonMessage({ id: id(14, 1), op: "open", url: "https://fresh.example" }, fakePort);
  const freshTab = replies.at(-1).tabId;
  assert.ok(removed.includes(tabB));
  assert.ok(removed.includes(tabC));
  assert.deepEqual([...sessions.keys()], [freshTab]);

  // "close" with no tabId and NO attached tab is a successful no-op even for
  // a fenced caller (nothing a stale client could clobber).
  await onPythonMessage({ id: id(14, 2), op: "close" }, fakePort);
  assert.equal(sessions.size, 0);
  await onPythonMessage({ id: id(15, 1), op: "close" }, fakePort);
  assert.equal(replies.at(-1).ok, true);

  // "tabs" with none attached replies an empty list.
  await onPythonMessage({ id: id(15, 2), op: "tabs" }, fakePort);
  assert.deepEqual(replies.at(-1).tabs, []);

  // Per-entry staleness: a later client generation cannot drive a tab
  // attached under an earlier one — but "tabs" is not fenced, so the new
  // generation can still list leftovers before re-"open"ing.
  await onPythonMessage({ id: id(16, 1), op: "open", url: "https://gen.example" }, fakePort);
  const genTab = replies.at(-1).tabId;
  await onPythonMessage(
    { id: id(17, 1), op: "cdp", method: "Page.enable", tabId: genTab },
    fakePort
  );
  assert.equal(replies.at(-1).ok, false);
  assert.match(replies.at(-1).error, /different client session/);
  // The implicit-target fence keeps the historical string byte-identical.
  await onPythonMessage({ id: id(17, 5), op: "cdp", method: "Page.enable" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.equal(replies.at(-1).error, "stale client session; open a tab first");
  await onPythonMessage({ id: id(17, 2), op: "tabs" }, fakePort);
  assert.deepEqual(replies.at(-1).tabs, [{ tabId: genTab }]);
  await onPythonMessage({ id: id(17, 3), op: "open", url: "https://gen2.example" }, fakePort);
  assert.ok(removed.includes(genTab)); // replace:true recovered the leftover
  assert.equal(sessions.size, 1);
  await onPythonMessage({ id: id(17, 4), op: "close" }, fakePort);
  assert.equal(sessions.size, 0);

  // "attach" keeps the single-tab invariant even from a multi-tab state:
  // every OTHER attached tab is torn down once the new attachment is secured.
  await onPythonMessage({ id: id(18, 1), op: "open", url: "https://x.example" }, fakePort);
  const multiA = replies.at(-1).tabId;
  await onPythonMessage(
    { id: id(18, 2), op: "open", url: "https://y.example", replace: false },
    fakePort
  );
  const multiB = replies.at(-1).tabId;
  activeTab = { id: 93 };
  await onPythonMessage({ id: id(19, 1), op: "attach" }, fakePort);
  assert.equal(replies.at(-1).ok, true);
  assert.deepEqual([...sessions.keys()], [93]);
  assert.equal(sessions.get(93).bridgeCreated, false);
  assert.ok(removed.includes(multiA));
  assert.ok(removed.includes(multiB));
  await onPythonMessage({ id: id(19, 2), op: "close" }, fakePort);
  assert.ok(!removed.includes(93)); // merely-attached: detached, never removed
  assert.equal(sessions.size, 0);
  activeTab = null;

  // Mixed table teardown: a merely-attached user tab plus a bridge-created
  // tab; the default "open" removes only the bridge-created one and merely
  // detaches the user's.
  activeTab = { id: 94 };
  await onPythonMessage({ id: id(20, 1), op: "attach" }, fakePort);
  assert.equal(replies.at(-1).ok, true);
  await onPythonMessage(
    { id: id(20, 2), op: "open", url: "https://mixed.example", replace: false },
    fakePort
  );
  const mixedBridge = replies.at(-1).tabId;
  assert.equal(sessions.get(94).bridgeCreated, false);
  assert.equal(sessions.get(mixedBridge).bridgeCreated, true);
  await onPythonMessage({ id: id(21, 1), op: "open", url: "https://after.example" }, fakePort);
  const afterTab = replies.at(-1).tabId;
  assert.ok(detached.includes(94));
  assert.ok(!removed.includes(94)); // user tab: detached, never removed
  assert.ok(removed.includes(mixedBridge)); // bridge tab: removed
  assert.deepEqual([...sessions.keys()], [afterTab]);
  activeTab = null;

  // "detach" with an explicit tabId releases exactly that tab, leaving even
  // a bridge-created one open; without tabId at >1 attached it is ambiguous.
  await onPythonMessage(
    { id: id(21, 2), op: "open", url: "https://second.example", replace: false },
    fakePort
  );
  const secondTab = replies.at(-1).tabId;
  await onPythonMessage({ id: id(21, 3), op: "detach" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.match(replies.at(-1).error, /ambiguous/);
  await onPythonMessage({ id: id(21, 4), op: "detach", tabId: afterTab }, fakePort);
  assert.equal(replies.at(-1).ok, true);
  assert.ok(detached.includes(afterTab));
  assert.ok(!removed.includes(afterTab)); // bridge-created, but detach leaves it open
  assert.deepEqual([...sessions.keys()], [secondTab]);

  // A FRESH generation naming a predecessor's leftover entry gets the
  // actionable cross-session error (an explicit tabId never bypasses the
  // generation fence); an unknown tabId on "detach" errors rather than
  // no-ops.
  await onPythonMessage({ id: id(22, 1), op: "close", tabId: secondTab }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.match(replies.at(-1).error, /different client session/);
  assert.ok(sessions.has(secondTab)); // fence held: entry untouched
  await onPythonMessage({ id: id(22, 2), op: "detach", tabId: 12345 }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.match(replies.at(-1).error, /12345/);

  // Non-boolean "replace" is rejected rather than silently tearing down.
  await onPythonMessage(
    { id: id(22, 3), op: "open", url: "https://bad.example", replace: "false" },
    fakePort
  );
  assert.equal(replies.at(-1).ok, false);
  assert.match(replies.at(-1).error, /replace/);
  assert.deepEqual([...sessions.keys()], [secondTab]); // untouched

  // Profile rename with a matching live port sweeps only entries owned by a
  // DIFFERENT identity; tabs opened under the wanted identity survive.
  sessions.set(500, {
    sessionGeneration: 42,
    owner: "com.chromebridge.bridge.old",
    bridgeCreated: true,
  });
  chrome.storage.local.get = async () => ({ bridgeProfileName: "personal" });
  await reconnectWithNewIdentity();
  assert.ok(removed.includes(500)); // old identity's bridge tab reaped
  assert.deepEqual([...sessions.keys()], [secondTab]); // wanted identity survives
  chrome.storage.local.get = async () => ({});

  // cleanup for the final dial case
  await onPythonMessage({ id: id(21, 5), op: "close", tabId: secondTab }, fakePort);
  assert.equal(replies.at(-1).ok, true);
  assert.equal(sessions.size, 0);

  // Exercise dial()'s real listener-level requestChain. A postMessage failure
  // makes the first handler reject; the chain must recover and process the
  // following request instead of remaining permanently rejected.
  // KEEP THIS THE FINAL CASE: it permanently swaps the global port,
  // storage.local.get, and connectNative — tab-op cases appended below it
  // would misroute their replies into chainedReplies instead of replies.
  // (No backticks in this template literal.)
  let receive;
  // Throw on the first TWO postMessage calls: the first kills the try-path
  // reply, the second kills onPythonMessage's own catch-path reply, so the
  // handler's promise rejects and ONLY the listener-level chain .catch can
  // recover delivery of the following request.
  let throwsLeft = 2;
  const chainedReplies = [];
  const nativePort = {
    onMessage: { addListener: (fn) => { receive = fn; } },
    onDisconnect: { addListener: () => {} },
    postMessage: (msg) => {
      if (throwsLeft > 0) {
        throwsLeft -= 1;
        throw new Error("simulated disconnected reply");
      }
      chainedReplies.push(msg);
    },
  };
  chrome.storage.local.get = async () => ({ bridgeProfileName: "personal" });
  chrome.runtime.connectNative = () => nativePort;
  port = null;
  await dial();
  receive({ id: id(4, 1), op: "unknown-first" });
  await new Promise((resolve) => setImmediate(resolve));
  receive({ id: id(4, 2), op: "unknown-second" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(chainedReplies.at(-1).id[2], 2);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
`;

eval(source + "\n" + tests);
