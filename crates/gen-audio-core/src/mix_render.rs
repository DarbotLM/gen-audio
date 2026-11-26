//! Bounded WAV rendering of validated library layers. This is an unregistered
//! processing artifact, not a synthesized speech or ga1 mix asset.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::{asset, asset_catalog, library_store, mastering, mix, paths};
use mastering::process;
use paths::Scratch;

const MAX_BYTES: usize = 256 * 1024 * 1024;
const DEADLINE: Duration = Duration::from_secs(120);
static ACTIVE: AtomicUsize = AtomicUsize::new(0);

struct Permit;
impl Drop for Permit {
    fn drop(&mut self) {
        ACTIVE.fetch_sub(1, Ordering::AcqRel);
    }
}

#[derive(Clone, Copy)]
struct Wav {
    rate: u64,
    channels: u64,
    frames: u64,
    format: u16,
    bits: u16,
    data_start: usize,
    data_len: usize,
}

fn wav(bytes: &[u8]) -> Result<Wav, String> {
    let bad = || "Malformed or unsupported WAV container".to_string();
    let u16_at = |offset| u16::from_le_bytes(bytes[offset..offset + 2].try_into().unwrap());
    let u32_at = |offset| u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
    if bytes.len() < 12
        || &bytes[..4] != b"RIFF"
        || &bytes[8..12] != b"WAVE"
        || u32_at(4) as usize + 8 != bytes.len()
    {
        return Err(bad());
    }
    let mut position = 12;
    let mut format = None;
    let mut data = None;
    while position < bytes.len() {
        if bytes.len() - position < 8 {
            return Err(bad());
        }
        let size = u32_at(position + 4) as usize;
        let body = position + 8;
        let end = body
            .checked_add(size)
            .filter(|n| *n <= bytes.len())
            .ok_or_else(bad)?;
        match &bytes[position..position + 4] {
            b"fmt " => {
                if format.is_some() || size < 16 {
                    return Err(bad());
                }
                let mut code = u16_at(body);
                if code == 0xfffe {
                    if size < 40
                        || u16_at(body + 16) != 22
                        || u16_at(body + 18) != u16_at(body + 14)
                        || bytes[body + 28..body + 40]
                            != [0, 0, 16, 0, 128, 0, 0, 170, 0, 56, 155, 113]
                    {
                        return Err(bad());
                    }
                    code = u32_at(body + 24).try_into().map_err(|_| bad())?;
                    let mask = u32_at(body + 20);
                    if mask != 0 && mask != if u16_at(body + 2) == 1 { 4 } else { 3 } {
                        return Err(bad());
                    }
                }
                format = Some((
                    code,
                    u16_at(body + 2),
                    u32_at(body + 4),
                    u32_at(body + 8),
                    u16_at(body + 12),
                    u16_at(body + 14),
                ));
            }
            b"data" => {
                if data.replace((body, size)).is_some() {
                    return Err(bad());
                }
            }
            _ => {}
        }
        position = end
            .checked_add(size & 1)
            .filter(|n| *n <= bytes.len())
            .ok_or_else(bad)?;
    }
    let (format, channels, rate, byte_rate, align, bits) = format.ok_or_else(bad)?;
    let (data_start, data_len) = data.ok_or_else(bad)?;
    if !matches!((format, bits), (1, 16) | (3, 32))
        || !(1..=2).contains(&channels)
        || ![16000, 24000, 44100, 48000].contains(&rate)
        || align != channels * (bits / 8)
        || byte_rate != rate * u32::from(align)
        || data_len == 0
        || data_len % usize::from(align) != 0
    {
        return Err(bad());
    }
    Ok(Wav {
        rate: rate.into(),
        channels: channels.into(),
        frames: (data_len / usize::from(align)) as u64,
        format,
        bits,
        data_start,
        data_len,
    })
}

