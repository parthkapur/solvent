import json

from demo import report
from demo.tests import fixture

EVENTS = fixture.build_events()


def test_scrub_blanks_the_subscription_id_and_leaves_the_rest():
    events = [{"t": 1, "lane": "sys", "kind": "azure_context", "sub": "secret-sub", "rg": "rg"},
              {"t": 2, "lane": "client"}]
    out = report.scrub(events)
    assert out[0]["sub"] == "" and out[0]["rg"] == "rg" and out[1] == events[1]
    assert events[0]["sub"] == "secret-sub"  # the input is untouched


def test_scrub_redacts_identifiers_in_lane_unavailable_reasons():
    reason = ("The client 'someone@example.com' with object id "
              "'0b5a0f52-7f3a-4c1e-9d5e-1a2b3c4d5e6f' does not have authorization to perform "
              "action over scope '/subscriptions/11111111-2222-3333-4444-555555555555"
              "/resourceGroups/solventdev-rg' or the scope is invalid.")
    events = [{"t": 1, "lane": "sys", "kind": "lane_unavailable", "name": "azure",
               "reason": reason}]
    out = report.scrub(events)[0]["reason"]
    for leaked in ("someone", "example.com", "0b5a0f52", "11111111", "555555555555"):
        assert leaked not in out
    assert "does not have authorization" in out and "solventdev-rg" in out
    assert events[0]["reason"] == reason  # the input is untouched


def test_numbers_report_the_error_budget_without_the_100_percent_clamp():
    n = report.numbers(EVENTS)
    assert n["budget_consumed_pct"] > 100  # four slow minutes against a 1% budget
    assert f"{n['budget_consumed_pct']}%" in report.render_postmortem(n)


def test_numbers_are_computed_from_the_events():
    n = report.numbers(EVENTS)
    assert n["denied_runaway"] == 240  # 2 per second for the 120 s the fixture runs
    assert n["slow_calls"] > 0 and n["minutes_over_slo"] >= 3
    by_rule = {a["rule"]: a for a in n["alerts"]}
    denied = by_rule["solventdev-denied-writes"]
    assert 300 <= denied["lag_s"] <= 600  # five to ten minutes, as the spec expects
    assert by_rule["solventdev-latency-slo"]["resolved"] is not None
    assert [p["key"] for p in n["phases"]] == ["baseline", "runaway", "quiet", "slow", "recovery"]


def test_postmortem_has_facts_filled_and_the_human_sections_marked():
    text = report.render_postmortem(report.numbers(EVENTS))
    assert "denied-writes" in text and "latency-slo" in text
    for heading in ("## Timeline (UTC)", "## Impact", "## Root cause", "## What went well",
                    "## Action items"):
        assert heading in text
    assert text.count("<!-- write after reviewing the run -->") == 3


def test_main_writes_both_files_and_will_not_overwrite_a_postmortem(tmp_path, monkeypatch):
    src = tmp_path / "events.jsonl"
    src.write_text("\n".join(json.dumps(e) for e in EVENTS))
    monkeypatch.setattr(report, "DOCS", tmp_path / "docs" / "demo")
    assert report.main([str(src)]) == 0
    html = (tmp_path / "docs" / "demo" / "report.html").read_text()
    assert "window.__EVENTS__ = [" in html and "__TOKEN__" not in html
    pm = tmp_path / "docs" / "demo" / "postmortem.md"
    pm.write_text("hand written")
    assert report.main([str(src)]) == 1
    assert pm.read_text() == "hand written"
    assert report.main([str(src), "--force"]) == 0
    assert pm.read_text() != "hand written"
