//! Bounded JSON metadata logs to stdout and optional rotating files; no provider text.
use serde_json::{Map, Value, json};
use std::{
    io::Write,
    path::PathBuf,
    sync::mpsc::{SyncSender, sync_channel},
};
use tracing_subscriber::fmt::MakeWriter;
#[derive(Clone)]
pub struct WriterFactory {
    sender: Option<SyncSender<Vec<u8>>>,
}
impl WriterFactory {
    pub fn new() -> Self {
        let sender = std::env::var("JOBSTREAM_LOG_DIR").ok().map(|path| {
            let (tx, rx) = sync_channel::<Vec<u8>>(512);
            std::thread::spawn(move || {
                let directory = PathBuf::from(path);
                if std::fs::create_dir_all(&directory).is_err() {
                    return;
                }
                let file = directory.join(format!("events-{}.jsonl", std::process::id()));
                prune(&directory);
                let mut size = std::fs::metadata(&file).map(|m| m.len()).unwrap_or(0);
                let mut count = 0;
                while let Ok(line) = rx.recv() {
                    count += 1;
                    if count % 100 == 0 {
                        prune(&directory);
                    }
                    if size + line.len() as u64 > 2 * 1024 * 1024 {
                        let _ = std::fs::rename(
                            file.with_extension("jsonl.1"),
                            file.with_extension("jsonl.2"),
                        );
                        let _ = std::fs::rename(&file, file.with_extension("jsonl.1"));
                        size = 0;
                    }
                    match std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(&file)
                        .and_then(|mut f| f.write_all(&line))
                    {
                        Ok(()) => size += line.len() as u64,
                        Err(_) => {
                            crate::metrics::metrics().log_drops.inc();
                        }
                    }
                }
            });
            tx
        });
        Self { sender }
    }
}
pub struct Writer {
    bytes: Vec<u8>,
    sender: Option<SyncSender<Vec<u8>>>,
    overflow: bool,
}
impl<'a> MakeWriter<'a> for WriterFactory {
    type Writer = Writer;
    fn make_writer(&'a self) -> Writer {
        Writer {
            bytes: Vec::new(),
            sender: self.sender.clone(),
            overflow: false,
        }
    }
}
impl Write for Writer {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        if !self.overflow && self.bytes.len() + buf.len() <= 16384 {
            self.bytes.extend_from_slice(buf);
        } else {
            self.overflow = true;
        }
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn safe(value: Value) -> Value {
    let mut data = Map::new();
    data.insert("schema_version".into(), json!(1));
    data.insert("timestamp".into(), json!(chrono::Utc::now().to_rfc3339()));
    data.insert("service".into(), json!("infinity"));
    data.insert("environment".into(), json!("development"));
    let severity = value["level"].as_str().unwrap_or("INFO");
    data.insert(
        "severity".into(),
        json!(if matches!(severity, "TRACE" | "DEBUG") {
            "DEBUG"
        } else if severity == "WARN" {
            "WARNING"
        } else {
            severity
        }),
    );
    let event = value["event"]
        .as_str()
        .or_else(|| value["message"].as_str())
        .unwrap_or("runtime_event");
    let event = match event {
        "evaluation started" => "evaluation_started",
        "evaluation finished" => "evaluation_finished",
        "evaluation accepted" => "evaluation_accepted",
        "runtime heartbeat" => "runtime_heartbeat",
        "runtime starting" => "runtime_starting",
        "runtime ready; waiting for evaluations" => "runtime_ready",
        "runtime database disconnected" => "runtime_database_disconnected",
        "metrics_listener_failed" => "metrics_listener_failed",
        "runtime_startup_failed" => "runtime_startup_failed",
        "runtime_panic" => "runtime_panic",
        "provider metadata persistence unavailable" => "provider_metadata_unavailable",
        "provider interruption metadata unavailable" => "provider_metadata_unavailable",
        "provider metadata retention cleanup unavailable" => "provider_retention_unavailable",
        "runtime persistence failed; refusing result" => "runtime_persistence_failed",
        "context tool started" => "context_tool_started",
        "context tool finished" => "context_tool_finished",
        "provider_call" => "provider_call",
        "evaluation_finished" => "evaluation_finished",
        _ => "runtime_event",
    };
    data.insert("event".into(), json!(event));
    let mut fields = value.as_object().cloned().unwrap_or_default();
    if let Some(spans) = value["spans"].as_array() {
        for span in spans {
            if let Some(map) = span.as_object() {
                fields.extend(map.clone());
            }
        }
    }
    for key in [
        "execution_id",
        "attempt_id",
        "call_id",
        "workflow_id",
        "request_id",
    ] {
        if let Some(s) = fields.get(key).and_then(Value::as_str)
            && s.len() == 36
            && uuid::Uuid::parse_str(s).is_ok()
        {
            data.insert(key.into(), json!(s));
        }
    }
    for key in [
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "active_evaluations",
    ] {
        if let Some(v) = fields.get(key).filter(|v| v.is_u64() || v.is_null()) {
            data.insert(key.into(), v.clone());
        }
    }
    if let Some(outcome) = fields.get("outcome").and_then(Value::as_str)
        && matches!(
            outcome,
            "running" | "completed" | "failed" | "cancelled" | "timeout" | "interrupted"
        )
    {
        data.insert("outcome".into(), json!(outcome));
    }
    if let Some(model) = fields.get("model").and_then(Value::as_str) {
        data.insert(
            "model".into(),
            json!(if model == "fake" {
                "fake"
            } else {
                "configured"
            }),
        );
    }
    if let Some(code) = fields.get("error_code").and_then(Value::as_str) {
        let category = match code {
            "" => "none",
            "agent_provider_throttled" => "throttled",
            "agent_provider_timeout" => "timeout",
            _ => "failure",
        };
        data.insert("error_category".into(), json!(category));
    }
    json!(data)
}

fn prune(directory: &std::path::Path) {
    let Ok(files) = std::fs::read_dir(directory) else {
        return;
    };
    let mut history = Vec::new();
    for file in files.flatten() {
        let name = file.file_name().to_string_lossy().into_owned();
        let Some(pid) = name
            .strip_prefix("events-")
            .and_then(|v| v.split('.').next())
            .and_then(|v| v.parse::<u32>().ok())
        else {
            continue;
        };
        if !name.contains(".jsonl")
            || pid == std::process::id()
            || std::path::Path::new(&format!("/proc/{pid}")).exists()
        {
            continue;
        }
        if let Ok(metadata) = file.metadata()
            && let Ok(modified) = metadata.modified()
        {
            history.push((modified, metadata.len(), file.path()));
        }
    }
    history.sort();
    let mut total: u64 = history.iter().map(|(_, size, _)| size).sum();
    for (modified, size, path) in history {
        if total <= 64 * 1024 * 1024 && modified.elapsed().unwrap_or_default().as_secs() < 86400 {
            break;
        }
        if std::fs::remove_file(path).is_ok() {
            total = total.saturating_sub(size);
        }
    }
}

impl Drop for Writer {
    fn drop(&mut self) {
        if self.overflow {
            crate::metrics::metrics().log_drops.inc();
            return;
        }
        if let Ok(value) = serde_json::from_slice::<Value>(&self.bytes)
            && let Ok(mut line) = serde_json::to_vec(&safe(value))
        {
            line.push(b'\n');
            let _ = std::io::stdout().write_all(&line);
            if let Some(sender) = &self.sender
                && sender.try_send(line).is_err()
            {
                crate::metrics::metrics().log_drops.inc();
            }
        } else {
            crate::metrics::metrics().log_drops.inc();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn oversized_events_are_bounded_and_counted_as_loss() {
        let before = crate::metrics::metrics().log_drops.get();
        let mut writer = Writer {
            bytes: Vec::new(),
            sender: None,
            overflow: false,
        };
        writer.write_all(&vec![b'x'; 20000]).unwrap();
        assert!(writer.bytes.len() <= 16384);
        drop(writer);
        assert!(crate::metrics::metrics().log_drops.get() > before);
    }
    #[test]
    fn untrusted_fields_and_messages_never_reach_json_logs() {
        let secret = "private_token_and_cv_contents";
        let output = safe(
            json!({"message":secret,"model":secret,"error_code":secret,"prompt":secret,"input_tokens":5}),
        );
        assert!(!output.to_string().contains(secret));
        assert_eq!(output["event"], "runtime_event");
        assert_eq!(output["model"], "configured");
        assert_eq!(output["error_category"], "failure");
        assert_eq!(output["input_tokens"], 5);
    }
}
