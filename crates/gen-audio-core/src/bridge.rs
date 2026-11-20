//! Build argv for the Python CLIs. The process is spawned with an argument
//! vector, never a shell string.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::{json, Value};

use crate::paths::{read_trusted_script, read_user_repo_file, Scratch};
use crate::redact::redact_secrets;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PythonTool {
    Improve,
    Spectrogram,
    Cube,
    Compare,
    Synth,
}

impl PythonTool {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "improve" => Some(Self::Improve),
            "spectrogram" => Some(Self::Spectrogram),
            "cube_revision" | "cube" => Some(Self::Cube),
            "compare" => Some(Self::Compare),
            "synth" => Some(Self::Synth),
            _ => None,
        }
    }

    fn script(self) -> &'static str {
        match self {
            Self::Improve => "scripts/improve.py",
            Self::Spectrogram => "scripts/spectrogram.py",
            Self::Cube => "scripts/cube_revision.py",
            Self::Compare => "scripts/compare_wavs.py",
            Self::Synth => "scripts/synth_kokoro_onnx.py",
        }
    }
}

#[derive(Debug)]
pub struct PythonPlan {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    /// Files a speech-producing run must leave behind. `run_plan` reports
    /// `synthesizedSpeech: true` only when they prove real audio was written.
    pub speech: Option<SpeechOutput>,
}

/// The WAV and its JSON manifest that `scripts/synth_kokoro_onnx.py` writes.
#[derive(Debug, Clone)]
pub struct SpeechOutput {
    pub wav: PathBuf,
    pub manifest: PathBuf,
}

/// The repo virtualenv interpreter, the one `scripts/toolbox.ps1` installs
/// every Python dependency into.
pub fn repo_venv_python(repo: &Path) -> PathBuf {
    if cfg!(windows) {
        repo.join(".venv").join("Scripts").join("python.exe")
    } else {
        repo.join(".venv").join("bin").join("python3")
    }
}

/// GEN_AUDIO_PYTHON when set; otherwise the repo virtualenv when it exists.
/// A bare `python` from PATH is used only when the checkout has no .venv.
pub fn python_program_for(repo: &Path) -> Result<PathBuf, String> {
    if std::env::var_os("GEN_AUDIO_PYTHON").is_none() {
        let venv = repo_venv_python(repo);
        if venv.is_file() {
            return Ok(venv);
        }
    }
    python_program()
}

pub fn python_program() -> Result<PathBuf, String> {
    let fallback = if cfg!(windows) { "python" } else { "python3" };
    let raw = std::env::var("GEN_AUDIO_PYTHON").unwrap_or_else(|_| fallback.to_string());
    if raw.is_empty() || raw.contains('\0') || raw.contains("..") {
        return Err("GEN_AUDIO_PYTHON is empty or contains ..".into());
    }
    let path = PathBuf::from(&raw);
    let name = path
        .file_name()
        .and_then(|part| part.to_str())
        .unwrap_or("");
    const ALLOWED: &[&str] = &["python", "python3", "python.exe", "python3.exe", "py", "py.exe"];
    if !ALLOWED.contains(&name) {
        return Err("GEN_AUDIO_PYTHON basename must be python, python3, or py".into());
    }
    Ok(path)
}

