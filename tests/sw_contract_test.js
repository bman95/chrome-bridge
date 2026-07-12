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
    attach: async () => {},
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
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
`;

eval(source + "\n" + tests);
