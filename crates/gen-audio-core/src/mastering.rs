//! Fixed-profile mastering of issued audio, not a speech-synthesis adapter.
//!
//! Inputs and outputs are scratch basenames. FFmpeg is executed directly with
//! fixed filters, bounded time and bounded output capture. The original audio
//! is never changed, and a successful result is evidence of audio processing,
//! not evidence that any speech was synthesized.

pub(crate) mod process;

use std::ffi::{OsStr, OsString};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::{json, Value};

use crate::asset::sha256_hex;
use crate::paths::{self, Scratch};

const SAMPLE_RATE: u32 = 24_000;
const MAX_SECONDS: u64 = 600;
const MAX_SAMPLES: u64 = SAMPLE_RATE as u64 * MAX_SECONDS;
const MAX_WAV_BYTES: usize = MAX_SAMPLES as usize * 2 + 65_536;
const MAX_REPORT_BYTES: usize = 64 * 1024;
const DEADLINE: Duration = Duration::from_secs(120);
const TARGET_I: f64 = -16.0;
const TARGET_TP: f64 = -1.5;
const TARGET_LRA: f64 = 4.0;
const I_TOLERANCE: f64 = 1.0;
const TP_TOLERANCE: f64 = 0.2;
const LRA_TOLERANCE: f64 = 1.0;
const CONDITIONING: &str = "highpass=f=60,lowpass=f=11000,\
    acompressor=threshold=-24dB:ratio=2.5:attack=15:release=180:makeup=2";
const ANALYSIS: &str = "loudnorm=I=-16:TP=-1.5:LRA=4:print_format=json";

#[derive(Clone, Debug, Serialize, PartialEq)]
struct Measurement {
    integrated_lufs: f64,
    true_peak_dbtp: f64,
    loudness_range_lu: f64,
    threshold_lufs: f64,
    target_offset_lu: f64,
}

#[derive(Debug)]
struct Loudnorm {
    input: Measurement,
    normalization_type: String,
}

#[derive(Debug, PartialEq, Eq)]
struct WavInfo {
    rate: u32,
    samples: u64,
}

struct Request {
    input: String,
    output: String,
}

impl Request {
    fn parse(scratch: &Scratch, args: &Value) -> Result<Self, String> {
        let object = args
            .as_object()
            .ok_or("mastering arguments must be an object")?;
        if object
            .keys()
            .any(|key| !matches!(key.as_str(), "input" | "output"))
        {
            return Err("mastering accepts only input and optional output WAV names".into());
        }
        let input = object
            .get("input")
            .and_then(Value::as_str)
            .ok_or("mastering input must be an issued WAV filename")?;
        let output = match object.get("output") {
            None => next_output_name(),
            Some(Value::String(name)) => name.clone(),
            _ => return Err("mastering output must be a WAV filename".into()),
        };
        for name in [input, output.as_str()] {
            if name.len() > 180 {
                return Err("mastering WAV names must be at most 180 bytes".into());
            }
            paths::write_name(&scratch.dir, name)?;
            if !name.ends_with(".wav") {
                return Err("mastering input and output must end in .wav".into());
            }
            let stem = name.strip_suffix(".wav").unwrap_or(name);
            if is_windows_device_name(stem) {
                return Err("mastering WAV name is a reserved device name".into());
            }
        }
        if input.eq_ignore_ascii_case(&output) {
            return Err("mastering never overwrites its input; choose a unique output".into());
        }
        Ok(Self {
            input: input.into(),
            output,
        })
    }
}

static NEXT_ARTIFACT: AtomicU64 = AtomicU64::new(1);

fn next_output_name() -> String {
    format!(
        "master-{}-{}.wav",
        std::process::id(),
        NEXT_ARTIFACT.fetch_add(1, Ordering::Relaxed)
    )
}

pub(crate) fn is_windows_device_name(stem: &str) -> bool {
    let base = stem
        .split('.')
        .next()
        .unwrap_or(stem)
        .trim_end_matches([' ', '.'])
        .to_ascii_uppercase();
    matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || base
            .strip_prefix("COM")
            .or_else(|| base.strip_prefix("LPT"))
            .is_some_and(|suffix| {
                suffix.len() == 1
                    && suffix
                        .as_bytes()
                        .first()
                        .is_some_and(|digit| (b'1'..=b'9').contains(digit))
            })
}

/// Master one non-silent mono 24 kHz PCM16 WAV, at most 600 seconds long.
///
/// A successful result persists a new WAV and JSON report. It means verified
/// audio processing only; `synthesizedSpeech` is always false.
pub fn master(scratch: &Scratch, args: &Value) -> Result<Value, String> {
    master_with_program(scratch, args, &ffmpeg_program()?)
}

