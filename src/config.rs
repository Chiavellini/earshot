//! Per-event configuration (spec §5.5): TOML schema, loader, local validation.
//!
//! Live-Kalshi validation (tickers exist and are open, rules text pulled) is
//! deliberately behind the `MarketValidator` trait so M2 can slot in a real
//! implementation without touching the loader.

use std::path::Path;

use serde::Deserialize;
use thiserror::Error;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventConfig {
    pub event: EventSection,
    #[serde(default)]
    pub pipeline: PipelineSection,
    #[serde(default)]
    pub detector: DetectorSection,
    pub sources: Vec<SourceConfig>,
    /// Dual dial-in arbitration across multiple `sources` (spec §5.2). Ignored
    /// when a single source is configured. Defaults to failover.
    #[serde(default)]
    pub arbiter: ArbiterConfig,
    pub kalshi: KalshiSection,
    /// Per-account Twilio binding (multi-event isolation). Optional; env-based
    /// signature material is used when absent.
    #[serde(default)]
    pub twilio: Option<TwilioSection>,
    #[serde(default)]
    pub risk: crate::risk::RiskConfig,
    /// `[perf]` — OS/RT tuning hooks (core pinning, SCHED_FIFO). All off by
    /// default; only meaningful on the tuned Linux event box (docs/OS-TUNING.md).
    #[serde(default)]
    pub perf: crate::perf::PerfSection,
    /// `[operator_handler]` — gated operator-screen conference path
    /// (docs/OPERATOR-HANDLER.md). Optional; absent = disabled. Consumed by
    /// `scripts/twilio-operator-dial.sh` / `scripts/operator-page.sh` (which
    /// additionally require the `--operator-i-am-paged` CLI flag — the double
    /// gate), never by the binary's audio path.
    #[serde(default)]
    pub operator_handler: Option<OperatorHandlerSection>,
    /// `[verifier]` — second-stage fill-path verifier (detection-accuracy
    /// program D2, 2026-08-18): an offline ASR re-decodes the last `tail_ms`
    /// of the leg that fired and the FILL intent is held until the keyword
    /// appears in that text; the 1¢ observe path never waits. Absent = off.
    #[serde(default)]
    pub verifier: Option<VerifierSection>,
    pub keywords: Vec<KeywordConfig>,
}