pub fn plan(
    tool: PythonTool,
    repo: &Path,
    scratch: &Scratch,
    args: &Value,
) -> Result<PythonPlan, String> {
    let script = read_trusted_script(repo, tool.script())?;
    let mut planned = vec![script.to_string_lossy().to_string()];
    let mut speech = None;
    match tool {
        PythonTool::Improve => {
            let input = scratch.open_input(req_name(args, "input")?)?;
            let output = scratch.prepare_output(req_name(args, "output")?)?;
            planned.push(input.to_string_lossy().to_string());
            planned.push("-o".into());
            planned.push(output.to_string_lossy().to_string());
        }
        PythonTool::Spectrogram => {
            let before = scratch.open_input(req_name(args, "before")?)?;
            planned.push("--before".into());
            planned.push(before.to_string_lossy().to_string());
            if let Some(after) = opt_name(args, "after")? {
                let after = scratch.open_input(after)?;
                planned.push("--after".into());
                planned.push(after.to_string_lossy().to_string());
            }
            planned.push("--out-dir".into());
            planned.push(scratch.dir.to_string_lossy().to_string());
            planned.push("--title".into());
            planned.push("gen-audio fixture".into());
        }
        PythonTool::Cube => {
            let input = scratch.open_input(req_name(args, "input")?)?;
            let output = scratch.prepare_output(req_name(args, "output")?)?;
            planned.push(input.to_string_lossy().to_string());
            planned.push("-o".into());
            planned.push(output.to_string_lossy().to_string());
            let steps = args.get("maxSteps").and_then(Value::as_u64).unwrap_or(4);
            if steps > 8 {
                return Err("maxSteps must be from 0 to 8".into());
            }
            planned.push("--max-steps".into());
            planned.push(steps.to_string());
        }
        PythonTool::Compare => {
            let engine = req_string(args, "engine")?;
            if !engine
                .chars()
                .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_')
                || engine.len() > 32
            {
                return Err("engine id must be a short lowercase slug".into());
            }
            let wav = scratch.open_input(req_name(args, "input")?)?;
            let output = scratch.prepare_output("compare-scores.json")?;
            planned.push(format!("{engine}={}", wav.to_string_lossy()));
            planned.push("-o".into());
            planned.push(output.to_string_lossy().to_string());
        }
        PythonTool::Synth => {
            let script_rel = args
                .get("script")
                .and_then(Value::as_str)
                .unwrap_or("examples/podcast_script_sample.txt");
            let cast_rel = args
                .get("castMap")
                .and_then(Value::as_str)
                .unwrap_or("voices/cast_map.example.json");
            let script_path = read_user_repo_file(repo, script_rel)?;
            let cast_path = read_user_repo_file(repo, cast_rel)?;
            let output = scratch.prepare_output(req_name(args, "output")?)?;
            let model_dir = model_dir(repo);
            let model = kokoro_file("GEN_AUDIO_KOKORO_MODEL", &model_dir, "kokoro-v1.0.onnx");
            let voices = kokoro_file("GEN_AUDIO_KOKORO_VOICES", &model_dir, "voices-v1.0.bin");
            let (Some(model), Some(voices)) = (model, voices) else {
                return Err(format!(
                    "synth was not started: GEN_AUDIO_KOKORO_MODEL and GEN_AUDIO_KOKORO_VOICES are unset and {} has no kokoro-v1.0.onnx and voices-v1.0.bin. No speech was invented.",
                    model_dir.display()
                ));
            };
            let model_path = bounded_model_file(&model_dir, &model)?;
            let voices_path = bounded_model_file(&model_dir, &voices)?;
            speech = Some(SpeechOutput { manifest: output.with_extension("json"), wav: output.clone() });
            planned.push(script_path.to_string_lossy().to_string());
            planned.push("--cast-map".into());
            planned.push(cast_path.to_string_lossy().to_string());
            planned.push("-o".into());
            planned.push(output.to_string_lossy().to_string());
            planned.push("--model".into());
            planned.push(model_path.to_string_lossy().to_string());
            planned.push("--voices".into());
            planned.push(voices_path.to_string_lossy().to_string());
        }
    }
    Ok(PythonPlan {
        program: python_program_for(repo)?,
        args: planned,
        cwd: repo.to_path_buf(),
        speech,
    })
}

/// Argv for `gen_audio.cli.generate` via the trusted shim. The prompt stays in the work file.
pub fn plan_generate(
    repo: &Path,
    work: &Path,
    personas: &[String],
    engine: &str,
    duration_s: f64,
) -> Result<PythonPlan, String> {
    let script = read_trusted_script(repo, "scripts/generate.py")?;
    if personas.is_empty() || personas.len() > 8 {
        return Err("personas must be 1 to 8 ids".into());
    }
    for id in personas {
        if id.len() > 32 || !id.chars().all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_') {
            return Err("persona id is invalid".into());
        }
    }
    if engine.len() > 32 || !engine.chars().all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_') {
        return Err("engine id must be a short lowercase slug".into());
    }
    if !(0.5..=1800.0).contains(&duration_s) {
        return Err("duration must be from 0.5 to 1800 seconds".into());
    }
    let prompt = work.join("scripts").join("prompt.txt");
    let prompt = prompt.canonicalize().map_err(|err| format!("prompt file: {err}"))?;
    let work_canon = work.canonicalize().map_err(|err| format!("work directory: {err}"))?;
    if !prompt.starts_with(&work_canon) {
        return Err("prompt file is outside the work directory".into());
    }
    Ok(PythonPlan {
        program: python_program_for(repo)?,
        speech: None,
        args: vec![
            script.to_string_lossy().to_string(),
            "--prompt-file".into(),
            prompt.to_string_lossy().to_string(),
            "--personas".into(),
            personas.join(","),
            "--engine".into(),
            engine.to_string(),
            "--duration-s".into(),
            format!("{duration_s}"),
            "--out-dir".into(),
            work_canon.to_string_lossy().to_string(),
        ],
        cwd: repo.to_path_buf(),
    })
}

