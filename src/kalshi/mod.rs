//! Kalshi execution layer (spec §5.7).
//!
//! Layer doors (see docs/layers/): the executor's entry door is the bounded
//! `tokio::sync::mpsc::Receiver<DecisionIntent>` fed by the decision stage;
//! its exit door is `OrderRecord`s into the report + orders on the wire.
//! All API facts cited from VERIFICATIONS.md, not memory.

pub mod auth;
pub mod book;
pub mod executor;
pub mod rest;
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

/// Order ids created BY THIS PROCESS. The kill switch cancels exactly these
/// and nothing else — the account may carry unrelated resting orders from
/// other systems (observed live: 53 of them), and an account-wide cancel-all
/// would destroy them. Never widen this scope.
#[derive(Clone, Default)]
pub struct OrderRegistry(std::sync::Arc<std::sync::Mutex<Vec<String>>>);

impl OrderRegistry {
    pub fn register(&self, order_id: String) {
        self.0.lock().unwrap().push(order_id);
    }
    pub fn ids(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }
    pub fn len(&self) -> usize {
        self.0.lock().unwrap().len()
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
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
