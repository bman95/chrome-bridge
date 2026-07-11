"""Tests for the profile registry (~/.config/chrome-bridge/profiles.json)."""

from __future__ import annotations

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
        chrome_user_data_dir=r"C:\Users\bfrias\AppData\Local\Google\Chrome\User Data",
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