fn command(plan: &PythonPlan) -> Command {
    let mut command = Command::new(&plan.program);
    command.args(&plan.args).current_dir(&plan.cwd);
    for key in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
    ] {
        command.env_remove(key);
    }
    command
}

pub fn start_plan(plan: &PythonPlan) -> Result<std::process::Child, String> {
    let mut command = command(plan);
    command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    // Own process group so a timeout or cancel can signal the python child
    // and anything it spawned.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command
        .spawn()
        .map_err(|err| format!("failed to start python: {err}"))
}

pub fn run_plan(plan: &PythonPlan) -> Result<Value, String> {
    let output = command(plan)
        .output()
        .map_err(|err| format!("failed to start python: {err}"))?;
    let stdout = redact_secrets(&String::from_utf8_lossy(&output.stdout));
    let stderr = redact_secrets(&String::from_utf8_lossy(&output.stderr));
    let mut result = json!({
        "ok": output.status.success(),
        "code": output.status.code(),
        "stdout": tail(&stdout),
        "stderr": tail(&stderr),
        "synthesizedSpeech": false
    });
    if let Some(speech) = &plan.speech {
        match speech_evidence(speech, output.status.success()) {
            Ok(evidence) => {
                result["synthesizedSpeech"] = json!(true);
                result["speechEvidence"] = evidence;
            }
            Err(reason) => result["speechRefusal"] = json!(reason),
        }
    }
    Ok(result)
}

/// Proof that a synth run wrote real speech: the engine exited 0, the WAV is a
/// RIFF/WAVE file with a non-empty data chunk, and the manifest names the
/// engine and the same frame count and sample rate as the WAV.
pub fn speech_evidence(speech: &SpeechOutput, exited_ok: bool) -> Result<Value, String> {
    if !exited_ok {
        return Err("the synth engine exited with a failure".into());
    }
    let wav = read_wav_header(&speech.wav)?;
    let manifest_text = std::fs::read_to_string(&speech.manifest)
        .map_err(|err| format!("synth manifest {}: {err}", speech.manifest.display()))?;
    let manifest: Value =
        serde_json::from_str(&manifest_text).map_err(|err| format!("synth manifest is not JSON: {err}"))?;
    let engine = manifest
        .get("engine")
        .and_then(Value::as_str)
        .filter(|engine| !engine.is_empty())
        .ok_or("synth manifest names no engine")?;
    let samples = manifest.get("samples").and_then(Value::as_u64).unwrap_or(0);
    let rate = manifest.get("sample_rate").and_then(Value::as_u64).unwrap_or(0);
    if samples == 0 || wav.frames == 0 {
        return Err("synth wrote no audio samples".into());
    }
    if samples != wav.frames {
        return Err(format!("synth manifest says {samples} samples but the WAV holds {}", wav.frames));
    }
    if rate != u64::from(wav.sample_rate) {
        return Err(format!("synth manifest says {rate} Hz but the WAV is {} Hz", wav.sample_rate));
    }
    Ok(json!({
        "wav": speech.wav.to_string_lossy(),
        "bytes": wav.bytes,
        "engine": engine,
        "sampleRate": wav.sample_rate,
        "channels": wav.channels,
        "frames": wav.frames,
        "seconds": wav.frames as f64 / f64::from(wav.sample_rate)
    }))
}

struct WavHeader {
    bytes: u64,
    sample_rate: u32,
    channels: u16,
    frames: u64,
}

