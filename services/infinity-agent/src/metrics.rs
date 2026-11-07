//! Process-local bounded metrics. RAII guards cover dropped/cancelled futures.
use axum::{body::Body, extract::MatchedPath, http::Request, middleware::Next, response::Response};
use prometheus_client::{
    encoding::text::encode,
    metrics::{counter::Counter, family::Family, gauge::Gauge, histogram::Histogram},
    registry::Registry,
};
use std::{sync::OnceLock, time::Instant};

type Labels = Vec<(String, String)>;
fn histogram() -> Histogram {
    Histogram::new([
        0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, 180.0,
    ])
}
type Histograms = Family<Labels, Histogram, fn() -> Histogram>;
pub struct Metrics {
    pub evaluations: Family<Labels, Counter>,
    pub active: Gauge,
    pub recovered: Counter,
    pub log_drops: Counter,
    pub calls: Family<Labels, Counter>,
    pub tokens: Family<Labels, Counter>,
    pub unknown: Counter,
    http: Family<Labels, Counter>,
    http_time: Histograms,
    provider_time: Histograms,
    inflight: Gauge,
}
static METRICS: OnceLock<Metrics> = OnceLock::new();
pub fn metrics() -> &'static Metrics {
    METRICS.get_or_init(|| Metrics {
        http_time: Family::new_with_constructor(histogram as fn() -> Histogram),
        provider_time: Family::new_with_constructor(histogram as fn() -> Histogram),
        evaluations: Family::default(),
        active: Gauge::default(),
        recovered: Counter::default(),
        log_drops: Counter::default(),
        calls: Family::default(),
        tokens: Family::default(),
        unknown: Counter::default(),
        http: Family::default(),
        inflight: Gauge::default(),
    })
}
pub fn label(key: &str, value: &str) -> Labels {
    vec![(key.into(), value.into())]
}
fn registry() -> Registry {
    let m = metrics();
    let mut r = Registry::with_prefix("jobstream_infinity");
    r.register(
        "evaluations",
        "Evaluation attempt transitions",
        m.evaluations.clone(),
    );
    r.register(
        "active_evaluations",
        "Active evaluation attempts",
        m.active.clone(),
    );
    r.register("log_dropped", "File events dropped", m.log_drops.clone());
    r.register("recovered", "Recovered evaluations", m.recovered.clone());
    r.register(
        "provider_calls",
        "Terminal provider attempt outcomes",
        m.calls.clone(),
    );
    r.register(
        "provider_tokens",
        "Known provider tokens only",
        m.tokens.clone(),
    );
    r.register(
        "provider_unknown_usage",
        "Provider calls with missing usage",
        m.unknown.clone(),
    );
    r.register("http_requests", "Inbound HTTP responses", m.http.clone());
    r.register(
        "http_duration_seconds",
        "Inbound HTTP latency",
        m.http_time.clone(),
    );
    r.register(
        "provider_duration_seconds",
        "Provider attempt duration",
        m.provider_time.clone(),
    );
    r.register(
        "http_in_progress",
        "Active inbound requests",
        m.inflight.clone(),
    );
    r
}
pub struct Evaluation {
    outcome: &'static str,
}
impl Evaluation {
    pub fn new() -> Self {
        metrics().active.inc();
        metrics()
            .evaluations
            .get_or_create(&label("outcome", "started"))
            .inc();
        Self {
            outcome: "cancelled",
        }
    }
    pub fn finish(&mut self, value: &str) {
        self.outcome = match value {
            "completed" => "completed",
            "failed" => "failed",
            _ => "cancelled",
        };
    }
}
impl Drop for Evaluation {
    fn drop(&mut self) {
        metrics().active.dec();
        metrics()
            .evaluations
            .get_or_create(&label("outcome", self.outcome))
            .inc();
        tracing::info!(event = "evaluation_finished", outcome = self.outcome);
    }
}

