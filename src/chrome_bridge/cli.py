"""chrome-bridge CLI: run scripts against a chosen Chrome profile, plus a few
one-shot convenience commands for ad-hoc shell use.
"""

from __future__ import annotations

import socket
import sys
from typing import Optional

import typer

from . import install as install_mod
from . import profiles as profiles_mod
from .bridge.client import BridgeError, ChromeBridge
from .runner import run_script

app = typer.Typer(help="Drive your real, everyday Chrome from Python scripts.")
profiles_app = typer.Typer(help="Manage registered Chrome profiles.")
app.add_typer(profiles_app, name="profiles")

# Kept as one overrideable root for tests; in production this is a stable,
# user-owned data directory, never the source checkout or site-packages.
_PROJECT_ROOT = install_mod.default_install_dir()


@profiles_app.command("list")
def profiles_list() -> None:
    """List registered profiles and their ports."""
    registered = profiles_mod.list_profiles()
    if not registered:
        typer.echo("No profiles registered yet. Run `chrome-bridge init --profile <name>`.")
        raise typer.Exit(code=0)
    for name, cfg in sorted(registered.items()):
        detail = cfg.chrome_profile_directory or "?"
        typer.echo(f"{name}\tport={cfg.port}\tchrome_profile={detail}")


@profiles_app.command("rm")
def profiles_rm(name: str) -> None:
    """Remove a profile: its registry entry, its generated launcher/manifest
    files, and (on Windows) its native-messaging host registry key.

    Reports exactly what was removed and what was already absent. Exits non-zero
    only when no registry entry existed for the name.
    """
    try:
        profiles_mod.validate_profile_name(name)
    except profiles_mod.ProfileError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    removed_entry = profiles_mod.remove_profile(name)
    if removed_entry:
        typer.echo(f"Removed profile {name!r} from the registry.")
    else:
        typer.echo(f"No profile named {name!r} in the registry.")

    # Native-host registry key: Windows only; gracefully skipped elsewhere.
    if sys.platform == "win32":
        removed_key = install_mod.unregister_native_host(name)
        if removed_key:
            typer.echo(f"Removed native-host key for {name!r}.")
        else:
            typer.echo(f"No native-host key for {name!r} to remove.")
    else:
        typer.echo("Skipped native-host registry key (Windows-only).")

    # Generated launcher + manifest files, plus the profile's auth-token file.
    for path in (
        install_mod.launcher_path(_PROJECT_ROOT, name),
        install_mod.manifest_path(_PROJECT_ROOT, name),
        profiles_mod.token_path(name),
    ):
        if path.exists():
            path.unlink()
            typer.echo(f"Deleted {path.name}.")
        else:
            typer.echo(f"No {path.name} to delete.")

    if not removed_entry:
        raise typer.Exit(code=1)


