// Options page for Chrome Bridge — lets the user set THIS Chrome profile's
// name once, so sw.js knows which per-profile native host to dial. See the
// "MULTI-PROFILE IDENTITY" block in sw.js and docs/multi-profile.md.

// Matches the profile-name validation chrome_bridge.profiles enforces
// (lowercase alnum + underscore) — it becomes part of a native
// messaging host name and of generated filenames, both of which are picky
// about characters.
const NAME_RE = /^[a-z0-9][a-z0-9_]*$/;

const input = document.getElementById("profile-name");
const status = document.getElementById("status");

async function load() {
  const { bridgeProfileName } = await chrome.storage.local.get("bridgeProfileName");
  if (bridgeProfileName) {
    input.value = bridgeProfileName;
    status.textContent = `Currently set to "${bridgeProfileName}".`;
  }
}

async function save() {
  const name = input.value.trim();
  if (!NAME_RE.test(name)) {
    status.textContent =
      "Invalid name — use lowercase letters, digits, or underscores, starting with a letter or digit.";
    return;
  }
  await chrome.storage.local.set({ bridgeProfileName: name });
  status.textContent = `Saved. Reconnecting as "${name}"...`;
}

document.getElementById("save").addEventListener("click", save);
load();
