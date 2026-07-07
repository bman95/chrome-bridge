# Multi-profile design

The motivating use case: you want to script your **personal** Google/Amazon/
whatever account from a tool (e.g. an AI coding assistant's own browser
extension) that is otherwise bound to a different account (e.g. your work
account). Chrome profiles already isolate cookies/sessions per account, so
"which account" becomes "which Chrome profile the script talks to" —
decoupled entirely from whatever identity launched the script.

## The mechanics

- Extensions loaded via "Load unpacked" are **per-profile** — loading
  `extension/` into profile A does not make it available in profile B. You
  load it into **every** profile you want scriptable.
- Each profile's Chrome independently spawns its own native-host process via
  `chrome.runtime.connectNative()`. Since the host binds one fixed loopback
  port, **two profiles cannot share a port** — one port per profile.
- The harder problem: **Chrome passes no profile-identifying information to
  the spawned host process** (no profile directory in argv or env), and a
  native-messaging host manifest name resolves to exactly one launch command.
  A single shared host name therefore cannot route to different ports per
  profile — the host process itself has no way to know which profile launched
  it.

**Resolution: one native host name per profile**
(`com.chromebridge.bridge.<profile>`), each with its own:
- Registry key: `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.chromebridge.bridge.<profile>`
- Manifest file: `nmh-manifest.<profile>.local.json` (gitignored)
- Launcher: `run_nmh_<profile>.bat`, with that profile's port baked in via
  `set CBR_BRIDGE_PORT=<port>`

The extension side needs a small per-profile identity too, since the same
unpacked `extension/` code is loaded into every profile: `extension/options.html`
lets you type the profile's name once per profile-load, stored in
`chrome.storage.local`. `sw.js`'s `connect()` reads it and dials
`com.chromebridge.bridge.<profileName>`.

All of this is automated by `chrome-bridge init --profile <name>` — see the
README for the walkthrough.

## Profile registry

`~/.config/chrome-bridge/profiles.json` maps a profile name to its port (and,
for display only, which OS Chrome profile it corresponds to):

```json
{
  "profiles": {
    "personal": {
      "port": 8765,
      "chrome_user_data_dir": null,
      "chrome_profile_directory": "Default",
      "extension_id": "abcdefghijklmnopabcdefghijklmnop",
      "registered_at": null
    },
    "work": {
      "port": 8766,
      "chrome_user_data_dir": null,
      "chrome_profile_directory": "Profile 1",
      "extension_id": "abcdefghijklmnopabcdefghijklmnop",
      "registered_at": null
    }
  }
}
```

chrome-bridge never launches or manipulates Chrome itself — the registry only
records where to find an already-running profile's bridge. You point
`ChromeBridge(profile="work")` or `chrome-bridge run script.py --profile work`
at a name, and it resolves to that profile's port.

## Verify-manually-during-build items

These are real Chrome platform behaviors that could not be verified without a
live two-profile Chrome setup; they were resolved conservatively (build for
the harder case) but should be confirmed on your machine:

1. **Whether the same unpacked extension path yields the same or a different
   extension ID across profiles.** The tooling always treats
   `allowed_origins` as a growable array regardless (`install.write_manifest`
   / `install.patch_allowed_origins`), so this is safe either way — it only
   affects whether you'll see one id or several across your profiles.
2. **Whether Chrome truly exposes zero profile identity to the spawned host
   process.** This is what forces the per-profile-host-name design above. If
   it turns out Chrome does expose it (undocumented), the design could
   simplify to one shared host name — but nothing here would need to change
   to keep working if that's wrong; it would just mean the current design is
   more conservative than strictly necessary.
3. **Two profiles' hosts binding two different ports simultaneously with no
   cross-talk**, and each profile's `ChromeBridge(profile=...)` reaching only
   its own tab. Verify by running `chrome-bridge status --profile <name>` for
   each profile after a two-profile `init`.
