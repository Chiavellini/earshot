//! Request signing (VERIFICATIONS.md Q3, all facts empirically confirmed):
//! RSA-PSS/SHA-256 (salt = digest len) over `{unix_ms}{METHOD}{path}` where
//! path includes `/trade-api/v2` and excludes the query string. Headers:
//! KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP. Timestamp window: ±30 s
//! (measured), so pre-signed headers are refreshed every 10 s and rejected
//! at 20 s age — 3× and 1.5× margin respectively.
//!
//! The signature does NOT cover the body: order bodies are pre-serialized at
//! arm time and only these three headers are computed near fire time.

use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use arc_swap::ArcSwap;
use base64::Engine;
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::SigningKey;
use rsa::sha2::Sha256;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use rsa::RsaPrivateKey;

use crate::clock::MonotonicNs;
use crate::kalshi::KalshiError;

pub const HDR_KEY: &str = "KALSHI-ACCESS-KEY";
pub const HDR_SIG: &str = "KALSHI-ACCESS-SIGNATURE";
pub const HDR_TS: &str = "KALSHI-ACCESS-TIMESTAMP";

/// Presigned headers older than this are considered stale and re-signed
/// inline (measured server window is ±30 s).
pub const PRESIGN_MAX_AGE: Duration = Duration::from_secs(20);
/// Refresher cadence.
pub const PRESIGN_REFRESH: Duration = Duration::from_secs(10);

pub fn now_ms() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis()
}

pub struct Signer {
    key_id: String,
    signing_key: SigningKey<Sha256>,
}