pub(crate) fn ffmpeg_program() -> Result<OsString, String> {
    match std::env::var_os("GEN_AUDIO_FFMPEG") {
        None => Ok("ffmpeg".into()),
        Some(program) => {
            let path = Path::new(&program);
            if program.is_empty()
                || (!path.is_absolute()
                    && (path.components().count() != 1
                        || program.to_string_lossy().contains(['/', '\\', ':'])))
            {
                return Err(
                    "GEN_AUDIO_FFMPEG must be an absolute executable path or PATH name".into(),
                );
            }
            Ok(program)
        }
    }
}

fn master_with_program(scratch: &Scratch, args: &Value, program: &OsStr) -> Result<Value, String> {
    let start = Instant::now();
    let request = Request::parse(scratch, args)?;
    let source = scratch
        .open_input(&request.input)
        .map_err(|_| "mastering input must be an issued scratch WAV".to_string())?;
    let source_bytes = read_bounded(&source, MAX_WAV_BYTES)?;
    let wav = validate_wav(&source_bytes)?;
    let source_hash = sha256_hex(&source_bytes);
    let manifest_name = report_name(&request.output)?;
    let destination = destination_paths(scratch, &request.output, &manifest_name)?;
    let work = StagingDir::create(&scratch.dir)?;
    let snapshot = work.path.join("source.wav");
    write_new(&snapshot, &source_bytes)?;

    let raw = measure(program, &snapshot, ANALYSIS, start, "raw measurement")?;
    let conditioned = measure(
        program,
        &snapshot,
        &format!("{CONDITIONING},{ANALYSIS}"),
        start,
        "conditioned first pass",
    )?;
    let second_filter = second_pass_filter(&conditioned.input);
    let staged_wav = work.path.join("render.wav");
    let mut render = command(program, &snapshot, &second_filter);
    render
        .args([
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "-fs",
        ])
        .arg(MAX_WAV_BYTES.to_string())
        .arg(&staged_wav);
    let rendered =
        parse_loudnorm(&execute(&mut render, start, "measured second pass", 1024)?.stderr)
            .map_err(|error| format!("mastering second pass: {error}"))?;

    let final_bytes = read_bounded(&staged_wav, MAX_WAV_BYTES)?;
    validate_rendered(&final_bytes, wav.samples)?;
    let final_pass = measure(
        program,
        &staged_wav,
        ANALYSIS,
        start,
        "final independent measurement",
    )?;
    validate_loudness(&final_pass.input)?;
    if read_bounded(&staged_wav, MAX_WAV_BYTES)? != final_bytes {
        return Err("mastering output changed during loudness verification".into());
    }
    if read_bounded(
        &scratch
            .open_input(&request.input)
            .map_err(|_| "mastering input changed during processing".to_string())?,
        MAX_WAV_BYTES,
    )? != source_bytes
        || read_bounded(&snapshot, MAX_WAV_BYTES)? != source_bytes
    {
        return Err("mastering input changed during processing; no output published".into());
    }
    remaining(start)?;

    let result = json!({
        "ok": true,
        "processedAudio": true,
        "verifiedAudio": true,
        "synthesizedSpeech": false,
        "wav": request.output,
        "manifest": manifest_name,
        "sample_rate": wav.rate,
        "samples": wav.samples,
        "duration_s": wav.samples as f64 / f64::from(wav.rate),
        "sha256": sha256_hex(&final_bytes),
        "sourceSha256": source_hash,
        "report": {
            "profile": "prototype-master-v1",
            "target": {
                "integrated_lufs": TARGET_I,
                "true_peak_dbtp": TARGET_TP,
                "loudness_range_lu": TARGET_LRA
            },
            "raw": raw.input,
            "conditioned": conditioned.input,
            "final": final_pass.input,
            "normalization_type": rendered.normalization_type,
            "linear_requested": true,
            "chain": [
                {"filter": "highpass", "frequency_hz": 60},
                {"filter": "lowpass", "frequency_hz": 11000},
                {"filter": "acompressor", "threshold_db": -24, "ratio": 2.5,
                 "attack_ms": 15, "release_ms": 180, "makeup_linear": 2},
                {"filter": "loudnorm", "passes": 2}
            ],
            "validation": {
                "independent_final_measurement": true,
                "integrated_lufs_tolerance_lu": I_TOLERANCE,
                "true_peak_max_dbtp": TARGET_TP + TP_TOLERANCE,
                "loudness_range_max_lu": TARGET_LRA + LRA_TOLERANCE,
                "sample_count_preserved": true,
                "speech_alignment_verified": false
            },
            "limits": {
                "max_duration_s": MAX_SECONDS,
                "max_samples": MAX_SAMPLES,
                "max_wav_bytes": MAX_WAV_BYTES,
                "max_report_bytes": MAX_REPORT_BYTES
            }
        }
    });
    let mut report = result.clone();
    report["schema_version"] = json!("1.0");
    report["kind"] = json!("mastered-audio");
    report["source"] = json!({
        "wav": request.input,
        "sha256": source_hash,
        "sample_rate": wav.rate,
        "samples": wav.samples
    });
    let report_bytes =
        serde_json::to_vec_pretty(&report).map_err(|_| "mastering report encoding failed")?;
    if report_bytes.len() > MAX_REPORT_BYTES {
        return Err("mastering report exceeds byte limit".into());
    }
    let staged_report = work.path.join("report.json");
    write_new(&staged_report, &report_bytes)?;
    let persisted_report = read_bounded(&staged_report, MAX_REPORT_BYTES)?;
    if persisted_report != report_bytes
        || serde_json::from_slice::<Value>(&persisted_report)
            .ok()
            .as_ref()
            != Some(&report)
    {
        return Err("mastering report failed persistence verification".into());
    }
    remaining(start)?;
    publish(
        scratch,
        &staged_wav,
        &staged_report,
        &destination,
        &final_bytes,
        &report_bytes,
    )?;
    Ok(result)
}

