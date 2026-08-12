//! Kalshi execution layer (spec §5.7).
//!
//! Layer doors (see docs/layers/): the executor's entry door is the bounded
//! `tokio::sync::mpsc::Receiver<DecisionIntent>` fed by the decision stage;
//! its exit door is `OrderRecord`s into the report + orders on the wire.
//! All API facts cited from VERIFICATIONS.md, not memory.

pub mod auth;
pub mod book;
pub mod executor;
pub mod fix;
pub mod order_evidence;
pub mod rest;
pub mod transport;
pub mod validator;

use thiserror::Error;

#[derive(Debug, Clone)]
pub struct KalshiEnv {
    pub rest_base: &'static str, // includes /trade-api/v2
    pub ws_url: &'static str,
    pub name: &'static str,
}

impl KalshiEnv {
    pub const fn prod() -> Self {
        KalshiEnv {
            rest_base: "https://external-api.kalshi.com/trade-api/v2",
            ws_url: "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
            name: "prod",
        }
    }
    pub const fn demo() -> Self {
        KalshiEnv {
            rest_base: "https://external-api.demo.kalshi.co/trade-api/v2",
            ws_url: "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
            name: "demo",
        }
    }
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "prod" => Some(Self::prod()),
            "demo" => Some(Self::demo()),
            _ => None,
        }
    }
    /// Path prefix that must be part of every signed message (VERIFICATIONS Q3).
    pub fn signed_path(&self, endpoint: &str) -> String {
        debug_assert!(endpoint.starts_with('/'));
        format!("/trade-api/v2{endpoint}")
    }
}

/// Session-owned order identities and the linearization boundary between a
/// new send attempt and kill. A client id is registered *before* transport
/// invocation; the exchange order id is attached later when observed or
/// exact-id reconciliation finds it. This keeps ambiguous attempts inside the
/// kill scope without ever widening cancellation to account-wide orders.
#[derive(Default)]
struct OrderRegistryState {
    order_ids: std::collections::HashSet<String>,
    pending_client_ids: std::collections::HashSet<String>,
    seen_client_ids: std::collections::HashSet<String>,
    active_attempts: usize,
    killed: bool,
}

#[derive(Default)]
struct OrderRegistryInner {
    state: std::sync::Mutex<OrderRegistryState>,
    changed: tokio::sync::Notify,
}

#[derive(Clone, Default)]
pub struct OrderRegistry(std::sync::Arc<OrderRegistryInner>);

/// Proof that an attempt linearized before the session kill boundary. The
/// permit is held through the transport outcome; dropping it lets kill-time
/// reconciliation know no earlier-authorized send can still appear later.
pub struct OrderAttemptPermit {
    registry: OrderRegistry,
    active: bool,
}

impl Drop for OrderAttemptPermit {
    fn drop(&mut self) {
        if !self.active {
            return;
        }
        let mut state = self.registry.0.state.lock().unwrap();
        state.active_attempts = state.active_attempts.saturating_sub(1);
        self.active = false;
        drop(state);
        self.registry.0.changed.notify_waiters();
    }
}

impl OrderRegistry {
    /// Atomically authorize one unique client id unless kill already won the
    /// boundary. If this call wins first, kill sees both an active attempt and
    /// its exact reconciliation id before it can take a cancellation snapshot.
    pub fn begin_attempt(
        &self,
        client_order_id: String,
    ) -> Result<OrderAttemptPermit, &'static str> {
        let mut state = self.0.state.lock().unwrap();
        if state.killed {
            return Err("session kill boundary is closed");
        }
        if !state.seen_client_ids.insert(client_order_id.clone()) {
            return Err("client_order_id already attempted in this session");
        }
        state.pending_client_ids.insert(client_order_id);
        state.active_attempts += 1;
        Ok(OrderAttemptPermit {
            registry: self.clone(),
            active: true,
        })
    }

    /// Close the boundary to all future attempts. Existing permits remain
    /// visible through `active_attempts()` until their transport calls finish.
    pub fn kill(&self) {
        self.0.state.lock().unwrap().killed = true;
        self.0.changed.notify_waiters();
    }

    pub fn is_killed(&self) -> bool {
        self.0.state.lock().unwrap().killed
    }

    pub fn register(&self, order_id: String) {
        self.0.state.lock().unwrap().order_ids.insert(order_id);
        self.0.changed.notify_waiters();
    }

    /// Attach an exchange id proved to belong to this exact client id and
    /// remove the attempt from the unresolved set.
    pub fn register_for_client(&self, client_order_id: &str, order_id: String) {
        let mut state = self.0.state.lock().unwrap();
        state.pending_client_ids.remove(client_order_id);
        state.order_ids.insert(order_id);
        drop(state);
        self.0.changed.notify_waiters();
    }

    /// Mark a conclusively non-live attempt (pre-write failure or terminal
    /// rejection) resolved without adding anything to the cancel set.
    pub fn resolve_without_order(&self, client_order_id: &str) {
        self.0
            .state
            .lock()
            .unwrap()
            .pending_client_ids
            .remove(client_order_id);
        self.0.changed.notify_waiters();
    }

    pub fn ids(&self) -> Vec<String> {
        let mut ids: Vec<_> = self
            .0
            .state
            .lock()
            .unwrap()
            .order_ids
            .iter()
            .cloned()
            .collect();
        ids.sort();
        ids
    }

    pub fn pending_client_ids(&self) -> Vec<String> {
        let mut ids: Vec<_> = self
            .0
            .state
            .lock()
            .unwrap()
            .pending_client_ids
            .iter()
            .cloned()
            .collect();
        ids.sort();
        ids
    }

    pub fn active_attempts(&self) -> usize {
        self.0.state.lock().unwrap().active_attempts
    }

    pub async fn changed(&self) {
        self.0.changed.notified().await;
    }

    pub fn len(&self) -> usize {
        self.0.state.lock().unwrap().order_ids.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod order_registry_tests {
    use super::*;

    #[test]
    fn kill_linearizes_against_attempt_and_retains_pending_client_id() {
        let registry = OrderRegistry::default();
        let permit = registry.begin_attempt("client-a".into()).unwrap();
        registry.kill();

        assert!(registry.begin_attempt("client-b".into()).is_err());
        assert_eq!(registry.pending_client_ids(), vec!["client-a"]);
        assert_eq!(registry.active_attempts(), 1);
        drop(permit);
        assert_eq!(registry.active_attempts(), 0);
        assert_eq!(registry.pending_client_ids(), vec!["client-a"]);
    }

    #[test]
    fn exact_client_resolution_deduplicates_cancel_ids() {
        let registry = OrderRegistry::default();
        let permit = registry.begin_attempt("client-a".into()).unwrap();
        registry.register_for_client("client-a", "order-a".into());
        registry.register("order-a".into());
        drop(permit);

        assert!(registry.pending_client_ids().is_empty());
        assert_eq!(registry.ids(), vec!["order-a"]);
        assert!(registry.begin_attempt("client-a".into()).is_err());
    }
}

#[derive(Debug, Error)]
pub enum KalshiError {
    #[error("credentials: {0}")]
    Credentials(String),
    #[error("http: {0}")]
    Http(#[from] reqwest::Error),
    #[error("api {status}: {body}")]
    Api { status: u16, body: String },
    #[error("websocket: {0}")]
    Ws(String),
    #[error("risk refusal: {0}")]
    Risk(String),
    #[error("validation: {0}")]
    Validation(String),
}