fn read_wav_header(path: &Path) -> Result<WavHeader, String> {
    use std::io::Read;
    let file = std::fs::File::open(path).map_err(|err| format!("synth WAV {}: {err}", path.display()))?;
    let bytes = file.metadata().map_err(|err| format!("synth WAV: {err}"))?.len();
    let mut head = Vec::new();
    file.take(1 << 16).read_to_end(&mut head).map_err(|err| format!("synth WAV: {err}"))?;
    if head.len() < 12 || &head[0..4] != b"RIFF" || &head[8..12] != b"WAVE" {
        return Err("synth output is not a RIFF/WAVE file".into());
    }
    let (mut pos, mut format, mut data) = (12usize, None, None);
    while pos + 8 <= head.len() {
        let id = &head[pos..pos + 4];
        let size = u32::from_le_bytes([head[pos + 4], head[pos + 5], head[pos + 6], head[pos + 7]]) as u64;
        let body = pos + 8;
        if id == b"fmt " && body + 16 <= head.len() {
            let channels = u16::from_le_bytes([head[body + 2], head[body + 3]]);
            let rate = u32::from_le_bytes([head[body + 4], head[body + 5], head[body + 6], head[body + 7]]);
            let block_align = u16::from_le_bytes([head[body + 12], head[body + 13]]);
            format = Some((channels, rate, block_align));
        } else if id == b"data" {
            data = Some(size.min(bytes.saturating_sub(body as u64)));
            break;
        }
        pos = body + size as usize + (size as usize & 1);
    }
    let (channels, sample_rate, block_align) = format.ok_or("synth WAV has no fmt chunk")?;
    let data = data.ok_or("synth WAV has no data chunk")?;
    if channels == 0 || sample_rate == 0 || block_align == 0 {
        return Err("synth WAV fmt chunk is invalid".into());
    }
    Ok(WavHeader { bytes, sample_rate, channels, frames: data / u64::from(block_align) })
}

/// GEN_AUDIO_MODEL_DIR when set, otherwise `<repo>/models`.
pub fn model_dir(repo: &Path) -> PathBuf {
    match std::env::var_os("GEN_AUDIO_MODEL_DIR") {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => repo.join("models"),
    }
}

/// The env var when set (a bare file name resolves inside the model dir),
/// otherwise the default file name when it exists in the model dir.
fn kokoro_file(var: &str, model_dir: &Path, default: &str) -> Option<PathBuf> {
    match std::env::var(var) {
        Ok(raw) if !raw.is_empty() => {
            let path = PathBuf::from(&raw);
            Some(if path.is_absolute() { path } else { model_dir.join(path) })
        }
        _ => Some(model_dir.join(default)).filter(|path| path.is_file()),
    }
}

fn bounded_model_file(model_dir: &Path, raw: &Path) -> Result<PathBuf, String> {
    let root = model_dir
        .canonicalize()
        .map_err(|err| format!("model directory {}: {err}", model_dir.display()))?;
    let path = raw
        .canonicalize()
        .map_err(|err| format!("model path {}: {err}", raw.display()))?;
    if !path.starts_with(&root) {
        return Err("model path is outside GEN_AUDIO_MODEL_DIR".into());
    }
    if !path.is_file() {
        return Err("model path is not a file".into());
    }
    Ok(path)
}

fn req_name<'a>(args: &'a Value, key: &str) -> Result<&'a str, String> {
    args.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string field {key}"))
}

fn opt_name<'a>(args: &'a Value, key: &str) -> Result<Option<&'a str>, String> {
    match args.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.as_str())),
        _ => Err(format!("field {key} must be a string")),
    }
}

fn req_string<'a>(args: &'a Value, key: &str) -> Result<&'a str, String> {
    req_name(args, key)
}

fn tail(text: &str) -> String {
    let max = 2000;
    if text.chars().count() <= max {
        text.to_string()
    } else {
        text.chars().take(max).collect()
    }
}

