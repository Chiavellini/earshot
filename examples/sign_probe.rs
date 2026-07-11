//! Kalshi signing / demo-parity probe (VERIFICATIONS.md Q3 + Q6).
//!
//! Read-only by construction: the only endpoints touched are
//! GET /portfolio/balance, GET /markets, and a WS orderbook subscribe.
//! No order endpoints exist in this file.
//!
//! Usage:
//!   source ~/.kalshi/env
//!   cargo run --example sign_probe --features probe [-- demo|live|both]
//!
//! Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH (PKCS#1 or PKCS#8 RSA PEM).

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::Engine;
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::SigningKey;
use rsa::sha2::Sha256;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use rsa::RsaPrivateKey;

const DEMO_REST: &str = "https://external-api.demo.kalshi.co/trade-api/v2";
const LIVE_REST: &str = "https://external-api.kalshi.com/trade-api/v2";
const DEMO_WS: &str = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2";
const LIVE_WS: &str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2";

struct Signer {
    key_id: String,
    signing_key: SigningKey<Sha256>,
}

impl Signer {
    fn from_env() -> Result<Self, String> {
        let key_id = std::env::var("KALSHI_API_KEY_ID")
            .map_err(|_| "KALSHI_API_KEY_ID not set (source ~/.kalshi/env)")?;
        let pem_path = std::env::var("KALSHI_PRIVATE_KEY_PATH")
            .map_err(|_| "KALSHI_PRIVATE_KEY_PATH not set")?;
        let pem = std::fs::read_to_string(&pem_path)
            .map_err(|e| format!("read {pem_path}: {e}"))?;
        let key = RsaPrivateKey::from_pkcs1_pem(&pem)
            .or_else(|_| RsaPrivateKey::from_pkcs8_pem(&pem))
            .map_err(|e| format!("parse private key: {e}"))?;
        Ok(Signer {
            key_id,
            // SigningKey::new → PSS with salt length = SHA-256 digest length,
            // matching the documented scheme.
            signing_key: SigningKey::new(key),
        })
    }

    /// Headers for `{ts_ms}{METHOD}{path}` where path includes /trade-api/v2
    /// and excludes the query string (per quick_start docs).
    fn headers(&self, method: &str, path: &str, ts_ms: u128) -> [(String, String); 3] {
        let msg = format!("{ts_ms}{method}{path}");
        let sig = self
            .signing_key
            .sign_with_rng(&mut rsa::rand_core::OsRng, msg.as_bytes());
        [
            ("KALSHI-ACCESS-KEY".into(), self.key_id.clone()),
            (
                "KALSHI-ACCESS-SIGNATURE".into(),
                base64::engine::general_purpose::STANDARD.encode(sig.to_bytes()),
            ),
            ("KALSHI-ACCESS-TIMESTAMP".into(), ts_ms.to_string()),
        ]
    }
}

fn now_ms() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis()
}

fn get_balance(
    client: &reqwest::blocking::Client,
    signer: &Signer,
    base: &str,
    skew_ms: i128,
) -> Result<(u16, String), reqwest::Error> {
    let path = "/trade-api/v2/portfolio/balance";
    let url = format!("{}{}", base.trim_end_matches("/trade-api/v2"), path);
    let ts = (now_ms() as i128 + skew_ms) as u128;
    let mut req = client.get(&url);
    for (k, v) in signer.headers("GET", path, ts) {
        req = req.header(k, v);
    }
    let resp = req.send()?;
    let status = resp.status().as_u16();
    let body = resp.text().unwrap_or_default();
    Ok((status, body.chars().take(120).collect()))
}

fn probe_rest(signer: &Signer, label: &str, base: &str) -> bool {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap();

    println!("== {label} REST ({base})");
    let ok = match get_balance(&client, signer, base, 0) {
        Ok((200, body)) => {
            println!("  balance GET: 200 OK ({body})");
            true
        }
        Ok((status, body)) => {
            println!("  balance GET: {status} — {body}");
            false
        }
        Err(e) => {
            println!("  balance GET failed: {e}");
            false
        }
    };
    if !ok {
        return false;
    }

    // Clock-skew tolerance sweep (undocumented — this is the empirical probe).
    // Override points via SKEW_SECONDS="-25,-20,..." for boundary refinement.
    let skews: Vec<i128> = std::env::var("SKEW_SECONDS")
        .ok()
        .map(|s| s.split(',').filter_map(|x| x.trim().parse().ok()).collect())
        .unwrap_or_else(|| vec![-300, -120, -60, -30, -10, -3, 3, 10, 30, 60, 120, 300]);
    println!("  skew sweep (signed GET /portfolio/balance):");
    for skew_s in skews {
        match get_balance(&client, signer, base, skew_s * 1000) {
            Ok((status, _)) => println!("    ts {skew_s:+4} s → {status}"),
            Err(e) => println!("    ts {skew_s:+4} s → error: {e}"),
        }
        std::thread::sleep(Duration::from_millis(150)); // stay far from rate limits
    }
    true
}

