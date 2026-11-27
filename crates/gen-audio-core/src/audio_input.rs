//! One authorized, hash-checked audio boundary for mixing and native analysis.
//! Decoder input is a private snapshot; neither tool accepts arbitrary paths.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::{asset, asset_catalog, library_store, mastering, mix_render, paths::Scratch};
use mastering::process;

pub(crate) const MAX_BYTES: usize = 256 * 1024 * 1024;
pub(crate) const DEADLINE: Duration = Duration::from_secs(120);

pub(crate) struct Prepared {
    pub path: PathBuf,
    pub wav: mix_render::Wav,
    pub provenance: Value,
    pub input_bytes: usize,
    pub decoded_bytes: usize,
}

pub(crate) fn remaining(start: Instant) -> Result<Duration, String> {
    DEADLINE
        .checked_sub(start.elapsed())
        .filter(|d| !d.is_zero())
        .ok_or_else(|| "Audio operation exceeded its 120-second deadline".into())
}

pub(crate) fn command() -> Result<Command, String> {
    let mut command = Command::new(mastering::ffmpeg_program()?);
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
        "FFREPORT_FILE",
    ] {
        command.env_remove(key);
    }
    command.args([
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-v",
        "error",
        "-n",
        "-threads",
        "1",
    ]);
    Ok(command)
}

pub(crate) fn run(command: &mut Command, start: Instant) -> Result<Vec<u8>, String> {
    let output = process::run(
        command,
        process::Limits {
            deadline: remaining(start)?,
            stdout: 65536,
            stderr: 65536,
        },
    )?;
    if !output.status.success() {
        return Err("Native audio command failed; verify local decoder/encoder availability and source format".into());
    }
    Ok(output._stdout)
}

fn demuxer(extension: &str) -> Result<&'static str, String> {
    match extension {
        "wav" => Ok("wav"),
        "mp3" => Ok("mp3"),
        "flac" => Ok("flac"),
        "ogg" | "opus" => Ok("ogg"),
        "m4a" => Ok("mov"),
        "aac" => Ok("aac"),
        _ => Err("Audio input must be WAV, MP3, FLAC, OGG, Opus, M4A, or AAC".into()),
    }
}

fn decoder_options(command: &mut Command, format: &str) {
    command.args(["-protocol_whitelist", "file,pipe", "-f", format]);
    if format == "mov" {
        command.args(["-enable_drefs", "0", "-use_absolute_path", "0"]);
    }
}