/// The env vars a real synth reads.
#[cfg(any(test, feature = "test-support"))]
pub const KOKORO_ENV: [&str; 3] = ["GEN_AUDIO_KOKORO_MODEL", "GEN_AUDIO_KOKORO_VOICES", "GEN_AUDIO_MODEL_DIR"];
#[cfg(any(test, feature = "test-support"))]
static KOKORO_ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Test support only: clears the `GEN_AUDIO_KOKORO_*` vars for one test and
/// puts them back on drop, so a developer shell with real models set cannot
/// start a real synth from a unit test (PR #5 review fix 6; verification E3).
/// One process-wide lock, so every crate's tests in a binary share it.
///
/// Compiled only under `cfg(test)` or the `test-support` feature, which
/// gen-audio-mcp and gen-audio-acp enable in their `[dev-dependencies]`.
/// Release and normal builds of every crate never contain it (resolver 2
/// keeps dev-dependency features out of normal builds).
#[cfg(any(test, feature = "test-support"))]
pub struct NoKokoroEnv {
    saved: Vec<(&'static str, Option<std::ffi::OsString>)>,
    _lock: std::sync::MutexGuard<'static, ()>,
}

#[cfg(any(test, feature = "test-support"))]
impl NoKokoroEnv {
    #[allow(clippy::new_without_default)]
    pub fn new() -> Self {
        let lock = KOKORO_ENV_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let saved = KOKORO_ENV.iter().map(|name| (*name, std::env::var_os(name))).collect();
        for name in KOKORO_ENV {
            std::env::remove_var(name);
        }
        // Point the model dir at an empty directory so the repo's own models/
        // default cannot start a real synth from a test either.
        let empty = std::env::temp_dir().join(format!("gen-audio-no-models-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&empty);
        std::env::set_var("GEN_AUDIO_MODEL_DIR", &empty);
        Self { saved, _lock: lock }
    }
}

#[cfg(any(test, feature = "test-support"))]
impl Drop for NoKokoroEnv {
    fn drop(&mut self) {
        for (name, value) in &self.saved {
            match value {
                Some(value) => std::env::set_var(name, value),
                None => std::env::remove_var(name),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// G: NoKokoroEnv and its env list exist only for tests (cfg(test) or the
    /// test-support feature), and no crate enables that feature outside
    /// [dev-dependencies].
    #[test]
    fn no_kokoro_env_is_test_support_only() {
        let source = include_str!("bridge.rs");
        let gate = "#[cfg(any(test, feature = \"test-support\"))]\n";
        for item in ["pub const KOKORO_ENV", "static KOKORO_ENV_LOCK", "pub struct NoKokoroEnv", "impl NoKokoroEnv", "impl Drop for NoKokoroEnv"] {
            let at = source.find(&format!("\n{item}")).unwrap_or_else(|| panic!("{item} not found"));
            assert!(source[..at + 1].ends_with(gate), "{item} must sit right under {gate}");
        }
        for manifest in [include_str!("../../gen-audio-mcp/Cargo.toml"), include_str!("../../gen-audio-acp/Cargo.toml")] {
            let (normal, dev) = manifest.split_once("[dev-dependencies]").expect("a [dev-dependencies] table");
            assert!(!normal.contains("test-support"), "test-support enabled outside [dev-dependencies]");
            assert!(dev.contains("features = [\"test-support\"]"));
        }
    }

    /// Viewport binding and the library-root override are the same kind of seam
    /// as NoKokoroEnv: other crates' tests reach them through `test-support`,
    /// and a release build of this crate does not export them.
    #[test]
    fn viewport_and_library_test_seams_are_test_support_only() {
        let gate = "#[cfg(any(test, feature = \"test-support\"))]\n";
        let files = [
            (include_str!("viewport.rs"), &["pub struct ViewportHandle", "pub struct ViewportGuard", "pub fn bind_viewport", "thread_local!"][..]),
            (include_str!("library_store.rs"), &["pub fn set_root_override_for_test", "thread_local!"][..]),
        ];
        for (source, items) in files {
            for item in items {
                let at = source.find(&format!("\n{item}")).unwrap_or_else(|| panic!("{item} not found"));
                assert!(
                    source[..at + 1].ends_with(gate),
                    "{item} must sit right under the test-support gate"
                );
            }
        }
    }
    use crate::fixture::write_fixture_tone;
    use serde_json::json;

    #[test]
    fn improve_plan_rejects_paths_outside_work() {
        let scratch = Scratch::create().unwrap();
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap();
        write_fixture_tone(&scratch).unwrap();
        let good = plan(
            PythonTool::Improve,
            &repo,
            &scratch,
            &json!({"input": "fixture-tone.wav", "output": "fixture-24k.wav"}),
        )
        .unwrap();
        let improve_py = Path::new("scripts").join("improve.py");
        assert!(good.args.iter().any(|arg| Path::new(arg).ends_with(&improve_py)));
        assert!(plan(
            PythonTool::Improve,
            &repo,
            &scratch,
            &json!({"input": "../secret.wav", "output": "out.wav"}),
        )
        .is_err());
        let script_err = plan(
            PythonTool::Synth,
            &repo,
            &scratch,
            &json!({"output": "nope.wav", "script": "README.md"}),
        )
        .unwrap_err();
        assert!(script_err.contains("examples/"));
        let _ = std::fs::remove_dir_all(&scratch.dir);
    }

    #[test]
    fn synth_does_not_invent_audio_without_models() {
        let scratch = Scratch::create().unwrap();
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap();
        let _env = NoKokoroEnv::new();
        let err = plan(
            PythonTool::Synth,
            &repo,
            &scratch,
            &json!({"output": "should-not-exist.wav"}),
        )
        .unwrap_err();
        assert!(err.contains("No speech was invented"));
        let _ = std::fs::remove_dir_all(&scratch.dir);
    }

    fn write_wav(path: &Path, rate: u32, frames: u32) {
        let data_len = frames * 2;
        let mut bytes = Vec::new();
        bytes.extend_from_slice(b"RIFF");
        bytes.extend_from_slice(&(36 + data_len).to_le_bytes());
        bytes.extend_from_slice(b"WAVEfmt ");
        bytes.extend_from_slice(&16u32.to_le_bytes());
        bytes.extend_from_slice(&1u16.to_le_bytes());
        bytes.extend_from_slice(&1u16.to_le_bytes());
        bytes.extend_from_slice(&rate.to_le_bytes());
        bytes.extend_from_slice(&(rate * 2).to_le_bytes());
        bytes.extend_from_slice(&2u16.to_le_bytes());
        bytes.extend_from_slice(&16u16.to_le_bytes());
        bytes.extend_from_slice(b"data");
        bytes.extend_from_slice(&data_len.to_le_bytes());
        bytes.resize(bytes.len() + data_len as usize, 0x11);
        std::fs::write(path, bytes).unwrap();
    }

    fn speech_case(name: &str, rate: u32, frames: u32, manifest: Option<Value>) -> SpeechOutput {
        let dir = std::env::temp_dir().join(format!("gen-audio-speech-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let wav = dir.join("out.wav");
        write_wav(&wav, rate, frames);
        let manifest_path = dir.join("out.json");
        match manifest {
            Some(value) => std::fs::write(&manifest_path, value.to_string()).unwrap(),
            None => {
                let _ = std::fs::remove_file(&manifest_path);
            }
        }
        SpeechOutput { wav, manifest: manifest_path }
    }

    #[test]
    fn speech_evidence_requires_a_real_wav_and_matching_manifest() {
        let good = speech_case("good", 24000, 4800, Some(json!({"engine": "kokoro_onnx", "samples": 4800, "sample_rate": 24000})));
        let evidence = speech_evidence(&good, true).unwrap();
        assert_eq!(evidence["frames"], 4800);
        assert_eq!(evidence["engine"], "kokoro_onnx");
        assert!((evidence["seconds"].as_f64().unwrap() - 0.2).abs() < 1e-9);

        assert!(speech_evidence(&good, false).unwrap_err().contains("exited"));
        let empty = speech_case("empty", 24000, 0, Some(json!({"engine": "kokoro_onnx", "samples": 0, "sample_rate": 24000})));
        assert!(speech_evidence(&empty, true).unwrap_err().contains("no audio"));
        let missing = speech_case("missing", 24000, 4800, None);
        assert!(speech_evidence(&missing, true).unwrap_err().contains("manifest"));
        let mismatch = speech_case("mismatch", 24000, 4800, Some(json!({"engine": "kokoro_onnx", "samples": 9600, "sample_rate": 24000})));
        assert!(speech_evidence(&mismatch, true).unwrap_err().contains("9600"));
        let no_engine = speech_case("noengine", 24000, 4800, Some(json!({"samples": 4800, "sample_rate": 24000})));
        assert!(speech_evidence(&no_engine, true).unwrap_err().contains("engine"));
        let not_wav = speech_case("notwav", 24000, 4800, Some(json!({"engine": "kokoro_onnx", "samples": 4800, "sample_rate": 24000})));
        std::fs::write(&not_wav.wav, b"not audio at all").unwrap();
        assert!(speech_evidence(&not_wav, true).unwrap_err().contains("RIFF"));
    }

    #[test]
    fn python_defaults_to_the_repo_venv() {
        let repo = std::env::temp_dir().join(format!("gen-audio-venv-{}", std::process::id()));
        let venv = repo_venv_python(&repo);
        std::fs::create_dir_all(venv.parent().unwrap()).unwrap();
        std::fs::write(&venv, b"").unwrap();
        if std::env::var_os("GEN_AUDIO_PYTHON").is_none() {
            assert_eq!(python_program_for(&repo).unwrap(), venv);
        }
        let _ = std::fs::remove_dir_all(&repo);
    }
}
