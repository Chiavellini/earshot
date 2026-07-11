use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

use earshot::config::{self, SourceConfig};
use earshot::decision::DecisionTable;
use earshot::detector::stub::EnergyStubDetector;
use earshot::fixture::FixtureSpec;
use earshot::pipeline::run_pipeline;
use earshot::source::replay::ReplaySource;
use earshot::telemetry;
use earshot::types::SourceId;

#[derive(Parser)]
#[command(name = "earshot", about = "Audio-signal latency trader (M0 skeleton)")]
struct Cli {
    /// Emit JSON logs (default: human-readable)
    #[arg(long, global = true)]
    json_logs: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Synthesize the standard test fixture WAV (tone bursts at 2/5/8 s)
    GenFixture {
        #[arg(long, default_value = "fixture.wav")]
        out: PathBuf,
    },
    /// Replay a recorded session through the pipeline and report latency
    Replay {
        #[arg(long)]
        config: PathBuf,
        #[arg(long, default_value = "report.json")]
        report: PathBuf,
        /// Override replay speed (0 = as fast as possible)
        #[arg(long)]
        speed: Option<f32>,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    telemetry::init_logging(cli.json_logs);
    match run(cli.command) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!("{e}");
            ExitCode::FAILURE
        }
    }
}

fn run(command: Command) -> Result<(), Box<dyn std::error::Error>> {
    match command {
        Command::GenFixture { out } => {
            FixtureSpec::standard().write_wav(&out)?;
            println!("wrote standard fixture (10 s, 8 kHz, bursts @ 2/5/8 s) to {}", out.display());
            Ok(())
        }
        Command::Replay { config: config_path, report, speed } => {
            let cfg = config::load(&config_path)?;

            // M0 arms only the replay source; sip/hls parse but have no adapter yet.
            let (path, cfg_speed) = cfg
                .sources
                .iter()
                .find_map(|s| match s {
                    SourceConfig::Replay { path, speed } => Some((path.clone(), *speed)),
                    _ => None,
                })
                .ok_or("no replay source in config (M0 supports replay only)")?;
            // Relative WAV paths resolve against the config file's directory.
            let wav_path = {
                let p = PathBuf::from(&path);
                if p.is_absolute() {
                    p
                } else {
                    config_path.parent().unwrap_or_else(|| std::path::Path::new(".")).join(p)
                }
            };

            let source = Box::new(ReplaySource::from_wav(
                &wav_path,
                speed.unwrap_or(cfg_speed),
                SourceId(0),
            )?);
            let detector = Box::new(EnergyStubDetector::for_standard_fixture());
            let table = DecisionTable::from_keywords(&cfg.keywords);

            let out = run_pipeline(&cfg.event.id, cfg.pipeline.chunk_ms, source, detector, table)?;
            print!("\n{}", out.render_table());
            out.write_json(&report)?;
            println!("\nreport written to {}", report.display());

            if out.drops.total() > 0 {
                return Err(format!("{} frames/chunks dropped — stalled stage?", out.drops.total()).into());
            }
            Ok(())
        }
    }
}
