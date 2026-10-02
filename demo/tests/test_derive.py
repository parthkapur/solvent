from demo import derive

T0 = 1_780_000_020_000  # minute-aligned


def client(sec, outcome="allowed", rtt=500.0, warmup=False):
    ev = {"t": T0 + sec * 1000, "lane": "client", "outcome": outcome, "rtt_ms": rtt}
    if warmup:
        ev["warmup"] = True
    return ev


def test_p95_nearest_rank():
    assert derive.p95([]) is None
    assert derive.p95([10]) == 10
    assert derive.p95(list(range(1, 101))) == 95


def test_kpis_windows_and_budget():
    events = [client(0, rtt=100), client(30, rtt=3000), client(-200, "denied"),
              client(5, warmup=True, rtt=99999)]
    k = derive.kpis(events, T0 + 60_000)
    # the denied call is 260 s old: inside the 5-minute window, outside the 1-minute one;
    # warmup never counts
    assert k["calls_1m"] == 2
    assert k["p95_ms"] == 3000
    assert k["denied_5m"] == 1
    assert (k["allowed_total"], k["slow_total"]) == (2, 1)
    assert k["budget_left_pct"] == 0.0  # 1 slow of 2 is 50%, against a 1% budget


def test_kpis_reports_budget_consumed_without_the_clamp():
    events = [client(i, rtt=3000 if i < 5 else 100) for i in range(100)]  # 5% slow vs a 1% budget
    k = derive.kpis(events, T0 + 200_000)
    assert k["budget_left_pct"] == 0.0
    assert k["budget_consumed_pct"] == 500.0


def test_kpis_with_no_traffic_is_calm():
    k = derive.kpis([], T0)
    assert (k["calls_1m"], k["p95_ms"], k["budget_left_pct"]) == (0, None, 100.0)


def azure(rows, until):
    return {"t": T0, "lane": "azure", "kind": "series", "rows": rows, "data_until": until}


def row(minute_offset, decision, calls):
    from datetime import UTC, datetime

    iso = datetime.fromtimestamp(T0 / 1000 + minute_offset * 60, UTC).isoformat()
    return {"minute": iso.replace("+00:00", "Z"), "decision": decision, "calls": calls}


START = {"t": T0, "lane": "control", "kind": "start"}


def test_reconcile_pending_ok_and_unaccounted():
    events = [START] + [client(i) for i in range(4)] + [client(9, "denied")]
    assert derive.reconcile(events, settled=False)["allowed"]["status"] == "pending"

    events.append({"t": T0 + 5000, "lane": "server", "decision": "allowed"})
    events += [{"t": T0 + 6000, "lane": "server", "decision": "allowed"}] * 3
    assert derive.reconcile(events, settled=False)["allowed"]["status"] == "ok"

    only_client = [START] + [client(i) for i in range(4)]
    r = derive.reconcile(only_client, settled=True)["allowed"]
    assert (r["client"], r["gap"], r["status"]) == (4, 4, "unaccounted")


def test_reconcile_uses_azure_when_it_has_more_than_the_server_lane():
    events = [START, client(0), client(1), azure([row(0, "allowed", 2)], "x")]
    r = derive.reconcile(events, settled=True)["allowed"]
    assert (r["server"], r["azure"], r["status"]) == (0, 2, "ok")


def test_reconcile_ignores_azure_rows_from_before_the_run():
    events = [START, client(0), azure([row(-10, "allowed", 50), row(0, "allowed", 1)], "x")]
    assert derive.reconcile(events, settled=True)["allowed"]["azure"] == 1


def test_settled_needs_traffic_done_and_azure_caught_up():
    done = {"t": T0 + 120_000, "lane": "phase", "kind": "traffic_done"}
    assert derive.settled([START]) is False
    assert derive.settled([START, done]) is False
    behind = azure([row(0, "allowed", 1)], row(0, "allowed", 1)["minute"])
    assert derive.settled([START, done, behind]) is False
    caught = azure([row(2, "allowed", 1)], row(2, "allowed", 1)["minute"])
    assert derive.settled([START, done, caught]) is True


def test_settled_uses_the_last_call_not_the_moment_traffic_was_declared_done():
    # The last call is in minute 1; traffic_done is stamped 2 s into minute 2, where no calls
    # exist, so Azure can never have a bin for minute 2. It has caught up once it has minute 1.
    done = {"t": T0 + 122_000, "lane": "phase", "kind": "traffic_done"}
    caught = azure([row(1, "allowed", 1)], row(1, "allowed", 1)["minute"])
    assert derive.settled([START, client(115), done, caught]) is True
    behind = azure([row(0, "allowed", 1)], row(0, "allowed", 1)["minute"])
    assert derive.settled([START, client(115), done, behind]) is False


def test_minutes_over_slo_counts_minutes_whose_p95_breaks_the_objective():
    events = [client(s, rtt=500) for s in range(0, 60, 5)]
    events += [client(60 + s, rtt=3500) for s in range(0, 60, 5)]
    events += [client(120 + s, rtt=3500, outcome="denied") for s in range(0, 60, 5)]
    assert derive.minutes_over_slo(events) == 1
