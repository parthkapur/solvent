import pytest

from demo import run


@pytest.mark.parametrize("url", [
    "https://solventdev-app.happyocean-04abcb36.eastus.azurecontainerapps.io",
    "http://127.0.0.1:8000",
    "http://localhost:8000/mcp",
])
def test_check_target_allows_dev_and_local(url):
    run.check_target(url)


@pytest.mark.parametrize("url", [
    "https://solvent-app.happyocean-04abcb36.eastus.azurecontainerapps.io",  # prod
    "https://solventdev-app.evil.com",  # a lookalike prefix
    "https://evil.example/solventdev-app.x.azurecontainerapps.io",  # host is evil.example
    "https://solventdev-app.x.azurecontainerapps.io.evil.com",
    "http://solventdev-app.x.azurecontainerapps.io",  # the bearer key must not go in cleartext
    "not a url",
    "",
])
def test_check_target_refuses_everything_else(url):
    with pytest.raises(SystemExit):
        run.check_target(url)


def test_dry_run_prints_the_schedule_and_the_denial_budget(capsys):
    assert run.main(["--dry-run", "--profile", "live"]) == 0
    out = capsys.readouterr().out
    assert "Runaway agent" in out and "Slow dependency" in out
    assert "at most 3" in out


def test_format_line_covers_each_lane(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    t0 = 1_000_000
    client = {"t": t0 + 1500, "lane": "client", "agent": "monitor", "tool": "get_resource_health",
              "outcome": "allowed", "reason": "ok", "rtt_ms": 812.0, "fault_asked_ms": 3000}
    line = run.format_line(client, t0)
    assert "allowed" in line and "812ms" in line and "+3000ms fault" in line
    denied = {**client, "outcome": "denied", "reason": "approval_required", "fault_asked_ms": 0}
    assert "approval_required" in run.format_line(denied, t0)
    server = {"t": t0, "lane": "server", "decision": "allowed", "tool": "t", "reason": "read",
              "latency_ms": 900.0, "gate_ms": 0.1, "fault_ms": 3000, "caller": "shared_key"}
    server_line = run.format_line(server, t0)
    assert "gate 0.1ms" in server_line and "fault 3000ms" in server_line
    alert = {"t": t0, "lane": "alert", "rule": "solventdev-latency-slo", "state": "Fired"}
    assert "Fired" in run.format_line(alert, t0)
    phase = {"t": t0, "lane": "phase", "kind": "start", "index": 1, "of": 5,
             "title": "Runaway agent"}
    assert "phase 2/5" in run.format_line(phase, t0)
    gone = {"t": t0, "lane": "sys", "kind": "lane_unavailable", "name": "server",
            "reason": "no az"}
    assert "unavailable" in run.format_line(gone, t0)
    assert run.format_line({"t": t0, "lane": "kpi"}, t0) is None


def test_step_mode_needs_the_page_because_next_lives_there(monkeypatch):
    monkeypatch.setenv("SOLVENT_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("SOLVENT_API_KEY", "k")
    with pytest.raises(SystemExit) as e:
        run.main(["--step"])
    assert "--ui" in str(e.value)
    assert run.main(["--dry-run", "--step"]) == 0  # a dry run never advances anything


def test_missing_credentials_are_a_clear_error(monkeypatch):
    monkeypatch.delenv("SOLVENT_URL", raising=False)
    monkeypatch.delenv("SOLVENT_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        run.main(["--profile", "live"])
    assert "SOLVENT_URL" in str(e.value)
