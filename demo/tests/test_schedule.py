import pytest

from demo import schedule


def test_baseline_never_reaches_the_alert():
    assert schedule.baseline_denials() <= 3
    assert schedule.baseline_denials() <= schedule.DENIED_ALERT_THRESHOLD - 2


def test_profiles_have_the_five_phases_in_order():
    for phases in schedule.PROFILES.values():
        assert [p.key for p in phases] == ["baseline", "runaway", "quiet", "slow", "recovery"]


@pytest.mark.parametrize("profile,minutes", [("full", 50), ("live", 15)])
def test_profile_lengths(profile, minutes):
    assert schedule.total_minutes(profile) == minutes


@pytest.mark.parametrize("profile", ["full", "live"])
def test_each_incident_can_actually_trip_its_alert(profile):
    phases = {p.key: p for p in schedule.PROFILES[profile]}
    runaway = phases["runaway"]
    assert runaway.runaway_s <= runaway.minutes * 60
    assert runaway.runaway_s * schedule.RUNAWAY_PER_S > schedule.DENIED_ALERT_THRESHOLD
    assert phases["slow"].fault_ms > schedule.SLO_MS
    assert all(p.fault_ms == 0 for k, p in phases.items() if k != "slow")
