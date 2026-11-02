//! Singleton runtime persistence. Every mutating trait call awaits a PostgreSQL write.
use async_trait::async_trait;
use infinity_agent_core::{
    message::InfinityMessage,
    stores::{InMemoryConversationStore, InMemoryStateStore},
    system::UserChoice,
    traits::{ConversationStore, SpawnContext, StateStore},
};
use rap_protocol::{ChoiceId, ThreadId, ToolCallId};
use serde_json::{Value, json};
use std::sync::Arc;
use tokio_postgres::Client;

pub type Error = Box<dyn std::error::Error + Send + Sync>;
#[derive(Clone)]
pub struct Stores {
    pub conversation: InMemoryConversationStore,
    pub state: InMemoryStateStore,
    pub db: Option<Arc<Client>>,
}
impl Stores {
    pub fn new(db: Option<Arc<Client>>) -> Self {
        Self {
            conversation: InMemoryConversationStore::new(),
            state: InMemoryStateStore::new(),
            db,
        }
    }
    pub async fn restore(&self, id: &ThreadId<str>) -> Result<(), Error> {
        if let Some(db) = &self.db
            && let Some(row) = db
                .query_opt(
                    "SELECT conversation, state FROM infinity_runtime.threads WHERE id=$1",
                    &[&id.as_str()],
                )
                .await?
        {
            let c: Value = row.get(0);
            let s: Value = row.get(1);
            if !c.is_null() {
                if !c["info"].is_null() {
                    self.conversation
                        .set_thread_info(id, serde_json::from_value(c["info"].clone())?);
                }
                self.conversation
                    .set_thread_messages(id, serde_json::from_value(c["messages"].clone())?);
                self.conversation.set_thread_compaction_summaries(
                    id,
                    serde_json::from_value(c["summaries"].clone())?,
                );
            }
            if !s.is_null() {
                self.state.set_thread_state(id, serde_json::from_value(s)?);
            }
        }
        Ok(())
    }
    async fn persist_conversation(&self, id: &ThreadId<str>) -> Result<(), Error> {
        if let Some(db) = &self.db {
            let snapshot = json!({"info": self.conversation.thread_info(id), "messages": self.conversation.thread_messages(id).unwrap_or_default(), "summaries": self.conversation.thread_compaction_summaries(id)});
            db.execute("INSERT INTO infinity_runtime.threads(id,conversation) VALUES($1,$2) ON CONFLICT(id) DO UPDATE SET conversation=$2", &[&id.as_str(), &snapshot]).await?;
        }
        Ok(())
    }
    async fn persist_state(&self, id: &ThreadId<str>) -> Result<(), Error> {
        if let Some(db) = &self.db {
            let snapshot = serde_json::to_value(self.state.thread_state(id))?;
            db.execute("INSERT INTO infinity_runtime.threads(id,state) VALUES($1,$2) ON CONFLICT(id) DO UPDATE SET state=$2", &[&id.as_str(), &snapshot]).await?;
        }
        Ok(())
    }
}

#[derive(Debug)]
pub struct StoreError(String);
impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for StoreError {}

