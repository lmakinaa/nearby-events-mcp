import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeSite, make_event
from nearby_events_mcp import server
from nearby_events_mcp.client import EventsClient, EventsError
from nearby_events_mcp.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def _payload(result):
    # FastMCP.call_tool returns (content, structured) or content depending on version
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


@pytest.fixture
def wired(make_client, monkeypatch):
    fake = FakeSite([make_event(1, "A", free=True, followers=10), make_event(2, "B", pmin=9, pmax=9)],
                          page_size=50)
    c = make_client(fake, default_lat=1.3521, default_lng=103.8198)
    monkeypatch.setattr(server, "_client", c)
    monkeypatch.setattr(server, "_settings", c.s)
    return fake


async def test_tool_list():
    names = {t.name for t in await server.mcp.list_tools()}
    assert names == {"search_events", "daily_events", "get_event", "list_categories", "resolve_location"}


async def test_search_events_tool_uses_default_location(wired):
    out = _payload(await server.mcp.call_tool("search_events", {"price": "free", "max_results": 5}))
    assert out["origin"]["label"] == "default location" and out["count"] == 2
    sent = wired.searches[0]["event_search"]
    assert sent["price"] == "free" and sent["point_radius"]["radius"] == "10km"


async def test_daily_events_one_search_per_day(wired):
    out = _payload(await server.mcp.call_tool("daily_events", {"days": 3, "start_date": "2099-01-01"}))
    assert list(out["days"]) == ["2099-01-01", "2099-01-02", "2099-01-03"]
    assert [s["event_search"]["date_range"]["from"] for s in wired.searches] == list(out["days"])


async def test_no_location_error(make_client, monkeypatch):
    c = make_client(FakeSite([]))
    monkeypatch.setattr(server, "_client", c)
    monkeypatch.setattr(server, "_settings", Settings())
    with pytest.raises(Exception, match="No location"):
        await server.mcp.call_tool("search_events", {})


async def test_list_categories():
    out = _payload(await server.mcp.call_tool("list_categories", {}))
    assert out["categories"]["101"] == "Business" and out["formats"]["10"] == "Networking"


def test_stdio_handshake_via_installer_smoke_test():
    """Spawn the real server as Claude Desktop would and speak MCP over stdio."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import install_claude_desktop as inst

    import os
    env = {"PYTHONPATH": str(ROOT / "src")}
    names = inst.smoke_test(sys.executable, env)
    assert "search_events" in names and "get_event" in names
    assert os.environ.get("PYTHONPATH") != "x"  # noqa: keep flake quiet


def test_installer_writes_and_merges_config(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    env = {**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, str(ROOT / "scripts/install_claude_desktop.py"), "--config", str(cfg),
                        "--python", sys.executable, "--location", "Orchard Road, Singapore",
                        "--radius-km", "7"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(cfg.read_text())
    assert data["theme"] == "dark" and "other" in data["mcpServers"]  # untouched
    ent = data["mcpServers"]["nearby-events"]
    assert ent["args"] == ["-m", "nearby_events_mcp"]
    assert ent["env"]["NEARBY_EVENTS_DEFAULT_LOCATION"] == "Orchard Road, Singapore"
    assert ent["env"]["NEARBY_EVENTS_DEFAULT_RADIUS_KM"] == "7"
    assert list(tmp_path.glob("*.bak-*"))  # backup made
    r = subprocess.run([sys.executable, str(ROOT / "scripts/install_claude_desktop.py"), "--config", str(cfg),
                        "--uninstall"], capture_output=True, text=True)
    assert r.returncode == 0 and "nearby-events" not in json.loads(cfg.read_text())["mcpServers"]
