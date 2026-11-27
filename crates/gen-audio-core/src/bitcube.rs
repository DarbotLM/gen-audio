//! Authorized audio analysis delegated to the existing .NET/F# execution host.
//! JSON points, a true Z-projected cube PNG and a spectrogram are one publication.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::OnceLock;
use std::time::Instant;

use serde_json::{json, Value};

use crate::{
    asset, audio_input, mastering, mix, mix_render,
    paths::{self, Scratch},
};

static ACTIVE: AtomicUsize = AtomicUsize::new(0);
struct Permit;
impl Drop for Permit {
    fn drop(&mut self) {
        ACTIVE.fetch_sub(1, Ordering::AcqRel);
    }
}

fn native_host(repo: Option<&Path>) -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("WINGEN_AUDIO_EXE") {
        let path = PathBuf::from(path);
        if !path.is_absolute() || !path.is_file() {
            return Err(
                "WINGEN_AUDIO_EXE must name an existing absolute native-host executable".into(),
            );
        }
        return Ok(path);
    }
    let mut candidates = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            candidates.push(parent.join("wingen.audio.exe"));
            candidates.push(parent.join("wingen.audio-x86_64-pc-windows-msvc.exe"));
        }
    }
    if let Some(repo) = repo {
        candidates.push(
            repo.join("apps")
                .join("desktop")
                .join("src-tauri")
                .join("binaries")
                .join("wingen.audio-x86_64-pc-windows-msvc.exe"),
        );
    }
    candidates.into_iter().find(|p| p.is_file()).ok_or_else(|| {
        "Build the native host or set WINGEN_AUDIO_EXE; no substitute analysis is generated".into()
    })
}

pub fn analyze(
    scratch: &Scratch,
    repo: Option<&Path>,
    args: &Value,
) -> Result<Value, (i32, String)> {
    static SOURCE: OnceLock<jsonschema::Validator> = OnceLock::new();
    let validator = SOURCE
        .get_or_init(|| jsonschema::validator_for(&mix::source_schema()).expect("source schema"));
    if !validator.is_valid(&args["source"]) {
        return Err((
            -32602,
            "bitcube requires a catalog uid or authorized hash-pinned source".into(),
        ));
    }
    let start_ms = match args.get("startMs") {
        None => 0,
        Some(value) => value
            .as_u64()
            .filter(|n| *n <= 600000)
            .ok_or((-32602, "startMs must be 0..600000".into()))?,
    };
    let duration_ms = match args.get("durationMs") {
        None => None,
        Some(value) => Some(
            value
                .as_u64()
                .filter(|n| (1..=60000).contains(n))
                .ok_or((-32602, "durationMs must be 1..60000".into()))?,
        ),
    };
    let output = args["output"]
        .as_str()
        .filter(|s| s.len() <= 100 && s.ends_with(".json"))
        .ok_or((
            -32602,
            "output must be a JSON basename of at most 100 characters".into(),
        ))?;
    let stem = output.strip_suffix(".json").unwrap();
    if mastering::is_windows_device_name(stem) {
        return Err((-32602, "Reserved output name".into()));
    }
    let names = [
        output.to_owned(),
        format!("{stem}-cube.png"),
        format!("{stem}-spectrogram.png"),
    ];
    let destinations: Vec<PathBuf> = names
        .iter()
        .map(|name| {
            let path = paths::write_name(&scratch.dir, name)
                .map_err(|_| (-32602, "Output must be a scratch basename".into()))?;
            if path.symlink_metadata().is_ok() {
                return Err((-32602, "Analysis output names must be unused".into()));
            }
            Ok(path)
        })
        .collect::<Result<_, (i32, String)>>()?;
    let host = match native_host(repo) {
        Ok(host) => host,
        Err(reason) => {
            return Ok(
                json!({"ok":false,"code":"not_configured","reason":reason,"synthesizedSpeech":false}),
            )
        }
    };
    if ACTIVE
        .try_update(Ordering::AcqRel, Ordering::Acquire, |n| {
            (n < 2).then_some(n + 1)
        })
        .is_err()
    {
        return Ok(
            json!({"ok":false,"code":"limit_exceeded","reason":"Two native analyses are already active"}),
        );
    }
    let _permit = Permit;
    let started = Instant::now();
    match execute(
        scratch,
        repo,
        args,
        &host,
        &names,
        &destinations,
        start_ms,
        duration_ms,
        started,
    ) {
        Ok(result) => Ok(result),
        Err(reason) => Ok(
            json!({"ok":false,"code":if started.elapsed() >= audio_input::DEADLINE {"analysis_timeout"} else {"analysis_failed"},
            "reason":reason,"synthesizedSpeech":false,"renderedAudio":false,"absolutePathsOmitted":true}),
        ),
    }
}