pub(crate) fn prepare(
    reference: &Value,
    scratch: &Scratch,
    repo: Option<&Path>,
    work: &Path,
    index: usize,
    rate: u64,
    start: Instant,
) -> Result<Prepared, String> {
    let (path, expected_sha, expected_bytes, honesty, envelope) =
        if let Some(uid) = reference.as_str() {
            let envelope =
                asset_catalog::require(uid).map_err(|_| "Source asset could not be validated")?;
            if envelope["kind"] != "audio_clip" {
                return Err("Only audio_clip catalog sources are implemented".into());
            }
            let media = envelope["media"]
                .as_array()
                .ok_or("Missing asset media")?
                .iter()
                .find(|item| item["role"] == "wav")
                .ok_or("Asset has no WAV")?;
            let relative = media["path"].as_str().ok_or("Invalid asset media path")?;
            let root = if relative.starts_with("generated/") {
                library_store::user_library_dir()?
            } else {
                repo.ok_or("Baked source needs the staged checkout library")?
                    .join("apps")
                    .join("desktop")
                    .join("public")
                    .join("library")
            };
            (
                mix_render::confined(&root, relative)?,
                media["sha256"].as_str().unwrap_or("").to_owned(),
                media["bytes"].as_u64(),
                envelope["honesty"].clone(),
                Some(envelope),
            )
        } else {
            let sha = reference["sha256"]
                .as_str()
                .filter(|s| asset::is_sha256_hex(s))
                .ok_or("File source requires lowercase SHA256")?
                .to_owned();
            let path = if let Some(issued) = reference["issued"].as_str() {
                scratch.open_input(issued)?
            } else {
                let relative = reference["path"]
                    .as_str()
                    .ok_or("Source requires path or issued")?;
                let root = std::env::var_os("GEN_AUDIO_INPUT_ROOT")
                    .filter(|p| !p.is_empty())
                    .ok_or("GEN_AUDIO_INPUT_ROOT is not configured")?;
                let root = PathBuf::from(root);
                if !root.is_absolute() {
                    return Err(
                        "GEN_AUDIO_INPUT_ROOT must be an absolute operator-owned directory".into(),
                    );
                }
                mix_render::confined(&root, relative)?
            };
            (
                path,
                sha,
                None,
                json!({"classification":"caller_supplied","speechAttributionVerified":false}),
                None,
            )
        };
    let extension = path
        .extension()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let format = demuxer(&extension)?;
    remaining(start)?;
    let bytes = mastering::read_bounded(&path, MAX_BYTES)?;
    if expected_bytes.is_some_and(|count| count != bytes.len() as u64)
        || asset::sha256_hex(&bytes) != expected_sha
    {
        return Err("Audio source bytes or SHA256 do not match the supplied identity".into());
    }
    if let Some(envelope) = &envelope {
        let original = mix_render::wav(&bytes)?;
        if envelope["fields"]["sample_rate_hz"] != original.rate
            || envelope["fields"]["channels"] != original.channels
            || envelope["fields"]["duration_ms"]
                != asset::ms_from_frames(original.frames, original.rate)
        {
            return Err("Catalog metadata does not match its WAV header".into());
        }
    }
    let input_bytes = bytes.len();
    let snapshot = work.join(format!("input-{index}.{extension}"));
    mastering::write_new(&snapshot, &bytes)?;
    drop(bytes);
    let ffmpeg = PathBuf::from(mastering::ffmpeg_program()?);
    let probe = if ffmpeg.is_absolute() {
        ffmpeg.with_file_name(if cfg!(windows) {
            "ffprobe.exe"
        } else {
            "ffprobe"
        })
    } else {
        PathBuf::from("ffprobe")
    };
    let mut probe = Command::new(probe);
    probe.args([
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,duration:format=duration,format_name",
        "-of",
        "json",
    ]);
    decoder_options(&mut probe, format);
    probe.arg(&snapshot);
    let info: Value = serde_json::from_slice(&run(&mut probe, start)?)
        .map_err(|_| "Decoder probe returned invalid metadata")?;
    let stream = info["streams"]
        .as_array()
        .and_then(|a| a.first())
        .ok_or("No audio stream found")?;
    let channels = stream["channels"]
        .as_u64()
        .filter(|n| (1..=2).contains(n))
        .ok_or("Only mono/stereo sources are supported")?;
    let input_rate = stream["sample_rate"]
        .as_str()
        .and_then(|s| s.parse::<u64>().ok())
        .filter(|n| (8000..=192000).contains(n))
        .ok_or("Source rate must be 8000..192000 Hz")?;
    for value in [&stream["duration"], &info["format"]["duration"]] {
        if let Some(duration) = value.as_str().filter(|s| *s != "N/A") {
            let duration = duration
                .parse::<f64>()
                .map_err(|_| "Invalid source duration")?;
            if !duration.is_finite() || duration <= 0.0 || duration > 600.1 {
                return Err("Source duration exceeds 600 seconds".into());
            }
        }
    }
    let destination = work.join(format!("decoded-{index}.wav"));
    let cap = (rate * channels * 4 * 600 + 65536) as usize;
    let mut decode = command()?;
    decoder_options(&mut decode, format);
    decode
        .arg("-i")
        .arg(&snapshot)
        .args([
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-af",
            "aresample=resampler=swr:filter_size=32:phase_shift=10:exact_rational=1",
            "-ar",
            &rate.to_string(),
            "-c:a",
            "pcm_f32le",
            "-t",
            "600.001",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-f",
            "wav",
            "-fs",
            &cap.to_string(),
        ])
        .arg(&destination);
    run(&mut decode, start)?;
    let decoded = mastering::read_bounded(&destination, cap)?;
    let wav = mix_render::wav(&decoded)?;
    if wav.rate != rate || wav.channels != channels || wav.format != 3 || wav.frames > rate * 600 {
        return Err("Decoded audio violates rate, channel, format or duration bounds".into());
    }
    for block in decoded[wav.data_start..wav.data_start + wav.data_len].chunks(65536) {
        remaining(start)?;
        if block
            .chunks_exact(4)
            .any(|v| !f32::from_le_bytes(v.try_into().unwrap()).is_finite())
        {
            return Err("Decoded PCM contains non-finite samples".into());
        }
    }
    let provenance = json!({"source":reference,"sha256":expected_sha,"bytes":input_bytes,
        "honesty":honesty,"codec":stream["codec_name"],"inputRate":input_rate,"channels":channels,
        "decodedRate":rate,"decodedFrames":wav.frames,"decodedSha256":asset::sha256_hex(&decoded),
        "resampled":input_rate != rate,"resampler":"ffmpeg-swr-filter32-phase10"});
    Ok(Prepared {
        path: destination,
        wav,
        provenance,
        input_bytes,
        decoded_bytes: decoded.len(),
    })
}
