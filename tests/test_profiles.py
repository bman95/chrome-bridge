"""Tests for the profile registry (~/.config/chrome-bridge/profiles.json)."""

from __future__ import annotations

import json

import pytest

from chrome_bridge import profiles


@pytest.fixture
def registry_path(tmp_path):
    return tmp_path / "profiles.json"


def test_list_profiles_empty_when_file_missing(registry_path):
    assert profiles.list_profiles(registry_path) == {}


def test_add_and_get_profile_round_trip(registry_path):
    profiles.add_profile("personal", 8765, path=registry_path)
    cfg = profiles.get_profile("personal", path=registry_path)
    assert cfg.name == "personal"
    assert cfg.port == 8765


def test_add_profile_persists_across_loads(registry_path):
    profiles.add_profile(
        "work",
        8766,
        chrome_profile_directory="Profile 1",
        extension_id="abcdefghijklmnopabcdefghijklmnop",
        registered_at="2026-07-07T10:00:00Z",
        path=registry_path,
    )
    reloaded = profiles.list_profiles(registry_path)
    assert set(reloaded) == {"work"}
    cfg = reloaded["work"]
    assert cfg.chrome_profile_directory == "Profile 1"
    assert cfg.extension_id == "abcdefghijklmnopabcdefghijklmnop"


