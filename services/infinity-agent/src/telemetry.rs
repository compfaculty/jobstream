//! Allowlisted provider metadata. No prompts, responses, credentials or exception text.
use crate::store::Error;
use serde::{Deserialize, Serialize};
use std::time::Instant;
use tokio_postgres::Client;
use uuid::Uuid;

#[derive(Clone, Serialize, Deserialize)]
pub struct Call {
    pub call_id: String,
    pub execution_id: String,
    pub attempt_id: String,
    pub model: String,
    pub started_at: String,
    pub finished_at: Option<String>,
    pub latency_ms: Option<i64>,
    pub outcome: String,
    pub error_code: String,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub provider_request_id: Option<String>,
    #[serde(skip)]
    pub metric: Option<std::sync::Arc<std::sync::Mutex<crate::metrics::ProviderCall>>>,
}
impl Call {
    pub fn new(execution: &str, attempt: &str, model: &str) -> Self {
        Self {
            call_id: Uuid::new_v4().to_string(),
            execution_id: execution.into(),
            attempt_id: attempt.into(),
            model: model.into(),
            started_at: chrono::Utc::now().to_rfc3339(),
            finished_at: None,
            latency_ms: None,
            outcome: "running".into(),
            error_code: String::new(),
            input_tokens: None,
            output_tokens: None,
            provider_request_id: None,
            metric: Some(std::sync::Arc::new(std::sync::Mutex::new(
                crate::metrics::ProviderCall::new(model),
            ))),
        }
    }
    pub async fn persist(&self, db: Option<&Client>) {
        tracing::info!(execution_id=%self.execution_id, attempt_id=%self.attempt_id,
            call_id=%self.call_id, model=%self.model, outcome=%self.outcome,
            latency_ms=self.latency_ms, error_code=%self.error_code,
            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
            "provider_call");
        if let Some(db) = db {
            let result = tokio::time::timeout(std::time::Duration::from_secs(1), async {
                let metadata = serde_json::to_value(self)?;
                db.execute("INSERT INTO infinity_runtime.provider_calls(call_id,execution_id,attempt_id,started_at,metadata) VALUES($1,$2,$3,$4::text::timestamptz,$5) ON CONFLICT(call_id) DO UPDATE SET metadata=$5", &[&self.call_id,&self.execution_id,&self.attempt_id,&self.started_at,&metadata]).await?;
                Ok::<(), Error>(())
            }).await;
            if !matches!(result, Ok(Ok(()))) {
                tracing::warn!(call_id=%self.call_id,"provider metadata persistence unavailable");
            }
        }
    }
    pub fn finish(&mut self, start: Instant, code: &str) {
        self.finished_at = Some(chrono::Utc::now().to_rfc3339());
        self.latency_ms = Some(start.elapsed().as_millis().min(i64::MAX as u128) as i64);
        self.outcome = if code.is_empty() {
            "completed"
        } else {
            "failed"
        }
        .into();
        self.error_code = code.into();
        if let Some(metric) = &self.metric
            && let Ok(mut metric) = metric.lock()
        {
            metric.finish(&self.outcome, code, self.input_tokens, self.output_tokens);
        }
    }
}

pub fn safe_request_id(value: &str) -> Option<String> {
    (!value.is_empty()
        && value.len() <= 200
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-')))
    .then(|| value.into())
}

/// A singleton advisory lock proves no old process can still own these calls.
pub async fn interrupt(db: &Client, attempt: Option<&str>, outcome: &str) -> Result<(), Error> {
    tokio::time::timeout(std::time::Duration::from_secs(1), db.execute("UPDATE infinity_runtime.provider_calls SET metadata=metadata || jsonb_build_object('outcome',$2::text,'error_code','agent_' || $2::text,'finished_at',to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'),'latency_ms',GREATEST(0,(EXTRACT(EPOCH FROM (clock_timestamp()-started_at))*1000)::bigint)) WHERE metadata->>'outcome'='running' AND ($1::text IS NULL OR attempt_id=$1)", &[&attempt,&outcome])).await??;
    Ok(())
}

pub async fn cleanup(db: &Client) -> Result<(), Error> {
    db.execute("DELETE FROM infinity_runtime.provider_calls WHERE started_at < now() - interval '30 days' AND metadata->>'outcome' <> 'running'", &[]).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn request_ids_are_bounded_metadata() {
        assert_eq!(
            safe_request_id("req_abc-123").as_deref(),
            Some("req_abc-123")
        );
        for value in ["", "secret body", "x\nAuthorization", &"a".repeat(201)] {
            assert!(safe_request_id(value).is_none());
        }
    }
    #[test]
    fn unknown_usage_is_null_and_finish_is_categorized() {
        let mut call = Call::new("execution", "attempt", "fake");
        assert!(serde_json::to_value(&call).unwrap()["input_tokens"].is_null());
        call.finish(Instant::now(), "agent_provider_throttled");
        assert_eq!(call.outcome, "failed");
        assert!(call.finished_at.is_some());
        assert!(call.latency_ms.unwrap() >= 0);
    }
    #[test]
    fn reading_metadata_never_creates_a_provider_attempt() {
        let call = Call::new("execution", "attempt", "fake");
        let restored: Call = serde_json::from_value(serde_json::to_value(&call).unwrap()).unwrap();
        assert!(restored.metric.is_none());
    }
}