#[async_trait]
impl ConversationStore for Stores {
    type Error = StoreError;
    async fn ensure_root_thread(&self, thread_id: &ThreadId<str>) -> Result<(), Self::Error> {
        let result = self
            .conversation
            .ensure_root_thread(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_conversation(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn thread_exists(&self, thread_id: &ThreadId<str>) -> Result<bool, Self::Error> {
        let result = self
            .conversation
            .thread_exists(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn load_history_up_to(
        &self,
        session_id: &ThreadId<str>,
        start_from: Option<i64>,
        up_to: Option<i64>,
    ) -> Result<Vec<InfinityMessage>, Self::Error> {
        let result = self
            .conversation
            .load_history_up_to(session_id, start_from, up_to)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn append_messages(
        &self,
        session_id: &ThreadId<str>,
        messages: Vec<(InfinityMessage, String)>,
    ) -> Result<(), Self::Error> {
        let result = self
            .conversation
            .append_messages(session_id, messages)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_conversation(session_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn spawn_thread(
        &self,
        parent_thread_id: &ThreadId<str>,
        spawn_tool_call_id: &ToolCallId<str>,
        is_for_subscription_event: bool,
        context: SpawnContext,
    ) -> Result<ThreadId, Self::Error> {
        let _ = (
            parent_thread_id,
            spawn_tool_call_id,
            is_for_subscription_event,
            context,
        );
        Err(StoreError("child agents disabled".into()))
    }
    async fn is_thread_closed(&self, thread_id: &ThreadId<str>) -> Result<bool, Self::Error> {
        let result = self
            .conversation
            .is_thread_closed(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn close_thread(&self, thread_id: &ThreadId<str>) -> Result<(), Self::Error> {
        let result = self
            .conversation
            .close_thread(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_conversation(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn is_subscription_event_thread(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<bool, Self::Error> {
        let result = self
            .conversation
            .is_subscription_event_thread(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn is_fresh_context_thread(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<bool, Self::Error> {
        let result = self
            .conversation
            .is_fresh_context_thread(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn get_thread_parent_info(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<Option<(ThreadId, ToolCallId)>, Self::Error> {
        let result = self
            .conversation
            .get_thread_parent_info(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn get_ancestor_chain(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<Vec<(ThreadId, i64)>, Self::Error> {
        let result = self
            .conversation
            .get_ancestor_chain(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn mark_thread_as_compaction(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<(), Self::Error> {
        let result = self
            .conversation
            .mark_thread_as_compaction(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_conversation(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn is_compaction_thread(&self, thread_id: &ThreadId<str>) -> Result<bool, Self::Error> {
        let result = self
            .conversation
            .is_compaction_thread(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn get_thread_spawn_order(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<Option<i64>, Self::Error> {
        let result = self
            .conversation
            .get_thread_spawn_order(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn save_compaction_summary(
        &self,
        thread_id: &ThreadId<str>,
        summary: &str,
        up_to_order: i64,
    ) -> Result<(), Self::Error> {
        let result = self
            .conversation
            .save_compaction_summary(thread_id, summary, up_to_order)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_conversation(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn load_latest_compaction_summary_up_to(
        &self,
        thread_id: &ThreadId<str>,
        up_to_order: Option<i64>,
    ) -> Result<Option<(String, i64)>, Self::Error> {
        let result = self
            .conversation
            .load_latest_compaction_summary_up_to(thread_id, up_to_order)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
}

#[async_trait]
impl StateStore for Stores {
    type Error = StoreError;
    async fn get_processed_ids(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<std::collections::HashSet<String>, Self::Error> {
        let result = self
            .state
            .get_processed_ids(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn add_processed_message_ids(
        &self,
        thread_id: &ThreadId<str>,
        message_ids: Vec<String>,
    ) -> Result<(), Self::Error> {
        let result = self
            .state
            .add_processed_message_ids(thread_id, message_ids)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_state(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn get_metadata(
        &self,
        root_thread_id: &ThreadId<str>,
    ) -> Result<Option<serde_json::Value>, Self::Error> {
        let result = self
            .state
            .get_metadata(root_thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn set_metadata(
        &self,
        root_thread_id: &ThreadId<str>,
        metadata: serde_json::Value,
    ) -> Result<(), Self::Error> {
        let result = self
            .state
            .set_metadata(root_thread_id, metadata)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_state(root_thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn get_active_subscriptions(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<Vec<ToolCallId>, Self::Error> {
        let result = self
            .state
            .get_active_subscriptions(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn add_active_subscription(
        &self,
        thread_id: &ThreadId<str>,
        tool_call_id: &ToolCallId<str>,
    ) -> Result<(), Self::Error> {
        let result = self
            .state
            .add_active_subscription(thread_id, tool_call_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_state(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn remove_active_subscription(
        &self,
        thread_id: &ThreadId<str>,
        tool_call_id: &ToolCallId<str>,
    ) -> Result<(), Self::Error> {
        let result = self
            .state
            .remove_active_subscription(thread_id, tool_call_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_state(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn add_pending_user_choice(
        &self,
        thread_id: &ThreadId<str>,
        choice: UserChoice,
    ) -> Result<(), Self::Error> {
        let result = self
            .state
            .add_pending_user_choice(thread_id, choice)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_state(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn remove_pending_user_choice(
        &self,
        thread_id: &ThreadId<str>,
        choice_id: &ChoiceId<str>,
    ) -> Result<(), Self::Error> {
        let result = self
            .state
            .remove_pending_user_choice(thread_id, choice_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        self.persist_state(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
    async fn get_pending_user_choices(
        &self,
        thread_id: &ThreadId<str>,
    ) -> Result<Vec<UserChoice>, Self::Error> {
        let result = self
            .state
            .get_pending_user_choices(thread_id)
            .await
            .map_err(|e| StoreError(e.to_string()))?;
        Ok(result)
    }
}