pub struct ProviderCall {
    start: Instant,
    done: bool,
    model: &'static str,
}
impl ProviderCall {
    pub fn new(model: &str) -> Self {
        Self {
            start: Instant::now(),
            done: false,
            model: if model == "fake" {
                "fake"
            } else {
                "configured"
            },
        }
    }
    pub fn finish(&mut self, outcome: &str, code: &str, input: Option<u64>, output: Option<u64>) {
        if self.done {
            return;
        }
        self.done = true;
        let outcome = match outcome {
            "completed" => "completed",
            "cancelled" => "cancelled",
            "timeout" => "timeout",
            _ => "failed",
        };
        let error = match code {
            "" => "none",
            "agent_provider_throttled" => "throttled",
            "agent_provider_timeout" => "timeout",
            "cancelled" => "cancelled",
            _ => "failure",
        };
        metrics()
            .calls
            .get_or_create(&vec![
                ("outcome".into(), outcome.into()),
                ("model".into(), self.model.into()),
                ("error".into(), error.into()),
            ])
            .inc();
        metrics()
            .provider_time
            .get_or_create(&label("model", self.model))
            .observe(self.start.elapsed().as_secs_f64());
        for (direction, value) in [("input", input), ("output", output)] {
            if let Some(value) = value {
                metrics()
                    .tokens
                    .get_or_create(&vec![
                        ("direction".into(), direction.into()),
                        ("model".into(), self.model.into()),
                    ])
                    .inc_by(value);
            }
        }
        if input.is_none() || output.is_none() {
            metrics().unknown.inc();
        }
    }
}
impl Drop for ProviderCall {
    fn drop(&mut self) {
        if !self.done {
            self.finish("cancelled", "cancelled", None, None);
        }
    }
}

struct HttpGuard {
    started: Instant,
    labels: Labels,
    status: &'static str,
    code: u16,
}
impl Drop for HttpGuard {
    fn drop(&mut self) {
        let m = metrics();
        m.inflight.dec();
        m.http_time
            .get_or_create(&self.labels)
            .observe(self.started.elapsed().as_secs_f64());
        let mut labels = self.labels.clone();
        labels.push(("status_class".into(), self.status.into()));
        labels.push((
            "status".into(),
            if self.code == 0 {
                "cancelled".into()
            } else {
                self.code.to_string()
            },
        ));
        m.http.get_or_create(&labels).inc();
    }
}
pub async fn http(request: Request<Body>, next: Next) -> Response {
    let route = match request
        .extensions()
        .get::<MatchedPath>()
        .map(MatchedPath::as_str)
    {
        Some("/v1/executions/{id}/{attempt}") => "execution",
        Some("/v1/operations/executions/{id}/calls") => "provider_calls",
        _ => "unknown",
    };
    let method = match request.method().as_str() {
        "GET" => "GET",
        "PUT" => "PUT",
        "DELETE" => "DELETE",
        _ => "OTHER",
    };
    metrics().inflight.inc();
    let mut guard = HttpGuard {
        started: Instant::now(),
        labels: vec![
            ("route".into(), route.into()),
            ("method".into(), method.into()),
        ],
        status: "cancelled",
        code: 0,
    };
    let response = next.run(request).await;
    guard.code = response.status().as_u16();
    guard.status = match response.status().as_u16() / 100 {
        2 => "2xx",
        3 => "3xx",
        4 => "4xx",
        _ => "5xx",
    };
    response
}
pub async fn serve() -> Result<(), crate::store::Error> {
    let Ok(bind) = std::env::var("INFINITY_METRICS_BIND") else {
        return Ok(());
    };
    let secret = std::fs::read_to_string(std::env::var("JOBSTREAM_SCRAPE_TOKEN_FILE")?)?
        .trim()
        .to_owned();
    if secret.len() < 32 {
        return Err("Scrape credential too short".into());
    }
    let router = axum::Router::new().route(
        "/metrics",
        axum::routing::get(move |headers: axum::http::HeaderMap| {
            let secret = secret.clone();
            async move {
                use subtle::ConstantTimeEq;
                let expected = format!("Bearer {secret}");
                let supplied = headers
                    .get("authorization")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("");
                if supplied.as_bytes().ct_eq(expected.as_bytes()).unwrap_u8() != 1 {
                    return (axum::http::StatusCode::UNAUTHORIZED, "").into_response();
                }
                let mut body = String::new();
                if encode(&mut body, &registry()).is_err() {
                    return (axum::http::StatusCode::SERVICE_UNAVAILABLE, "").into_response();
                }
                (
                    [(
                        "content-type",
                        "application/openmetrics-text; version=1.0.0; charset=utf-8",
                    )],
                    body,
                )
                    .into_response()
            }
        }),
    );
    use axum::response::IntoResponse;
    axum::serve(tokio::net::TcpListener::bind(bind).await?, router).await?;
    Ok(())
}