fn report_name(output: &str) -> Result<String, String> {
    let stem = output
        .strip_suffix(".wav")
        .ok_or("mastering output must end in .wav")?;
    Ok(format!("{stem}.json"))
}

pub(crate) fn destination_paths(
    scratch: &Scratch,
    wav_name: &str,
    report_name: &str,
) -> Result<(PathBuf, PathBuf), String> {
    let wav = paths::write_name(&scratch.dir, wav_name)?;
    let report = paths::write_name(&scratch.dir, report_name)?;
    for path in [&wav, &report] {
        match fs::symlink_metadata(path) {
            Ok(_) => return Err("mastering output names must be unique and unused".into()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err("mastering could not inspect output names".into()),
        }
    }
    Ok((wav, report))
}

fn validate_wav(bytes: &[u8]) -> Result<WavInfo, String> {
    if bytes.len() > MAX_WAV_BYTES {
        return Err("mastering WAV exceeds its byte limit".into());
    }
    let malformed = || {
        "mastering requires a non-silent mono 24000 Hz PCM16 WAV (not empty or malformed)"
            .to_string()
    };
    if bytes.len() < 12 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err(malformed());
    }
    let riff_end = u32::from_le_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]) as u64 + 8;
    if riff_end != bytes.len() as u64 {
        return Err(malformed());
    }

    let mut position = 12usize;
    let mut format = None;
    let mut data = None;
    while position < bytes.len() {
        if bytes.len() - position < 8 {
            return Err(malformed());
        }
        let size = u32::from_le_bytes([
            bytes[position + 4],
            bytes[position + 5],
            bytes[position + 6],
            bytes[position + 7],
        ]) as usize;
        let body = position + 8;
        let end = body
            .checked_add(size)
            .filter(|end| *end <= bytes.len())
            .ok_or_else(malformed)?;
        if &bytes[position..position + 4] == b"fmt " {
            if format.is_some() || size < 16 {
                return Err(malformed());
            }
            let fields = &bytes[body..body + 16];
            let audio_format = u16::from_le_bytes([fields[0], fields[1]]);
            let channels = u16::from_le_bytes([fields[2], fields[3]]);
            let rate = u32::from_le_bytes([fields[4], fields[5], fields[6], fields[7]]);
            let byte_rate = u32::from_le_bytes([fields[8], fields[9], fields[10], fields[11]]);
            let block_align = u16::from_le_bytes([fields[12], fields[13]]);
            let bits = u16::from_le_bytes([fields[14], fields[15]]);
            format = Some((audio_format, channels, rate, byte_rate, block_align, bits));
        } else if &bytes[position..position + 4] == b"data" {
            if data.is_some() {
                return Err(malformed());
            }
            data = Some((body, size));
        }
        position = end
            .checked_add(size & 1)
            .filter(|next| *next <= bytes.len())
            .ok_or_else(malformed)?;
    }

    let (audio_format, channels, rate, byte_rate, block_align, bits) =
        format.ok_or_else(malformed)?;
    let (data_start, data_size) = data.ok_or_else(malformed)?;
    if audio_format != 1
        || channels != 1
        || rate != SAMPLE_RATE
        || byte_rate != SAMPLE_RATE * 2
        || block_align != 2
        || bits != 16
        || data_size == 0
        || data_size % 2 != 0
    {
        return Err(malformed());
    }
    let samples = (data_size / 2) as u64;
    if samples > MAX_SAMPLES {
        return Err("mastering WAV exceeds the 600-second mono 24000 Hz PCM16 limit".into());
    }
    if bytes[data_start..data_start + data_size]
        .chunks_exact(2)
        .all(|sample| sample == [0, 0])
    {
        return Err(malformed());
    }
    Ok(WavInfo { rate, samples })
}

