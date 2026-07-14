# Multi-tab design

One `ChromeBridge` connection can drive **several attached tabs at once**.
The bridge is **single-tab by default, multi-tab opt-in**: the legacy facade
(`cdp.open(...)` / `cdp.navigate(...)` / ...) still drives exactly one tab at
a time, and nothing about it changed; `ChromeBridge.new_tab(url)` is the
opt-in that adds tabs alongside it.

```python
from chrome_bridge import ChromeBridge

with ChromeBridge(profile="personal") as bridge:
    a = bridge.new_tab("https://example.com")
    b = bridge.new_tab("https://example.org")
    a.wait_selector("#content")
    print(b.outer_html("h1"))
    b.close()          # closes that tab; `a` is untouched
```

## The handle model

Playwright-style split, one class per concern:

- **`ChromeBridge` owns the connection**: the loopback socket to the native
  host, the background reader thread, request-id correlation, a
  connection-wide event bus, and the per-connection op lock (`_op_lock`).
- **`Tab` owns one attached page**: `tab_id`, `current_url`, a per-tab
  detached flag, a per-tab event bus, and all the page primitives
  (`navigate`, `wait_selector`, `outer_html`, `click_selector`, `fill`,
  `evaluate`, ...). Every op a `Tab` sends stamps its `tabId` into the wire
  frame.
- **The legacy facade is an internal default `Tab`**: `ChromeBridge.open`,
  `.navigate`, `.wait_selector`, etc. delegate to it, so single-tab scripts
  and the CLI run unchanged.

`Tab.close()` closes that one tab (the extension removes it only if it was
bridge-created) and permanently retires the handle; other tabs are untouched.
`ChromeBridge.close()` ends the whole session, closing every open tab.

## Open semantics: `replace`

The wire `open` op takes an optional `replace` flag (default **true**):

- **`replace:true`** (the default, what the facade `open` sends) is
  single-tab mode: the extension tears down every currently attached tab
  *first* (removing each only if bridge-created), then creates and attaches
  the new one. Callers that never pass `replace` keep the historical
  one-tab-at-a-time behavior and cannot leak tabs by re-opening. This shape
  is also the **reaper**: it is the only op that reclaims leftover tabs from
  a dead client session (see "Generation fencing" below).
- **`replace:false`** (what `new_tab` sends) opens an *additional* tab:
  existing attachments are left untouched, and the caller addresses tabs
  explicitly by `tabId` from there on.

`attach` (join the user's active tab) is always single-tab, like a
`replace:true` open: once the new attachment is secured, every other attached
tab is torn down. A *failed* attach leaves the existing sessions fully
intact.

## Routing and the ambiguity rule

`cdp`, `close`, and `detach` take an optional `tabId`:

- **With `tabId`** — the op targets that attached tab; an unknown or
  unattached `tabId` fails with an actionable error.
- **Without `tabId`** — exactly one tab attached: it is the implicit target
  (the legacy behavior). Several attached: the op fails as **ambiguous**
  ("pass tabId") rather than guessing. None attached: `cdp` fails; `close`
  and `detach` are successful no-ops (there is no session state to clobber).

The Python client never relies on the implicit form for additional tabs:
every `Tab` op is `tabId`-stamped, precisely so it can never be resolved onto
the wrong tab.

Events flow the other way with the same discipline: every unsolicited event
(`cdp` events, `detached`) carries the source `tabId`, and the client routes
it to the matching `Tab` handle's event bus (`tab.on_event(method, cb)`; a
per-tab `"__detached__"` pseudo-event reports detaches). An event whose
`tabId` no live handle claims — a late event from an already-detached tab, or
from a pre-eviction session — is dropped (debug-logged) rather than routed,
so it can never fire a listener on the wrong handle. The one exception is a
`detached` event racing its own open/attach reply: it is *parked* and
re-dispatched once the reply publishes the `tabId`, so the race cannot lose a
detach.

## Detach and recovery

Two distinct failure shapes, recovered at two different scopes:

**One tab detaches (socket alive).** `chrome.debugger.onDetach` fires for
that tab only — target closed, DevTools opened, a restricted page navigated
to. The client fails any in-flight op *targeting that tab* with
`DetachedError` and marks only that handle detached; **other attached tabs
are untouched**. The next primitive on the detached handle re-attaches
idempotently by re-opening its `current_url` — with `replace:false` whenever
sibling tabs are live, so a re-attach never tears them down. A tab joined via
`attach()` records no URL by design, so it is never silently re-opened
(re-creating a tab the user closed is never wanted); the next primitive
raises `DetachedError` instead.

**The socket dies (every tab gone at once).** An MV3 service-worker eviction
drops the native port, which kills the host relay and the client's loopback
socket — all attachments die together. `ChromeBridge.reconnect()` rebuilds
the whole connection: it re-dials the host, then **eagerly re-opens every
handle that had a `current_url`, in creation order**, remapping each handle's
`tab_id` onto its fresh Chrome tab so existing `Tab` references keep working.
The first re-open goes out in the reaper shape (`replace:true` — nothing else
is attached yet, and it reclaims the dead session's leftover tabs
server-side); every later one is `replace:false` so it never tears down an
already-healed sibling.

All the re-opens share **one heal budget** (one connection-op allowance plus
a short load ceiling per restorable tab), with every heal leg re-clamped to
what remains as it starts — so a wedged worker cannot stack N tabs' full
per-op timeouts. A heal is never *started* with less than a minimal share of
the budget (a near-zero-timeout `open` can orphan a tab server-side when the
reply arrives late); a skipped or failed heal is per-tab best-effort — that
handle simply re-attaches lazily, with a full timeout, on its next primitive,
and one tab's failure never poisons its siblings.

A handle that never opened a URL has nothing to restore and is marked
**dead** after a reconnect: its next primitive raises an actionable
`BridgeError`, and an explicit `open`/`navigate`/`attach` starts a fresh
session on it and revives it.

## Generation fencing

Each client connection to the host gets a fresh generation. The extension's
session table records, per attached tab, the generation that created it; a
`cdp`/`close`/`detach` from a *different* generation is refused — an explicit
`tabId` does not bypass the fence, so a stale frame queued by a dead client
can never drive a tab its successor has since adopted. Events are stamped
with the generation of the entry that produced them, and the host drops any
event that doesn't match the live client session (the field never reaches
the Python client). A successor may *list* a predecessor's leftover tabs
(the read-only `tabs` op is deliberately unfenced) but not drive them; a
`replace:true` open is the reaper that reclaims them.

## Concurrency: serialized per connection

**Operations are serialized per connection, across all tabs.** The wire
protocol requires one request in flight at a time (the extension's session
table relies on it), and the client enforces it with `_op_lock` held across
each whole send→await round-trip. Holding N tabs therefore means callers
*interleave* ops across tabs; tabs are never driven in parallel, and two
threads sharing one `ChromeBridge` simply queue on the lock. Per-tab
pipelining is future work, tracked separately in issue #19 — do not design
against it existing today.
