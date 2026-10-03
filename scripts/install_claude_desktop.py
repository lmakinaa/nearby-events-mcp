#!/usr/bin/env python3
"""Install nearby-events-mcp and register it with Claude Desktop.

Standard library only. Does three things:
  1. creates .venv next to this project and `pip install -e .` into it (skip with --python)
  2. adds / updates a "nearby-events" entry in claude_desktop_config.json (a timestamped backup is made)
  3. smoke-tests the server over stdio (MCP initialize + tools/list)

Examples
  python scripts/install_claude_desktop.py --location "Orchard Road, Singapore"
  python scripts/install_claude_desktop.py --lat 1.3521 --lng 103.8198 --radius-km 8 --use-browser
  python scripts/install_claude_desktop.py --uninstall
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude/claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude/claude_desktop_config.json"


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")


def run(cmd: list[str]) -> None:
    print("  $", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


MIN_PY = (3, 10)


def py_version(python: str | Path) -> tuple[int, int] | None:
    try:
        out = subprocess.run([str(python), "-c", "import sys;print(sys.version_info[0],sys.version_info[1])"],
                             capture_output=True, text=True, timeout=20)
        major, minor = out.stdout.split()
        return int(major), int(minor)
    except Exception:
        return None


def find_modern_python() -> str | None:
    """A Python >= 3.10 already on this machine, or None."""
    if sys.version_info[:2] >= MIN_PY:
        return sys.executable
    names = [f"python3.{m}" for m in range(14, 9, -1)]
    dirs = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin")]
    candidates = [shutil.which(n) for n in names]
    candidates += [str(Path(d) / n) for d in dirs for n in names]
    candidates += [str(p) for p in sorted(Path("/Library/Frameworks/Python.framework/Versions").glob("3.*/bin/python3"),
                                          reverse=True)]
    for c in candidates:
        if c and Path(c).exists() and (py_version(c) or (0, 0)) >= MIN_PY:
            return c
    return None


def find_or_install_uv() -> str:
    uv = shutil.which("uv")
    for p in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv", Path("/opt/homebrew/bin/uv")):
        if not uv and p.exists():
            uv = str(p)
    if uv:
        return uv
    if platform.system() == "Windows":
        sys.exit("Python 3.10+ is required. Install it from https://www.python.org/downloads/ and re-run.")
    print("No Python 3.10+ found; installing uv (it will download a private Python 3.12)")
    run(["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"])
    uv_path = Path.home() / ".local/bin/uv"
    if not uv_path.exists():
        sys.exit("uv install failed. Install Python 3.10+ (e.g. `brew install python@3.12`) and re-run.")
    return str(uv_path)


def ensure_venv(use_browser: bool) -> Path:
    venv = ROOT / ".venv"
    py = venv_python(venv)
    if py.exists() and (py_version(py) or (0, 0)) < MIN_PY:
        print(f"Existing {venv} uses an old Python; recreating it")
        shutil.rmtree(venv)
    target = f"{ROOT}[browser]" if use_browser else str(ROOT)
    modern = find_modern_python()
    if modern:
        if not py.exists():
            print(f"Creating virtualenv at {venv} with {modern}")
            run([modern, "-m", "venv", str(venv)])
        print("Installing nearby-events-mcp")
        run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        run([str(py), "-m", "pip", "install", "--quiet", "-e", target])
    else:
        uv = find_or_install_uv()
        if not py.exists():
            print(f"Creating virtualenv at {venv} with uv-managed Python 3.12")
            run([uv, "venv", "--python", "3.12", str(venv)])
        print("Installing nearby-events-mcp")
        run([uv, "pip", "install", "--python", str(py), "-e", target])
    if use_browser:
        run([str(py), "-m", "playwright", "install", "chromium"])
    return py


def smoke_test(python: str, env: dict[str, str]) -> list[str]:
    """Speak MCP over stdio: initialize -> initialized -> tools/list. Returns tool names."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "installer-smoke-test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = "".join(json.dumps(m) + "\n" for m in msgs)
    proc = subprocess.Popen([python, "-m", "nearby_events_mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env={**os.environ, **env})
    assert proc.stdin and proc.stdout
    proc.stdin.write(payload)
    proc.stdin.flush()
    names: list[str] = []
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == 2:
            names = [t["name"] for t in msg["result"]["tools"]]
            break
    proc.terminate()
    return names


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError as exc:
        sys.exit(f"{path} is not valid JSON ({exc}). Fix it or pass --config to another file.")


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"Backed up existing config to {backup}")
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="nearby-events", help="server name shown in Claude Desktop")
    ap.add_argument("--config", type=Path, help="path to claude_desktop_config.json (auto-detected)")
    ap.add_argument("--python", help="use this interpreter (must already have nearby-events-mcp installed)")
    ap.add_argument("--location", help="default place for 'near me', e.g. 'Orchard Road, Singapore'")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lng", type=float)
    ap.add_argument("--radius-km", type=int, default=10)
    ap.add_argument("--base-url", default="https://www.eventbrite.sg",
                    help="Eventbrite domain, e.g. https://www.eventbrite.com")
    ap.add_argument("--use-browser", action="store_true",
                    help="bootstrap cookies with headless Chromium (installs Playwright)")
    ap.add_argument("--csrf-token", help="csrftoken cookie copied from your browser (fallback)")
    ap.add_argument("--print-only", action="store_true", help="show the config entry, change nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove the entry from Claude Desktop")
    ap.add_argument("--no-smoke-test", action="store_true")
    args = ap.parse_args()

    cfg_path = args.config or config_path()

    if args.uninstall:
        cfg = load_config(cfg_path)
        if cfg.get("mcpServers", {}).pop(args.name, None) is None:
            sys.exit(f"No '{args.name}' entry in {cfg_path}")
        save_config(cfg_path, cfg)
        print(f"Removed '{args.name}'. Restart Claude Desktop.")
        return

    if (args.lat is None) != (args.lng is None):
        sys.exit("--lat and --lng must be given together")

    env = {"NEARBY_EVENTS_BASE_URL": args.base_url, "NEARBY_EVENTS_DEFAULT_RADIUS_KM": str(args.radius_km)}
    if args.lat is not None:
        env["NEARBY_EVENTS_DEFAULT_LAT"], env["NEARBY_EVENTS_DEFAULT_LNG"] = str(args.lat), str(args.lng)
    elif args.location:
        env["NEARBY_EVENTS_DEFAULT_LOCATION"] = args.location
    else:
        print("! No default location set: Claude will need a place name/lat-lng in each request.\n"
              "  Re-run with --location 'Your Area, City' to make 'near me' work.")
    if args.use_browser:
        env["NEARBY_EVENTS_USE_BROWSER"] = "1"
    if args.csrf_token:
        env["NEARBY_EVENTS_CSRF_TOKEN"] = args.csrf_token

    if args.print_only and not args.python:
        python = str(venv_python(ROOT / ".venv"))
    else:
        python = args.python or str(ensure_venv(args.use_browser))

    entry = {"command": python, "args": ["-m", "nearby_events_mcp"], "env": env}
    if args.print_only:
        print(json.dumps({"mcpServers": {args.name: entry}}, indent=2))
        return

    if not args.no_smoke_test:
        print("Smoke-testing the server over stdio ...")
        tools = smoke_test(python, env)
        if not tools:
            sys.exit("Smoke test failed: server did not list any tools. Run "
                     f"`{python} -m nearby_events_mcp` to see the error.")
        print("  OK, tools:", ", ".join(tools))

    cfg = load_config(cfg_path)
    cfg.setdefault("mcpServers", {})[args.name] = entry
    save_config(cfg_path, cfg)
    print(f"\nRegistered '{args.name}' in {cfg_path}\nFully quit and reopen Claude Desktop, then try:\n"
          "  \"What free networking events are happening near me today?\"")


if __name__ == "__main__":
    main()