@app.command()
def init(
    profile: str = typer.Option(..., "--profile", help="Name for this Chrome profile."),
    port: Optional[int] = typer.Option(None, "--port", help="Override the auto-allocated port."),
) -> None:
    """Guided one-time setup for scripting a Chrome profile.

    Walks through: allocating a port, generating this profile's native-host
    manifest + launcher, registering it in the Windows registry, and then
    walking you through loading the extension into that profile's Chrome.
    Safe to re-run on an existing profile (each step is idempotent).
    """
    if sys.platform != "win32":
        typer.echo("chrome-bridge init currently only automates Windows registration.", err=True)
        raise typer.Exit(code=1)

    try:
        profiles_mod.validate_profile_name(profile)
    except profiles_mod.ProfileError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    registered = profiles_mod.list_profiles()
    existing = registered.get(profile)

    # Validate the port that will actually be used — whether it came from
    # --port, a (possibly legacy, pre-validation) registry entry, or the
    # allocator — before any prompt or write.
    try:
        if port is not None:
            chosen_port = port
        elif existing is not None:
            chosen_port = existing.port
        else:
            chosen_port = profiles_mod.allocate_port()
        profiles_mod.validate_port(chosen_port)
    except profiles_mod.ProfileError as exc:
        typer.echo(str(exc), err=True)
        if port is None and existing is not None:
            typer.echo(
                f"re-run with --port <n> to assign {profile!r} a valid port.",
                err=True,
            )
        raise typer.Exit(code=1) from exc

    conflict = profiles_mod.find_port_conflict(profile, chosen_port, registered)
    if conflict is not None:
        typer.echo(
            f"port {chosen_port} is already used by profile {conflict!r}; "
            "pick another port or remove that profile first.",
            err=True,
        )
        raise typer.Exit(code=1)

    if existing is not None:
        overwrite = typer.confirm(
            f"Profile {profile!r} is already registered (port {existing.port}). Overwrite?"
        )
        if not overwrite:
            raise typer.Exit(code=0)

    typer.echo(f"Using port {chosen_port} for profile {profile!r}.")

    chrome_profile_dir = typer.prompt(
        "Which Chrome profile directory is this? "
        "(open chrome://version in that profile's window, copy 'Profile Path', "
        "paste just the last folder name, e.g. 'Default' or 'Profile 1')",
        default=existing.chrome_profile_directory if existing else "Default",
    )

    extension_dir = install_mod.install_extension(_PROJECT_ROOT / "extension")

    typer.echo("")
    typer.echo("Now, in that Chrome profile's own window:")
    typer.echo("  1. Open chrome://extensions and enable Developer mode.")
    typer.echo(f"  2. Click 'Load unpacked' and select: {extension_dir}")
    typer.echo("  3. Note the extension ID shown on its card.")
    typer.echo("  4. Open the extension's 'Details' -> 'Extension options' page")
    typer.echo(f"     and set the profile name to: {profile}")
    pasted_extension_id = typer.prompt("Paste the extension ID here")
    try:
        extension_id = install_mod.normalize_extension_id(pasted_extension_id)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    try:
        installed = install_mod.install_profile(
            profile,
            chosen_port,
            chrome_profile_directory=chrome_profile_dir,
            extension_id=extension_id,
            artifacts_root=_PROJECT_ROOT,
        )
    except profiles_mod.ProfileError as exc:
        # Registry changed while init was waiting on the prompts above.
        typer.echo(str(exc), err=True)
        typer.echo(
            "the registry changed while init was running; the launcher, manifest, "
            "and native-host registration were already written, but the profile "
            "was not saved — re-run init.",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(f"Wrote launcher: {installed.launcher}")
    typer.echo(f"Wrote manifest: {installed.manifest}")
    typer.echo(f"Registered native host {install_mod.host_name(profile)!r} in HKCU.")
    typer.echo(f"Saved profile {profile!r} to the registry.")
    typer.echo("")
    typer.echo("Reload the extension in chrome://extensions, then verify with:")
    typer.echo(f"  chrome-bridge status --profile {profile}")


def _resolve_host_port(profile: Optional[str], host: Optional[str], port: Optional[int]) -> tuple[str, int]:
    if host is not None or port is not None:
        if port is None:
            typer.echo(
                "--host requires --port; there is no default port for an "
                "explicit host. Pass --port <n>, or use --profile <name>.",
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            profiles_mod.validate_port(port)
        except profiles_mod.ProfileError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        return host or "127.0.0.1", port
    if profile is not None:
        resolved = profiles_mod.resolve_port(profile)
        try:
            profiles_mod.validate_port(resolved)
        except profiles_mod.ProfileError as exc:
            typer.echo(str(exc), err=True)
            typer.echo(
                f"profile {profile!r} has an invalid registered port; re-run "
                f"`chrome-bridge init --profile {profile} --port <n>` to fix it.",
                err=True,
            )
            raise typer.Exit(code=2) from exc
        return "127.0.0.1", resolved
    typer.echo("Pass --profile <name> (or --host/--port for advanced use).", err=True)
    raise typer.Exit(code=2)


@app.command()
def status(
    profile: Optional[str] = typer.Option(None, "--profile", help="Registered profile name."),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """Check whether a profile's bridge loopback port is reachable right now."""
    h, p = _resolve_host_port(profile, host, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((h, p))
        typer.echo(f"OK: {h}:{p} is reachable.")
    except OSError as exc:
        typer.echo(f"UNREACHABLE: {h}:{p} — {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        sock.close()


@app.command()
def run(
    script: str = typer.Argument(..., help="Path to a Python script to run."),
    profile: Optional[str] = typer.Option(None, "--profile", help="Registered profile name."),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    script_args: list[str] = typer.Argument(None),
) -> None:
    """Run a script with a connected ChromeBridge available via get_bridge()."""
    h, p = _resolve_host_port(profile, host, port)
    bridge = ChromeBridge(host=h, port=p)
    try:
        run_script(script, bridge=bridge, script_args=script_args or [])
    except BridgeError as exc:
        typer.echo(f"bridge error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def nav(
    url: str,
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: navigate the active tab to URL, leaving the tab open."""
    h, p = _resolve_host_port(profile, host, port)
    cdp = ChromeBridge(host=h, port=p)
    try:
        cdp.connect()
        try:
            cdp.attach()
        except BridgeError as exc:
            # The structured error code distinguishes an empty window. Any other
            # attach failure (contention, restricted page) surfaces as-is —
            # a fresh tab is opened only when there is no tab at all.
            if exc.code != "no_active_tab":
                raise
            # Nothing to attach to at all: fall back to opening a fresh tab
            # (open() itself navigates); detach() below still leaves it open.
            cdp.open(url)
        else:
            cdp.navigate(url)
        typer.echo(f"navigated to {url}")
    except BridgeError as exc:
        typer.echo(f"bridge error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        cdp.detach()  # leave the tab open on exit


@app.command()
def html(
    selector: str = typer.Argument("body"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: print the active tab's outerHTML of the first SELECTOR match."""
    h, p = _resolve_host_port(profile, host, port)
    try:
        with ChromeBridge(host=h, port=p) as cdp:
            cdp.attach()  # act on the active tab; exit detaches without closing it
            typer.echo(cdp.outer_html(selector))
    except BridgeError as exc:
        typer.echo(f"bridge error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def click(
    selector: str,
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: click the first SELECTOR match in the active tab."""
    h, p = _resolve_host_port(profile, host, port)
    try:
        with ChromeBridge(host=h, port=p) as cdp:
            cdp.attach()  # act on the active tab; exit detaches without closing it
            cdp.click_selector(selector)
            typer.echo(f"clicked {selector}")
    except BridgeError as exc:
        typer.echo(f"bridge error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def fill(
    selector: str,
    text: str,
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: fill SELECTOR's value with TEXT in the active tab."""
    h, p = _resolve_host_port(profile, host, port)
    try:
        with ChromeBridge(host=h, port=p) as cdp:
            cdp.attach()  # act on the active tab; exit detaches without closing it
            ok = cdp.fill(selector, text)
            typer.echo("filled" if ok else "no matching element")
            if not ok:
                raise typer.Exit(code=1)
    except BridgeError as exc:
        typer.echo(f"bridge error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command(name="eval")
def eval_(
    expression: str,
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: evaluate a JS EXPRESSION in the active tab and print the result."""
    h, p = _resolve_host_port(profile, host, port)
    try:
        with ChromeBridge(host=h, port=p) as cdp:
            cdp.attach()  # act on the active tab; exit detaches without closing it
            typer.echo(cdp.evaluate(expression))
    except BridgeError as exc:
        typer.echo(f"bridge error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