/// `[verifier]` (`crate::verifier`). `mode = "shadow"` verifies and records
/// but gates nothing — the first deployment posture; `"gate"` holds fill
/// intents up to `budget_ms` past `decision_ts` and refuses on `Rejected`.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifierSection {
    #[serde(default)]
    pub mode: VerifierMode,
    /// Offline model directory (sherpa-onnx offline export: Parakeet-TDT
    /// transducer `encoder/decoder/joiner*.onnx`, or a NeMo CTC `model*.onnx`)
    /// + `tokens.txt`.
    pub model_dir: std::path::PathBuf,
    /// `nemo_transducer` (Parakeet-TDT / NeMo RNN-T) | `nemo_ctc` | `transducer`
    /// (icefall zipformer). Passed to sherpa as `model_type` for transducers.
    #[serde(default = "default_verifier_model_type")]
    pub model_type: String,
    #[serde(default = "default_verifier_threads")]
    pub num_threads: u32,
    /// Audio window ending at the firing chunk (ms).
    #[serde(default = "default_verifier_tail_ms")]
    pub tail_ms: u32,
    /// Hold budget measured from the intent's `decision_ts` (ms).
    #[serde(default = "default_verifier_budget_ms")]
    pub budget_ms: u32,
    /// `send` (default): a verdict that misses the budget lets the order go;
    /// `hold`: it is refused as `verifier_timeout`.
    #[serde(default)]
    pub on_timeout: VerifierOnTimeout,
    /// `intents` (default): verify only intent-bearing detections; `all_confirmed`:
    /// every Confirmed detection (shadow studies — grows the per-verdict sample).
    #[serde(default)]
    pub sample: VerifierSample,
    /// Market tickers whose FILL intents the verifier gates; `["*"]` = all.
    /// Shadow rows are written for every request regardless.
    #[serde(default)]
    pub tickers: Vec<String>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VerifierMode {
    #[default]
    Off,
    Shadow,
    Gate,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VerifierOnTimeout {
    #[default]
    Send,
    Hold,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VerifierSample {
    #[default]
    Intents,
    AllConfirmed,
}

fn default_verifier_model_type() -> String {
    "nemo_transducer".into()
}
fn default_verifier_threads() -> u32 {
    2
}
fn default_verifier_tail_ms() -> u32 {
    3500
}
fn default_verifier_budget_ms() -> u32 {
    500
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventSection {
    pub id: String,
    pub starts_at: String, // RFC 3339; parsed lazily — M0 never schedules
    /// Rehearsal posture (O6, 2026-08-16). Both schedulers hardcoded
    /// `--i-am-live --fillable` into every webcast unit, so a drill that
    /// exercised the detector end-to-end was scheduled fillable by
    /// construction, and its inertness rested entirely on getting per-word
    /// caps right in config. When true, the installers build a unit with NO
    /// fill permission and `schedule-proof.py` asserts the LOADED unit matches
    /// — so drill inertness becomes structural. The field lives here rather
    /// than in the RunPlan because the plan's field set is an exact-match
    /// schema (adding one invalidates every existing plan), while
    /// `config_sha256` already binds this file into `plan_sha256`.
    /// Default false: absent means a real event, never a rehearsal.
    #[serde(default)]
    pub rehearsal: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PipelineSection {
    #[serde(default = "default_chunk_ms")]
    pub chunk_ms: u32,
    /// Preproc upsamples any source below this rate (M1: 16 kHz only, for
    /// 16 kHz-native ASR models fed by 8 kHz telephony). Absent = passthrough.
    #[serde(default)]
    pub resample_to_hz: Option<u32>,
}

fn default_chunk_ms() -> u32 {
    160
}

impl Default for PipelineSection {
    fn default() -> Self {
        PipelineSection {
            chunk_ms: 160,
            resample_to_hz: None,
        }
    }
}

/// `[detector]` — engine selection (spec §5.4: the M1 bake-off dial).
#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DetectorSection {
    #[serde(default)]
    pub engine: EngineKind,
    /// Required iff `engine = "sherpa_kws"`.
    pub sherpa_kws: Option<SherpaKwsConfig>,
    /// Required iff `engine = "streaming_ctc"`.
    pub streaming_ctc: Option<StreamingCtcConfig>,
    /// Required iff `engine = "online_asr"`.
    pub online_asr: Option<OnlineAsrConfig>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineKind {
    /// RMS-threshold pipeline self-test (fires on the tone fixture).
    #[default]
    Stub,
    /// sherpa-onnx streaming keyword spotter (Zipformer transducer).
    SherpaKws,
    /// Custom low-lookahead streaming-CTC spotter (own prefix-firing on frame
    /// posteriors via `ort`); the latency-milestone engine.
    StreamingCtc,
    /// Any sherpa-onnx streaming ASR model (zipformer transducer, NeMo
    /// cache-aware conformer, zipformer2-CTC) driven through sherpa's
    /// `OnlineRecognizer`, with keyword matching on the emitted word stream and
    /// optional hotword biasing (`detector::online_asr`). The bench-many-models
    /// engine of the detection-accuracy program (2026-08-18).
    OnlineAsr,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SherpaKwsConfig {
    /// Unpacked model directory (see `scripts/fetch-models.sh`): encoder /
    /// decoder / joiner .onnx + tokens.txt.
    pub model_dir: std::path::PathBuf,
    #[serde(default = "default_num_threads")]
    pub num_threads: u32,
    #[serde(default = "default_max_active_paths")]
    pub max_active_paths: u32,
    /// Per-keyword-entry firing threshold unless overridden on the keyword.
    #[serde(default = "default_kws_threshold")]
    pub default_threshold: f32,
    /// Acoustic boost applied to keyword token paths during search.
    #[serde(default = "default_kws_boost")]
    pub default_boost: f32,
    /// Duplicate suppression per (keyword, tier), in stream-time ms.
    #[serde(default = "default_refractory_ms")]
    pub refractory_ms: u64,
    /// Load the fp32 encoder + joiner instead of the default int8 pair (decoder is
    /// fp32 either way). Diagnostic knob for the quantization arm of the
    /// miss-autopsy (docs/TELEPHONY-DETECTION-SPEC.md §10). Default false = the
    /// shipped int8 encoder/joiner + fp32 decoder.
    #[serde(default)]
    pub fp32: bool,
}

fn default_num_threads() -> u32 {
    1
}
fn default_max_active_paths() -> u32 {
    4
}
fn default_kws_threshold() -> f32 {
    0.25
}
fn default_kws_boost() -> f32 {
    2.0
}
fn default_refractory_ms() -> u64 {
    2000
}

/// `[detector.streaming_ctc]` — the custom low-lookahead spotter. Drives a
/// streaming-CTC encoder ONNX directly via `ort` and fires keywords with its own
/// prefix-firing logic on frame posteriors (`detector::streaming_ctc`).
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamingCtcConfig {
    /// Model directory: encoder `*.onnx` + `tokens.txt` (+ `bpe.model` for
    /// text→token compilation). See `scripts/fetch-models.sh`.
    pub model_dir: std::path::PathBuf,
    #[serde(default = "default_num_threads")]
    pub num_threads: u32,
    /// CTC blank token id (0 for k2/sherpa exports).
    #[serde(default)]
    pub blank_id: u32,
    /// Default per-entry firing confidence in (0, 1] unless overridden on the
    /// keyword. Confidence is the geometric-mean per-token posterior along the
    /// best CTC path (`ctc_scorer`).
    #[serde(default = "default_ctc_threshold")]
    pub default_threshold: f32,
    /// Duplicate suppression per (keyword, tier), in stream-time ms.
    #[serde(default = "default_refractory_ms")]
    pub refractory_ms: u64,
    /// Which encoder ONNX export to load from `model_dir`: `"int8"` (default —
    /// prefer the int8 export when several exist, exactly the pre-knob
    /// behavior) or `"fp32"` (require a non-int8 export; the detector refuses
    /// to build when only int8 exists). Counterpart of
    /// `[detector.sherpa_kws].fp32`, where fp32 measurably recovered recall at
    /// zero precision cost (docs/BAKEOFF.md §Miss-autopsy cell 1).
    #[serde(default = "default_ctc_precision")]
    pub precision: String,
}

fn default_ctc_threshold() -> f32 {
    0.5
}

fn default_ctc_precision() -> String {
    "int8".into()
}

/// `[detector.online_asr]` — sherpa-onnx `OnlineRecognizer` behind the
/// `Detector` trait (`detector::online_asr`). One config drives every sherpa
/// streaming model family; the directory layout selects the family.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OnlineAsrConfig {
    /// Model directory: `encoder/decoder/joiner*.onnx` (transducer) or
    /// `ctc-*.onnx` (zipformer2 CTC) or `model*.onnx` (NeMo CTC), plus
    /// `tokens.txt` (+ `bpe.model`/`bpe.vocab` for hotwords).
    pub model_dir: std::path::PathBuf,
    /// `auto` (classify by filenames) | `transducer` | `zipformer2_ctc` | `nemo_ctc`.
    #[serde(default = "default_online_asr_model_type")]
    pub model_type: String,
    /// `int8` (prefer int8 exports; fall back to fp32 when none ships) or
    /// `fp32` (require fp32; refuse when only int8 exists).
    #[serde(default = "default_ctc_precision")]
    pub precision: String,
    #[serde(default = "default_num_threads")]
    pub num_threads: u32,
    /// `modified_beam_search` (required for hotwords) | `greedy_search`.
    #[serde(default = "default_online_asr_decoding")]
    pub decoding_method: String,
    #[serde(default = "default_max_active_paths")]
    pub max_active_paths: u32,
    /// Bias the beam toward the configured keyword variants (transducer +
    /// modified_beam_search only; silently impossible on CTC/NeMo, where the
    /// engine logs `hotwords_active=false` at build).
    #[serde(default = "default_true")]
    pub hotwords: bool,
    /// Default per-phrase hotword score (sherpa ContextGraph scale — the same
    /// scale as `sherpa_kws` boost); a keyword's `boost` overrides it.
    #[serde(default = "default_hotwords_score")]
    pub hotwords_score: f32,
    /// Transducer blank penalty (emit-eagerness); 0.0 = model default.
    #[serde(default)]
    pub blank_penalty: f32,
    /// Endpointing: trailing silence after speech (rule1) / after any decode
    /// (rule2) and max utterance length (rule3). An endpoint completes the
    /// trailing word, then the stream is reset (encoder state kept).
    #[serde(default = "default_ep_rule1_ms")]
    pub endpoint_rule1_ms: u32,
    #[serde(default = "default_ep_rule2_ms")]
    pub endpoint_rule2_ms: u32,
    #[serde(default = "default_ep_rule3_ms")]
    pub endpoint_rule3_ms: u32,
    /// Duplicate suppression per keyword, in stream-time ms.
    #[serde(default = "default_refractory_ms")]
    pub refractory_ms: u64,
}

fn default_online_asr_model_type() -> String {
    "auto".into()
}
fn default_online_asr_decoding() -> String {
    "modified_beam_search".into()
}
fn default_true() -> bool {
    true
}
fn default_hotwords_score() -> f32 {
    1.5
}
fn default_ep_rule1_ms() -> u32 {
    2400
}
fn default_ep_rule2_ms() -> u32 {
    1200
}
fn default_ep_rule3_ms() -> u32 {
    600_000
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum SourceConfig {
    /// M0 test harness: plays a WAV with original (or scaled) timing.
    Replay {
        path: String,
        #[serde(default = "default_speed")]
        speed: f32, // 0 = as fast as possible
    },
    /// Earnings-call dial-in via Twilio Media Streams (M3): we run the WS
    /// server, Twilio's <Stream> connects to it (docs/TWILIO.md). The dial
    /// itself happens out-of-band (scripts/twilio-dial.sh).
    TwilioStream {
        bind: String,
        /// Max silence between WS messages once connected (default 30 s).
        /// Twilio streams silence as frames, so a stall is a dead call.
        #[serde(default)]
        idle_timeout_secs: Option<u64>,
        /// How long the FIRST accept waits for the call to land (default 900 s).
        #[serde(default)]
        accept_timeout_secs: Option<u64>,
        /// After a mid-call drop, how long a re-accept waits for the redial's
        /// stream before giving up as end-of-stream (default 120 s). Only
        /// meaningful with `max_reconnects > 0`.
        #[serde(default)]
        reaccept_window_secs: Option<u64>,
        /// How many mid-call drops the source survives by re-entering its
        /// accept loop (the redial itself is out-of-process —
        /// scripts/redial-supervisor.sh). Default 0 = one-shot, the
        /// pre-2026-07-29 behavior.
        #[serde(default)]
        max_reconnects: Option<u32>,
    },
    /// Raw SIP/RTP dial-in via a low-latency carrier (Telnyx/Bandwidth) or a
    /// bridge SIP URI directly (Option C). The adapter is GATED behind the
    /// live ingress bake-off (docs/INGRESS-BAKEOFF.md): the config parses and
    /// slots into the arbiter today, but `build_source` refuses to construct
    /// it until the bake-off names a winning carrier. No RTP code yet.
    Sip {
        /// PSTN number to dial (carrier path), if any.
        #[serde(default)]
        number: String,
        /// Direct bridge SIP URI (Option C: skips the PSTN carrier, may
        /// negotiate wideband G.722/Opus). Checked per-event as a pre-flight.
        #[serde(default)]
        sip_uri: String,
        /// Carrier label for the provider→lead table / arbiter accounting.
        #[serde(default)]
        carrier: String,
        #[serde(default)]
        dtmf: String,
        #[serde(default)]
        dial_at: String,
        /// Local UDP `host:port` the RTP media plane binds to receive the
        /// carrier's (or a SIP gateway's) media stream. When set, the media-plane
        /// adapter (`source::sip`) is constructed; when empty, the leg stays gated
        /// (signaling not built — see docs/INGRESS-BAKEOFF.md §5).
        #[serde(default)]
        rtp_bind: String,
    },
    /// Webcast fallback: ffmpeg decodes the URL to raw PCM (adapter #4,
    /// `source::pipe`). Tens of seconds behind the phone bridge — coverage
    /// for no-phone issuers and failed number hunts, not for racing.
    Hls { url: String },
    /// Generic subprocess audio: `cmd` writes raw s16le MONO PCM at
    /// `sample_rate_hz` to stdout (e.g. `yt-dlp -o - <url> | ffmpeg …` via
    /// `sh -c`). The honest primitive under `hls`; same webcast latency class.
    Pipe {
        cmd: Vec<String>,
        #[serde(default = "default_pipe_rate")]
        sample_rate_hz: u32,
        /// Recording/report label override (bake-off files are `<label>.wav`;
        /// the harness rejects duplicate labels — two same-kind legs MUST
        /// differ, the 07-17 smoke found the collision). Empty = `"pipe"`.
        #[serde(default)]
        label: String,
    },
}

impl SourceConfig {
    /// Provider/leg label: names bake-off recordings (`<label>.wav`), fanout
    /// report rows, and `DetectionRecord.leg`. Same-kind legs MUST set
    /// distinct `label`s (validated for fanout; the capture harness rejects
    /// collisions at run time).
    pub fn label(&self) -> String {
        match self {
            SourceConfig::Replay { .. } => "replay".into(),
            SourceConfig::TwilioStream { .. } => "twilio".into(),
            SourceConfig::Sip { carrier, .. } if !carrier.is_empty() => carrier.clone(),
            SourceConfig::Sip { .. } => "sip".into(),
            SourceConfig::Hls { .. } => "hls".into(),
            SourceConfig::Pipe { label, .. } if !label.is_empty() => label.clone(),
            SourceConfig::Pipe { .. } => "pipe".into(),
        }
    }
}

fn default_pipe_rate() -> u32 {
    16_000
}

fn default_speed() -> f32 {
    1.0
}

/// `[arbiter]` — how multiple `sources` are combined (spec §5.2).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArbiterConfig {
    #[serde(default)]
    pub mode: ArbiterMode,
    /// Failover: max quiet gap on the active leg before switching (ms).
    #[serde(default = "default_switch_ms")]
    pub switch_ms: u64,
    /// TakeFirst: audio buffered per leg before estimating the inter-leg lag (ms).
    #[serde(default = "default_align_ms")]
    pub align_ms: u64,
}

impl Default for ArbiterConfig {
    fn default() -> Self {
        ArbiterConfig {
            mode: ArbiterMode::default(),
            switch_ms: 200,
            align_ms: 2000,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ArbiterMode {
    /// Primary + standby failover (robust default).
    #[default]
    Failover,
    /// Per-position first-arrival wins after cross-correlation alignment.
    TakeFirst,
    /// NOT an arbiter at all: every source runs its own full detect chain and
    /// the flags are OR-ed at the shared one-shot decision table
    /// (`pipeline::run_pipeline_fanout`). Different legs hear different audio
    /// (07-22: "tariff" present on the capture leg, missing on the arm's own
    /// stream), so per-leg detection is a measured recall lever. `build_source`
    /// never constructs a `SourceArbiter` for this mode.
    Fanout,
}

fn default_switch_ms() -> u64 {
    200
}
fn default_align_ms() -> u64 {
    2000
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KalshiSection {
    pub series: String,
    #[serde(default = "default_env")]
    pub env: String, // "prod" | "demo"
    #[serde(default = "default_heartbeat")]
    pub heartbeat_secs: u64,
    /// Per-account credential binding (multi-event isolation, docs/MULTI-EVENT.md).
    /// When `key_id` + `private_key_path` are BOTH set, `arm` builds this event's
    /// Kalshi signer from THIS config — never from ambient env — so two concurrent
    /// events on two accounts can never sign with each other's key. When both are
    /// absent, `arm` falls back to the `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`
    /// env pair (the single-account posture; keeps every existing config working).
    #[serde(default)]
    pub key_id: Option<String>,
    /// Path to the RSA private-key PEM, resolved relative to the config file.
    #[serde(default)]
    pub private_key_path: Option<String>,
    /// Optional tripwire: when set, `arm` refuses unless it equals `key_id`,
    /// catching a config whose key was swapped without updating the declared
    /// intent. NOTE: this is a config-internal consistency check; the proof that
    /// the (key_id, key) pair is a real, coherent Kalshi credential comes from the
    /// signed `balance()` self-test in `connect_live_for`. Proving the key belongs
    /// to a SPECIFIC expected account is the remote-probe tier (out of scope here).
    #[serde(default)]
    pub expected_key_id: Option<String>,
    /// Order-entry wire: "rest" (default, unchanged behavior) or "fix"
    /// (lever E2 — requires FIX/Premier access; offline-built 2026-07-21,
    /// live validation pending; see VERIFICATIONS.md §Q-FIX).
    #[serde(default)]
    pub transport: OrderWire,
    /// FIX session parameters; consulted only when `transport = "fix"`.
    /// Omitting the whole section uses production defaults.
    #[serde(default)]
    pub fix: Option<FixSection>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum OrderWire {
    #[default]
    Rest,
    Fix,
}

/// `[kalshi.fix]` — see `kalshi::fix::FixConfig` (built from this in
/// `main.rs::arm`). Defaults are the production KalshiNR endpoint
/// (VERIFICATIONS Q-FIX F2).
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixSection {
    #[serde(default = "default_fix_host")]
    pub host: String,
    #[serde(default = "default_fix_port")]
    pub port: u16,
    #[serde(default = "default_fix_target")]
    pub target_comp_id: String,
    /// FIX API key id (SenderCompID, tag 49). Falls back to the REST key id;
    /// whether they are the same credential is a Q-FIX verify-at-live item.
    #[serde(default)]
    pub fix_key_id: Option<String>,
    /// Heartbeat interval (tag 108). Kalshi requires > 3 s (F4).
    #[serde(default = "default_fix_heartbeat")]
    pub heartbeat_secs: u32,
}

fn default_fix_host() -> String {
    "mm.fix.elections.kalshi.com".into()
}
fn default_fix_port() -> u16 {
    8228
}
fn default_fix_target() -> String {
    "KalshiNR".into()
}
fn default_fix_heartbeat() -> u32 {
    10
}

/// `[twilio]` — per-account Twilio Media Streams binding (multi-event isolation).
/// Optional. When present, `arm` pins the WS signature material from THIS config
/// instead of the process-global `TWILIO_AUTH_TOKEN` / `TWILIO_STREAM_URL`, so a
/// stream signed by the wrong account's auth token is rejected with a 403
/// (`src/source/twilio_ws.rs` `check_signature`). `account_sid` / `from_number`
/// are consumed by `scripts/launch-event.sh`, not the binary.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TwilioSection {
    /// Path (resolved relative to the config) to a file holding the account auth
    /// token — kept out of the repo, same posture as the Kalshi key.
    pub auth_token_path: String,
    /// The exact `wss://…` `<Stream url>` Twilio is configured with. Pinning it
    /// here means the signature base is never reconstructed from the client
    /// `Host` header, removing ngrok-host ambiguity between two events.
    pub stream_url: String,
    #[serde(default)]
    pub account_sid: Option<String>,
    #[serde(default)]
    pub from_number: Option<String>,
}

/// `[operator_handler]` — the gated human-first operator path for Class B
/// (live-operator-screened) bridges (docs/OPERATOR-HANDLER.md, VERIFICATIONS
/// Q-ADMIT). `enabled = true` is only HALF the gate: the operator scripts also
/// require `--operator-i-am-paged` at dial time. The default listen-only dial
/// (`scripts/twilio-dial.sh`) never reads this section.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperatorHandlerSection {
    /// Off by default; a config without this section (or with `enabled =
    /// false`) can never produce an operator-capable leg.
    #[serde(default)]
    pub enabled: bool,
    /// The ONLY number the pager leg may dial (the user's own cell). E.164,
    /// validated when `enabled`.
    #[serde(default)]
    pub cell_number: String,
    /// Namespace for Twilio `<Conference>` names so two events can never
    /// collide on a conference room. `[a-z0-9-]{1,24}`, validated when
    /// `enabled`.
    #[serde(default = "default_conference_prefix")]
    pub conference_prefix: String,
}

fn default_conference_prefix() -> String {
    "earshot".into()
}

/// Strict E.164: `^\+[1-9][0-9]{7,14}$` (a `+`, a non-zero digit, then 7..=14
/// more digits). Hand-rolled so config validation adds no regex dependency.
fn is_e164(s: &str) -> bool {
    let Some(rest) = s.strip_prefix('+') else {
        return false;
    };
    let b = rest.as_bytes();
    (8..=15).contains(&b.len()) && b[0] != b'0' && b.iter().all(|c| c.is_ascii_digit())
}

fn default_env() -> String {
    "prod".into()
}
fn default_heartbeat() -> u64 {
    15
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Policy {
    FastOnly,
    ConfirmedOnly,
    TwoTier,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeywordConfig {
    pub market_ticker: String,
    pub phrase: String,
    /// Compiled from the market rules text, not from intuition (spec §5.4).
    pub variants: Vec<String>,
    pub max_price_cents: u8,
    pub budget_contracts: u32,
    pub policy: Policy,
    #[serde(default)]
    pub rule_risk_haircut_cents: u8,
    /// Partial-word prefixes that fire `Tier::Fast` before the word completes
    /// (e.g. "reces" for "recession"). Each must be a strict case-insensitive
    /// prefix of some variant. Empty ⇒ the engine emits no Fast tier.
    #[serde(default)]
    pub fast_prefixes: Vec<String>,
    /// Engine firing thresholds; None ⇒ `[detector.sherpa_kws]` defaults.
    #[serde(default)]
    pub fast_threshold: Option<f32>,
    #[serde(default)]
    pub confirmed_threshold: Option<f32>,
    #[serde(default)]
    pub boost: Option<f32>,
    /// Decision-layer confidence gates per tier; 0.0 = no gating (M0 behavior).
    #[serde(default)]
    pub min_confidence_fast: f32,
    #[serde(default)]
    pub min_confidence_confirmed: f32,
}

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("failed to read {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("failed to parse TOML: {0}")]
    Parse(#[from] toml::de::Error),
    #[error("config invalid: {0}")]
    Invalid(String),
}

pub fn load(path: &Path) -> Result<EventConfig, ConfigError> {
    let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Io {
        path: path.display().to_string(),
        source,
    })?;
    let cfg: EventConfig = toml::from_str(&text)?;
    validate(&cfg)?;
    Ok(cfg)
}

/// A PARTIAL allowlist is the dangerous shape. `--i-am-live` only checks that
/// `allowlist_tickers` is non-empty, so a config that kept `prepare`'s
/// placeholder entry while its `[[keywords]]` were filled in real passes every
/// gate, arms cleanly, and then refuses **every** order at fire time with
/// `Refusal::NotAllowlisted`. All three configs staged for 2026-07-28/29 were in
/// exactly that state: 1 placeholder ticker against 16/15/43 keyword tickers.
///
/// Rule: an allowlist that exists must COVER the keywords. An empty allowlist is
/// still permitted (bench/replay configs, and it is honestly "no orders"); `arm`
/// warns loudly about an empty one, because a clean dry-run on an empty
/// allowlist proves the audio path and never the fire path.
///
/// Not folded into [`validate`] because bench and replay configs legitimately
/// carry keywords with no risk section at all; this is an *arm-path* obligation.
pub fn allowlist_covers_keywords(cfg: &EventConfig) -> Result<(), ConfigError> {
    if cfg.risk.allowlist_tickers.is_empty() {
        return Ok(());
    }
    let missing: Vec<&str> = cfg
        .keywords
        .iter()
        .map(|k| k.market_ticker.as_str())
        .filter(|t| !cfg.risk.allowlist_tickers.iter().any(|a| a == t))
        .collect();
    if missing.is_empty() {
        return Ok(());
    }
    Err(ConfigError::Invalid(format!(
        "[risk].allowlist_tickers is non-empty but does not cover {} of {} keyword \
         market_tickers — every order for these would be refused NotAllowlisted at \
         fire time. Missing: {:?}{}. Paste the [risk] block from `earshot prepare`.",
        missing.len(),
        cfg.keywords.len(),
        &missing[..missing.len().min(5)],
        if missing.len() > 5 { " …" } else { "" }
    )))
}

/// Persistence needs a consumer: `max_reconnects > 0` makes the source
/// re-enter its accept loop after a drop and write a redial marker — but the
/// marker path derives from `EARSHOT_REDIAL_DIR`, and without it the source
/// only WARNs mid-call ("re-accept is open but nobody was asked to redial")
/// and the re-accept window dies as EndOfStream. Fail at arm time instead of
/// discovering it mid-call. `redial_dir` is the env value, passed in so tests
/// don't touch process env; `launch-event.sh` always exports it, so only bare
/// `earshot arm` invocations can trip this — which is the point.
pub fn check_redial_dir_for_persistence(
    cfg: &EventConfig,
    redial_dir: Option<&str>,
) -> Result<(), ConfigError> {
    let wants_persistence = cfg
        .sources
        .iter()
        .any(|s| matches!(s, SourceConfig::TwilioStream { max_reconnects: Some(n), .. } if *n > 0));
    if wants_persistence && redial_dir.map_or(true, str::is_empty) {
        return Err(ConfigError::Invalid(
            "twilio_stream.max_reconnects > 0 but EARSHOT_REDIAL_DIR is unset — the source \
             would re-accept after a drop but nobody would ever be asked to redial, and the \
             run would end quietly at the re-accept window. launch-event.sh exports it; \
             export EARSHOT_REDIAL_DIR=<state dir>, or set max_reconnects = 0 for a \
             deliberate one-shot."
                .into(),
        ));
    }
    Ok(())
}

/// The CTC scorer implements NO acoustic boost — `detector::streaming_ctc`
/// compiles keywords with a fixed boost of 1.0 and never reads the per-entry
/// value — so keyword `boost` values are dead config under that engine. They
/// are sherpa_kws-scale numbers (e.g. from `prepare`'s `tuned_library`), and
/// silently discarding them has already caused a real tuning confusion:
/// sherpa-tuned boosts pasted into a streaming_ctc config looked applied but
/// did nothing. Returns the warning text so `validate` can shout it (and tests
/// can assert it) without changing any scoring behavior.
fn ctc_ignored_boost_warning(cfg: &EventConfig) -> Option<String> {
    if cfg.detector.engine != EngineKind::StreamingCtc {
        return None;
    }
    let boosted: Vec<&str> = cfg
        .keywords
        .iter()
        .filter(|k| k.boost.is_some())
        .map(|k| k.phrase.as_str())
        .collect();
    if boosted.is_empty() {
        return None;
    }
    Some(format!(
        "detector.engine = \"streaming_ctc\": keyword `boost` is NOT implemented by the \
         CTC scorer and is IGNORED for {} keyword(s) {:?} — boost is a sherpa_kws-scale \
         knob (e.g. prepare's tuned_library values); on this engine tune \
         `confirmed_threshold`/`fast_threshold`/`default_threshold` instead",
        boosted.len(),
        boosted
    ))
}

fn validate(cfg: &EventConfig) -> Result<(), ConfigError> {
    let fail = |msg: String| Err(ConfigError::Invalid(msg));

    if cfg.sources.is_empty() {
        return fail("at least one source is required".into());
    }
    for s in &cfg.sources {
        match s {
            SourceConfig::Hls { url } if url.is_empty() => {
                return fail("hls.url must be non-empty".into());
            }
            SourceConfig::Pipe {
                cmd,
                sample_rate_hz,
                ..
            } => {
                if cmd.is_empty() {
                    return fail("pipe.cmd must be non-empty".into());
                }
                if !(8_000..=48_000).contains(sample_rate_hz) {
                    return fail(format!(
                        "pipe.sample_rate_hz {sample_rate_hz} outside 8000..=48000"
                    ));
                }
            }
            _ => {}
        }
        if let SourceConfig::TwilioStream {
            bind,
            idle_timeout_secs,
            accept_timeout_secs,
            reaccept_window_secs,
            max_reconnects,
        } = s
        {
            if bind.parse::<std::net::SocketAddr>().is_err() {
                return fail(format!(
                    "twilio_stream.bind {bind:?} is not a socket address (e.g. \"127.0.0.1:8080\")"
                ));
            }
            // Bounds keep a typo from silently disabling the dead-call guard
            // (idle=0 would spin; idle=30000 would never notice a dead call).
            if let Some(v) = idle_timeout_secs {
                if !(5..=300).contains(v) {
                    return fail(format!(
                        "twilio_stream.idle_timeout_secs {v} outside 5..=300"
                    ));
                }
            }
            if let Some(v) = accept_timeout_secs {
                if !(10..=7200).contains(v) {
                    return fail(format!(
                        "twilio_stream.accept_timeout_secs {v} outside 10..=7200"
                    ));
                }
            }
            if let Some(v) = reaccept_window_secs {
                if !(10..=900).contains(v) {
                    return fail(format!(
                        "twilio_stream.reaccept_window_secs {v} outside 10..=900"
                    ));
                }
            }
            if let Some(v) = max_reconnects {
                if *v > 20 {
                    return fail(format!(
                        "twilio_stream.max_reconnects {v} > 20 — a bridge that drops us that often needs a different fix"
                    ));
                }
            }
        }
    }
    if cfg.arbiter.switch_ms == 0 || cfg.arbiter.align_ms == 0 {
        return fail("arbiter.switch_ms and arbiter.align_ms must be > 0".into());
    }
    if cfg.arbiter.mode == ArbiterMode::Fanout {
        if cfg.sources.len() < 2 {
            return fail(format!(
                "arbiter.mode = \"fanout\" requires >= 2 sources (got {}); a single \
                 source needs no fanout",
                cfg.sources.len()
            ));
        }
        // Every leg needs a distinct label: it names the report rows and
        // DetectionRecord.leg — a duplicate would make per-leg attribution
        // ambiguous (same collision the 07-17 capture smoke hit).
        let mut labels: Vec<String> = cfg.sources.iter().map(|s| s.label()).collect();
        labels.sort();
        for pair in labels.windows(2) {
            if pair[0] == pair[1] {
                return fail(format!(
                    "arbiter.mode = \"fanout\": duplicate source label {:?} — same-kind \
                     legs must set distinct `label`s",
                    pair[0]
                ));
            }
        }
        // [perf] core maps are single-chain; pinning N legs' threads onto one
        // core map would silently stack spinning/SCHED_FIFO threads per core.
        // Refuse loudly — webcast boxes run untuned, nothing is lost.
        let p = &cfg.perf;
        if p.realtime
            || p.spin_hot_rings
            || p.core_source.is_some()
            || p.core_preproc.is_some()
            || p.core_chunker.is_some()
            || p.core_detect.is_some()
            || p.core_executor.is_some()
        {
            return fail(
                "arbiter.mode = \"fanout\" requires a default [perf] section (core \
                 pin maps are single-chain; remove [perf] tuning or use a non-fanout mode)"
                    .into(),
            );
        }
    }
    // Per-account Kalshi binding: key_id and private_key_path are all-or-nothing,
    // and expected_key_id (a tripwire) is meaningless without a key_id to check.
    match (&cfg.kalshi.key_id, &cfg.kalshi.private_key_path) {
        (Some(_), None) | (None, Some(_)) => {
            return fail("[kalshi] key_id and private_key_path must be set together".into());
        }
        (None, None) if cfg.kalshi.expected_key_id.is_some() => {
            return fail("[kalshi] expected_key_id requires key_id + private_key_path".into());
        }
        _ => {}
    }
    if let (Some(exp), Some(key_id)) = (&cfg.kalshi.expected_key_id, &cfg.kalshi.key_id) {
        if exp != key_id {
            return fail(format!(
                "[kalshi] expected_key_id {exp:?} != key_id {key_id:?} (config tripwire)"
            ));
        }
    }
    // FIX wire (E2): heartbeat floor is a venue rule (108 must be > 3 s, Q-FIX
    // F4) — refuse at config time, not at the live Logon.
    if let Some(fix) = &cfg.kalshi.fix {
        if fix.heartbeat_secs <= 3 {
            return fail(format!(
                "[kalshi.fix] heartbeat_secs {} must be > 3 (Kalshi Logon rule)",
                fix.heartbeat_secs
            ));
        }
        if fix.host.is_empty() || fix.target_comp_id.is_empty() {
            return fail("[kalshi.fix] host and target_comp_id must be non-empty".into());
        }
    }
    if let Some(tw) = &cfg.twilio {
        if !tw.stream_url.starts_with("wss://") {
            return fail(format!(
                "[twilio] stream_url must be a wss:// URL, got {:?}",
                tw.stream_url
            ));
        }
    }
    // Operator handler (docs/OPERATOR-HANDLER.md): only an ENABLED section is
    // held to the field rules — a disabled/absent section is inert and the
    // operator scripts refuse on it regardless of field contents.
    if let Some(op) = &cfg.operator_handler {
        if op.enabled {
            if !is_e164(&op.cell_number) {
                return fail(format!(
                    "[operator_handler] enabled = true requires cell_number in strict E.164 \
                     (^\\+[1-9][0-9]{{7,14}}$), got {:?} — this is the ONLY number the pager \
                     leg may dial",
                    op.cell_number
                ));
            }
            let p = &op.conference_prefix;
            let prefix_ok = !p.is_empty()
                && p.len() <= 24
                && p.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
            if !prefix_ok {
                return fail(format!(
                    "[operator_handler] conference_prefix must match [a-z0-9-]{{1,24}}, got {p:?}"
                ));
            }
        }
    }
    if cfg.keywords.is_empty() {
        return fail("at least one keyword is required".into());
    }
    if cfg.pipeline.chunk_ms == 0 || cfg.pipeline.chunk_ms > 1000 {
        return fail(format!(
            "pipeline.chunk_ms must be in 1..=1000, got {}",
            cfg.pipeline.chunk_ms
        ));
    }
    if !(1..=99).contains(&cfg.perf.rt_priority) {
        return fail(format!(
            "perf.rt_priority must be in 1..=99 (SCHED_FIFO range), got {}",
            cfg.perf.rt_priority
        ));
    }
    if cfg.perf.spin_hot_rings {
        if !cfg.perf.realtime {
            return fail("perf.spin_hot_rings requires perf.realtime = true".into());
        }
        // Spinning consumers are preproc, chunker, detect. A spinning
        // SCHED_FIFO thread never yields — sharing a core starves the peer
        // (and anything else pinned there), so all assigned hot cores must be
        // pairwise distinct.
        let cores = [
            ("core_source", cfg.perf.core_source),
            ("core_preproc", cfg.perf.core_preproc),
            ("core_chunker", cfg.perf.core_chunker),
            ("core_detect", cfg.perf.core_detect),
            ("core_executor", cfg.perf.core_executor),
        ];
        for (name, c) in [cores[1], cores[2], cores[3]] {
            if c.is_none() {
                return fail(format!(
                    "perf.spin_hot_rings requires perf.{name} to be set"
                ));
            }
        }
        let assigned: Vec<_> = cores
            .iter()
            .filter_map(|(n, c)| c.map(|c| (*n, c)))
            .collect();
        for i in 0..assigned.len() {
            for j in i + 1..assigned.len() {
                if assigned[i].1 == assigned[j].1 {
                    return fail(format!(
                        "perf.spin_hot_rings: {} and {} share core {} — spinning threads must not share a core",
                        assigned[i].0, assigned[j].0, assigned[i].1
                    ));
                }
            }
        }
    }
    if let Some(hz) = cfg.pipeline.resample_to_hz {
        if hz != 16_000 {
            return fail(format!(
                "pipeline.resample_to_hz: only 16000 is supported in M1 (2x from 8 kHz telephony), got {hz}"
            ));
        }
    }
    if cfg.detector.engine == EngineKind::SherpaKws && cfg.detector.sherpa_kws.is_none() {
        return fail(
            "detector.engine = \"sherpa_kws\" requires a [detector.sherpa_kws] section".into(),
        );
    }
    if let Some(sk) = &cfg.detector.sherpa_kws {
        if !(0.0..=1.0).contains(&sk.default_threshold) || sk.default_threshold == 0.0 {
            return fail(format!(
                "detector.sherpa_kws.default_threshold must be in (0, 1], got {}",
                sk.default_threshold
            ));
        }
        if sk.num_threads == 0 {
            return fail("detector.sherpa_kws.num_threads must be > 0".into());
        }
    }
    if cfg.detector.engine == EngineKind::StreamingCtc && cfg.detector.streaming_ctc.is_none() {
        return fail(
            "detector.engine = \"streaming_ctc\" requires a [detector.streaming_ctc] section"
                .into(),
        );
    }
    if let Some(sc) = &cfg.detector.streaming_ctc {
        if !(0.0..=1.0).contains(&sc.default_threshold) || sc.default_threshold == 0.0 {
            return fail(format!(
                "detector.streaming_ctc.default_threshold must be in (0, 1], got {}",
                sc.default_threshold
            ));
        }
        if sc.num_threads == 0 {
            return fail("detector.streaming_ctc.num_threads must be > 0".into());
        }
        if sc.precision != "int8" && sc.precision != "fp32" {
            return fail(format!(
                "detector.streaming_ctc.precision must be \"int8\" or \"fp32\", got {:?}",
                sc.precision
            ));
        }
    }
    if cfg.detector.engine == EngineKind::OnlineAsr && cfg.detector.online_asr.is_none() {
        return fail(
            "detector.engine = \"online_asr\" requires a [detector.online_asr] section".into(),
        );
    }
    if let Some(oa) = &cfg.detector.online_asr {
        if oa.num_threads == 0 {
            return fail("detector.online_asr.num_threads must be > 0".into());
        }
        if oa.precision != "int8" && oa.precision != "fp32" {
            return fail(format!(
                "detector.online_asr.precision must be \"int8\" or \"fp32\", got {:?}",
                oa.precision
            ));
        }
        if !matches!(
            oa.model_type.as_str(),
            "auto" | "transducer" | "zipformer2_ctc" | "nemo_ctc"
        ) {
            return fail(format!(
                "detector.online_asr.model_type must be auto|transducer|zipformer2_ctc|nemo_ctc, got {:?}",
                oa.model_type
            ));
        }
        if oa.decoding_method != "modified_beam_search" && oa.decoding_method != "greedy_search" {
            return fail(format!(
                "detector.online_asr.decoding_method must be modified_beam_search|greedy_search, got {:?}",
                oa.decoding_method
            ));
        }
        if oa.hotwords && oa.decoding_method != "modified_beam_search" {
            return fail(
                "detector.online_asr.hotwords = true requires decoding_method = \"modified_beam_search\" \
                 (sherpa consults hotwords only in the beam search)"
                    .into(),
            );
        }
        if oa.max_active_paths == 0 {
            return fail("detector.online_asr.max_active_paths must be > 0".into());
        }
        if oa.endpoint_rule1_ms == 0 || oa.endpoint_rule2_ms == 0 || oa.endpoint_rule3_ms == 0 {
            return fail("detector.online_asr.endpoint_rule*_ms must be > 0 (sherpa maps 0 to its own defaults)".into());
        }
    }
    if cfg.detector.engine == EngineKind::OnlineAsr {
        // Fast prefixes have no meaning on a full-word text matcher; refusing is
        // louder than a WARN (which rides stdout into plan compile — see the
        // execution-profile.sh capture hazard) and prevents a silently missing tier.
        let with_prefixes: Vec<&str> = cfg
            .keywords
            .iter()
            .filter(|k| !k.fast_prefixes.is_empty())
            .map(|k| k.phrase.as_str())
            .collect();
        if !with_prefixes.is_empty() {
            return fail(format!(
                "detector.engine = \"online_asr\" matches complete words only and emits no Fast tier; \
                 remove fast_prefixes from {:?}",
                with_prefixes
            ));
        }
        let fast_only: Vec<&str> = cfg
            .keywords
            .iter()
            .filter(|k| k.policy == Policy::FastOnly)
            .map(|k| k.phrase.as_str())
            .collect();
        if !fast_only.is_empty() {
            return fail(format!(
                "detector.engine = \"online_asr\" never emits Tier::Fast, so policy = \"fast_only\" \
                 can never fire: {:?}",
                fast_only
            ));
        }
    }
    if let Some(v) = &cfg.verifier {
        if v.num_threads == 0 {
            return fail("verifier.num_threads must be > 0".into());
        }
        if !(1000..=10_000).contains(&v.tail_ms) {
            return fail(format!(
                "verifier.tail_ms must be in 1000..=10000, got {}",
                v.tail_ms
            ));
        }
        if !(100..=5000).contains(&v.budget_ms) {
            return fail(format!(
                "verifier.budget_ms must be in 100..=5000, got {}",
                v.budget_ms
            ));
        }
        if !matches!(
            v.model_type.as_str(),
            "nemo_transducer" | "nemo_ctc" | "transducer"
        ) {
            return fail(format!(
                "verifier.model_type must be nemo_transducer|nemo_ctc|transducer, got {:?}",
                v.model_type
            ));
        }
        let known: std::collections::HashSet<&str> = cfg
            .keywords
            .iter()
            .map(|k| k.market_ticker.as_str())
            .collect();
        for t in &v.tickers {
            if t != "*" && !known.contains(t.as_str()) {
                return fail(format!(
                    "verifier.tickers names unknown market_ticker {t:?}"
                ));
            }
        }
        if v.mode == VerifierMode::Gate && v.tickers.is_empty() {
            return fail(
                "verifier.mode = \"gate\" with no verifier.tickers gates nothing — list the fill \
                 words to gate (or [\"*\"]) or use mode = \"shadow\""
                    .into(),
            );
        }
    }
    if let Some(msg) = ctc_ignored_boost_warning(cfg) {
        tracing::warn!("{msg}");
    }

    let mut seen = std::collections::HashSet::new();
    for kw in &cfg.keywords {
        if !seen.insert(kw.market_ticker.as_str()) {
            return fail(format!("duplicate market_ticker {}", kw.market_ticker));
        }
        if kw.variants.is_empty() {
            return fail(format!("keyword {:?} has no variants", kw.phrase));
        }
        if !(1..=99).contains(&kw.max_price_cents) {
            return fail(format!(
                "keyword {:?}: max_price_cents must be 1..=99, got {}",
                kw.phrase, kw.max_price_cents
            ));
        }
        if kw.budget_contracts == 0 {
            return fail(format!(
                "keyword {:?}: budget_contracts must be > 0",
                kw.phrase
            ));
        }
        for p in &kw.fast_prefixes {
            let p_lc = p.to_lowercase();
            let is_strict_prefix = kw
                .variants
                .iter()
                .any(|v| v.to_lowercase().starts_with(&p_lc) && v.len() > p.len());
            if !is_strict_prefix {
                return fail(format!(
                    "keyword {:?}: fast_prefix {p:?} is not a strict prefix of any variant (typo?)",
                    kw.phrase
                ));
            }
        }
        for (name, t) in [
            ("fast_threshold", kw.fast_threshold),
            ("confirmed_threshold", kw.confirmed_threshold),
        ] {
            if let Some(t) = t {
                if !(t > 0.0 && t <= 1.0) {
                    return fail(format!(
                        "keyword {:?}: {name} must be in (0, 1], got {t}",
                        kw.phrase
                    ));
                }
            }
        }
        for (name, c) in [
            ("min_confidence_fast", kw.min_confidence_fast),
            ("min_confidence_confirmed", kw.min_confidence_confirmed),
        ] {
            if !(0.0..=1.0).contains(&c) {
                return fail(format!(
                    "keyword {:?}: {name} must be in [0, 1], got {c}",
                    kw.phrase
                ));
            }
        }
    }
    Ok(())
}

/// M2 seam: validates config against live Kalshi state at arm time (tickers
/// exist and are open, rules text stored for audit). Refuse to arm on failure.
pub trait MarketValidator {
    fn validate(&self, cfg: &EventConfig) -> Result<(), ConfigError>;
}

/// M0/M1: no live exchange access — always passes.
pub struct NoopMarketValidator;

impl MarketValidator for NoopMarketValidator {
    fn validate(&self, _cfg: &EventConfig) -> Result<(), ConfigError> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn example_path() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("configs/example-event.toml")
    }

    #[test]
    /// The staging defect that would have silently voided the 07-28 PayPal
    /// arm — the ONLY scheduled live Δrace measurement — by refusing all 16
    /// orders. Scans every checked-in config so the next one cannot ship it.
    fn no_checked_in_config_has_a_partial_allowlist() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("configs");
        let mut checked = 0;
        for entry in std::fs::read_dir(&dir).unwrap().flatten() {
            let path = entry.path();
            if path.extension().map(|e| e != "toml").unwrap_or(true) {
                continue;
            }
            let Ok(cfg) = load(&path) else { continue };
            checked += 1;
            assert!(
                allowlist_covers_keywords(&cfg).is_ok(),
                "{}: {}",
                path.display(),
                allowlist_covers_keywords(&cfg).unwrap_err()
            );
        }
        assert!(
            checked > 5,
            "expected to scan several configs, scanned {checked}"
        );
    }

    #[test]
    fn a_partial_allowlist_is_rejected_but_an_empty_one_is_not() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("configs/webcast-pypl-26jul28-arm.toml");
        let mut cfg = load(&path).unwrap();
        assert!(allowlist_covers_keywords(&cfg).is_ok());

        // The exact shape that shipped: prepare's placeholder survives, the
        // keywords are real. Non-empty, so every live gate passes.
        cfg.risk.allowlist_tickers = vec!["KXEARNINGSMENTIONPYPL-26JUL28-CAPT".into()];
        let err = allowlist_covers_keywords(&cfg).unwrap_err().to_string();
        assert!(err.contains("does not cover"), "{err}");

        // Empty stays legal: bench/replay configs, and it is honestly "no orders".
        cfg.risk.allowlist_tickers.clear();
        assert!(allowlist_covers_keywords(&cfg).is_ok());
    }

    #[test]
    fn parses_checked_in_example_verbatim() {
        let cfg = load(&example_path()).unwrap();
        assert_eq!(cfg.event.id, "AAPL-Q3-2026");
        assert_eq!(cfg.pipeline.chunk_ms, 160);
        assert_eq!(cfg.sources.len(), 1);
        assert!(matches!(cfg.sources[0], SourceConfig::Replay { .. }));
        assert_eq!(cfg.keywords.len(), 1);
        assert_eq!(cfg.keywords[0].max_price_cents, 97);
        assert_eq!(cfg.keywords[0].policy, Policy::TwoTier);
    }

    #[test]
    fn parses_checked_in_twilio_live_config_verbatim() {
        let path =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("configs/twilio-live.toml");
        let cfg = load(&path).unwrap();
        assert!(matches!(cfg.sources[0], SourceConfig::TwilioStream { .. }));
        assert_eq!(cfg.detector.engine, EngineKind::SherpaKws);
        // Mention-probe posture invariants: every keyword 1¢ × 1 contract,
        // confirmed-only, and allowlisted.
        for kw in &cfg.keywords {
            assert_eq!(kw.max_price_cents, 1, "{}", kw.market_ticker);
            assert_eq!(kw.budget_contracts, 1, "{}", kw.market_ticker);
            assert_eq!(kw.policy, Policy::ConfirmedOnly, "{}", kw.market_ticker);
            assert!(cfg.risk.allowlist_tickers.contains(&kw.market_ticker));
        }
    }

    #[test]
    fn parses_checked_in_dpz_capture_config_verbatim() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("configs/webcast-dpz-capture.toml");
        let cfg = load(&path).unwrap();
        // Four capture legs; same-kind pipe legs disambiguate via `label`
        // (leg 1 keeps the empty default → "pipe" recording filename).
        assert_eq!(cfg.sources.len(), 4);
        assert!(matches!(&cfg.sources[0], SourceConfig::Pipe { label, .. } if label.is_empty()));
        assert!(matches!(&cfg.sources[1], SourceConfig::Hls { .. }));
        assert!(
            matches!(&cfg.sources[2], SourceConfig::Pipe { label, .. } if label == "pipe_tuned")
        );
        assert!(
            matches!(&cfg.sources[3], SourceConfig::Pipe { label, .. } if label == "streamlink")
        );
    }

    #[test]
    fn parses_checked_in_ladder_config_verbatim() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("configs/webcast-ladder-bakeoff.toml");
        let cfg = load(&path).unwrap();
        assert_eq!(cfg.sources.len(), 3);
        assert!(matches!(&cfg.sources[0], SourceConfig::Pipe { label, .. } if label.is_empty()));
        assert!(
            matches!(&cfg.sources[1], SourceConfig::Pipe { label, .. } if label == "pipe_tuned")
        );
        assert!(
            matches!(&cfg.sources[2], SourceConfig::Pipe { label, .. } if label == "streamlink")
        );
    }

    #[test]
    fn rejects_bad_twilio_bind() {
        let text = base_toml().replace(
            "kind  = \"replay\"\npath  = \"fixture.wav\"\nspeed = 1.0                       # 0 = as fast as possible (tests)",
            "kind = \"twilio_stream\"\nbind = \"not-an-addr\"",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(super::validate(&cfg).is_err());
    }

    fn base_toml() -> String {
        std::fs::read_to_string(example_path()).unwrap()
    }

    /// base_toml with its replay source swapped for a twilio_stream source
    /// carrying the given extra lines.
    fn twilio_toml(extra: &str) -> String {
        base_toml().replace(
            "kind  = \"replay\"\npath  = \"fixture.wav\"\nspeed = 1.0                       # 0 = as fast as possible (tests)",
            &format!("kind = \"twilio_stream\"\nbind = \"127.0.0.1:8080\"\n{extra}"),
        )
    }

    #[test]
    fn twilio_persistence_knobs_parse_and_bound() {
        // Absent knobs (every existing config) must keep parsing.
        let cfg: EventConfig = toml::from_str(&twilio_toml("")).unwrap();
        assert!(super::validate(&cfg).is_ok());
        assert!(matches!(
            cfg.sources[0],
            SourceConfig::TwilioStream {
                max_reconnects: None,
                reaccept_window_secs: None,
                ..
            }
        ));

        let cfg: EventConfig = toml::from_str(&twilio_toml(
            "idle_timeout_secs = 45\naccept_timeout_secs = 1200\nreaccept_window_secs = 90\nmax_reconnects = 3",
        ))
        .unwrap();
        assert!(super::validate(&cfg).is_ok());
        assert!(matches!(
            cfg.sources[0],
            SourceConfig::TwilioStream {
                idle_timeout_secs: Some(45),
                accept_timeout_secs: Some(1200),
                reaccept_window_secs: Some(90),
                max_reconnects: Some(3),
                ..
            }
        ));

        // Out-of-bounds values fail validation loudly, per knob.
        for bad in [
            "idle_timeout_secs = 3",
            "idle_timeout_secs = 301",
            "accept_timeout_secs = 5",
            "reaccept_window_secs = 5",
            "reaccept_window_secs = 901",
            "max_reconnects = 21",
        ] {
            let cfg: EventConfig = toml::from_str(&twilio_toml(bad)).unwrap();
            assert!(super::validate(&cfg).is_err(), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn persistence_without_redial_dir_refuses_at_arm() {
        // Gap E (2026-08-04): max_reconnects > 0 with no EARSHOT_REDIAL_DIR
        // used to only WARN mid-call — the source re-accepted but nobody was
        // ever asked to redial. Arm-time refusal, env passed in as a value so
        // this test never touches process env.
        let cfg: EventConfig = toml::from_str(&twilio_toml("max_reconnects = 2")).unwrap();
        let err = check_redial_dir_for_persistence(&cfg, None)
            .unwrap_err()
            .to_string();
        assert!(err.contains("EARSHOT_REDIAL_DIR"), "{err}");
        let err = check_redial_dir_for_persistence(&cfg, Some(""))
            .unwrap_err()
            .to_string();
        assert!(err.contains("EARSHOT_REDIAL_DIR"), "{err}");
        assert!(check_redial_dir_for_persistence(&cfg, Some("state/EV")).is_ok());

        // One-shot postures (unset or 0) never need the dir — bridge-probe
        // and every pre-2026-07-29 config stay bare.
        let cfg: EventConfig = toml::from_str(&twilio_toml("")).unwrap();
        assert!(check_redial_dir_for_persistence(&cfg, None).is_ok());
        let cfg: EventConfig = toml::from_str(&twilio_toml("max_reconnects = 0")).unwrap();
        assert!(check_redial_dir_for_persistence(&cfg, None).is_ok());
    }

    #[test]
    fn rejects_unknown_source_kind() {
        let text = base_toml().replace("kind  = \"replay\"", "kind  = \"carrier_pigeon\"");
        assert!(toml::from_str::<EventConfig>(&text).is_err());
    }

    #[test]
    fn rejects_price_cap_100() {
        let text = base_toml().replace(
            "max_price_cents         = 97",
            "max_price_cents         = 100",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    #[test]
    fn rejects_empty_variants() {
        let text = base_toml().replace(
            "variants                = [\"artificial intelligence\", \"artificial intelligences\"]",
            "variants                = []",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    #[test]
    fn parses_checked_in_fanout_arm_template_verbatim() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("configs/webcast-fanout-arm.toml");
        let cfg = load(&path).unwrap();
        assert_eq!(cfg.arbiter.mode, ArbiterMode::Fanout);
        assert_eq!(cfg.sources.len(), 2);
        assert_eq!(cfg.sources[0].label(), "streamlink");
        assert_eq!(cfg.sources[1].label(), "pipe_tuned");
        // Template ships in the probe posture: 1¢ × 1 contract.
        assert_eq!(cfg.keywords[0].max_price_cents, 1);
        assert_eq!(cfg.keywords[0].budget_contracts, 1);
    }

    #[test]
    fn fanout_parses_and_validates_with_distinct_labels() {
        let text = base_toml()
            + "\n[arbiter]\nmode = \"fanout\"\n\n[[sources]]\nkind = \"pipe\"\ncmd = [\"cat\"]\nlabel = \"legb\"\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert_eq!(cfg.arbiter.mode, ArbiterMode::Fanout);
        assert_eq!(cfg.sources.len(), 2);
        assert_eq!(cfg.sources[0].label(), "replay");
        assert_eq!(cfg.sources[1].label(), "legb");
        validate(&cfg).unwrap();
    }

    #[test]
    fn fanout_rejects_single_source() {
        let text = base_toml() + "\n[arbiter]\nmode = \"fanout\"\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains(">= 2 sources"), "{err}");
    }

    #[test]
    fn fanout_rejects_duplicate_labels() {
        // Two pipe legs both defaulting to label "pipe".
        let text = base_toml()
            + "\n[arbiter]\nmode = \"fanout\"\n\
               \n[[sources]]\nkind = \"pipe\"\ncmd = [\"cat\"]\n\
               \n[[sources]]\nkind = \"pipe\"\ncmd = [\"cat\"]\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("duplicate source label"), "{err}");
    }

    #[test]
    fn fanout_rejects_tuned_perf_section() {
        let text = base_toml()
            + "\n[arbiter]\nmode = \"fanout\"\n\
               \n[[sources]]\nkind = \"pipe\"\ncmd = [\"cat\"]\nlabel = \"legb\"\n\
               \n[perf]\nrealtime = true\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("default [perf]"), "{err}");
        // A pinned core alone (realtime off) is refused too — the pin maps
        // are single-chain.
        let text = base_toml()
            + "\n[arbiter]\nmode = \"fanout\"\n\
               \n[[sources]]\nkind = \"pipe\"\ncmd = [\"cat\"]\nlabel = \"legb\"\n\
               \n[perf]\ncore_detect = 3\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    #[test]
    fn detector_defaults_to_stub() {
        let cfg = load(&example_path()).unwrap();
        assert_eq!(cfg.detector.engine, EngineKind::Stub);
        assert!(cfg.detector.sherpa_kws.is_none());
        assert_eq!(cfg.pipeline.resample_to_hz, None);
        assert!(cfg.keywords[0].fast_prefixes.is_empty());
        assert_eq!(cfg.keywords[0].min_confidence_fast, 0.0);
    }

    #[test]
    fn sherpa_engine_requires_its_section() {
        let text = base_toml() + "\n[detector]\nengine = \"sherpa_kws\"\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("[detector.sherpa_kws]"), "{err}");
    }

    #[test]
    fn sherpa_section_parses_with_defaults() {
        let text = base_toml()
            + "\n[detector]\nengine = \"sherpa_kws\"\n[detector.sherpa_kws]\nmodel_dir = \"models/kws\"\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        validate(&cfg).unwrap();
        let sk = cfg.detector.sherpa_kws.unwrap();
        assert_eq!(sk.num_threads, 1);
        assert_eq!(sk.max_active_paths, 4);
        assert_eq!(sk.default_threshold, 0.25);
        assert_eq!(sk.default_boost, 2.0);
        assert_eq!(sk.refractory_ms, 2000);
    }

    // --- [detector.streaming_ctc] precision knob + ignored-boost warning ---

    /// base_toml with a streaming_ctc detector section carrying `extra` lines.
    fn ctc_toml(extra: &str) -> String {
        base_toml()
            + "\n[detector]\nengine = \"streaming_ctc\"\n\n[detector.streaming_ctc]\nmodel_dir = \"models/ctc\"\n"
            + extra
    }

    #[test]
    fn ctc_precision_defaults_to_int8() {
        let cfg: EventConfig = toml::from_str(&ctc_toml("")).unwrap();
        validate(&cfg).unwrap();
        assert_eq!(cfg.detector.streaming_ctc.unwrap().precision, "int8");
    }

    #[test]
    fn ctc_precision_fp32_accepted_others_rejected() {
        let cfg: EventConfig = toml::from_str(&ctc_toml("precision = \"fp32\"\n")).unwrap();
        validate(&cfg).unwrap();
        assert_eq!(cfg.detector.streaming_ctc.unwrap().precision, "fp32");
        // Exact strings only — a case/typo variant silently defaulting would
        // void an A/B, so it must refuse loudly.
        for bad in ["fp16", "INT8", "Fp32", "", "int-8"] {
            let cfg: EventConfig =
                toml::from_str(&ctc_toml(&format!("precision = \"{bad}\"\n"))).unwrap();
            let err = validate(&cfg).unwrap_err().to_string();
            assert!(err.contains("precision"), "{bad:?}: {err}");
        }
    }

    #[test]
    fn ctc_engine_warns_on_ignored_keyword_boost() {
        // No keyword boost → no warning.
        let cfg: EventConfig = toml::from_str(&ctc_toml("")).unwrap();
        assert!(ctc_ignored_boost_warning(&cfg).is_none());

        // A keyword `boost` under streaming_ctc → warning names the keyword
        // and says IGNORED (the CTC scorer implements no boost).
        let text = ctc_toml("").replace(
            "policy                  = \"two_tier\"",
            "policy                  = \"two_tier\"\nboost                   = 3.0",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_ok()); // warns, never rejects
        let msg = ctc_ignored_boost_warning(&cfg).unwrap();
        assert!(msg.contains("IGNORED"), "{msg}");
        assert!(msg.contains("artificial intelligence"), "{msg}");

        // The same boost under a non-CTC engine (base_toml defaults to stub;
        // sherpa_kws implements boost for real) → silent.
        let text = base_toml().replace(
            "policy                  = \"two_tier\"",
            "policy                  = \"two_tier\"\nboost                   = 3.0",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(ctc_ignored_boost_warning(&cfg).is_none());
    }

    #[test]
    fn rejects_non_16k_resample_target() {
        let text = base_toml().replace("chunk_ms = 160", "chunk_ms = 160\nresample_to_hz = 44100");
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    #[test]
    fn rejects_fast_prefix_that_matches_no_variant() {
        let text = base_toml().replace(
            "policy                  = \"two_tier\"",
            "policy                  = \"two_tier\"\nfast_prefixes           = [\"artifishul\"]",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("fast_prefix"), "{err}");
    }

    #[test]
    fn accepts_valid_fast_prefix_case_insensitive() {
        let text = base_toml().replace(
            "policy                  = \"two_tier\"",
            "policy                  = \"two_tier\"\nfast_prefixes           = [\"Artificial intel\"]",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        validate(&cfg).unwrap();
    }

    #[test]
    fn rejects_out_of_range_min_confidence() {
        let text = base_toml().replace(
            "policy                  = \"two_tier\"",
            "policy                  = \"two_tier\"\nmin_confidence_fast     = 1.5",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    #[test]
    fn arbiter_defaults_to_failover_and_parses_take_first() {
        // Absent [arbiter] → failover defaults.
        let cfg = load(&example_path()).unwrap();
        assert_eq!(cfg.arbiter.mode, ArbiterMode::Failover);
        assert_eq!(cfg.arbiter.switch_ms, 200);
        assert_eq!(cfg.arbiter.align_ms, 2000);

        // Explicit [arbiter] parses.
        let text =
            base_toml() + "\n[arbiter]\nmode = \"take_first\"\nswitch_ms = 120\nalign_ms = 1500\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        validate(&cfg).unwrap();
        assert_eq!(cfg.arbiter.mode, ArbiterMode::TakeFirst);
        assert_eq!(cfg.arbiter.switch_ms, 120);
    }

    #[test]
    fn parses_gated_sip_leg_with_carrier() {
        let text = base_toml()
            + "\n[[sources]]\nkind = \"sip\"\ncarrier = \"telnyx\"\nsip_uri = \"sip:bridge@x\"\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        validate(&cfg).unwrap();
        assert_eq!(cfg.sources.len(), 2);
        assert!(
            matches!(&cfg.sources[1], SourceConfig::Sip { carrier, .. } if carrier == "telnyx")
        );
    }

    #[test]
    fn rejects_duplicate_ticker() {
        let mut text = base_toml();
        let dup = text[text.find("[[keywords]]").unwrap()..].to_string();
        text.push_str("\n");
        text.push_str(&dup);
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    // --- multi-event account binding (docs/MULTI-EVENT.md) ---

    /// Inject lines under the example's `[kalshi]` section.
    fn with_kalshi(extra: &str) -> EventConfig {
        let text = base_toml().replace(
            "series = \"KXAAPLMENTION\"",
            &format!("series = \"KXAAPLMENTION\"\n{extra}"),
        );
        toml::from_str(&text).unwrap()
    }

    #[test]
    fn kalshi_creds_absent_is_env_posture() {
        // The unchanged example (no key_id) must still validate — env fallback.
        let cfg = load(&example_path()).unwrap();
        assert!(cfg.kalshi.key_id.is_none());
        assert!(cfg.kalshi.private_key_path.is_none());
        validate(&cfg).unwrap();
    }

    #[test]
    fn kalshi_full_binding_parses_and_validates() {
        let cfg = with_kalshi(
            "key_id = \"acct-A\"\nprivate_key_path = \"keys/a.pem\"\nexpected_key_id = \"acct-A\"",
        );
        validate(&cfg).unwrap();
        assert_eq!(cfg.kalshi.key_id.as_deref(), Some("acct-A"));
        assert_eq!(cfg.kalshi.private_key_path.as_deref(), Some("keys/a.pem"));
    }

    #[test]
    fn kalshi_key_id_without_key_path_rejected() {
        let cfg = with_kalshi("key_id = \"acct-A\"");
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("must be set together"), "{err}");
    }

    #[test]
    fn kalshi_expected_key_id_mismatch_rejected() {
        let cfg = with_kalshi(
            "key_id = \"acct-A\"\nprivate_key_path = \"keys/a.pem\"\nexpected_key_id = \"acct-B\"",
        );
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("expected_key_id"), "{err}");
    }

    #[test]
    fn kalshi_expected_key_id_without_binding_rejected() {
        let cfg = with_kalshi("expected_key_id = \"acct-A\"");
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("expected_key_id requires"), "{err}");
    }

    // --- [operator_handler] (docs/OPERATOR-HANDLER.md, gated) ---

    /// base_toml with an [operator_handler] section appended.
    fn with_operator(extra: &str) -> EventConfig {
        let text = base_toml() + "\n[operator_handler]\n" + extra + "\n";
        toml::from_str(&text).unwrap()
    }

    #[test]
    fn operator_handler_absent_by_default() {
        // Every existing config carries no section — must stay None and valid.
        let cfg = load(&example_path()).unwrap();
        assert!(cfg.operator_handler.is_none());
    }

    #[test]
    fn operator_handler_enabled_parses_and_validates() {
        let cfg = with_operator("enabled = true\ncell_number = \"+525545436717\"");
        validate(&cfg).unwrap();
        let op = cfg.operator_handler.unwrap();
        assert!(op.enabled);
        assert_eq!(op.cell_number, "+525545436717");
        assert_eq!(op.conference_prefix, "earshot"); // default
    }

    #[test]
    fn operator_handler_disabled_section_is_inert() {
        // enabled = false (or defaulted): field rules are not enforced — the
        // section is inert and the scripts refuse on the enabled gate anyway.
        for extra in ["enabled = false", "", "cell_number = \"not-a-number\""] {
            let cfg = with_operator(extra);
            validate(&cfg).unwrap();
            assert!(!cfg.operator_handler.as_ref().unwrap().enabled);
        }
    }

    #[test]
    fn operator_handler_enabled_rejects_bad_cell_numbers() {
        for bad in [
            "",                     // empty
            "5545436717",           // no +
            "+05545436717",         // leading zero
            "+52 55 4543 6717",     // spaces
            "+521234",              // too short (< 8 digits)
            "+5255454367171234567", // too long (> 15 digits)
            "+52554543671a",        // non-digit
        ] {
            let cfg = with_operator(&format!("enabled = true\ncell_number = \"{bad}\""));
            let err = validate(&cfg).unwrap_err().to_string();
            assert!(err.contains("cell_number"), "{bad:?}: {err}");
        }
    }

    #[test]
    fn operator_handler_enabled_rejects_bad_conference_prefix() {
        for bad in [
            "",
            "Earshot",                   // uppercase
            "ear shot",                  // space
            "ear_shot",                  // underscore
            "abcdefghijklmnopqrstuvwxy", // 25 chars
        ] {
            let cfg = with_operator(&format!(
                "enabled = true\ncell_number = \"+525545436717\"\nconference_prefix = \"{bad}\""
            ));
            let err = validate(&cfg).unwrap_err().to_string();
            assert!(err.contains("conference_prefix"), "{bad:?}: {err}");
        }
        // Boundary: 24 chars of the allowed charset passes.
        let cfg = with_operator(
            "enabled = true\ncell_number = \"+525545436717\"\nconference_prefix = \"abcdefghijklmnopqrst-123\"",
        );
        validate(&cfg).unwrap();
    }

    #[test]
    fn operator_handler_rejects_unknown_fields() {
        let text = base_toml() + "\n[operator_handler]\nenabled = true\nclips_dir = \"x\"\n";
        assert!(toml::from_str::<EventConfig>(&text).is_err());
    }

    #[test]
    fn twilio_section_parses_and_requires_wss() {
        let ok = base_toml()
            + "\n[twilio]\nauth_token_path = \"tok.txt\"\nstream_url = \"wss://a.ngrok.io/media\"\n";
        let cfg: EventConfig = toml::from_str(&ok).unwrap();
        validate(&cfg).unwrap();
        assert_eq!(
            cfg.twilio.as_ref().unwrap().stream_url,
            "wss://a.ngrok.io/media"
        );

        let bad = base_toml()
            + "\n[twilio]\nauth_token_path = \"tok.txt\"\nstream_url = \"https://a.ngrok.io/media\"\n";
        let cfg: EventConfig = toml::from_str(&bad).unwrap();
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("wss://"), "{err}");
    }
}
