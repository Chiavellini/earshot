//! Throwaway-grade empirical probe for the sherpa-onnx KWS API (M1 phase 3
//! verification: keywords_buf format, @label round-trip, timestamps,
//! reset-after-hit). Run: cargo run --example kws_probe
use sherpa_onnx::{KeywordSpotter, KeywordSpotterConfig, Wave};

fn main() {
    let dir = "models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01";
    let mut config = KeywordSpotterConfig::default();
    config.model_config.transducer.encoder =
        Some(format!("{dir}/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"));
    config.model_config.transducer.decoder =
        Some(format!("{dir}/decoder-epoch-12-avg-2-chunk-16-left-64.onnx"));
    config.model_config.transducer.joiner =
        Some(format!("{dir}/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"));
    config.model_config.tokens = Some(format!("{dir}/tokens.txt"));
    config.model_config.num_threads = 1;
    // Line format under test: tokens then :boost #threshold @label modifiers.
    // kw0 has BOTH a prefix (Fast) and full (Confirmed) entry — the load-bearing
    // question is whether a prefix hit blocks the full-sequence hit.
    config.keywords_buf = Some(
        "▁ L IGHT :2.0 #0.18 @kw0_fast\n▁ L IGHT ▁UP :2.0 #0.25 @kw0_conf\n▁LOVE LY :2.0 #0.18 @kw1_fast\n▁LOVE LY ▁CHI L D :2.0 #0.25 @kw1_conf\n▁FOR E VER :2.0 #0.25 @kw2_conf\n"
            .into(),
    );

    let kws = KeywordSpotter::create(&config).expect("create keyword spotter");
    let do_reset = std::env::args().nth(1).as_deref() != Some("--no-reset");
    println!("reset_after_hit={do_reset}");
    for wav_name in ["0.wav", "1.wav"] {
        let wave = Wave::read(&format!("{dir}/test_wavs/{wav_name}")).expect("read wave");
        println!("== {wav_name} rate={} samples={}", wave.sample_rate(), wave.samples().len());
        let stream = kws.create_stream();
        // Feed the wav TWICE back-to-back to test same-keyword re-fire,
        // in 20 ms slices like the real pipeline would.
        let hop = (wave.sample_rate() as usize) / 50;
        let samples: Vec<f32> = [wave.samples(), wave.samples()].concat();
        let samples = &samples[..];
        let mut fed = 0usize;
        while fed < samples.len() {
            let end = (fed + hop).min(samples.len());
            stream.accept_waveform(wave.sample_rate(), &samples[fed..end]);
            fed = end;
            while kws.is_ready(&stream) {
                kws.decode(&stream);
                if let Some(r) = kws.get_result(&stream) {
                    if !r.keyword.is_empty() {
                        println!(
                            "  HIT keyword={:?} tokens={:?} timestamps={:?} start_time={} at_fed_ms={} json={}",
                            r.keyword,
                            r.tokens_arr,
                            r.timestamps,
                            r.start_time,
                            fed * 1000 / wave.sample_rate() as usize,
                            r.json,
                        );
                        if do_reset {
                            kws.reset(&stream);
                        }
                    }
                }
            }
        }
        stream.input_finished();
        while kws.is_ready(&stream) {
            kws.decode(&stream);
            if let Some(r) = kws.get_result(&stream) {
                if !r.keyword.is_empty() {
                    println!("  TAIL HIT keyword={:?} timestamps={:?}", r.keyword, r.timestamps);
                    kws.reset(&stream);
                }
            }
        }
    }
}