fn remaining(start: Instant) -> Result<Duration, String> {
    DEADLINE
        .checked_sub(start.elapsed())
        .filter(|time| !time.is_zero())
        .ok_or_else(|| "Mix render exceeded its 120-second deadline".into())
}

fn refusal(code: &str, reason: &str) -> Value {
    json!({"ok":false,"code":code,"reason":reason,"renderedAudio":false,
        "synthesizedSpeech":false,"absolutePathsOmitted":true})
}

fn frames(value: &Value, key: &str, rate: u64) -> u64 {
    (value[key].as_u64().expect("normalized milliseconds") * rate + 500) / 1000
}

fn gain(value: &Value) -> f64 {
    10f64.powf(value["gain_mdb"].as_i64().expect("normalized gain") as f64 / 20000.0)
}

fn fades(value: &Value, length: u64, rate: u64) -> Result<String, String> {
    let fade_in = frames(value, "fade_in_ms", rate);
    let fade_out = frames(value, "fade_out_ms", rate);
    if fade_in + fade_out > length {
        return Err(
            "Fades must fit without overlapping within the audible layer or project span".into(),
        );
    }
    let mut filters = String::new();
    if fade_in > 0 {
        filters += &format!(",afade=t=in:ss=0:ns={fade_in}:curve=tri");
    }
    if fade_out > 0 {
        filters += &format!(
            ",afade=t=out:ss={}:ns={fade_out}:curve=tri",
            length - fade_out
        );
    }
    Ok(filters)
}

fn confined(root: &Path, relative: &str) -> Result<PathBuf, String> {
    asset::check_media_path(relative).map_err(|_| "Invalid library media path")?;
    let root = root
        .canonicalize()
        .map_err(|_| "Library media root is unavailable")?;
    let mut path = root.clone();
    for part in Path::new(relative).components() {
        path.push(part);
        let meta = fs::symlink_metadata(&path).map_err(|_| "Library source media is missing")?;
        if meta.file_type().is_symlink() {
            return Err("Library source symlinks are not accepted".into());
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if meta.file_attributes() & 0x400 != 0 {
                return Err("Library source reparse points are not accepted".into());
            }
        }
    }
    let path = path
        .canonicalize()
        .map_err(|_| "Library source media is missing")?;
    if !path.starts_with(root) || !path.is_file() {
        return Err("Library source escaped its root".into());
    }
    Ok(path)
}

struct Source {
    path: PathBuf,
    media: Value,
    envelope: Value,
    wav: Wav,
}

fn verified_bytes(source: &Source) -> Result<Vec<u8>, String> {
    let bytes = mastering::read_bounded(&source.path, MAX_BYTES)?;
    if bytes.len() as u64 != source.media["bytes"].as_u64().unwrap_or(0)
        || asset::sha256_hex(&bytes) != source.media["sha256"].as_str().unwrap_or("")
    {
        return Err("Library source bytes or SHA256 do not match the asset envelope".into());
    }
    Ok(bytes)
}