fn validate_rendered(bytes: &[u8], source_samples: u64) -> Result<(), String> {
    let rendered = validate_wav(bytes)?;
    if rendered.samples != source_samples {
        return Err("mastering changed the sample count or produced truncated audio".into());
    }
    Ok(())
}

fn validate_loudness(measured: &Measurement) -> Result<(), String> {
    if ![
        measured.integrated_lufs,
        measured.true_peak_dbtp,
        measured.loudness_range_lu,
        measured.threshold_lufs,
        measured.target_offset_lu,
    ]
    .iter()
    .all(|value| value.is_finite())
    {
        return Err("mastering loudness is non-finite; silence cannot be normalized".into());
    }
    if (measured.integrated_lufs - TARGET_I).abs() > I_TOLERANCE
        || measured.true_peak_dbtp > TARGET_TP + TP_TOLERANCE
        || !(0.0..=(TARGET_LRA + LRA_TOLERANCE)).contains(&measured.loudness_range_lu)
    {
        return Err(format!(
            "mastering final loudness failed verification: {:.2} LUFS, {:.2} dBTP, {:.2} LU LRA",
            measured.integrated_lufs, measured.true_peak_dbtp, measured.loudness_range_lu
        ));
    }
    Ok(())
}

fn second_pass_filter(measured: &Measurement) -> String {
    format!(
        "{CONDITIONING},loudnorm=I=-16:TP=-1.5:LRA=4:measured_I={}:measured_TP={}:\
         measured_LRA={}:measured_thresh={}:offset={}:linear=true:print_format=json",
        measured.integrated_lufs,
        measured.true_peak_dbtp,
        measured.loudness_range_lu,
        measured.threshold_lufs,
        measured.target_offset_lu
    )
}

fn command(program: &OsStr, input: &Path, filter: &str) -> Command {
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
        "FFREPORT_FILE",
    ] {
        command.env_remove(key);
    }
    command.args([
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "info",
        "-n",
        "-filter_threads",
        "1",
        "-threads",
        "1",
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "wav",
        "-i",
    ]);
    command.arg(input).args([
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-threads",
        "1",
        "-af",
        filter,
    ]);
    command
}

fn remaining(start: Instant) -> Result<Duration, String> {
    DEADLINE
        .checked_sub(start.elapsed())
        .filter(|time| !time.is_zero())
        .ok_or_else(|| "mastering exceeded its total 120-second deadline".into())
}

fn execute(
    command: &mut Command,
    start: Instant,
    phase: &str,
    stdout: usize,
) -> Result<process::Output, String> {
    let output = process::run(
        command,
        process::Limits {
            deadline: remaining(start)?,
            stdout,
            stderr: 64 * 1024,
        },
    )
    .map_err(|error| format!("mastering {phase}: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "mastering {phase}: FFmpeg failed (exit {:?}); check GEN_AUDIO_FFMPEG and filter support",
            output.status.code()
        ));
    }
    Ok(output)
}

fn measure(
    program: &OsStr,
    path: &Path,
    filter: &str,
    start: Instant,
    phase: &str,
) -> Result<Loudnorm, String> {
    let mut command = command(program, path, filter);
    command.args(["-f", "null", "-"]);
    parse_loudnorm(&execute(&mut command, start, phase, 1024)?.stderr)
        .map_err(|error| format!("mastering {phase}: {error}"))
}

fn parse_loudnorm(stderr: &[u8]) -> Result<Loudnorm, String> {
    let start = stderr
        .iter()
        .rposition(|byte| *byte == b'{')
        .ok_or("FFmpeg loudnorm did not return measurement JSON")?;
    let end = stderr[start..]
        .iter()
        .position(|byte| *byte == b'}')
        .ok_or("FFmpeg loudnorm returned incomplete measurement JSON")?
        + start;
    let report: Value = serde_json::from_slice(&stderr[start..=end])
        .map_err(|_| "FFmpeg loudnorm returned invalid measurement JSON")?;
    let number = |field: &str, min: f64, max: f64| -> Result<f64, String> {
        let value = match report.get(field) {
            Some(Value::String(text)) => text.parse::<f64>().ok(),
            Some(Value::Number(number)) => number.as_f64(),
            _ => None,
        }
        .ok_or_else(|| format!("FFmpeg loudnorm missing or invalid {field}"))?;
        if !value.is_finite() {
            return Err(format!(
                "FFmpeg loudnorm {field} is non-finite; silence cannot be normalized"
            ));
        }
        if !(min..=max).contains(&value) {
            return Err(format!(
                "FFmpeg loudnorm {field} is outside its supported range"
            ));
        }
        Ok(value)
    };
    let input = Measurement {
        integrated_lufs: number("input_i", -99.0, 0.0)?,
        true_peak_dbtp: number("input_tp", -99.0, 99.0)?,
        loudness_range_lu: number("input_lra", 0.0, 99.0)?,
        threshold_lufs: number("input_thresh", -99.0, 0.0)?,
        target_offset_lu: number("target_offset", -99.0, 99.0)?,
    };
    for (field, min, max) in [
        ("output_i", -99.0, 0.0),
        ("output_tp", -99.0, 99.0),
        ("output_lra", 0.0, 99.0),
        ("output_thresh", -99.0, 0.0),
    ] {
        number(field, min, max)?;
    }
    let normalization_type = report["normalization_type"]
        .as_str()
        .filter(|value| matches!(*value, "linear" | "dynamic"))
        .ok_or("FFmpeg loudnorm returned an unsupported normalization_type")?
        .to_string();
    Ok(Loudnorm {
        input,
        normalization_type,
    })
}

