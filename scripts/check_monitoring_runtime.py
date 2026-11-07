"""Read-only Grafana provisioning/query checks. Never print credentials or log bodies."""

import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = (
    "jobstream-overview",
    "jobstream-django",
    "jobstream-infinity",
    "jobstream-investigations",
)


def main():
    password = (ROOT / ".monitoring-secrets/grafana-admin").read_text().strip()
    with httpx.Client(
        base_url="http://127.0.0.1:3000", auth=("admin", password), timeout=20, trust_env=False
    ) as client:
        for uid in DASHBOARDS:
            result = client.get("/api/dashboards/uid/" + uid)
            result.raise_for_status()
            dashboard = result.json()["dashboard"]
            print(f"{uid}: {len(dashboard['panels'])} provisioned panels")
        for uid in ("jobstream-prometheus", "jobstream-loki"):
            result = client.get("/api/datasources/uid/" + uid + "/health")
            result.raise_for_status()
            assert result.json()["status"] == "OK", uid
            print(f"{uid}: healthy")
        proxy = "/api/datasources/proxy/uid/jobstream-prometheus/api/v1/"
        result = client.get(proxy + "query", params={"query": "up"})
        result.raise_for_status()
        targets = result.json()["data"]["result"]
        print(
            "Scrape status: "
            + json.dumps(
                {row["metric"].get("service", "?"): row["value"][1] for row in targets},
                sort_keys=True,
            )
        )
        # Parse every provisioned PromQL expression through the real query engine.
        for file in (ROOT / "deploy/monitoring/dashboards").glob("*.json"):
            dashboard = json.loads(file.read_text())
            for panel in dashboard["panels"]:
                if panel.get("datasource", {}).get("uid") == "jobstream-loki":
                    for target in panel.get("targets", []):
                        expr = target["expr"]
                        for variable, value in [
                            ("$service", ".+"),
                            ("$severity", ".+"),
                            ("${event:regex}", ".*"),
                            ("${correlation:regex}", ""),
                            ("$__interval", "1m"),
                        ]:
                            expr = expr.replace(variable, value)
                        response = client.get(
                            "/api/datasources/proxy/uid/jobstream-loki/loki/api/v1/query_range",
                            params={"query": expr, "limit": 10, "step": 60},
                        )
                        response.raise_for_status()
                        assert response.json()["status"] == "success", panel["title"]
                    continue
                if panel.get("datasource", {}).get("uid") != "jobstream-prometheus":
                    continue
                for target in panel.get("targets", []):
                    expr = target["expr"]
                    for variable in ("service", "queue", "provider", "route"):
                        expr = expr.replace("$" + variable, ".*")
                    for variable, value in [
                        ("$__rate_interval", "1m"),
                        ("$__interval", "1m"),
                        ("$__range", "1h"),
                    ]:
                        expr = expr.replace(variable, value)
                    response = client.get(proxy + "query", params={"query": expr})
                    response.raise_for_status()
                    assert response.json()["status"] == "success", panel["title"]
        print("Provisioned PromQL and LogQL queries: valid")


if __name__ == "__main__":
    main()
