//! Stateless OpenAI Responses adapter. Never log provider bodies or credentials.
use crate::store::Error;
use async_trait::async_trait;
use futures_util::stream;
use infinity_provider_protocol::{message::ReasoningContent, *};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use tokio::sync::Mutex;

pub struct Provider {
    pub model: String,
    pub fake: bool,
    pub client: reqwest::Client,
    pub key: String,
    pub max_output: u64,
    pub max_input: usize,
    pub calls: Arc<AtomicUsize>,
    pub retry_after: Arc<AtomicUsize>,
    pub usage: Arc<Mutex<Value>>,
    pub failure: Arc<Mutex<Option<(String, bool, u64)>>>,
    pub db: Option<Arc<tokio_postgres::Client>>,
    pub attempt: String,
}
fn fatal(code: &str) -> CompletionError {
    CompletionError::provider(ErrorClass::Fatal, code)
}

pub fn input_items(request: &CompletionRequest) -> Result<Vec<Value>, CompletionError> {
    let mut input = vec![];
    for message in &request.chat_history {
        match message {
            Message::User { content } => {
                for part in content {
                    match part {
                        UserContent::Text(t) => input.push(json!({"role":"user","content":t.text})),
                        UserContent::ToolResult(t) => {
                            let text = t
                                .content
                                .iter()
                                .filter_map(|c| match c {
                                    ToolResultContent::Text(t) => Some(t.text.as_str()),
                                    _ => None,
                                })
                                .collect::<Vec<_>>()
                                .join("\n");
                            input.push(json!({"type":"function_call_output","call_id":t.call_id.as_ref().unwrap_or(&t.id),"output":text}));
                        }
                        _ => return Err(fatal("agent_unsupported_content")),
                    }
                }
            }
            Message::Assistant { content } => {
                for part in content {
                    match part {
                    AssistantContent::Text(t) => input.push(json!({"role":"assistant","content":t.text})),
                    AssistantContent::ToolCall(t) => input.push(json!({"type":"function_call","id":t.id,"call_id":t.call_id.as_ref().unwrap_or(&t.id),"name":t.function.name,"arguments":t.function.arguments.to_string()})),
                    AssistantContent::Reasoning(r) => {
                        for c in &r.content { if let ReasoningContent::Encrypted(raw) = c { input.push(serde_json::from_str(raw)?); } }
                    }
                }
                }
            }
        }
    }
    Ok(input)
}