pub type Headers3 = [(&'static str, String); 3];

impl Signer {
    pub fn from_env() -> Result<Self, KalshiError> {
        let key_id = std::env::var("KALSHI_API_KEY_ID")
            .map_err(|_| KalshiError::Credentials("KALSHI_API_KEY_ID not set".into()))?;
        let pem_path = std::env::var("KALSHI_PRIVATE_KEY_PATH")
            .map_err(|_| KalshiError::Credentials("KALSHI_PRIVATE_KEY_PATH not set".into()))?;
        let pem = std::fs::read_to_string(&pem_path)
            .map_err(|e| KalshiError::Credentials(format!("read {pem_path}: {e}")))?;
        Self::from_pem(key_id, &pem)
    }

    pub fn from_pem(key_id: String, pem: &str) -> Result<Self, KalshiError> {
        let key = RsaPrivateKey::from_pkcs1_pem(pem)
            .or_else(|_| RsaPrivateKey::from_pkcs8_pem(pem))
            .map_err(|e| KalshiError::Credentials(format!("parse private key: {e}")))?;
        Ok(Signer { key_id, signing_key: SigningKey::new(key) })
    }

    pub fn key_id(&self) -> &str {
        &self.key_id
    }

    /// `path` must include `/trade-api/v2` and exclude any query string.
    pub fn headers(&self, method: &str, path: &str, ts_ms: u128) -> Headers3 {
        debug_assert!(path.starts_with("/trade-api/"), "sign full path from API root");
        debug_assert!(!path.contains('?'), "never sign the query string");
        let msg = format!("{ts_ms}{method}{path}");
        let sig = self
            .signing_key
            .sign_with_rng(&mut rsa::rand_core::OsRng, msg.as_bytes());
        [
            (HDR_KEY, self.key_id.clone()),
            (HDR_SIG, base64::engine::general_purpose::STANDARD.encode(sig.to_bytes())),
            (HDR_TS, ts_ms.to_string()),
        ]
    }

    pub fn headers_now(&self, method: &str, path: &str) -> Headers3 {
        self.headers(method, path, now_ms())
    }
}

pub struct PresignedHeaders {
    pub headers: Headers3,
    pub signed_at: MonotonicNs,
}

/// Keeps fresh presigned headers for ONE (method, path) pair — in practice
/// `POST /trade-api/v2/portfolio/events/orders`, which every order create
/// shares. Hot path: `load()` is a wait-free ArcSwap read; the RSA cost
/// (~100-500 µs) lives on the refresher task.
pub struct Presigner {
    signer: Arc<Signer>,
    method: &'static str,
    path: String,
    current: ArcSwap<PresignedHeaders>,
}

impl Presigner {
    pub fn new(signer: Arc<Signer>, method: &'static str, path: String) -> Arc<Self> {
        let initial = PresignedHeaders {
            headers: signer.headers_now(method, &path),
            signed_at: MonotonicNs::now(),
        };
        Arc::new(Presigner {
            signer,
            method,
            path,
            current: ArcSwap::from_pointee(initial),
        })
    }

    /// Spawn the 10 s refresh loop on the given runtime handle.
    pub fn spawn_refresher(self: &Arc<Self>, handle: &tokio::runtime::Handle) {
        let this = self.clone();
        handle.spawn(async move {
            let mut tick = tokio::time::interval(PRESIGN_REFRESH);
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                tick.tick().await;
                this.refresh();
            }
        });
    }

    pub fn refresh(&self) {
        self.current.store(Arc::new(PresignedHeaders {
            headers: self.signer.headers_now(self.method, &self.path),
            signed_at: MonotonicNs::now(),
        }));
    }

    /// Fresh headers, with the age of the presigned entry in ns (for the
    /// `presign_age` telemetry edge). Falls back to an inline sign if the
    /// refresher has stalled past PRESIGN_MAX_AGE — correctness over speed.
    pub fn get(&self) -> (Headers3, u64) {
        let cur = self.current.load();
        let age = MonotonicNs::now().saturating_since(cur.signed_at);
        if age <= PRESIGN_MAX_AGE.as_nanos() as u64 {
            (cur.headers.clone(), age)
        } else {
            tracing::warn!(age_ms = age / 1_000_000, "presigned headers stale; inline signing");
            (self.signer.headers_now(self.method, &self.path), age)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rsa::pkcs1::EncodeRsaPrivateKey;
    use rsa::pss::VerifyingKey;
    use rsa::signature::Verifier;

    fn test_signer() -> (Signer, RsaPrivateKey) {
        let key = RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
        let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
        (Signer::from_pem("test-key-id".into(), &pem).unwrap(), key)
    }

    #[test]
    fn signature_verifies_against_exact_message_format() {
        let (signer, key) = test_signer();
        let ts: u128 = 1703123456789;
        let path = "/trade-api/v2/portfolio/balance";
        let headers = signer.headers("GET", path, ts);

        assert_eq!(headers[0], (HDR_KEY, "test-key-id".to_string()));
        assert_eq!(headers[2], (HDR_TS, "1703123456789".to_string()));

        let sig_bytes = base64::engine::general_purpose::STANDARD
            .decode(&headers[1].1)
            .unwrap();
        let verifying: VerifyingKey<Sha256> = VerifyingKey::new(key.to_public_key());
        let sig = rsa::pss::Signature::try_from(sig_bytes.as_slice()).unwrap();
        // The exact documented message: {ts}{METHOD}{path-with-prefix}.
        verifying
            .verify(format!("{ts}GET{path}").as_bytes(), &sig)
            .expect("PSS signature must verify against the documented message");
        // And NOT against a near-miss (path without prefix).
        assert!(verifying
            .verify(format!("{ts}GET/portfolio/balance").as_bytes(), &sig)
            .is_err());
    }

    #[test]
    fn presigner_serves_fresh_and_falls_back_when_stale() {
        let (signer, _) = test_signer();
        let presigner = Presigner::new(
            Arc::new(signer),
            "POST",
            "/trade-api/v2/portfolio/events/orders".into(),
        );
        let (h1, age1) = presigner.get();
        assert!(age1 < PRESIGN_MAX_AGE.as_nanos() as u64);
        assert_eq!(h1[0].0, HDR_KEY);

        // Force staleness by backdating the stored entry.
        let stale = PresignedHeaders {
            headers: h1.clone(),
            signed_at: MonotonicNs(0),
        };
        presigner.current.store(Arc::new(stale));
        let (h2, age2) = presigner.get();
        assert!(age2 > PRESIGN_MAX_AGE.as_nanos() as u64);
        // Inline fallback produced a fresh timestamp, not the stale one.
        assert_ne!(h1[2].1, h2[2].1);
    }
}