async fn probe_ws(signer: &Signer, label: &str, ws_url: &str, rest_base: &str) {
    use futures_util::{SinkExt, StreamExt};

    println!("== {label} WS ({ws_url})");

    // Find an open market to subscribe to (public endpoint, still signed-capable).
    let ticker = match reqwest::Client::new()
        .get(format!("{rest_base}/markets?limit=1&status=open"))
        .timeout(Duration::from_secs(10))
        .send()
        .await
    {
        Ok(r) => r
            .json::<serde_json::Value>()
            .await
            .ok()
            .and_then(|v| v["markets"][0]["ticker"].as_str().map(String::from)),
        Err(e) => {
            println!("  market discovery failed: {e}");
            None
        }
    };
    let Some(ticker) = ticker else {
        println!("  no open market found — skipping WS probe");
        return;
    };
    println!("  subscribing orderbook_delta for {ticker}");

    let path = "/trade-api/ws/v2";
    let mut req_builder = tokio_tungstenite::tungstenite::http::Request::builder()
        .uri(ws_url)
        .header("Host", ws_url.trim_start_matches("wss://").split('/').next().unwrap())
        .header("Upgrade", "websocket")
        .header("Connection", "Upgrade")
        .header(
            "Sec-WebSocket-Key",
            tokio_tungstenite::tungstenite::handshake::client::generate_key(),
        )
        .header("Sec-WebSocket-Version", "13");
    for (k, v) in signer.headers("GET", path, now_ms()) {
        req_builder = req_builder.header(k, v);
    }
    let request = req_builder.body(()).unwrap();

    match tokio::time::timeout(Duration::from_secs(10), tokio_tungstenite::connect_async(request))
        .await
    {
        Ok(Ok((mut ws, resp))) => {
            println!("  handshake: {}", resp.status());
            let sub = serde_json::json!({
                "id": 1,
                "cmd": "subscribe",
                "params": {"channels": ["orderbook_delta"], "market_ticker": ticker}
            });
            ws.send(tokio_tungstenite::tungstenite::Message::Text(sub.to_string()))
                .await
                .ok();
            for _ in 0..3 {
                match tokio::time::timeout(Duration::from_secs(10), ws.next()).await {
                    Ok(Some(Ok(msg))) => {
                        let text = msg.into_text().unwrap_or_default();
                        let kind = serde_json::from_str::<serde_json::Value>(&text)
                            .ok()
                            .and_then(|v| v["type"].as_str().map(String::from))
                            .unwrap_or_else(|| "?".into());
                        println!("  recv: type={kind} ({} bytes)", text.len());
                        if kind == "orderbook_snapshot" {
                            break;
                        }
                    }
                    Ok(Some(Err(e))) => {
                        println!("  ws error: {e}");
                        break;
                    }
                    Ok(None) => {
                        println!("  ws closed by server");
                        break;
                    }
                    Err(_) => {
                        println!("  ws timeout waiting for message");
                        break;
                    }
                }
            }
            ws.close(None).await.ok();
        }
        Ok(Err(e)) => println!("  handshake failed: {e}"),
        Err(_) => println!("  handshake timeout"),
    }
}

fn main() {
    let target = std::env::args().nth(1).unwrap_or_else(|| "both".into());
    let signer = match Signer::from_env() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("error: {e}");
            std::process::exit(2);
        }
    };

    let rt = tokio::runtime::Runtime::new().unwrap();
    let mut any_ok = false;

    if target == "demo" || target == "both" {
        if probe_rest(&signer, "demo", DEMO_REST) {
            any_ok = true;
            rt.block_on(probe_ws(&signer, "demo", DEMO_WS, DEMO_REST));
        } else {
            println!("  (key not valid on demo — demo has separate credentials)");
        }
    }
    if target == "live" || target == "both" {
        if probe_rest(&signer, "live", LIVE_REST) {
            any_ok = true;
            rt.block_on(probe_ws(&signer, "live", LIVE_WS, LIVE_REST));
        }
    }
    std::process::exit(if any_ok { 0 } else { 1 });
}