fn execute(
    scratch: &Scratch,
    repo: Option<&Path>,
    args: &Value,
    host: &Path,
    names: &[String; 3],
    destinations: &[PathBuf],
    start_ms: u64,
    duration_ms: Option<u64>,
    started: Instant,
) -> Result<Value, String> {
    let work = mastering::StagingDir::create(&scratch.dir)?;
    let source = audio_input::prepare(
        &args["source"],
        scratch,
        repo,
        &work.path,
        0,
        24000,
        started,
    )?;
    let first = start_ms * 24;
    let available = source
        .wav
        .frames
        .checked_sub(first)
        .filter(|n| *n > 0)
        .ok_or("Analysis starts beyond the source")?;
    let count = duration_ms.map(|ms| ms * 24).unwrap_or(available);
    if count > 60 * 24000 || count > available {
        return Err("Analysis must fit the source and 60-second window; specify durationMs explicitly for longer audio".into());
    }
    let window = work.path.join("analysis-window.wav");
    let mut trim = audio_input::command()?;
    trim.args(["-protocol_whitelist", "file,pipe", "-f", "wav", "-i"])
        .arg(&source.path)
        .args([
            "-af",
            &format!(
                "atrim=start_sample={first}:end_sample={},asetpts=PTS-STARTPTS",
                first + count
            ),
            "-map_metadata",
            "-1",
            "-c:a",
            "pcm_f32le",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-f",
            "wav",
        ])
        .arg(&window);
    audio_input::run(&mut trim, started)?;
    let window_bytes = mastering::read_bounded(&window, 24 * 60000 * 8 + 65536)?;
    let info = mix_render::wav(&window_bytes)?;
    if info.frames != count || info.channels != source.wav.channels {
        return Err("Analysis window duration or channels changed unexpectedly".into());
    }
    let window_hash = asset::sha256_hex(&window_bytes);
    drop(window_bytes);
    let staged: Vec<PathBuf> = names.iter().map(|name| work.path.join(name)).collect();
    let mut command = std::process::Command::new(host);
    for key in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GEN_AUDIO_MCP_TOKEN",
        "GEN_AUDIO_MCP_BOOT_NONCE",
    ] {
        command.env_remove(key);
    }
    command
        .arg("bitcube")
        .arg("--input")
        .arg(&window)
        .arg("--output")
        .arg(&staged[0])
        .arg("--png")
        .arg(&staged[1])
        .arg("--spectrogram")
        .arg(&staged[2])
        .arg("--stem")
        .arg(names[0].trim_end_matches(".json"));
    audio_input::run(&mut command, started)?;
    let mut doc: Value =
        serde_json::from_slice(&mastering::read_bounded(&staged[0], 4 * 1024 * 1024)?)
            .map_err(|_| "Native bitcube returned invalid JSON")?;
    if doc["source_sha256"] != window_hash
        || doc["sample_rate"] != 24000
        || doc["cube_revision"] != 4
        || doc["analysis"]["channels"].as_array().map(Vec::len) != Some(info.channels as usize)
    {
        return Err("Native bitcube source/rate/channel provenance did not match its input".into());
    }
    let cube = mastering::read_bounded(&staged[1], 8 * 1024 * 1024)?;
    let spectrogram = mastering::read_bounded(&staged[2], 8 * 1024 * 1024)?;
    for (bytes, height) in [(&cube, 900u32), (&spectrogram, 760u32)] {
        if bytes.len() < 33
            || &bytes[..8] != b"\x89PNG\r\n\x1a\n"
            || &bytes[12..16] != b"IHDR"
            || bytes[16..20] != 1200u32.to_be_bytes()
            || bytes[20..24] != height.to_be_bytes()
        {
            return Err("Native analysis did not produce the requested PNG images".into());
        }
    }
    doc["source"] = args["source"].clone();
    doc["source_sha256"] = source.provenance["sha256"].clone();
    doc["analysis_wav_sha256"] = json!(window_hash);
    doc["provenance"]["input"] = source.provenance;
    doc["provenance"]["window"] = json!({"startMs":start_ms,"frames":count,"sampleRate":24000,
        "channelsPreserved":true,"analysisDownmixOnly":true,"speakerAttributionInferred":false});
    doc["artifacts"] = json!({"cubePng":names[1],"spectrogramPng":names[2],
        "cubeSha256":asset::sha256_hex(&cube),"spectrogramSha256":asset::sha256_hex(&spectrogram)});
    let document =
        serde_json::to_vec_pretty(&doc).map_err(|_| "Could not encode analysis provenance")?;
    if document.len() > 4 * 1024 * 1024 {
        return Err("Bitcube JSON exceeds 4 MiB".into());
    }
    // Replace only the host-owned staging document, never a caller file.
    std::fs::remove_file(&staged[0]).map_err(|_| "Could not finalize native provenance")?;
    mastering::write_new(&staged[0], &document)?;
    audio_input::remaining(started)?;
    mastering::publish_files(
        scratch,
        &[
            (&staged[0], &destinations[0], &document),
            (&staged[1], &destinations[1], &cube),
            (&staged[2], &destinations[2], &spectrogram),
        ],
    )?;
    let artifacts: Vec<Value> = [
        (&document, "application/json"),
        (&cube, "image/png"),
        (&spectrogram, "image/png"),
    ]
    .iter()
    .enumerate()
    .map(|(index, (bytes, mime))| {
        json!({"name":names[index],"mime":mime,
            "bytes":bytes.len(),"sha256":asset::sha256_hex(bytes)})
    })
    .collect();
    Ok(
        json!({"ok":true,"analysisOnly":true,"synthesizedSpeech":false,"renderedAudio":false,
        "absolutePathsOmitted":true,"sourceSha256":doc["source_sha256"],"analysisWavSha256":window_hash,
        "analysisRate":24000,"sourceChannels":info.channels,"frames":count,
        "artifacts":artifacts,"analysis":doc["analysis"],"layers":doc["layers"],
        "note":"Native F# 3D time/frequency/layer projection and spectrogram; metrics are heuristic, not speaker attribution or calibrated loudness"}),
    )
}