/// Render a synchronous, stereo float-WAV mix; unsupported processing refuses
/// before staging any output. All source files, including muted ones, are verified.
pub fn render(
    scratch: &Scratch,
    repo: Option<&Path>,
    args: &Value,
) -> Result<Value, (i32, String)> {
    let validation = mix::validate(&args["project"])?;
    if validation["ok"] != true {
        return Ok(validation);
    }
    let project = &validation["normalized"];
    let output = args["output"]
        .as_str()
        .ok_or((-32602, "output must be a WAV basename".into()))?;
    if output.len() > 128
        || !output.ends_with(".wav")
        || mastering::is_windows_device_name(output.trim_end_matches(".wav"))
    {
        return Err((
            -32602,
            "output must be a non-device WAV basename of at most 128 characters".into(),
        ));
    }
    let report_name = format!("{}.json", output.trim_end_matches(".wav"));
    let destination =
        mastering::destination_paths(scratch, output, &report_name).map_err(|_| {
            (
                -32602,
                "Output names must be confined, unused scratch basenames".into(),
            )
        })?;
    if project["channels"] != 2
        || project["master"]["limiter"] != false
        || project["master"].get("target_lufs_milli").is_some()
        || project["master"]["true_peak_mdbtp"] != -1000
        || project["layers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|layer| layer.get("duck").is_some() || layer["loop"]["mode"] != "off")
    {
        return Ok(refusal("not_configured", "This WAV mode requires stereo output, limiter:false, no loudness target, default inactive true-peak ceiling, loop:off and no ducking"));
    }
    if ACTIVE
        .try_update(Ordering::AcqRel, Ordering::Acquire, |n| {
            (n < 2).then_some(n + 1)
        })
        .is_err()
    {
        return Ok(refusal(
            "limit_exceeded",
            "Two WAV mixes are already active; no job was queued",
        ));
    }
    let _permit = Permit;
    let start = Instant::now();
    match render_validated(
        scratch,
        repo,
        project,
        &validation["projectSha256"],
        output,
        &report_name,
        &destination,
        start,
    ) {
        Ok(result) => Ok(result),
        Err(reason) => Ok(refusal(
            if start.elapsed() >= DEADLINE {
                "render_timeout"
            } else {
                "render_failed"
            },
            &reason,
        )),
    }
}

fn render_validated(
    scratch: &Scratch,
    repo: Option<&Path>,
    project: &Value,
    project_hash: &Value,
    output: &str,
    report_name: &str,
    destination: &(PathBuf, PathBuf),
    start: Instant,
) -> Result<Value, String> {
    let rate = project["sample_rate_hz"].as_u64().unwrap();
    let length = frames(project, "duration_ms", rate);
    let output_cap = (length * 8 + 65536) as usize;
    if output_cap > MAX_BYTES {
        return Err("Output exceeds 256 MiB".into());
    }
    let master_fades = fades(&project["master"], length, rate)?;
    let layers = project["layers"].as_array().unwrap();
    let mut sources = BTreeMap::new();
    let mut total_bytes = 0u64;
    for layer in layers {
        remaining(start)?;
        let uid = layer["source"].as_str().unwrap();
        if sources.contains_key(uid) {
            continue;
        }
        let envelope =
            asset_catalog::require(uid).map_err(|_| "Source asset could not be revalidated")?;
        if envelope["kind"] != "audio_clip" {
            return Err("Only audio_clip sources are implemented".into());
        }
        let media = envelope["media"]
            .as_array()
            .unwrap()
            .iter()
            .find(|media| media["role"] == "wav")
            .ok_or("Source has no WAV media")?
            .clone();
        let relative = media["path"]
            .as_str()
            .ok_or("Source WAV has no media path")?;
        let root = if relative.starts_with("generated/") {
            library_store::user_library_dir()?
        } else {
            repo.ok_or("Baked library audio requires a checkout with staged locked WAVs")?
                .join("apps")
                .join("desktop")
                .join("public")
                .join("library")
        };
        let path = confined(&root, relative)?;
        total_bytes = total_bytes
            .checked_add(media["bytes"].as_u64().ok_or("Invalid source size")?)
            .filter(|total| *total <= MAX_BYTES as u64)
            .ok_or("Combined source WAVs exceed 256 MiB")?;
        let bytes = mastering::read_bounded(&path, MAX_BYTES)?;
        if bytes.len() as u64 != media["bytes"].as_u64().unwrap()
            || asset::sha256_hex(&bytes) != media["sha256"].as_str().unwrap()
        {
            return Err("Library source bytes or SHA256 do not match the asset envelope".into());
        }
        let info = wav(&bytes)?;
        if info.format != 1
            || info.bits != 16
            || info.rate != rate
            || envelope["fields"]["sample_rate_hz"] != info.rate
            || envelope["fields"]["channels"] != info.channels
            || envelope["fields"]["duration_ms"] != asset::ms_from_frames(info.frames, info.rate)
        {
            return Err("Sources must be PCM16 WAVs at the project rate with matching catalog duration/channels; resampling is not implemented".into());
        }
        sources.insert(
            uid.to_owned(),
            Source {
                path,
                media,
                envelope,
                wav: info,
            },
        );
    }
    let any_solo = layers.iter().any(|layer| layer["solo"] == true);
    let mut graph = String::new();
    let mut inputs = Vec::new();
    let mut audible = Vec::new();
    for layer in layers {
        let uid = layer["source"].as_str().unwrap();
        let source = &sources[uid];
        let trim_start = frames(layer, "trim_start_ms", rate);
        let trim_end = frames(layer, "trim_end_ms", rate);
        let available = source
            .wav
            .frames
            .checked_sub(trim_start + trim_end)
            .filter(|n| *n > 0)
            .ok_or("Rounded trims leave no source frames")?;
        let offset = frames(layer, "offset_ms", rate);
        let span = available.min(length.saturating_sub(offset));
        // Even muted options are checked; toggling mute must not conceal an
        // unsupported fade span or source format.
        let layer_fades = fades(layer, span, rate)?;
        if layer["mute"] == true
            || (any_solo && layer["solo"] != true && layer["solo_safe"] != true)
            || span == 0
        {
            continue;
        }
        let index = inputs.len();
        inputs.push(uid.to_owned());
        let theta =
            (layer["pan_milli"].as_i64().unwrap() as f64 + 1000.0) * std::f64::consts::PI / 4000.0;
        let (mut left, mut right) = (theta.cos(), theta.sin());
        if source.wav.channels == 2 {
            left = (left * 2f64.sqrt()).min(1.0);
            right = (right * 2f64.sqrt()).min(1.0);
        }
        let right_channel = if source.wav.channels == 1 { 0 } else { 1 };
        graph += &format!("[{index}:a]atrim=start_sample={trim_start}:end_sample={},asetpts=PTS-STARTPTS{layer_fades},volume={:.17}:precision=double,pan=stereo|c0={left:.17}*c0|c1={right:.17}*c{right_channel},adelay={offset}S:all=1,apad=whole_len={length},atrim=end_sample={length}[l{index}];", trim_start + span, gain(layer));
        audible.push(
            json!({"id":layer["id"],"source":uid,"startFrame":offset,"sourceStartFrame":trim_start,
            "frames":span,"leftGain":left * gain(layer),"rightGain":right * gain(layer)}),
        );
    }
    if inputs.is_empty() {
        graph += &format!("anullsrc=r={rate}:cl=stereo,atrim=end_sample={length}[sum];");
    } else {
        for index in 0..inputs.len() {
            graph += &format!("[l{index}]");
        }
        graph += &format!(
            "amix=inputs={}:normalize=0:dropout_transition=0[sum];",
            inputs.len()
        );
    }
    graph += &format!("[sum]volume={:.17}:precision=double{master_fades},aformat=sample_fmts=flt:channel_layouts=stereo[out]", gain(&project["master"]));
    remaining(start)?;
    let work = mastering::StagingDir::create(&scratch.dir)?;
    let mut snapshots = BTreeMap::new();
    for (index, (uid, source)) in sources.iter().enumerate() {
        remaining(start)?;
        let snapshot = work.path.join(format!("source-{index}.wav"));
        mastering::write_new(&snapshot, &verified_bytes(source)?)?;
        snapshots.insert(uid.clone(), snapshot);
    }
    let program = mastering::ffmpeg_program()?;
    let mut command = Command::new(program);
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
        "FFREPORT",
    ] {
        command.env_remove(key);
    }
    command.args([
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-n",
        "-filter_complex_threads",
        "1",
    ]);
    for uid in &inputs {
        command
            .args([
                "-threads",
                "1",
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "wav",
                "-i",
            ])
            .arg(&snapshots[uid]);
    }
    let staged_wav = work.path.join("render.wav");
    command
        .args([
            "-filter_complex",
            &graph,
            "-map",
            "[out]",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-c:a",
            "pcm_f32le",
            "-ar",
            &rate.to_string(),
            "-ac",
            "2",
            "-threads",
            "1",
            "-f",
            "wav",
            "-fs",
            &output_cap.to_string(),
        ])
        .arg(&staged_wav);
    let run = process::run(
        &mut command,
        process::Limits {
            deadline: remaining(start)?,
            stdout: 1024,
            stderr: 64 * 1024,
        },
    )?;
    if !run.status.success() {
        return Err("FFmpeg mixing failed; check GEN_AUDIO_FFMPEG and filter support".into());
    }
    let bytes = mastering::read_bounded(&staged_wav, output_cap)?;
    let info = wav(&bytes)?;
    if info.format != 3
        || info.bits != 32
        || info.rate != rate
        || info.channels != 2
        || info.frames != length
    {
        return Err("Rendered WAV format or frame count failed verification".into());
    }
    let mut peak = 0.0f32;
    let mut energy = 0.0f64;
    for block in bytes[info.data_start..info.data_start + info.data_len].chunks(65536) {
        remaining(start)?;
        for raw in block.chunks_exact(4) {
            let value = f32::from_le_bytes(raw.try_into().unwrap());
            if !value.is_finite() || value.abs() > 1.0 {
                return Err("Mix exceeds full scale or has non-finite PCM; lower gains (limiting is not implemented)".into());
            }
            peak = peak.max(value.abs());
            energy += f64::from(value).powi(2);
        }
    }
    let provenance: Vec<Value> = sources
        .iter()
        .map(|(uid, source)| {
            json!({
                "uid":uid,"sha256":source.media["sha256"],"bytes":source.media["bytes"],
                "honesty":source.envelope["honesty"]
            })
        })
        .collect();
    let result = json!({"ok":true,"renderedAudio":true,"processedAudio":true,"verifiedAudio":true,
        "synthesizedSpeech":false,"absolutePathsOmitted":true,"wav":output,"manifest":report_name,
        "format":"wav-f32","sampleRate":rate,"channels":2,"frames":length,
        "durationMs":asset::ms_from_frames(length, rate),"sha256":asset::sha256_hex(&bytes),
        "bytes":bytes.len(),"projectSha256":project_hash,"samplePeak":peak,"rms":(energy / (length * 2) as f64).sqrt(),
        "fixture":sources.values().any(|s| s.envelope["honesty"]["fixture"] == true),
        "containsSynthesizedSpeech":inputs.iter().any(|uid| sources[uid].envelope["honesty"]["synthesized_speech"] == true),
        "assetMinted":false,"libraryImported":false,
        "provenance":{"schema":"wingen-audio-mix-wav-1","renderer":"ffmpeg-native-f32-v1",
            "project":project,"sources":provenance,"audibleLayers":audible,
            "filterGraphSha256":asset::sha256_hex(graph.as_bytes()),
            "sourceHashesVerified":true,"loudnessMeasured":false,"truePeakMeasured":false},
        "limitations":["No ga1 mix asset or library import","No resampling, ducking, loops, loudness normalization, limiter, jobs, cancellation, or Teams rendition"]});
    let report_bytes =
        serde_json::to_vec_pretty(&result).map_err(|_| "Could not encode mix provenance")?;
    if report_bytes.len() > 64 * 1024 {
        return Err("Mix provenance exceeds 64 KiB".into());
    }
    let staged_report = work.path.join("report.json");
    mastering::write_new(&staged_report, &report_bytes)?;
    remaining(start)?;
    mastering::publish(
        scratch,
        &staged_wav,
        &staged_report,
        destination,
        &bytes,
        &report_bytes,
    )?;
    Ok(result)
}