pub(crate) fn read_bounded(path: &Path, cap: usize) -> Result<Vec<u8>, String> {
    let file = fs::File::open(path).map_err(|_| "mastering could not read a reserved artifact")?;
    let metadata = file
        .metadata()
        .map_err(|_| "mastering could not inspect a reserved artifact")?;
    if !metadata.is_file() || metadata.len() > cap as u64 {
        return Err("mastering artifact exceeds its byte limit".into());
    }
    let mut bytes = Vec::with_capacity(metadata.len().min(cap as u64) as usize);
    file.take((cap as u64).saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|_| "mastering could not read a reserved artifact")?;
    if bytes.len() > cap {
        return Err("mastering artifact exceeds its byte limit".into());
    }
    Ok(bytes)
}

pub(crate) fn write_new(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = match options.open(path) {
        Ok(file) => file,
        Err(_) => return Err("mastering could not create a reserved artifact".into()),
    };
    let persisted = file
        .write_all(bytes)
        .and_then(|_| file.sync_all())
        .map_err(|_| "mastering could not persist a reserved artifact".to_string());
    drop(file);
    if let Err(error) = persisted {
        let _ = fs::remove_file(path);
        return Err(error);
    }
    Ok(())
}

pub(crate) struct StagingDir {
    pub(crate) path: PathBuf,
}

impl StagingDir {
    pub(crate) fn create(scratch: &Path) -> Result<Self, String> {
        for _ in 0..32 {
            let id = NEXT_ARTIFACT.fetch_add(1, Ordering::Relaxed);
            let path = scratch.join(format!("master-work-{}-{id}", std::process::id()));
            match fs::create_dir(&path) {
                Ok(()) => {
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))
                            .map_err(|_| "mastering could not secure its staging directory")?;
                    }
                    return Ok(Self { path });
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(_) => return Err("mastering could not create its staging directory".into()),
            }
        }
        Err("mastering could not reserve a staging directory".into())
    }
}

impl Drop for StagingDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

struct Publication {
    created: Vec<PathBuf>,
    committed: bool,
}

impl Drop for Publication {
    fn drop(&mut self) {
        if !self.committed {
            for path in &self.created {
                let _ = fs::remove_file(path);
            }
        }
    }
}

