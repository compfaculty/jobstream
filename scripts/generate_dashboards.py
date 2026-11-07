"""Generate native, provisioned Grafana dashboards. No external plugins/assets."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "deploy/monitoring/dashboards"
PROM = {"type": "prometheus", "uid": "jobstream-prometheus"}
LOKI = {"type": "loki", "uid": "jobstream-loki"}
LINKS = [
    {"title": title, "url": "/d/" + uid, "type": "link", "keepTime": True, "includeVars": True}
    for title, uid in [
        ("Overview", "jobstream-overview"),
        ("Django & collection", "jobstream-django"),
        ("Infinity", "jobstream-infinity"),
        ("Investigations", "jobstream-investigations"),
    ]
]


def variable(name, query, source=PROM):
    return {
        "name": name,
        "label": name.title(),
        "type": "query",
        "datasource": source,
        "query": query,
        "refresh": 1,
        "includeAll": True,
        "allValue": ".+" if source == LOKI else ".*",
        "multi": True,
        "current": {"text": "All", "value": "$__all"},
    }


def dashboard(uid, title, description, variables):
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "schemaVersion": 41,
        "version": 1,
        "editable": False,
        "style": "dark",
        "tags": ["jobstream", "provisioned"],
        "timezone": "browser",
        "refresh": "15s",
        "time": {"from": "now-1h", "to": "now"},
        "links": LINKS,
        "templating": {"list": variables},
        "annotations": {"list": []},
        "panels": [],
    }


def text(d, title, content, y, h=3):
    d["panels"].append(
        {
            "id": len(d["panels"]) + 1,
            "type": "text",
            "title": title,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": h},
            "options": {"mode": "markdown", "content": content},
        }
    )


def panel(
    d,
    title,
    expr,
    x,
    y,
    w=12,
    h=8,
    kind="timeseries",
    unit="short",
    legend="{{service}}",
    description="",
):
    p = {
        "id": len(d["panels"]) + 1,
        "title": title,
        "description": description,
        "type": kind,
        "datasource": PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"expr": expr, "refId": "A", "legendFormat": legend}],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line",
                    "lineInterpolation": "smooth",
                    "lineWidth": 2,
                    "fillOpacity": 14,
                    "showPoints": "never",
                    "spanNulls": False,
                },
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "orange", "value": 0.8},
                        {"color": "red", "value": 1},
                    ],
                },
                "links": [
                    {
                        "title": "Investigate selected scope and time range",
                        "url": (
                            "/d/jobstream-investigations?${__url_time_range}&"
                            + (
                                "var-service=infinity"
                                if d["uid"] == "jobstream-infinity"
                                else "${service:queryparam}"
                            )
                        ),
                    }
                ],
            },
            "overrides": [],
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {
                "displayMode": "table",
                "placement": "bottom",
                "calcs": ["lastNotNull", "max"],
            },
        },
    }
    if kind == "stat":
        p["options"] = {
            "colorMode": "background",
            "graphMode": "area",
            "textMode": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
            "orientation": "auto",
        }
        p["fieldConfig"]["defaults"]["noValue"] = "Unavailable"
    if kind == "heatmap":
        p["targets"][0].update(format="heatmap", legendFormat="{{le}}")
        p["options"] = {
            "calculate": False,
            "color": {"mode": "scheme", "scheme": "Spectral", "steps": 64},
            "yAxis": {"unit": unit},
            "legend": {"show": True},
            "tooltip": {"show": True},
        }
    d["panels"].append(p)
    return p


service = variable("service", "label_values(up, service)")
queue = variable("queue", "label_values(jobstream_queue_depth, queue)")
provider = variable("provider", "label_values(jobstream_outbound_attempts_total, provider)")
overview = dashboard(
    "jobstream-overview",
    "Jobstream · Mission control",
    (
        "Application health, collection progress and worker capacity. Payl"
        "oad charts show observed bodies, not network traffic."
    ),
    [service, queue],
)
text(
    overview,
    "JOBSTREAM / MISSION CONTROL",
    (
        "### A clear view of the pipeline\nRequests → collection → evaluati"
        "on → preparation → approved delivery. **Gaps mean unavailable tel"
        "emetry**, not zero activity. Local alerts have no external receiv"
        "er."
    ),
    0,
)
panel(
    overview,
    "Targets online",
    'sum(up{job=~"jobstream|infinity",service=~"$service"})',
    0,
    3,
    6,
    4,
    "stat",
)
panel(
    overview,
    "Snapshot age",
    "time() - jobstream_snapshot_timestamp_seconds",
    6,
    3,
    6,
    4,
    "stat",
    "s",
)
panel(overview, "Queue backlog", 'sum(jobstream_queue_depth{queue=~"$queue"})', 12, 3, 6, 4, "stat")
panel(
    overview,
    "Review backlog",
    "jobstream_pending_company_reviews + jobstream_pending_source_reviews",
    18,
    3,
    6,
    4,
    "stat",
)
panel(
    overview,
    "HTTP pulse · requests / second",
    (
        'sum by(service) (rate(jobstream_http_requests_total{route!="healt'
        'h",service=~"$service"}[$__rate_interval]))'
    ),
    0,
    7,
    unit="reqps",
)
panel(
    overview,
    "Response latency · p95",
    (
        "histogram_quantile(0.95,sum by(le,service)(rate(jobstream_http_du"
        'ration_seconds_bucket{route!="health",service=~"$service"}[$__rat'
        "e_interval])))"
    ),
    12,
    7,
    unit="s",
    description="Requires observed requests. Empty intervals remain gaps.",
)
panel(
    overview, "Queue pressure", 'jobstream_queue_depth{queue=~"$queue"}', 0, 15, legend="{{queue}}"
)
panel(
    overview,
    "Collection outcomes",
    "sum by(outcome)(rate(jobstream_ingestion_runs_total[$__rate_interval]))",
    12,
    15,
    unit="ops",
    legend="{{outcome}}",
)
panel(
    overview,
    "HTTP failures · 5xx / second",
    (
        'sum by(service)(rate(jobstream_http_requests_total{status=~"5..",'
        'service=~"$service"}[$__rate_interval]))'
    ),
    0,
    23,
    unit="reqps",
)
panel(
    overview,
    "New jobs · finalized imports",
    "sum(increase(jobstream_ingestion_new_jobs_total[$__range]))",
    12,
    23,
    kind="stat",
    description="Process counters reset on container restart. Not a database stock count.",
)
panel(
    overview,
    "Target health over time",
    'up{job=~"jobstream|infinity",service=~"$service"}',
    0,
    31,
    24,
    6,
    "state-timeline",
)

django = dashboard(
    "jobstream-django",
    "Jobstream · Django & collection",
    "HTTP, outbound bodies, collection providers and Celery task attempts.",
    [
        service,
        queue,
        provider,
        variable("route", "label_values(jobstream_http_requests_total, route)"),
    ],
)
text(
    django,
    "APPLICATION / COLLECTION",
    (
        "### From request to completed work\nFilter routes, providers and q"
        "ueues. Liveness probes are separate. Retries are task attempt out"
        "comes; body bytes exclude SMTP and browser-managed traffic."
    ),
    0,
)
panel(
    django,
    "Route throughput",
    (
        'sum by(route,status)(rate(jobstream_http_requests_total{route=~"$'
        'route",route!="health",service=~"$service"}[$__rate_interval]))'
    ),
    0,
    3,
    unit="reqps",
    legend="{{route}} · {{status}}",
)
panel(
    django,
    "Latency distribution",
    (
        'sum by(le)(rate(jobstream_http_duration_seconds_bucket{route=~"$r'
        'oute",route!="health",service=~"$service"}[$__rate_interval]))'
    ),
    12,
    3,
    kind="heatmap",
    unit="s",
)
for i, q in enumerate([0.5, 0.95, 0.99]):
    panel(
        django,
        f"Response latency · p{int(q * 100)}",
        (
            f"histogram_quantile({q},sum by(le,route)(rate("
            'jobstream_http_duration_seconds_bucket{route=~"$route",'
            'route!="health",service=~"$service"}[$__rate_interval])))'
        ),
        i * 8,
        11,
        8,
        6,
        unit="s",
        legend="{{route}}",
    )
panel(django, "In-flight responses", 'jobstream_http_in_progress{service=~"$service"}', 0, 17)
panel(
    django,
    "Observed outbound payload",
    (
        "sum by(provider)(rate(jobstream_outbound_payload_bytes_total{prov"
        'ider=~"$provider"}[$__rate_interval]))'
    ),
    12,
    17,
    unit="Bps",
    legend="{{provider}}",
    description="Decoded bytes at bounded read points; excludes unknown bodies and wire overhead.",
)
panel(
    django,
    "Provider outcomes",
    (
        "sum by(provider,status_class,error)(rate(jobstream_outbound_attem"
        'pts_total{provider=~"$provider"}[$__rate_interval]))'
    ),
    0,
    25,
    unit="ops",
    legend="{{provider}} · {{status_class}} · {{error}}",
)
panel(
    django,
    "Provider latency · p95",
    (
        "histogram_quantile(0.95,sum by(le,provider)(rate(jobstream_outbou"
        'nd_duration_seconds_bucket{provider=~"$provider"}[$__rate_interva'
        "l])))"
    ),
    12,
    25,
    unit="s",
    legend="{{provider}}",
)
panel(
    django,
    "Task outcomes · includes retries",
    (
        'sum by(task,state)(rate(jobstream_tasks_total{queue=~"$queue",sta'
        'te!="started"}[$__rate_interval]))'
    ),
    0,
    33,
    unit="ops",
    legend="{{task}} · {{state}}",
)
panel(
    django,
    "Running tasks by queue",
    'sum by(queue)(jobstream_tasks_running{queue=~"$queue"})',
    12,
    33,
    legend="{{queue}}",
)
panel(
    django,
    "Throttles",
    (
        "sum by(provider)(rate(jobstream_outbound_throttles_total{provider"
        '=~"$provider"}[$__rate_interval]))'
    ),
    0,
    41,
    legend="{{provider}}",
)
panel(
    django,
    "Liveness requests",
    'sum by(status)(rate(jobstream_http_requests_total{route="health"}[$__rate_interval]))',
    12,
    41,
    unit="reqps",
    legend="{{status}}",
)

infinity = dashboard(
    "jobstream-infinity",
    "Jobstream · Infinity runtime",
    (
        "Runtime and provider diagnostics. Optional disabled Infinity has "
        "no scrape target and is not an incident."
    ),
    [],
)
text(
    infinity,
    "INFINITY / REASONING RUNTIME",
    (
        "### Evaluation health without private content\nOnly known token us"
        "age is counted. No prompts, CVs, response bodies or monetary esti"
        "mates. A disabled runtime displays **Unavailable**."
    ),
    0,
)
panel(infinity, "Runtime reachable", 'up{job="infinity"}', 0, 3, 6, 4, "stat")
panel(infinity, "Active evaluations", "jobstream_infinity_active_evaluations", 6, 3, 6, 4, "stat")
panel(
    infinity,
    "Known input tokens",
    'sum(increase(jobstream_infinity_provider_tokens_total{direction="input"}[$__range]))',
    12,
    3,
    6,
    4,
    "stat",
)
panel(
    infinity,
    "Unknown usage calls",
    "sum(increase(jobstream_infinity_provider_unknown_usage_total[$__range]))",
    18,
    3,
    6,
    4,
    "stat",
)
panel(
    infinity,
    "Attempt outcomes",
    "sum by(outcome)(rate(jobstream_infinity_evaluations_total[$__rate_interval]))",
    0,
    7,
    unit="ops",
    legend="{{outcome}}",
)
panel(
    infinity,
    "Inbound latency · p95",
    (
        "histogram_quantile(0.95,sum by(le,route)(rate(jobstream_infinity_"
        "http_duration_seconds_bucket[$__rate_interval])))"
    ),
    12,
    7,
    unit="s",
    legend="{{route}}",
)
panel(
    infinity,
    "Provider outcomes",
    "sum by(outcome)(rate(jobstream_infinity_provider_calls_total[$__rate_interval]))",
    0,
    15,
    unit="ops",
    legend="{{outcome}}",
)
panel(
    infinity,
    "Provider latency distribution",
    "sum by(le)(rate(jobstream_infinity_provider_duration_seconds_bucket[$__rate_interval]))",
    12,
    15,
    kind="heatmap",
    unit="s",
)
panel(
    infinity,
    "Known tokens / second",
    "sum by(direction)(rate(jobstream_infinity_provider_tokens_total[$__rate_interval]))",
    0,
    23,
    legend="{{direction}}",
)
panel(infinity, "Recovery events", "jobstream_infinity_recovered_total", 12, 23, kind="stat")

logs = dashboard(
    "jobstream-investigations",
    "Jobstream · Investigations",
    (
        "Restricted operational logs; delayed, duplicated or dropped event"
        "s are possible. Not an audit store."
    ),
    [
        variable("service", "label_values(service)", LOKI),
        variable("severity", "label_values(severity)", LOKI),
        {
            "name": "event",
            "type": "custom",
            "query": (
                "http_completed,http_exception,task_started,task_finished,ingestio"
                "n_completed,provider_call,evaluation_finished,runtime_event"
            ),
            "includeAll": True,
            "allValue": ".*",
            "current": {"text": "All", "value": "$__all"},
        },
        {
            "name": "correlation",
            "label": "Correlation UUID (optional)",
            "type": "textbox",
            "current": {"text": "", "value": ""},
        },
    ],
)
text(
    logs,
    "INVESTIGATE / FOLLOW THE CHAIN",
    (
        "### Connect the signal to the sequence\nChoose a time range, servi"
        "ce and severity. Paste a request, workflow, task, execution or at"
        "tempt UUID to follow it across services. Logs expire after seven "
        "days; missing events may indicate disabled collection, drops or r"
        "otation."
    ),
    0,
)
p = panel(
    logs,
    "Errors over time",
    (
        'sum by(service)(count_over_time({service=~"$service",severity=~"E'
        'RROR|CRITICAL"}[$__interval]))'
    ),
    0,
    3,
    16,
    7,
    legend="{{service}}",
)
p["datasource"] = LOKI
p["targets"][0]["queryType"] = "range"
panel(
    logs,
    "Application log drops",
    "sum by(service,reason)(increase(jobstream_log_dropped_total[$__range]))",
    16,
    3,
    8,
    7,
    kind="stat",
    legend="{{service}} · {{reason}}",
)
logs["panels"].append(
    {
        "id": len(logs["panels"]) + 1,
        "type": "logs",
        "title": "Live investigation stream",
        "datasource": LOKI,
        "gridPos": {"x": 0, "y": 10, "w": 24, "h": 18},
        "targets": [
            {
                "refId": "A",
                "expr": (
                    '{service=~"$service",severity=~"$severity"} | json | event=~"${ev'
                    'ent:regex}" |~ "${correlation:regex}"'
                ),
                "queryType": "range",
                "maxLines": 500,
            }
        ],
        "options": {
            "showTime": True,
            "showLabels": False,
            "wrapLogMessage": True,
            "prettifyLogMessage": True,
            "sortOrder": "Descending",
            "enableLogDetails": True,
            "dedupStrategy": "exact",
        },
    }
)

# Collection health belongs beside the logs it protects.
for item in logs["panels"]:
    if item["type"] == "logs":
        item["gridPos"]["y"] = 16
panel(
    logs,
    "Collector drops / rejected lines",
    "sum by(service)(increase(loki_write_dropped_entries_total[$__range]))",
    0,
    10,
    8,
    6,
    legend="{{service}} · shipping",
)
logs["panels"][-1]["targets"].extend(
    [
        {
            "refId": "B",
            "expr": "sum by(service)(increase(loki_process_dropped_lines_total[$__range]))",
            "legendFormat": "{{service}} · parser/rate limit",
        },
        {
            "refId": "C",
            "expr": "sum by(service)(increase(loki_discarded_samples_total[$__range]))",
            "legendFormat": "{{service}} · rejected by Loki",
        },
    ]
)
panel(
    logs,
    "File buffer occupancy",
    'jobstream_log_buffer_events{service=~"$service"}',
    8,
    10,
    8,
    6,
    legend="{{service}}",
)
panel(
    logs,
    "Monitoring filesystem headroom",
    "min(jobstream_monitoring_disk_available_bytes)",
    16,
    10,
    8,
    6,
    kind="stat",
    unit="bytes",
)
recent = dict(next(item for item in logs["panels"] if item["type"] == "logs"))
recent.update(
    id=len(logs["panels"]) + 1, title="Recent failures", gridPos={"x": 0, "y": 34, "w": 24, "h": 10}
)
recent["targets"] = [
    {
        "refId": "A",
        "expr": '{service=~"$service",severity=~"ERROR|CRITICAL"}',
        "queryType": "range",
        "maxLines": 100,
    }
]
logs["panels"].append(recent)

# Operational color semantics: stale/unhealthy states must never look healthy.
for data in (overview, django, infinity, logs):
    for item in data["panels"]:
        if item["type"] != "stat":
            continue
        defaults = item["fieldConfig"]["defaults"]
        defaults["color"] = {"mode": "thresholds"}
        defaults["thresholds"]["steps"] = [
            {"color": "#5794F2", "value": None},
        ]
        if item["title"] == "Targets online":
            item["title"] = "Target availability"
            item["targets"][0]["expr"] = (
                'sum(up{job=~"jobstream|infinity",service=~"$service"}) / '
                'count(up{job=~"jobstream|infinity",service=~"$service"})'
            )
            defaults.update(unit="percentunit", min=0, max=1)
            defaults["thresholds"]["steps"] = [
                {"color": "red", "value": None},
                {"color": "orange", "value": 0.9},
                {"color": "green", "value": 1},
            ]
        if item["title"] == "Monitoring filesystem headroom":
            defaults["thresholds"]["steps"] = [
                {"color": "red", "value": None},
                {"color": "orange", "value": 2 * 1024**3},
                {"color": "#5794F2", "value": 4 * 1024**3},
            ]
        if item["title"] == "Snapshot age":
            item["targets"][0]["expr"] = (
                "(time() - jobstream_snapshot_timestamp_seconds) "
                "and jobstream_snapshot_present == 1"
            )
            defaults["thresholds"]["steps"] = [
                {"color": "green", "value": None},
                {"color": "orange", "value": 90},
                {"color": "red", "value": 180},
            ]
        if item["title"] in {"Queue backlog", "Review backlog"}:
            defaults["thresholds"]["steps"] = [
                {"color": "#5794F2", "value": None},
                {"color": "orange", "value": 20},
                {"color": "red", "value": 100},
            ]

for name, data in [
    ("overview", overview),
    ("django", django),
    ("infinity", infinity),
    ("investigations", logs),
]:
    (ROOT / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
