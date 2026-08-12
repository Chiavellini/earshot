//! `latprobe` — region/edge RTT probe (Work Order §2; the blocking dependency).
//!
//! The ingress vendor choice is coupled to the compute-region choice: Twilio's
//! ashburn edge next to us-east-1 neutralizes the TCP objection, while a
//! Chicago box would haul audio over a lossier path. Region is decided from
//! numbers, not vendor blogs — this measures RTT + loss from *this box* to each
//! candidate edge/POP/API endpoint and recommends the region with the best tail.
//!
//! RTT is the TCP three-way-handshake time (portable, no root/ICMP), timed with
//! `clock::MonotonicNs`. Loss = connections that timed out or were refused.

use std::time::Duration;

use serde::Serialize;
use tokio::net::TcpStream;

use crate::clock::MonotonicNs;

pub struct LatprobeArgs {
    /// `host` or `host:port` (defaults to :443) endpoints to probe.
    pub targets: Vec<String>,
    pub samples: u32,
    pub connect_timeout: Duration,
}

#[derive(Debug, Serialize)]
pub struct LatprobeReport {
    pub samples: u32,
    pub targets: Vec<TargetStat>,
    pub recommendation: String,
}

#[derive(Debug, Serialize)]
pub struct TargetStat {
    pub target: String,
    pub ok: u32,
    pub loss_pct: f64,
    pub rtt_p50_us: u64,
    pub rtt_p95_us: u64,
    pub rtt_p99_us: u64,
    pub rtt_max_us: u64,
}

fn with_default_port(target: &str) -> String {
    if target.contains(':') {
        target.to_string()
    } else {
        format!("{target}:443")
    }
}

pub async fn run_latprobe(args: &LatprobeArgs) -> LatprobeReport {
    let mut targets = Vec::new();
    for t in &args.targets {
        targets.push(probe_target(&with_default_port(t), args.samples, args.connect_timeout).await);
    }

    // Recommend the lowest p99 RTT among targets with acceptable loss (< 5%).
    let best = targets
        .iter()
        .filter(|t| t.loss_pct < 5.0 && t.ok > 0)
        .min_by_key(|t| t.rtt_p99_us);
    let recommendation = match best {
        Some(t) => format!(
            "{} — p99 {:.1} ms, loss {:.1}%",
            t.target,
            t.rtt_p99_us as f64 / 1000.0,
            t.loss_pct
        ),
        None => "no target met the loss threshold — widen the set or check connectivity".into(),
    };

    LatprobeReport {
        samples: args.samples,
        targets,
        recommendation,
    }
}

async fn probe_target(target: &str, samples: u32, timeout: Duration) -> TargetStat {
    let mut rtts: Vec<u64> = Vec::new();
    let mut ok = 0u32;
    for _ in 0..samples {
        let t0 = MonotonicNs::now();
        match tokio::time::timeout(timeout, TcpStream::connect(target)).await {
            Ok(Ok(stream)) => {
                rtts.push(MonotonicNs::now().saturating_since(t0));
                ok += 1;
                drop(stream);
            }
            Ok(Err(_)) | Err(_) => {} // refused or timed out → counted as loss
        }
        // Small gap so bursts don't self-congest the handshake path.
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    rtts.sort_unstable();
    let loss_pct = (samples - ok) as f64 / samples.max(1) as f64 * 100.0;
    TargetStat {
        target: target.to_string(),
        ok,
        loss_pct,
        rtt_p50_us: pct(&rtts, 0.50) / 1000,
        rtt_p95_us: pct(&rtts, 0.95) / 1000,
        rtt_p99_us: pct(&rtts, 0.99) / 1000,
        rtt_max_us: rtts.last().copied().unwrap_or(0) / 1000,
    }
}

fn pct(sorted: &[u64], q: f64) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let rank = ((q * sorted.len() as f64).ceil() as usize).clamp(1, sorted.len());
    sorted[rank - 1]
}

impl LatprobeReport {
    pub fn render_table(&self) -> String {
        let mut out = format!("latprobe  samples={}\n\n", self.samples);
        out.push_str(&format!(
            "{:<32} {:>4} {:>7} {:>9} {:>9} {:>9}\n",
            "target", "ok", "loss%", "p50µs", "p95µs", "p99µs"
        ));
        for t in &self.targets {
            out.push_str(&format!(
                "{:<32} {:>4} {:>6.1} {:>9} {:>9} {:>9}\n",
                t.target, t.ok, t.loss_pct, t.rtt_p50_us, t.rtt_p95_us, t.rtt_p99_us
            ));
        }
        out.push_str(&format!(
            "\nrecommend region/edge: {}\n",
            self.recommendation
        ));
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn loopback_target_has_low_loss_and_rtt() {
        // A real listener on loopback: every connect succeeds, loss 0.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        // Keep accepting so connects complete.
        tokio::spawn(async move {
            loop {
                let _ = listener.accept().await;
            }
        });

        let report = run_latprobe(&LatprobeArgs {
            targets: vec![addr.to_string()],
            samples: 5,
            connect_timeout: Duration::from_millis(500),
        })
        .await;
        assert_eq!(report.targets.len(), 1);
        assert_eq!(report.targets[0].ok, 5);
        assert_eq!(report.targets[0].loss_pct, 0.0);
        assert!(report.recommendation.contains(&addr.to_string()));
    }

    #[tokio::test]
    async fn refused_target_is_all_loss() {
        // Port 1 on loopback: connection refused → 100% loss, no recommendation.
        let report = run_latprobe(&LatprobeArgs {
            targets: vec!["127.0.0.1:1".into()],
            samples: 3,
            connect_timeout: Duration::from_millis(300),
        })
        .await;
        assert_eq!(report.targets[0].ok, 0);
        assert_eq!(report.targets[0].loss_pct, 100.0);
        assert!(report.recommendation.contains("no target"));
    }

    #[test]
    fn default_port_appended() {
        assert_eq!(
            with_default_port("edge.example.com"),
            "edge.example.com:443"
        );
        assert_eq!(
            with_default_port("edge.example.com:5060"),
            "edge.example.com:5060"
        );
    }
}