#[async_trait]
impl ModelProvider for Provider {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, Error> {
        Ok(vec![ModelEntry {
            model_id: self.model.clone(),
            display_name: self.model.clone(),
            context_window: 128000,
            max_output_tokens: Some(self.max_output),
            supports_image_input: false,
        }])
    }
    async fn invoke_model(
        &self,
        model_id: &str,
        request: CompletionRequest,
    ) -> Result<ModelStream, CompletionError> {
        let result = self.complete(model_id, request).await;
        if let Err(e) = &result {
            let (code, retryable, delay) = match e {
                CompletionError::ProviderError { message, class } => (
                    message.clone(),
                    matches!(class, ErrorClass::Transient | ErrorClass::Throttled),
                    if *class == ErrorClass::Throttled {
                        self.retry_after.load(Ordering::SeqCst) as u64
                    } else {
                        5
                    },
                ),
                _ => ("agent_provider_invalid_output".into(), false, 0),
            };
            // Stop the runtime's internal retries; Django owns bounded retry policy.
            let mut failure = self.failure.lock().await;
            if failure.is_none() {
                *failure = Some((code, retryable, delay));
            }
        }
        result.map_err(|_| fatal("agent_provider_failed"))
    }
}
impl Provider {
    async fn complete(
        &self,
        model_id: &str,
        request: CompletionRequest,
    ) -> Result<ModelStream, CompletionError> {
        if self.fake && model_id != "fake" {
            return Err(fatal("agent_fake_model_name_required"));
        }
        if model_id != self.model {
            return Err(fatal("agent_unknown_model"));
        }
        let n = self.calls.fetch_add(1, Ordering::SeqCst);
        if n >= 4 {
            return Err(fatal("agent_completion_limit"));
        }
        if let Some(db) = &self.db {
            db.execute(
                "UPDATE infinity_runtime.executions SET calls=$2 WHERE attempt=$1",
                &[&self.attempt, &((n + 1) as i32)],
            )
            .await
            .map_err(|_| fatal("agent_store_unavailable"))?;
        }
        let input = input_items(&request)?;
        if serde_json::to_string(&input)?.len() > self.max_input * 8 + 32768 {
            return Err(fatal("agent_input_budget"));
        }
        let has_context = request.chat_history.iter().any(|m| matches!(m, Message::User{content} if content.iter().any(|c| matches!(c, UserContent::ToolResult(_)))));
        if self.fake {
            let delay = std::env::var("INFINITY_FAKE_DELAY_MS")
                .ok()
                .and_then(|v| v.parse::<u64>().ok())
                .unwrap_or(0)
                .min(10000);
            if has_context {
                tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
            }
            let chunk = if !has_context {
                StreamChunk::ToolCall(ToolCall::new(
                    "fake-context",
                    "evaluation_context",
                    json!({}),
                ))
            } else {
                let context = request
                    .chat_history
                    .iter()
                    .find_map(|m| {
                        if let Message::User { content } = m {
                            content.iter().find_map(|c| {
                                if let UserContent::ToolResult(t) = c {
                                    t.content.iter().find_map(|v| {
                                        if let ToolResultContent::Text(t) = v {
                                            serde_json::from_str::<Value>(&t.text).ok()
                                        } else {
                                            None
                                        }
                                    })
                                } else {
                                    None
                                }
                            })
                        } else {
                            None
                        }
                    })
                    .ok_or_else(|| fatal("agent_context_missing"))?;
                let ids: Vec<Value> = context["context"]["facts"]
                    .as_array()
                    .ok_or_else(|| fatal("agent_context_invalid"))?
                    .iter()
                    .map(|f| f["id"].clone())
                    .collect();
                StreamChunk::Text(json!({"ordered_fact_ids":ids,"highlight_fact_ids":ids.iter().take(3).collect::<Vec<_>>(),"unmatched_requirements":[]}).to_string())
            };
            return Ok(Box::pin(stream::iter(vec![
                Ok(StreamChunk::Reasoning(reasoning_item(
                    &json!({"type":"reasoning","id":format!("rs_fake_{n}"),"encrypted_content":"fixture","summary":[]}),
                ))),
                Ok(chunk),
                Ok(StreamChunk::Final(FinalResponse {
                    usage: Some(Usage {
                        input_tokens: 0,
                        output_tokens: 0,
                        total_tokens: 0,
                        cached_input_tokens: 0,
                    }),
                })),
            ])));
        }
        let tools: Vec<Value> = request.tools.iter().map(|t| json!({"type":"function","name":t.name,"description":t.description,"parameters":t.parameters,"strict":true})).collect();
        let schema = json!({"type":"object","additionalProperties":false,"properties":{
            "ordered_fact_ids":{"type":"array","items":{"type":"string"}},
            "highlight_fact_ids":{"type":"array","items":{"type":"string"}},
            "unmatched_requirements":{"type":"array","items":{"type":"string"}}
        },"required":["ordered_fact_ids","highlight_fact_ids","unmatched_requirements"]});
        let body = json!({"model":self.model,"store":false,"include":["reasoning.encrypted_content"],
            "instructions":request.preamble,"input":input,"tools":tools,"parallel_tool_calls":false,
            "tool_choice": if has_context {json!("none")} else {json!({"type":"function","name":"evaluation_context"})},
            "max_output_tokens":self.max_output,
            "text":{"format":{"type":"json_schema","name":"vacancy_evaluation","strict":true,"schema":schema}}});
        let response = self
            .client
            .post("https://api.openai.com/v1/responses")
            .bearer_auth(&self.key)
            .json(&body)
            .send()
            .await
            .map_err(|_| {
                CompletionError::provider(ErrorClass::Transient, "agent_provider_unavailable")
            })?;
        let status = response.status().as_u16();
        if status == 429 {
            let delay = retry_delay(
                response
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("60"),
            )
            .map_err(|_| fatal("agent_retry_requires_review"))?;
            self.retry_after.store(delay as usize, Ordering::SeqCst);
            return Err(CompletionError::provider(
                ErrorClass::Throttled,
                "agent_provider_throttled",
            ));
        }
        if status >= 500 {
            return Err(CompletionError::provider(
                ErrorClass::Transient,
                "agent_provider_unavailable",
            ));
        }
        if status != 200 {
            return Err(fatal("agent_provider_rejected"));
        }
        let mut response = response;
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| {
            CompletionError::provider(ErrorClass::Transient, "agent_provider_unavailable")
        })? {
            if bytes.len() + chunk.len() > 256000 {
                return Err(fatal("agent_provider_response_too_large"));
            }
            bytes.extend_from_slice(&chunk);
        }
        let data: Value = serde_json::from_slice(&bytes)?;
        let usage = &data["usage"];

