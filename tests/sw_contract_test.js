const assert = require("node:assert/strict");
const fs = require("node:fs");

const debuggerEvents = [];
const debuggerDetaches = [];
const storageChanges = [];
const alarmEvents = [];
const removed = [];
const detached = [];
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
    sendCommand: async () => ({}),
    onEvent: { addListener: (fn) => debuggerEvents.push(fn) },
    onDetach: { addListener: (fn) => debuggerDetaches.push(fn) },
  },
};

const source = fs.readFileSync("extension/sw.js", "utf8");
const tests = String.raw`
(async () => {
  const replies = [];
  const fakePort = {
    bridgeHostName: "com.chromebridge.bridge.personal",
    postMessage: (msg) => replies.push(msg),
  };
  port = fakePort;

  const id = (generation, original) => [RELAY_SESSION_ID_MARKER, generation, original];
  await onPythonMessage({ id: id(1, 1), op: "open", url: "https://example.com" }, fakePort);
  assert.equal(dbgTabId, 1);
  assert.equal(dbgTabOwned, true);
  assert.equal(dbgTabSessionGeneration, 1);

  // Attaching the already-focused bridge tab hands ownership to the user and
  // transfers the session generation. Closing must detach but never remove it.
  activeTab = { id: 1 };
  await onPythonMessage({ id: id(2, 1), op: "attach" }, fakePort);
  assert.equal(dbgTabOwned, false);
  assert.equal(dbgTabSessionGeneration, 2);
  await onPythonMessage({ id: id(2, 2), op: "close" }, fakePort);
  assert.deepEqual(detached, [1]);
  assert.deepEqual(removed, []);

  // An owned tab is reaped by onDetach and its event retains the tab-bound
  // generation used by nmh.py's event fence.
  await onPythonMessage({ id: id(3, 1), op: "open", url: "https://example.org" }, fakePort);
  assert.equal(dbgTabId, 2);
  debuggerDetaches[0]({ tabId: 2 }, "target_closed");
  await Promise.resolve();
  assert.deepEqual(removed, [2]);
  const event = replies.find((msg) => msg.event && msg.type === "detached");
  assert.equal(event.sessionGeneration, 3);

  // attach with NO active tab replies ok:false with the structured errorCode
  // the CLI's nav fallback keys on (cli.py branches on "no_active_tab").
  activeTab = null;
  await onPythonMessage({ id: id(4, 1), op: "attach" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.equal(replies.at(-1).errorCode, "no_active_tab");
  assert.equal(dbgTabId, null);

  // "detach" with no tab attached is idempotent ok:true even for a fenced
  // caller, whose generation cannot match the (null) session generation.
  await onPythonMessage({ id: id(4, 2), op: "detach" }, fakePort);
  assert.equal(replies.at(-1).ok, true);

  // Attaching a DIFFERENT active tab while an owned bridge tab is held: the
  // single-tab teardown removes the owned tab; the user tab attaches unowned.
  await onPythonMessage({ id: id(5, 1), op: "open", url: "https://example.net" }, fakePort);
  const ownedTab = dbgTabId;
  activeTab = { id: 90 };
  await onPythonMessage({ id: id(6, 1), op: "attach" }, fakePort);
  assert.equal(dbgTabId, 90);
  assert.equal(dbgTabOwned, false);
  assert.ok(removed.includes(ownedTab));
  assert.ok(!removed.includes(90));

  // A failed attach (restricted page, contention) leaves the prior session
  // fully intact — acquire-before-release. Hold an OWNED tab, then make the
  // attach to a different user tab throw.
  await onPythonMessage(
    { id: id(7, 1), op: "open", url: "https://example.com/keep" },
    fakePort
  );
  const keptTab = dbgTabId;
  assert.ok(!removed.includes(90)); // the unowned tab 90 was detached, not removed
  activeTab = { id: 91 };
  attachFailure = new Error("Cannot attach to this target");
  await onPythonMessage({ id: id(8, 1), op: "attach" }, fakePort);
  assert.equal(replies.at(-1).ok, false);
  assert.equal(dbgTabId, keptTab); // prior session intact
  assert.equal(dbgTabOwned, true);
  assert.equal(dbgTabSessionGeneration, 7);
  assert.ok(!removed.includes(keptTab));
  assert.ok(!removed.includes(91)); // the user's tab is never removed on failure

  // The onDetach reaper must NEVER remove a merely-attached user tab; it
  // still emits the detached event and clears the reference.
  await onPythonMessage({ id: id(9, 1), op: "attach" }, fakePort);
  assert.equal(dbgTabId, 91);
  debuggerDetaches[0]({ tabId: 91 }, "target_closed");
  await Promise.resolve();
  assert.ok(!removed.includes(91));
  const userDetach = replies.filter((m) => m.event && m.type === "detached").at(-1);
  assert.equal(userDetach.reason, "target_closed");
  assert.equal(userDetach.sessionGeneration, 9);
  assert.equal(dbgTabId, null);

  // Exercise dial()'s real listener-level requestChain. A postMessage failure
  // makes the first handler reject; the chain must recover and process the
  // following request instead of remaining permanently rejected.
  let receive;
  let throwOnce = true;
  const chainedReplies = [];
  const nativePort = {
    onMessage: { addListener: (fn) => { receive = fn; } },
    onDisconnect: { addListener: () => {} },
    postMessage: (msg) => {
      if (throwOnce) {
        throwOnce = false;
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
