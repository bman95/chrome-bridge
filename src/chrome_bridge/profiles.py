"""Profile registry: maps a named Chrome profile to its bridge loopback port.

Each Chrome profile a user wants to script gets its own native-messaging host
(``com.chromebridge.bridge.<profile>``) and its own loopback port, since Chrome
gives a spawned native host no way to know which profile launched it — see
``docs/multi-profile.md``. This module owns the on-disk registry
(``~/.config/chrome-bridge/profiles.json``) that ties a profile name to its
port and (for display only) which OS Chrome profile it maps to.

The bridge itself never launches or manipulates Chrome; the registry only
records where to find an already-running profile's bridge.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_BASE_PORT = 8765
MAX_PORT = 65535

# Matches the same rule extension/options.js enforces client-side: the name
# becomes part of a native-messaging host name and of generated filenames,
# both picky about characters.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ProfileError(ValueError):
    """A profile name or registry operation is invalid."""


@dataclass
class ProfileConfig:
    name: str
    port: int
    chrome_profile_directory: str | None = None
    extension_id: str | None = None
    registered_at: str | None = None


@dataclass
class _Registry:
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)


def validate_profile_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ProfileError(
            f"invalid profile name {name!r}: use lowercase letters, digits, "
            "hyphens, or underscores, starting with a letter or digit"
        )


def validate_port(port: int) -> None:
    # isinstance guard: a legacy/hand-edited registry can hold a non-int port
    # (e.g. "8765"), which must reject cleanly, not raise TypeError on `<=`.
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= MAX_PORT:
        raise ProfileError(
            f"invalid port {port!r}: must be an integer between 1 and {MAX_PORT}"
        )


def _coerce_port(value: object) -> int | None:
    """Best-effort int view of a stored registry port, for comparisons.

    Legacy/hand-edited registries can hold non-int ports (e.g. ``"8765"``);
    comparing those raw would silently defeat duplicate detection and gap
    allocation. Returns None when the value has no integer interpretation.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError:
            return None
    return None


def find_port_conflict(
    name: str, port: int, profiles_by_name: dict[str, ProfileConfig]
) -> str | None:
    """Name of a profile other than ``name`` already holding ``port``, if any."""
    for other_name, other in profiles_by_name.items():
        if other_name != name and _coerce_port(other.port) == port:
            return other_name
    return None


def default_registry_path() -> Path:
    return Path.home() / ".config" / "chrome-bridge" / "profiles.json"


def token_path(profile: str) -> Path:
    """Absolute path to a named profile's ``0600`` auth-token file.

    The host writes a fresh random token here on startup and the client reads it
    to authenticate its first frame; the file lives beside the registry in
    ``~/.config/chrome-bridge/``. Shared by host (writer) and client (reader).
    """
    return default_registry_path().parent / f"token-{profile}.txt"


def port_token_path(port: int) -> Path:
    """Auth-token path for a bridge identified only by port (no named profile).

    Used by ``--host/--port``-only setups that never registered a profile. The
    ``port.`` infix carries a ``.``, which :func:`validate_profile_name` forbids
    in a profile name, so this filename can never collide with a
    :func:`token_path` one.
    """
    return default_registry_path().parent / f"token-port.{port}.txt"


def find_profile_by_port(port: int, path: Path | None = None) -> str | None:
    """Name of a registered profile bound to ``port``, or ``None`` if none is.

    Lets a host/port-only client resolve the profile-named token file the host
    wrote (``chrome-bridge`` passes host+port even for ``--profile`` usage).
    """
    for name, cfg in list_profiles(path).items():
        if _coerce_port(cfg.port) == port:
            return name
    return None


def load_registry(path: Path | None = None) -> _Registry:
    """Load the profile registry, or an empty one if it doesn't exist yet.

    A registry that cannot be parsed OR has the wrong shape (hand-edited and
    legacy files are part of this module's threat model — see ``_coerce_port``)
    raises :class:`ProfileError`, never a raw ``json``/``TypeError``/
    ``AttributeError``: callers that must degrade gracefully (the token-path
    resolution in the host and client) catch one well-known error type.
    """
    p = path or default_registry_path()
    if not p.exists():
        return _Registry()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # Ignore unknown keys so a registry written by an older version (which
        # persisted now-removed fields such as ``chrome_user_data_dir``) still
        # loads.
        known = {f.name for f in fields(ProfileConfig)} - {"name"}
        profiles = {
            name: ProfileConfig(name=name, **{k: v for k, v in cfg.items() if k in known})
            for name, cfg in data.get("profiles", {}).items()
        }
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProfileError(f"corrupt profile registry at {p}: {exc}") from exc
    return _Registry(profiles=profiles)


def save_registry(registry: _Registry, path: Path | None = None) -> None:
    p = path or default_registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profiles": {
            name: {k: v for k, v in asdict(cfg).items() if k != "name"}
            for name, cfg in registry.profiles.items()
        }
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def list_profiles(path: Path | None = None) -> dict[str, ProfileConfig]:
    return load_registry(path).profiles


def get_profile(name: str, path: Path | None = None) -> ProfileConfig:
    registry = load_registry(path)
    try:
        return registry.profiles[name]
    except KeyError:
        raise ProfileError(
            f"no profile named {name!r} — run `chrome-bridge init --profile {name}` first"
        ) from None


def resolve_port(name: str, path: Path | None = None) -> int:
    """Resolve a registered profile name to its bridge loopback port."""
    return get_profile(name, path=path).port


def allocate_port(path: Path | None = None, *, base: int = DEFAULT_BASE_PORT) -> int:
    """Pick the lowest port >= ``base`` not used by any registered profile."""
    validate_port(base)
    used = {
        coerced
        for cfg in list_profiles(path).values()
        if (coerced := _coerce_port(cfg.port)) is not None
    }
    port = base
    while port in used:
        port += 1
    if port > MAX_PORT:
        raise ProfileError(f"no free port available between {base} and {MAX_PORT}")
    return port


def add_profile(
    name: str,
    port: int,
    *,
    chrome_profile_directory: str | None = None,
    extension_id: str | None = None,
    registered_at: str | None = None,
    path: Path | None = None,
) -> ProfileConfig:
    """Add or overwrite a profile entry and persist the registry."""
    validate_profile_name(name)
    validate_port(port)
    registry = load_registry(path)
    conflict = find_port_conflict(name, port, registry.profiles)
    if conflict is not None:
        raise ProfileError(f"port {port} is already used by profile {conflict!r}")
    cfg = ProfileConfig(
        name=name,
        port=port,
        chrome_profile_directory=chrome_profile_directory,
        extension_id=extension_id,
        registered_at=registered_at,
    )
    registry.profiles[name] = cfg
    save_registry(registry, path)
    return cfg


def remove_profile(name: str, path: Path | None = None) -> bool:
    """Remove a profile entry; returns True if it existed."""
    registry = load_registry(path)
    if name not in registry.profiles:
        return False
    del registry.profiles[name]
    save_registry(registry, path)
    return True
