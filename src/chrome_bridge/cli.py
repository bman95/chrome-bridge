"""chrome-bridge CLI: run scripts against a chosen Chrome profile, plus a few
one-shot convenience commands for ad-hoc shell use.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Optional

import typer

from . import install as install_mod
from . import profiles as profiles_mod
from .bridge.client import BridgeError, ChromeBridge
from .runner import run_script

app = typer.Typer(help="Drive your real, everyday Chrome from Python scripts.")
profiles_app = typer.Typer(help="Manage registered Chrome profiles.")
app.add_typer(profiles_app, name="profiles")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    """Remove a profile's registry entry (does not touch Chrome or the registry key)."""
    if profiles_mod.remove_profile(name):
        typer.echo(f"Removed profile {name!r} from the registry.")
    else:
        typer.echo(f"No profile named {name!r}.")
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

    launcher = install_mod.write_launcher(_PROJECT_ROOT, profile, chosen_port)
    typer.echo(f"Wrote launcher: {launcher}")

    existing_ids = [existing.extension_id] if existing and existing.extension_id else []
    manifest = install_mod.write_manifest(_PROJECT_ROOT, profile, extension_ids=existing_ids)
    typer.echo(f"Wrote manifest: {manifest}")

    install_mod.register_native_host(profile, manifest)
    typer.echo(f"Registered native host {install_mod.host_name(profile)!r} in HKCU.")

    typer.echo("")
    typer.echo("Now, in that Chrome profile's own window:")
    typer.echo("  1. Open chrome://extensions and enable Developer mode.")
    typer.echo(f"  2. Click 'Load unpacked' and select: {_PROJECT_ROOT / 'extension'}")
    typer.echo("  3. Note the extension ID shown on its card.")
    typer.echo("  4. Open the extension's 'Details' -> 'Extension options' page")
    typer.echo(f"     and set the profile name to: {profile}")
    extension_id = typer.prompt("Paste the extension ID here")

    install_mod.patch_allowed_origins(manifest, extension_id)
    typer.echo(f"Patched allowed_origins in {manifest}.")

    try:
        profiles_mod.add_profile(
            profile,
            chosen_port,
            chrome_user_data_dir=None,
            chrome_profile_directory=chrome_profile_dir,
            extension_id=extension_id,
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
    typer.echo(f"Saved profile {profile!r} to the registry.")
    typer.echo("")
    typer.echo("Reload the extension in chrome://extensions, then verify with:")
    typer.echo(f"  chrome-bridge status --profile {profile}")


def _resolve_host_port(profile: Optional[str], host: Optional[str], port: Optional[int]) -> tuple[str, int]:
    if host is not None or port is not None:
        if port is not None:
            try:
                profiles_mod.validate_port(port)
            except profiles_mod.ProfileError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc
        return host or "127.0.0.1", port if port is not None else profiles_mod.DEFAULT_BASE_PORT
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
    """One-shot: navigate the profile's tab to URL."""
    h, p = _resolve_host_port(profile, host, port)
    with ChromeBridge(host=h, port=p) as cdp:
        cdp.navigate(url)
        typer.echo(f"navigated to {url}")


@app.command()
def html(
    selector: str = typer.Argument("body"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: print the outerHTML of the first element matching SELECTOR."""
    h, p = _resolve_host_port(profile, host, port)
    with ChromeBridge(host=h, port=p) as cdp:
        typer.echo(cdp.outer_html(selector))


@app.command()
def click(
    selector: str,
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: click the first element matching SELECTOR."""
    h, p = _resolve_host_port(profile, host, port)
    with ChromeBridge(host=h, port=p) as cdp:
        cdp.click_selector(selector)
        typer.echo(f"clicked {selector}")


@app.command()
def fill(
    selector: str,
    text: str,
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: fill SELECTOR's value with TEXT."""
    h, p = _resolve_host_port(profile, host, port)
    with ChromeBridge(host=h, port=p) as cdp:
        ok = cdp.fill(selector, text)
        typer.echo("filled" if ok else "no matching element")
        if not ok:
            raise typer.Exit(code=1)


@app.command(name="eval")
def eval_(
    expression: str,
    profile: Optional[str] = typer.Option(None, "--profile"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """One-shot: evaluate a JS EXPRESSION in the page and print the result."""
    h, p = _resolve_host_port(profile, host, port)
    with ChromeBridge(host=h, port=p) as cdp:
        value = cdp._evaluate(expression)
        typer.echo(value)


if __name__ == "__main__":
    app()