pub(crate) fn publish(
    scratch: &Scratch,
    staged_wav: &Path,
    staged_report: &Path,
    destination: &(PathBuf, PathBuf),
    final_bytes: &[u8],
    report_bytes: &[u8],
) -> Result<(), String> {
    let mut publication = Publication {
        created: Vec::new(),
        committed: false,
    };
    for (from, to) in [
        (staged_wav, &destination.0),
        (staged_report, &destination.1),
    ] {
        fs::hard_link(from, to)
            .map_err(|_| "mastering could not publish its reserved artifacts")?;
        publication.created.push(to.clone());
    }
    scratch
        .adopt_new_files()
        .map_err(|_| "mastering could not issue its verified artifacts")?;
    let output_name = destination
        .0
        .file_name()
        .and_then(OsStr::to_str)
        .ok_or("invalid mastering artifact name")?;
    let report_name = destination
        .1
        .file_name()
        .and_then(OsStr::to_str)
        .ok_or("invalid mastering artifact name")?;
    let persisted_wav = read_bounded(
        &scratch
            .open_input(output_name)
            .map_err(|_| "mastering WAV was not persisted")?,
        final_bytes.len(),
    )?;
    let persisted_report = read_bounded(
        &scratch
            .open_input(report_name)
            .map_err(|_| "mastering report was not persisted")?,
        report_bytes.len(),
    )?;
    if persisted_wav != final_bytes || persisted_report != report_bytes {
        return Err("mastering artifacts failed persistence verification".into());
    }
    publication.committed = true;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(seconds: u32, amplitude: f64) -> Vec<u8> {
        let samples = SAMPLE_RATE * seconds;
        let data_len = samples * 2;
        let mut wav = Vec::with_capacity(data_len as usize + 44);
        wav.extend_from_slice(b"RIFF");
        wav.extend_from_slice(&(36 + data_len).to_le_bytes());
        wav.extend_from_slice(b"WAVE");
        wav.extend_from_slice(b"fmt ");
        wav.extend_from_slice(&16u32.to_le_bytes());
        wav.extend_from_slice(&1u16.to_le_bytes());
        wav.extend_from_slice(&1u16.to_le_bytes());
        wav.extend_from_slice(&SAMPLE_RATE.to_le_bytes());
        wav.extend_from_slice(&(SAMPLE_RATE * 2).to_le_bytes());
        wav.extend_from_slice(&2u16.to_le_bytes());
        wav.extend_from_slice(&16u16.to_le_bytes());
        wav.extend_from_slice(b"data");
        wav.extend_from_slice(&data_len.to_le_bytes());
        for index in 0..samples {
            let time = index as f64 / f64::from(SAMPLE_RATE);
            let sample =
                (amplitude * (time * 440.0 * std::f64::consts::TAU).sin() * 32767.0).round() as i16;
            wav.extend_from_slice(&sample.to_le_bytes());
        }
        wav
    }

    fn loudnorm_json() -> Value {
        json!({
            "input_i": "-26.00", "input_tp": "-20.00", "input_lra": "2.50",
            "input_thresh": "-36.00", "output_i": "-16.00", "output_tp": "-10.00",
            "output_lra": "2.50", "output_thresh": "-26.00",
            "normalization_type": "linear", "target_offset": "0.10"
        })
    }

    fn parsed(value: &Value) -> Result<Loudnorm, String> {
        parse_loudnorm(&serde_json::to_vec(value).unwrap())
    }

    #[test]
    fn mastering_arguments_are_strict_scratch_wav_names() {
        let scratch = Scratch::create().unwrap();
        for args in [
            json!(null),
            json!([]),
            json!({}),
            json!({"input": 2}),
            json!({"input":"x.wav", "output":null}),
            json!({"input":"x.wav", "filter":"anull"}),
            json!({"input":"x.wav", "ffmpeg":"cmd.exe"}),
            json!({"input":"x.json"}),
            json!({"input":"x.wav", "output":"x.wav"}),
            json!({"input":"x.wav", "output":"X.wav"}),
        ] {
            assert!(Request::parse(&scratch, &args).is_err(), "{args}");
        }
        for name in [
            "",
            "../x.wav",
            r"..\x.wav",
            "/x.wav",
            r"C:\x.wav",
            r"\\server\x.wav",
            "https://host/x.wav",
            "x.wav:stream",
            ".hidden.wav",
            "NUL.wav",
            "x;whoami.wav",
        ] {
            assert!(
                Request::parse(&scratch, &json!({"input":name})).is_err(),
                "{name}"
            );
            assert!(
                Request::parse(&scratch, &json!({"input":"x.wav","output":name})).is_err(),
                "{name}"
            );
        }
        let first = Request::parse(&scratch, &json!({"input":"x.wav"})).unwrap();
        let second = Request::parse(&scratch, &json!({"input":"x.wav"})).unwrap();
        assert_ne!(first.output, second.output);
    }

    #[test]
    fn mastering_command_is_shell_free_no_clobber_and_bounded() {
        let command = command(OsStr::new("ffmpeg"), Path::new("issued.wav"), ANALYSIS);
        assert_eq!(command.get_program(), "ffmpeg");
        let args: Vec<_> = command
            .get_args()
            .map(|argument| argument.to_string_lossy())
            .collect();
        assert!(args.contains(&"-n".into()));
        assert!(!args.contains(&"-y".into()));
        assert!(args.windows(2).any(|pair| pair == ["-i", "issued.wav"]));
        assert!(args.windows(2).any(|pair| pair == ["-af", ANALYSIS]));
        assert!(args
            .windows(2)
            .any(|pair| pair == ["-protocol_whitelist", "file,pipe"]));
        assert!(remaining(Instant::now()).unwrap() <= DEADLINE);
        assert!(remaining(Instant::now() - DEADLINE - Duration::from_secs(1)).is_err());
    }

    #[test]
    fn mastering_report_preserves_fractional_pcm_durations_through_json() {
        for samples in 240_000..241_000 {
            let report = json!({
                "sample_rate": SAMPLE_RATE,
                "samples": samples,
                "duration_s": samples as f64 / f64::from(SAMPLE_RATE)
            });
            let bytes = serde_json::to_vec_pretty(&report).unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&bytes).unwrap(),
                report,
                "sample count {samples}"
            );
        }
    }

    #[test]
    fn mastering_pcm_contract_checks_format_duration_samples_and_silence() {
        let wav = tone(1, 0.1);
        assert_eq!(
            validate_wav(&wav).unwrap(),
            WavInfo {
                rate: SAMPLE_RATE,
                samples: SAMPLE_RATE as u64
            }
        );
        assert!(validate_rendered(&wav, 23_999).is_err());
        assert!(validate_wav(&wav[..wav.len() - 1]).is_err());
        assert!(validate_wav(&tone(1, 0.0)).is_err());
        for (index, value) in [(20, 3), (22, 2), (24, 0), (34, 32)] {
            let mut invalid = wav.clone();
            invalid[index] = value;
            assert!(validate_wav(&invalid).is_err());
        }
        assert!(validate_wav(&tone(601, 0.1)).is_err());
        let oversized = vec![0; MAX_WAV_BYTES + 1];
        assert!(validate_wav(&oversized).unwrap_err().contains("byte limit"));
    }

    #[test]
    fn mastering_measurements_fail_closed_and_drive_the_second_pass() {
        for field in [
            "input_i",
            "input_tp",
            "input_lra",
            "input_thresh",
            "target_offset",
            "output_i",
            "output_tp",
            "output_lra",
            "output_thresh",
        ] {
            for invalid in [
                json!("NaN"),
                json!("inf"),
                json!("-inf"),
                json!(null),
                json!({}),
                json!(true),
                json!("1:evil"),
            ] {
                let mut report = loudnorm_json();
                report[field] = invalid;
                assert!(parsed(&report).is_err(), "{field}");
            }
            let mut report = loudnorm_json();
            report.as_object_mut().unwrap().remove(field);
            assert!(parsed(&report).is_err());
        }
        for stderr in [b"no json".as_slice(), b"{", b"{bad}"] {
            assert!(parse_loudnorm(stderr).is_err());
        }
        let pass = parsed(&loudnorm_json()).unwrap();
        let filter = second_pass_filter(&pass.input);
        for part in [
            CONDITIONING,
            "measured_I=-26",
            "measured_TP=-20",
            "measured_LRA=2.5",
            "measured_thresh=-36",
            "offset=0.1",
            "linear=true",
            "print_format=json",
        ] {
            assert!(filter.contains(part), "{filter}");
        }
        let mut report = loudnorm_json();
        report["normalization_type"] = json!("dynamic");
        assert_eq!(
            parsed(&report).unwrap().normalization_type.as_str(),
            "dynamic"
        );
        report["normalization_type"] = json!("unknown");
        assert!(parsed(&report).is_err());
    }

    #[test]
    fn mastering_quality_gate_checks_lufs_true_peak_and_lra() {
        let mut measured = parsed(&loudnorm_json()).unwrap().input;
        measured.integrated_lufs = TARGET_I;
        measured.true_peak_dbtp = TARGET_TP;
        measured.loudness_range_lu = TARGET_LRA;
        assert!(validate_loudness(&measured).is_ok());
        for value in [-17.01, -14.99, f64::NAN] {
            let mut invalid = measured.clone();
            invalid.integrated_lufs = value;
            assert!(validate_loudness(&invalid).is_err());
        }
        for value in [-1.29, 0.0, f64::INFINITY] {
            let mut invalid = measured.clone();
            invalid.true_peak_dbtp = value;
            assert!(validate_loudness(&invalid).is_err());
        }
        for value in [-1.0, 5.01, f64::NAN] {
            let mut invalid = measured.clone();
            invalid.loudness_range_lu = value;
            assert!(validate_loudness(&invalid).is_err());
        }
    }

    struct FakeFfmpeg {
        _scratch: Scratch,
        good: PathBuf,
        fail: PathBuf,
    }

    impl FakeFfmpeg {
        fn compile() -> Self {
            let scratch = Scratch::create().unwrap();
            let extension = if cfg!(windows) { "exe" } else { "" };
            let suffix = if extension.is_empty() {
                String::new()
            } else {
                format!(".{extension}")
            };
            let source = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("tests")
                .join("support")
                .join("fake_ffmpeg.rs");
            let good = scratch.dir.join(format!("fake-ffmpeg{}", suffix));
            let output = Command::new("rustc")
                .arg("--edition=2021")
                .arg(&source)
                .arg("-o")
                .arg(&good)
                .output()
                .expect("compile executable fake FFmpeg");
            assert!(
                output.status.success(),
                "fake FFmpeg compilation failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
            let fail = scratch.dir.join(format!("fake-ffmpeg-fail{}", suffix));
            fs::copy(&good, &fail).unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mut permissions = fs::metadata(&good).unwrap().permissions();
                permissions.set_mode(0o700);
                fs::set_permissions(&good, permissions).unwrap();
                let mut permissions = fs::metadata(&fail).unwrap().permissions();
                permissions.set_mode(0o700);
                fs::set_permissions(&fail, permissions).unwrap();
            }
            Self {
                _scratch: scratch,
                good,
                fail,
            }
        }
    }

    #[test]
    fn executable_fake_ffmpeg_is_persistent_bounded_and_path_redacting() {
        let fake = FakeFfmpeg::compile();
        let scratch = Scratch::create().unwrap();
        let source_bytes = tone(8, 0.1);
        let source_path = scratch.prepare_output("non-speech.wav").unwrap();
        write_new(&source_path, &source_bytes).unwrap();

        let result = master_with_program(
            &scratch,
            &json!({"input":"non-speech.wav","output":"master.wav"}),
            fake.good.as_os_str(),
        )
        .unwrap();
        assert_eq!(result["ok"], true);
        assert_eq!(result["processedAudio"], true);
        assert_eq!(result["verifiedAudio"], true);
        assert_eq!(result["synthesizedSpeech"], false);
        assert_eq!(result["samples"], 8 * SAMPLE_RATE);
        assert_eq!(result["duration_s"], 8.0);
        assert_eq!(result["sourceSha256"], sha256_hex(&source_bytes));
        assert_ne!(result["sha256"], result["sourceSha256"]);
        assert_eq!(
            fs::read(scratch.open_input("non-speech.wav").unwrap()).unwrap(),
            source_bytes
        );
        let mastered = fs::read(scratch.open_input("master.wav").unwrap()).unwrap();
        assert_eq!(result["sha256"], sha256_hex(&mastered));
        assert_eq!(
            validate_wav(&mastered).unwrap(),
            WavInfo {
                rate: SAMPLE_RATE,
                samples: 8 * SAMPLE_RATE as u64
            }
        );
        let report = fs::read(scratch.open_input("master.json").unwrap()).unwrap();
        let report: Value = serde_json::from_slice(&report).unwrap();
        assert_eq!(report["schema_version"], "1.0");
        assert_eq!(report["kind"], "mastered-audio");
        assert_eq!(report["processedAudio"], true);
        assert_eq!(report["verifiedAudio"], true);
        assert_eq!(report["synthesizedSpeech"], false);
        assert_eq!(report["report"], result["report"]);
        assert_eq!(report["report"]["final"]["integrated_lufs"], TARGET_I);
        let report_text = serde_json::to_string(&report).unwrap();
        assert!(!report_text.contains(&scratch.dir.to_string_lossy().to_string()));
        assert_eq!(fs::read_dir(&scratch.dir).unwrap().count(), 3);
        assert!(master_with_program(
            &scratch,
            &json!({"input":"non-speech.wav","output":"master.wav"}),
            fake.good.as_os_str(),
        )
        .is_err());

        let failure = master_with_program(
            &scratch,
            &json!({"input":"non-speech.wav","output":"failure.wav"}),
            fake.fail.as_os_str(),
        )
        .unwrap_err();
        assert!(failure.contains("FFmpeg failed"), "{failure}");
        assert!(!failure.contains("private"), "{failure}");
        assert!(!failure.contains("secret"), "{failure}");
        assert!(!failure.contains(&scratch.dir.to_string_lossy().to_string()));
        assert!(scratch.open_input("failure.wav").is_err());
        assert!(scratch.open_input("failure.json").is_err());

        let missing = scratch.dir.join("missing-ffmpeg");
        let failure = master_with_program(
            &scratch,
            &json!({"input":"non-speech.wav","output":"missing.wav"}),
            missing.as_os_str(),
        )
        .unwrap_err();
        assert!(failure.contains("FFmpeg"), "{failure}");
        assert!(!failure.contains(&scratch.dir.to_string_lossy().to_string()));
        assert!(scratch.open_input("missing.wav").is_err());

        let mut hang = Command::new(&fake.good);
        hang.arg("hang");
        let failure = process::run(
            &mut hang,
            process::Limits {
                deadline: Duration::from_millis(100),
                stdout: 32,
                stderr: 32,
            },
        )
        .unwrap_err();
        assert!(failure.contains("deadline"), "{failure}");

        let mut flood = Command::new(&fake.good);
        flood.arg("flood-stderr");
        let failure = process::run(
            &mut flood,
            process::Limits {
                deadline: Duration::from_secs(5),
                stdout: 32,
                stderr: 32,
            },
        )
        .unwrap_err();
        assert!(failure.contains("stderr"), "{failure}");
    }
}