def test_load_registry_ignores_unknown_keys(registry_path):
    """A registry written by an older version can carry now-removed fields
    (e.g. ``chrome_user_data_dir``); loading it must ignore them, not crash."""
    registry_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "personal": {
                        "port": 8765,
                        "chrome_user_data_dir": "C:\\legacy\\path",
                        "chrome_profile_directory": "Default",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    reloaded = profiles.list_profiles(registry_path)
    assert set(reloaded) == {"personal"}
    cfg = reloaded["personal"]
    assert cfg.port == 8765
    assert cfg.chrome_profile_directory == "Default"
    assert not hasattr(cfg, "chrome_user_data_dir")


def test_resolve_port_returns_registered_port(registry_path):
    profiles.add_profile("personal", 8765, path=registry_path)
    assert profiles.resolve_port("personal", path=registry_path) == 8765


def test_get_profile_missing_raises_profile_error(registry_path):
    with pytest.raises(profiles.ProfileError, match="no profile named"):
        profiles.get_profile("nope", path=registry_path)


def test_resolve_port_missing_raises_profile_error(registry_path):
    with pytest.raises(profiles.ProfileError):
        profiles.resolve_port("nope", path=registry_path)


def test_allocate_port_starts_at_base_when_empty(registry_path):
    assert profiles.allocate_port(registry_path) == profiles.DEFAULT_BASE_PORT


def test_allocate_port_fills_gap_between_used_ports(registry_path):
    profiles.add_profile("a", 8765, path=registry_path)
    profiles.add_profile("b", 8770, path=registry_path)
    assert profiles.allocate_port(registry_path) == 8766


def test_allocate_port_skips_contiguously_used_ports(registry_path):
    profiles.add_profile("a", 8765, path=registry_path)
    profiles.add_profile("b", 8766, path=registry_path)
    assert profiles.allocate_port(registry_path) == 8767


def test_allocate_port_reuses_port_of_removed_profile(registry_path):
    profiles.add_profile("a", 8765, path=registry_path)
    profiles.add_profile("b", 8766, path=registry_path)
    profiles.remove_profile("a", path=registry_path)
    assert profiles.allocate_port(registry_path) == 8765


def test_allocate_port_raises_when_range_exhausted(registry_path):
    profiles.add_profile("a", profiles.MAX_PORT, path=registry_path)
    with pytest.raises(profiles.ProfileError, match="no free port"):
        profiles.allocate_port(registry_path, base=profiles.MAX_PORT)


def test_allocate_port_rejects_out_of_range_base(registry_path):
    with pytest.raises(profiles.ProfileError, match="invalid port"):
        profiles.allocate_port(registry_path, base=0)


def test_allocate_port_treats_legacy_string_port_as_used(registry_path):
    """A legacy/hand-edited registry can hold string ports; the allocator must
    not reissue them to a second profile."""
    registry_path.write_text(
        json.dumps({"profiles": {"legacy": {"port": "8765"}}}), encoding="utf-8"
    )
    assert profiles.allocate_port(registry_path) == 8766


def test_add_profile_rejects_port_stored_as_legacy_string(registry_path):
    registry_path.write_text(
        json.dumps({"profiles": {"legacy": {"port": "8765"}}}), encoding="utf-8"
    )
    with pytest.raises(profiles.ProfileError, match="already used"):
        profiles.add_profile("new", 8765, path=registry_path)


def test_add_profile_overwrites_existing_entry(registry_path):
    profiles.add_profile("personal", 8765, path=registry_path)
    profiles.add_profile("personal", 9999, path=registry_path)
    assert profiles.resolve_port("personal", path=registry_path) == 9999
    assert len(profiles.list_profiles(registry_path)) == 1


def test_remove_profile_returns_true_when_existed(registry_path):
    profiles.add_profile("personal", 8765, path=registry_path)
    assert profiles.remove_profile("personal", path=registry_path) is True
    assert profiles.list_profiles(registry_path) == {}


def test_remove_profile_returns_false_when_absent(registry_path):
    assert profiles.remove_profile("nope", path=registry_path) is False


@pytest.mark.parametrize(
    "name",
    ["Personal", "my profile", "-personal", "personal!", ""],
)
def test_validate_profile_name_rejects_invalid(name):
    with pytest.raises(profiles.ProfileError):
        profiles.validate_profile_name(name)


@pytest.mark.parametrize("name", ["personal", "work-2", "work_2", "p2"])
def test_validate_profile_name_accepts_valid(name):
    profiles.validate_profile_name(name)  # must not raise


def test_add_profile_rejects_invalid_name(registry_path):
    with pytest.raises(profiles.ProfileError):
        profiles.add_profile("Not Valid", 8765, path=registry_path)


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_validate_port_rejects_out_of_range(port):
    with pytest.raises(profiles.ProfileError, match="invalid port"):
        profiles.validate_port(port)


@pytest.mark.parametrize("port", ["8765", None, 8765.5, True])
def test_validate_port_rejects_non_int(port):
    with pytest.raises(profiles.ProfileError, match="invalid port"):
        profiles.validate_port(port)


@pytest.mark.parametrize("port", [1, 8765, 65535])
def test_validate_port_accepts_valid(port):
    profiles.validate_port(port)  # must not raise


def test_add_profile_rejects_out_of_range_port(registry_path):
    with pytest.raises(profiles.ProfileError, match="invalid port"):
        profiles.add_profile("personal", 0, path=registry_path)
    assert not registry_path.exists()


def test_add_profile_rejects_port_used_by_other_profile(registry_path):
    profiles.add_profile("a", 8765, path=registry_path)
    with pytest.raises(profiles.ProfileError, match="already used"):
        profiles.add_profile("b", 8765, path=registry_path)
    assert set(profiles.list_profiles(registry_path)) == {"a"}


def test_add_profile_allows_same_profile_keeping_its_port(registry_path):
    profiles.add_profile("personal", 8765, path=registry_path)
    profiles.add_profile("personal", 8765, path=registry_path)  # must not raise
    assert profiles.resolve_port("personal", path=registry_path) == 8765


def test_registry_file_is_well_formed_json(registry_path):
    profiles.add_profile("personal", 8765, path=registry_path)
    text = registry_path.read_text(encoding="utf-8")
    assert '"personal"' in text
    assert '"port": 8765' in text


# --------------------------------------------------------------------------- #
# Auth-token paths + profile-by-port lookup (issue #7)
# --------------------------------------------------------------------------- #


def test_token_path_beside_registry(registry_path, monkeypatch):
    monkeypatch.setattr(profiles, "default_registry_path", lambda: registry_path)
    assert profiles.token_path("personal") == registry_path.parent / "token-personal.txt"


def test_port_token_path_beside_registry(registry_path, monkeypatch):
    monkeypatch.setattr(profiles, "default_registry_path", lambda: registry_path)
    assert profiles.port_token_path(8765) == registry_path.parent / "token-port.8765.txt"


def test_token_path_and_port_token_path_never_collide(registry_path, monkeypatch):
    # A profile literally named "port-8765" is valid; its token file must still
    # not collide with the port-8765 fallback (the '.' infix guarantees this).
    monkeypatch.setattr(profiles, "default_registry_path", lambda: registry_path)
    profiles.validate_profile_name("port-8765")  # confirms it is a valid name
    assert profiles.token_path("port-8765") != profiles.port_token_path(8765)


def test_find_profile_by_port_returns_match(registry_path):
    profiles.add_profile("personal", 8765, path=registry_path)
    profiles.add_profile("work", 8766, path=registry_path)
    assert profiles.find_profile_by_port(8766, path=registry_path) == "work"


def test_find_profile_by_port_none_when_absent(registry_path):
    profiles.add_profile("personal", 8765, path=registry_path)
    assert profiles.find_profile_by_port(9999, path=registry_path) is None


def test_find_profile_by_port_matches_legacy_string_port(registry_path):
    registry_path.write_text(
        json.dumps({"profiles": {"legacy": {"port": "8765"}}}), encoding="utf-8"
    )
    assert profiles.find_profile_by_port(8765, path=registry_path) == "legacy"


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",  # syntax corruption
        "[1, 2, 3]",  # top-level non-object
        '{"profiles": [1]}',  # profiles not a dict
        '{"profiles": {"a": {}}}',  # entry missing port
        '{"profiles": {"a": 5}}',  # entry not an object
    ],
)
def test_load_registry_raises_profile_error_on_corrupt_registry(registry_path, payload):
    # Hand-edited/legacy registries are part of this module's threat model:
    # any parse OR shape failure must surface as one well-known ProfileError
    # (a ValueError), never a raw json/TypeError/AttributeError — the host and
    # client token-path resolvers rely on that to degrade instead of crash.
    registry_path.write_text(payload, encoding="utf-8")
    with pytest.raises(profiles.ProfileError, match="corrupt profile registry"):
        profiles.load_registry(registry_path)
