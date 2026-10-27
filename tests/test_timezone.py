import yaml
import pytest

from artel import tz
from artel.providers import overview, save_timezone


def test_sakhalin_renders_utc_plus_11():
    cfg = {"ui": {"timezone": "Asia/Sakhalin"}}
    assert tz.fmt_ts(0, "%H:%M", cfg) == "11:00"
    assert tz.fmt_ts(0, "%Y-%m-%d %H:%M", {"ui": {"timezone": "UTC"}}) == "1970-01-01 00:00"


def test_valid_zone():
    assert tz.valid_zone("Asia/Sakhalin")
    for bad in ("Mars/Base", "", None, 5, "../etc/passwd", "/etc/localtime"):
        assert not tz.valid_zone(bad)


def test_default_works_without_config():
    assert len(tz.fmt_ts(0, "%H:%M")) == 5
    assert tz.fmt_ts(0, "%H:%M", {}) == tz.fmt_ts(0, "%H:%M", {"ui": {"timezone": "bogus"}})
    assert tz.zone_name({})


def test_invalid_rejected_and_not_saved(tmp_path):
    p = tmp_path / "artel.yaml"
    cfg = {}
    with pytest.raises(ValueError):
        save_timezone(cfg, "Mars/Base", p)
    assert not p.exists() and "ui" not in cfg


def test_persistence_round_trip(tmp_path):
    p = tmp_path / "artel.yaml"
    cfg = {}
    assert save_timezone(cfg, "Asia/Sakhalin", p) == {"configured": "Asia/Sakhalin", "effective": "Asia/Sakhalin"}
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["ui"]["timezone"] == "Asia/Sakhalin"
    assert tz.fmt_ts(0, "%H:%M", cfg) == "11:00"
    with pytest.raises(ValueError):
        save_timezone(cfg, "Nope/Zone", p)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["ui"]["timezone"] == "Asia/Sakhalin"
    assert save_timezone(cfg, "", p)["configured"] == ""
    assert "timezone" not in yaml.safe_load(p.read_text(encoding="utf-8"))["ui"]


def test_overview_has_timezone(monkeypatch):
    import artel.providers as pv

    for f in ("codex_status", "claude_code_status", "api_keys", "routing"):
        monkeypatch.setattr(pv, f, lambda c: {})
    assert overview({"ui": {"timezone": "Asia/Sakhalin"}})["timezone"]["configured"] == "Asia/Sakhalin"


def test_cli_event_line_uses_zone():
    from artel.cli import _event_line

    line = _event_line({"kind": "x", "ts": 0, "seq": 1}, {"ui": {"timezone": "Asia/Sakhalin"}})
    assert " 11:00:00 " in line


def test_dashboard_timezone_wiring():
    import re
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "artel" / "web" / "index.html").read_text(encoding="utf-8")
    assert "const fmtTime=(ts,withDate)" in html and "Intl.DateTimeFormat" in html
    assert "/api/providers/timezone" in html and 'id="tz"' in html
    assert "Intl.supportedValuesOf('timeZone')" in html and 'id="tzl"' in html
    assert not re.search(r"\*1000\)\s*\.toLocale", html)


def test_post_timezone_endpoint(tmp_path, monkeypatch):
    import json
    import threading
    import urllib.error
    import urllib.request
    from artel.httpd import LocalHTTPServer

    import yaml

    from artel.project import DEFAULT_CONFIG, deep_merge
    from artel.web.server import Runner, make_handler

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path))
    runner = Runner(deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}))
    httpd = LocalHTTPServer(("127.0.0.1", 0), make_handler(runner))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/providers/timezone"

    def post(zone):
        req = urllib.request.Request(url, json.dumps({"timezone": zone}).encode(), {"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=5)

    try:
        assert json.load(post("Asia/Sakhalin"))["configured"] == "Asia/Sakhalin"
        with pytest.raises(urllib.error.HTTPError) as e:
            post("Mars/Olympus")
        assert e.value.code == 400
        assert yaml.safe_load((tmp_path / "artel.yaml").read_text(encoding="utf-8"))["ui"]["timezone"] == "Asia/Sakhalin"
    finally:
        httpd.shutdown()
        httpd.server_close()
