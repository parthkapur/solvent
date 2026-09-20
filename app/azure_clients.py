"""Thin Azure SDK calls. Everything here is monkeypatched in tests."""

import os
import time
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

from azure.identity import DefaultAzureCredential


@cache
def _cred() -> DefaultAzureCredential:
    return DefaultAzureCredential()


def _sub() -> str:
    return os.environ["AZURE_SUBSCRIPTION_ID"]


def cost_summary(days: int) -> dict[str, Any]:
    from azure.mgmt.costmanagement import CostManagementClient
    from azure.mgmt.costmanagement.models import (
        QueryAggregation,
        QueryDataset,
        QueryDefinition,
        QueryGrouping,
        QueryTimePeriod,
    )

    now = datetime.now(UTC)
    q = QueryDefinition(
        type="ActualCost",
        timeframe="Custom",
        time_period=QueryTimePeriod(from_property=now - timedelta(days=days), to=now),
        dataset=QueryDataset(
            granularity="None",
            aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
            grouping=[QueryGrouping(type="Dimension", name="ResourceGroupName")],
        ),
    )
    res = CostManagementClient(_cred()).query.usage(scope=f"/subscriptions/{_sub()}", parameters=q)
    cols = [c.name for c in res.columns]
    return {"days": days, "rows": [dict(zip(cols, r)) for r in res.rows]}


def resource_health(resource_group: str) -> dict[str, Any]:
    # The resourcehealth SDK speaks an api-version this provider no longer serves; call REST directly.
    import httpx

    token = _cred().get_token("https://management.azure.com/.default").token
    url = (
        f"https://management.azure.com/subscriptions/{_sub()}/resourceGroups/{resource_group}"
        "/providers/Microsoft.ResourceHealth/availabilityStatuses"
    )
    # ponytail: fresh subscriptions intermittently 409 while provider registration propagates.
    for attempt in range(3):
        r = httpx.get(url, params={"api-version": "2025-05-01"}, headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 409 or attempt == 2:
            break
        time.sleep(2)
    r.raise_for_status()
    items = [
        {
            "id": s["id"].split("/providers/Microsoft.ResourceHealth")[0],
            "state": s["properties"]["availabilityState"],
        }
        for s in r.json()["value"]
    ]
    return {"resource_group": resource_group, "items": items}


def restart_container_app(name: str) -> dict[str, Any]:
    from azure.mgmt.appcontainers import ContainerAppsAPIClient

    # SDK 5.x defaults to an API version the provider does not serve yet; pin the newest GA one.
    client = ContainerAppsAPIClient(_cred(), _sub(), api_version="2025-01-01")
    rg = os.environ["AZURE_RESOURCE_GROUP"]
    rev = client.container_apps.get(rg, name).latest_revision_name
    client.container_apps_revisions.restart_revision(rg, name, rev)
    return {"restarted": name, "revision": rev}