        let input_tokens = usage["input_tokens"]
            .as_u64()
            .ok_or_else(|| fatal("agent_usage_missing"))?;
        let output_tokens = usage["output_tokens"]
            .as_u64()
            .ok_or_else(|| fatal("agent_usage_missing"))?;
        {
            let mut total = self.usage.lock().await;
            *total = json!({"input_tokens":total["input_tokens"].as_u64().unwrap_or(0)+input_tokens,"output_tokens":total["output_tokens"].as_u64().unwrap_or(0)+output_tokens});
            if let Some(db) = &self.db {
                db.execute(
                    "UPDATE infinity_runtime.executions SET usage=$2 WHERE attempt=$1",
                    &[&self.attempt, &*total],
                )
                .await
                .map_err(|_| fatal("agent_store_unavailable"))?;
            }
        }

        if data["status"] != "completed" {
            return Err(fatal("agent_provider_incomplete"));
        }
        let mut chunks = Vec::new();
        for item in data["output"]
            .as_array()
            .ok_or_else(|| fatal("agent_provider_invalid_output"))?
        {
            match item["type"].as_str() {
                Some("message") => {
                    for part in item["content"]
                        .as_array()
                        .ok_or_else(|| fatal("agent_provider_invalid_output"))?
                    {
                        if part["type"] == "output_text" {
                            chunks.push(Ok(StreamChunk::Text(
                                part["text"]
                                    .as_str()
                                    .ok_or_else(|| fatal("agent_provider_invalid_output"))?
                                    .into(),
                            )));
                        }
                    }
                }
                Some("function_call") => {
                    if item["name"] != "evaluation_context" || has_context {
                        return Err(fatal("agent_forbidden_tool"));
                    }
                    let mut call = ToolCall::new(
                        item["id"]
                            .as_str()
                            .ok_or_else(|| fatal("agent_invalid_tool"))?,
                        "evaluation_context",
                        serde_json::from_str(
                            item["arguments"]
                                .as_str()
                                .ok_or_else(|| fatal("agent_invalid_tool"))?,
                        )?,
                    );
                    call.call_id = Some(
                        item["call_id"]
                            .as_str()
                            .ok_or_else(|| fatal("agent_invalid_tool"))?
                            .into(),
                    );
                    chunks.push(Ok(StreamChunk::ToolCall(call)));
                }
                Some("reasoning") => chunks.push(Ok(StreamChunk::Reasoning(reasoning_item(item)))),
                _ => return Err(fatal("agent_provider_invalid_output")),
            }
        }
        if !has_context
            && chunks
                .iter()
                .filter(|chunk| matches!(chunk, Ok(StreamChunk::ToolCall(_))))
                .count()
                != 1
        {
            return Err(fatal("agent_context_required"));
        }
        chunks.push(Ok(StreamChunk::Final(FinalResponse {
            usage: Some(Usage {
                input_tokens: usage["input_tokens"].as_u64().unwrap_or(0),
                output_tokens: usage["output_tokens"].as_u64().unwrap_or(0),
                total_tokens: usage["total_tokens"].as_u64().unwrap_or(0),
                cached_input_tokens: 0,
            }),
        })));
        Ok(Box::pin(stream::iter(chunks)))
    }
}

/// Infinity 0.2.0 drops reasoning without a signature marker. Preserve the opaque
/// Responses item, including its encrypted content, across tool turns and restarts.
/// The marker is an adapter completeness flag, not a cryptographic signature.
pub fn reasoning_item(item: &Value) -> Reasoning {
    Reasoning {
        id: item["id"].as_str().map(str::to_owned),
        content: vec![
            ReasoningContent::Text {
                text: String::new(),
                signature: Some("responses-complete-item".into()),
            },
            ReasoningContent::Encrypted(item.to_string()),
        ],
    }
}

pub fn retry_delay(value: &str) -> Result<u64, ()> {
    let delay = if let Ok(seconds) = value.parse::<u64>() {
        seconds
    } else if let Ok(at) = httpdate::parse_http_date(value) {
        at.duration_since(std::time::SystemTime::now())
            .unwrap_or_default()
            .as_secs()
    } else {
        60
    };
    if delay > 3600 {
        Err(())
    } else {
        Ok(delay.max(1))
    }
}
