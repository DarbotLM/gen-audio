//! Vendor and local connector surfaces.
//!
//! MCP, ACP, and the harness are local. Copilot, Claude, GPT, and Gemini
//! stay in mock mode unless a credential is set AND `GEN_AUDIO_CONNECTOR_LIVE=1`.
//! Mock completions do not call the network and are not model output.

#[cfg(test)]
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::net::ToSocketAddrs;
use std::time::Duration;

use gen_audio_core::redact::redact_secrets;
use serde::Serialize;
use serde_json::{json, Value};

pub const CONNECTOR_IDS: &[&str] = &[
    "mcp", "acp", "harness", "copilot", "claude", "gpt", "gemini",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Mock,
    Live,
    Local,
    /// A live request is enabled, but liveness/authentication has not been probed.
    Configured,
    TokenPresent,
    Misconfigured,
}

#[derive(Debug, Clone, Serialize)]
pub struct HealthReport {
    pub connector_id: String,
    pub mode: Mode,
    pub authenticated: bool,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Completion {
    pub connector_id: String,
    pub text: String,
    pub mock: bool,
    pub model: String,
}

#[derive(Debug)]
pub struct ConnectorError {
    pub message: String,
}

impl ConnectorError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: redact_secrets(&message.into()),
        }
    }
}

impl std::fmt::Display for ConnectorError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

pub trait Connector {
    fn id(&self) -> &'static str;
    fn health(&self) -> HealthReport;
    fn complete(&self, prompt: &str) -> Result<Completion, ConnectorError>;
}

type ConnectorFactory = fn() -> Box<dyn Connector>;

/// Runtime connector registry. Factories are used so connector instances do not
/// retain process-global credentials or one request's mutable state.
pub struct ConnectorRegistry {
    factories: BTreeMap<&'static str, ConnectorFactory>,
}

impl ConnectorRegistry {
    pub fn new() -> Self {
        Self {
            factories: BTreeMap::new(),
        }
    }

    pub fn register(
        &mut self,
        id: &'static str,
        factory: ConnectorFactory,
    ) -> Result<(), ConnectorError> {
        let probe = factory();
        if probe.id() != id {
            return Err(ConnectorError::new(format!(
                "connector registry key {id} does not match plugin id {}",
                probe.id()
            )));
        }
        if self.factories.insert(id, factory).is_some() {
            return Err(ConnectorError::new(format!("duplicate connector {id}")));
        }
        Ok(())
    }

    pub fn validate(&self) -> Result<(), ConnectorError> {
        for (id, factory) in &self.factories {
            if factory().id() != *id {
                return Err(ConnectorError::new(format!(
                    "connector {id} factory changed identity"
                )));
            }
        }
        Ok(())
    }

    pub fn ids(&self) -> impl Iterator<Item = &'static str> + '_ {
        self.factories.keys().copied()
    }

    pub fn connector(&self, id: &str) -> Option<Box<dyn Connector>> {
        self.factories.get(id).map(|factory| factory())
    }

    pub fn roster(&self) -> Result<Vec<Box<dyn Connector>>, ConnectorError> {
        self.validate()?;
        Ok(self.factories.values().map(|factory| factory()).collect())
    }
}

impl Default for ConnectorRegistry {
    fn default() -> Self {
        Self::new()
    }
}

fn local_mcp() -> Box<dyn Connector> {
    Box::new(LocalConnector::new(
        "mcp",
        "Local stateless MCP server (stdio and optional loopback HTTP). No vendor credential.",
    ))
}
fn local_acp() -> Box<dyn Connector> {
    Box::new(LocalConnector::new(
        "acp",
        "Local ACP agent with explicitly in-process session storage for this stdio lifetime.",
    ))
}
fn local_harness() -> Box<dyn Connector> {
    Box::new(LocalConnector::new(
        "harness",
        "Local harness plans tools and writes JSONL traces. It does not invent speech audio.",
    ))
}
fn copilot() -> Box<dyn Connector> {
    Box::new(CopilotConnector)
}
fn claude() -> Box<dyn Connector> {
    Box::new(ClaudeConnector)
}
fn gpt() -> Box<dyn Connector> {
    Box::new(GptConnector)
}
fn gemini() -> Box<dyn Connector> {
    Box::new(GeminiConnector)
}

pub fn default_registry() -> Result<ConnectorRegistry, ConnectorError> {
    let mut registry = ConnectorRegistry::new();
    for (id, factory) in [
        ("mcp", local_mcp as ConnectorFactory),
        ("acp", local_acp as ConnectorFactory),
        ("harness", local_harness as ConnectorFactory),
        ("copilot", copilot as ConnectorFactory),
        ("claude", claude as ConnectorFactory),
        ("gpt", gpt as ConnectorFactory),
        ("gemini", gemini as ConnectorFactory),
    ] {
        registry.register(id, factory)?;
    }
    registry.validate()?;
    Ok(registry)
}

pub fn roster() -> Vec<Box<dyn Connector>> {
    default_registry()
        .and_then(|registry| registry.roster())
        .expect("built-in connector registry must validate")
}

pub fn health_all() -> Vec<HealthReport> {
    roster().iter().map(|item| item.health()).collect()
}

pub fn health_one(id: &str) -> Result<HealthReport, ConnectorError> {
    default_registry()?
        .connector(id)
        .map(|item| item.health())
        .ok_or_else(|| ConnectorError::new(format!("unknown connector {id}")))
}

pub fn complete_one(id: &str, prompt: &str) -> Result<Completion, ConnectorError> {
    let connector = default_registry()?
        .connector(id)
        .ok_or_else(|| ConnectorError::new(format!("unknown connector {id}")))?;
    connector.complete(prompt)
}

struct LocalConnector {
    id: &'static str,
    detail: &'static str,
}

impl LocalConnector {
    fn new(id: &'static str, detail: &'static str) -> Self {
        Self { id, detail }
    }
}

impl Connector for LocalConnector {
    fn id(&self) -> &'static str {
        self.id
    }

    fn health(&self) -> HealthReport {
        HealthReport {
            connector_id: self.id.to_string(),
            mode: Mode::Local,
            authenticated: false,
            detail: self.detail.to_string(),
        }
    }

    fn complete(&self, prompt: &str) -> Result<Completion, ConnectorError> {
        check_prompt(prompt)?;
        Err(ConnectorError::new(format!(
            "connector '{}' is a local protocol surface, not a model completion provider; use its typed tool/session API",
            self.id
        )))
    }
}

struct CopilotConnector;
struct ClaudeConnector;
struct GptConnector;
struct GeminiConnector;

impl Connector for CopilotConnector {
    fn id(&self) -> &'static str {
        "copilot"
    }
    fn health(&self) -> HealthReport {
        vendor_health(
            "copilot",
            token_present(&["COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"]),
            "GitHub Models / optional Copilot Studio endpoint. Mock until GEN_AUDIO_CONNECTOR_LIVE=1.",
        )
    }
    fn complete(&self, prompt: &str) -> Result<Completion, ConnectorError> {
        check_prompt(prompt)?;
        let token = first_env(&["COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"]);
        if !live_enabled() || token.is_none() {
            return Ok(mock_completion("copilot", "github-models", prompt));
        }
        let token = token.unwrap();
        if let Ok(studio) = std::env::var("COPILOT_STUDIO_ENDPOINT") {
            let url = require_https_or_loopback(&studio)?;
            let body = json!({"prompt": prompt, "source": "gen-audio"});
            let authorization = "Bearer ".to_owned() + &token;
            let text = send_json(&url, &[("authorization", &authorization)], &body)?;
            return Ok(Completion {
                connector_id: "copilot".into(),
                text,
                mock: false,
                model: "copilot-studio-endpoint".into(),
            });
        }
        let model = std::env::var("COPILOT_GITHUB_MODEL").unwrap_or_else(|_| "gpt-4o-mini".into());
        check_model(&model)?;
        let body = chat_body(&model, prompt);
        let authorization = "Bearer ".to_owned() + &token;
        let text = send_json(
            "https://models.github.ai/inference/chat/completions",
            &[
                ("authorization", &authorization),
                ("accept", "application/vnd.github+json"),
            ],
            &body,
        )?;
        Ok(Completion {
            connector_id: "copilot".into(),
            text,
            mock: false,
            model,
        })
    }
}

impl Connector for ClaudeConnector {
    fn id(&self) -> &'static str {
        "claude"
    }
    fn health(&self) -> HealthReport {
        if claude_code_enabled() {
            let available = claude_bin().is_some();
            let detail = if available {
                if live_enabled() {
                    "Claude Code CLI is selected and available; GEN_AUDIO_CONNECTOR_LIVE=1 permits execution. Authentication is not verified by health. The Anthropic API key is ignored while CLI mode is selected."
                } else {
                    "Claude Code CLI is selected and available, but execution remains disabled until GEN_AUDIO_CONNECTOR_LIVE=1. The Anthropic API key is ignored while CLI mode is selected."
                }
            } else {
                "GEN_AUDIO_CLAUDE_CODE_CLI=1 selects the Claude Code CLI, but no valid claude binary was found. The Anthropic API key is not used while CLI mode is selected."
            };
            return HealthReport {
                connector_id: "claude".into(),
                mode: if available {
                    Mode::Configured
                } else {
                    Mode::Misconfigured
                },
                authenticated: false,
                detail: detail.into(),
            };
        }
        vendor_health(
            "claude",
            token_present(&["ANTHROPIC_API_KEY"]),
            "Anthropic Messages API. GEN_AUDIO_CLAUDE_CODE_CLI is not enabled.",
        )
    }
    fn complete(&self, prompt: &str) -> Result<Completion, ConnectorError> {
        check_prompt(prompt)?;
        if claude_code_enabled() {
            if !live_enabled() {
                return Ok(mock_completion("claude", "claude-code-cli", prompt));
            }
            return run_claude_code(prompt);
        }
        let Some(key) = first_env(&["ANTHROPIC_API_KEY"]) else {
            return Ok(mock_completion("claude", "claude-api", prompt));
        };
        if !live_enabled() {
            return Ok(mock_completion("claude", "claude-api", prompt));
        }
        let model =
            std::env::var("ANTHROPIC_MODEL").unwrap_or_else(|_| "claude-3-5-haiku-latest".into());
        check_model(&model)?;
        let body = json!({
            "model": model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}]
        });
        let text = send_json(
            "https://api.anthropic.com/v1/messages",
            &[("x-api-key", &key), ("anthropic-version", "2023-06-01")],
            &body,
        )?;
        Ok(Completion {
            connector_id: "claude".into(),
            text,
            mock: false,
            model,
        })
    }
}

impl Connector for GptConnector {
    fn id(&self) -> &'static str {
        "gpt"
    }
    fn health(&self) -> HealthReport {
        vendor_health(
            "gpt",
            token_present(&["OPENAI_API_KEY"]),
            "OpenAI chat completions. Base URL must be https, or http loopback.",
        )
    }
    fn complete(&self, prompt: &str) -> Result<Completion, ConnectorError> {
        check_prompt(prompt)?;
        let Some(key) = first_env(&["OPENAI_API_KEY"]) else {
            return Ok(mock_completion("gpt", "openai", prompt));
        };
        if !live_enabled() {
            return Ok(mock_completion("gpt", "openai", prompt));
        }
        let model = std::env::var("OPENAI_MODEL").unwrap_or_else(|_| "gpt-4o-mini".into());
        check_model(&model)?;
        let base =
            std::env::var("OPENAI_BASE_URL").unwrap_or_else(|_| "https://api.openai.com/v1".into());
        let base = require_https_or_loopback(base.trim_end_matches('/'))?;
        let body = chat_body(&model, prompt);
        let authorization = "Bearer ".to_owned() + &key;
        let text = send_json(
            &format!("{base}/chat/completions"),
            &[("authorization", &authorization)],
            &body,
        )?;
        Ok(Completion {
            connector_id: "gpt".into(),
            text,
            mock: false,
            model,
        })
    }
}

impl Connector for GeminiConnector {
    fn id(&self) -> &'static str {
        "gemini"
    }
    fn health(&self) -> HealthReport {
        vendor_health(
            "gemini",
            token_present(&["GEMINI_API_KEY", "GOOGLE_API_KEY"]),
            "Gemini generateContent. The key is sent as the x-goog-api-key header, not the query string.",
        )
    }
    fn complete(&self, prompt: &str) -> Result<Completion, ConnectorError> {
        check_prompt(prompt)?;
        let key = first_env(&["GEMINI_API_KEY", "GOOGLE_API_KEY"]);
        let Some(key) = key else {
            return Ok(mock_completion("gemini", "gemini", prompt));
        };
        if !live_enabled() {
            return Ok(mock_completion("gemini", "gemini", prompt));
        }
        let model = std::env::var("GEMINI_MODEL").unwrap_or_else(|_| "gemini-2.0-flash".into());
        check_model(&model)?;
        let url = format!(
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        );
        let body = json!({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}]
        });
        let text = send_json(&url, &[("x-goog-api-key", &key)], &body)?;
        Ok(Completion {
            connector_id: "gemini".into(),
            text,
            mock: false,
            model,
        })
    }
}

pub fn chat_body(model: &str, prompt: &str) -> Value {
    json!({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256
    })
}

pub fn claude_code_args(bin: &str) -> Vec<String> {
    vec![
        bin.to_string(),
        "-p".into(),
        "--output-format".into(),
        "text".into(),
    ]
}

fn run_claude_code(prompt: &str) -> Result<Completion, ConnectorError> {
    let bin = claude_bin().ok_or_else(|| {
        ConnectorError::new("GEN_AUDIO_CLAUDE_CODE_CLI=1 but no claude binary was found on PATH")
    })?;
    let name = bin.file_name().and_then(|part| part.to_str()).unwrap_or("");
    if name != "claude" && name != "claude.exe" {
        return Err(ConnectorError::new(
            "CLAUDE_CODE_BIN must point at a file named claude",
        ));
    }
    let mut child = std::process::Command::new(&bin);
    child
        .args(["-p", "--output-format", "text"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    for key in [
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
    ] {
        child.env_remove(key);
    }
    let mut child = child
        .spawn()
        .map_err(|err| ConnectorError::new(format!("claude binary failed to start: {err}")))?;
    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        stdin
            .write_all(prompt.as_bytes())
            .map_err(|err| ConnectorError::new(format!("claude stdin: {err}")))?;
    }
    let output = child
        .wait_with_output()
        .map_err(|err| ConnectorError::new(format!("claude binary failed: {err}")))?;
    if !output.status.success() {
        return Err(ConnectorError::new(format!(
            "claude binary exited {}: {}",
            output.status,
            tail(&String::from_utf8_lossy(&output.stderr))
        )));
    }
    Ok(Completion {
        connector_id: "claude".into(),
        text: redact_secrets(&tail(&String::from_utf8_lossy(&output.stdout))),
        mock: false,
        model: "claude-code-cli".into(),
    })
}

fn claude_bin() -> Option<std::path::PathBuf> {
    let configured = first_env(&["CLAUDE_CODE_BIN"]);
    let name = configured.as_deref().unwrap_or("claude");
    let path = which(name)?;
    let file_name = path.file_name().and_then(|part| part.to_str())?;
    (file_name == "claude" || file_name == "claude.exe").then_some(path)
}

fn which(name: &str) -> Option<std::path::PathBuf> {
    if name.contains('\0') || name.contains("..") {
        return None;
    }
    if name.contains('/') || name.contains('\\') {
        let path = std::path::PathBuf::from(name);
        return executable_file(&path).then_some(path);
    }
    let paths = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&paths) {
        let candidate = dir.join(name);
        if executable_file(&candidate) {
            return Some(candidate);
        }
        #[cfg(windows)]
        {
            let exe = dir.join(format!("{name}.exe"));
            if executable_file(&exe) {
                return Some(exe);
            }
        }
    }
    None
}

fn executable_file(path: &std::path::Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        return path
            .metadata()
            .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
            .unwrap_or(false);
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn vendor_health(id: &str, token: bool, detail: &str) -> HealthReport {
    let mode = if !token {
        Mode::Mock
    } else if live_enabled() {
        Mode::Configured
    } else {
        Mode::TokenPresent
    };
    let detail = if !token {
        format!("{detail} Offline mock mode: no credential is configured.")
    } else if live_enabled() {
        format!("{detail} Live calls are enabled, but authentication is not verified until a completion succeeds.")
    } else {
        format!("{detail} Credential present; live calls remain explicitly disabled until GEN_AUDIO_CONNECTOR_LIVE=1.")
    };
    HealthReport {
        connector_id: id.to_string(),
        mode,
        // Possessing a token is not evidence that a remote service accepted it.
        authenticated: false,
        detail,
    }
}

fn mock_completion(id: &str, model: &str, prompt: &str) -> Completion {
    Completion {
        connector_id: id.into(),
        text: format!(
            "mock connector '{id}' did not invoke a provider and this text is not model output. Prompt length: {}.",
            prompt.chars().count()
        ),
        mock: true,
        model: model.into(),
    }
}

// The process environment is one table. Tests that need a credential present
// or absent install an overlay for this thread instead of `set_var`, so a
// neighbour cannot observe the mutation. Unlisted keys still read the process.
#[cfg(test)]
thread_local! {
    static ENV_OVERLAY: RefCell<Option<BTreeMap<String, Option<String>>>> = const { RefCell::new(None) };
}

#[cfg(test)]
struct EnvGuard {
    previous: Option<BTreeMap<String, Option<String>>>,
}

#[cfg(test)]
impl Drop for EnvGuard {
    fn drop(&mut self) {
        let previous = self.previous.take();
        ENV_OVERLAY.with(|slot| *slot.borrow_mut() = previous);
    }
}

#[cfg(test)]
fn bind_env(values: BTreeMap<String, Option<String>>) -> EnvGuard {
    ENV_OVERLAY.with(|slot| {
        let previous = slot.borrow_mut().replace(values);
        EnvGuard { previous }
    })
}

fn env_var(name: &str) -> Result<String, std::env::VarError> {
    #[cfg(test)]
    {
        let over = ENV_OVERLAY.with(|slot| {
            slot.borrow()
                .as_ref()
                .and_then(|map| map.get(name).cloned())
        });
        if let Some(value) = over {
            return match value {
                Some(text) => Ok(text),
                None => Err(std::env::VarError::NotPresent),
            };
        }
    }
    std::env::var(name)
}

fn claude_code_enabled() -> bool {
    env_var("GEN_AUDIO_CLAUDE_CODE_CLI")
        .ok()
        .is_some_and(|value| value.trim() == "1")
}

fn live_enabled() -> bool {
    env_var("GEN_AUDIO_CONNECTOR_LIVE")
        .ok()
        .is_some_and(|value| value.trim() == "1")
}

fn token_present(names: &[&str]) -> bool {
    first_env(names).is_some()
}

fn first_env(names: &[&str]) -> Option<String> {
    names.iter().find_map(|name| {
        env_var(name)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    })
}

fn check_prompt(prompt: &str) -> Result<(), ConnectorError> {
    let len = prompt.chars().count();
    if len == 0 || len > 8_000 {
        return Err(ConnectorError::new("prompt must be 1 to 8000 characters"));
    }
    Ok(())
}

fn check_model(model: &str) -> Result<(), ConnectorError> {
    if model.is_empty()
        || model.len() > 80
        || !model
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'))
        || model.contains("..")
    {
        return Err(ConnectorError::new("model id has unsupported characters"));
    }
    Ok(())
}

fn header_value_ok(value: &str) -> Result<(), ConnectorError> {
    if value.is_empty() || value.chars().any(|ch| ch.is_control()) {
        return Err(ConnectorError::new(
            "header value contains control characters",
        ));
    }
    Ok(())
}

pub fn require_https_or_loopback(url: &str) -> Result<String, ConnectorError> {
    parse_endpoint(url).map_err(ConnectorError::new)?;
    if url.starts_with("https://") && !loopback_endpoint(&url) {
        assert_public_destination(&url).map_err(ConnectorError::new)?;
    }
    Ok(url.to_string())
}

fn loopback_endpoint(url: &str) -> bool {
    let Ok((scheme, host, _, _)) = parse_endpoint(url) else {
        return false;
    };
    scheme == "http" || host == "127.0.0.1" || host == "localhost" || host == "::1"
}

fn parse_endpoint(url: &str) -> Result<(String, String, Option<u16>, String), String> {
    if url.len() < 8 || url.len() > 300 {
        return Err("endpoint length is out of range".into());
    }
    if url
        .chars()
        .any(|ch| ch.is_control() || ch.is_whitespace() || matches!(ch, '\\' | '@' | '?' | '#'))
    {
        return Err("endpoint contains credentials, whitespace, or a query string".into());
    }
    let (scheme, rest) = url
        .split_once("://")
        .ok_or_else(|| "endpoint is missing a scheme".to_string())?;
    if scheme != "https" && scheme != "http" {
        return Err("endpoint scheme must be https or http".into());
    }
    let (hostport, path) = match rest.split_once('/') {
        Some((hostport, path)) => (hostport, format!("/{path}")),
        None => (rest, String::new()),
    };
    if hostport.is_empty() || path.contains("//") {
        return Err("endpoint host is empty".into());
    }
    let (host, port) = split_host_port(hostport)?;
    if scheme == "http" && host != "127.0.0.1" && host != "localhost" {
        return Err("http is only allowed for 127.0.0.1 and localhost".into());
    }
    if host.contains("..") || host.starts_with('.') || host.ends_with('.') {
        return Err("endpoint host is not a single name".into());
    }
    Ok((scheme.to_string(), host, port, path))
}

fn split_host_port(hostport: &str) -> Result<(String, Option<u16>), String> {
    if let Some(inner) = hostport.strip_prefix('[') {
        let (host, rest) = inner
            .split_once(']')
            .ok_or_else(|| "bad ipv6 host".to_string())?;
        let port = if let Some(raw) = rest.strip_prefix(':') {
            Some(parse_port(raw)?)
        } else if rest.is_empty() {
            None
        } else {
            return Err("bad ipv6 host".into());
        };
        if host != "::1" {
            return Err("ipv6 http endpoints must be [::1]".into());
        }
        return Ok((host.to_string(), port));
    }
    let (host, port) = match hostport.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() && port.chars().all(|ch| ch.is_ascii_digit()) => {
            (host, Some(parse_port(port)?))
        }
        _ => (hostport, None),
    };
    if host.is_empty() || host.contains(':') {
        return Err("endpoint host is invalid".into());
    }
    Ok((host.to_string(), port))
}

fn parse_port(raw: &str) -> Result<u16, String> {
    let port: u16 = raw.parse().map_err(|_| "bad port".to_string())?;
    if port == 0 {
        return Err("port must be between 1 and 65535".into());
    }
    Ok(port)
}

fn assert_public_destination(url: &str) -> Result<(), String> {
    let (_, host, port, _) = parse_endpoint(url)?;
    if is_blocked_name(&host) {
        return Err("endpoint host is not a public name".into());
    }
    if let Ok(ip) = host.parse::<std::net::IpAddr>() {
        if ip_blocked(ip) {
            return Err("endpoint address is not public".into());
        }
        return Ok(());
    }
    let allow_private = std::env::var("GEN_AUDIO_CONNECTOR_ALLOW_PRIVATE")
        .ok()
        .as_deref()
        == Some("1");
    if allow_private {
        return Ok(());
    }
    let socket_port = port.unwrap_or(443);
    let looked_up = (host.as_str(), socket_port)
        .to_socket_addrs()
        .map_err(|_| "endpoint host did not resolve".to_string())?;
    let mut saw = false;
    for addr in looked_up {
        saw = true;
        if ip_blocked(addr.ip()) {
            return Err("endpoint resolved to a non-public address".into());
        }
    }
    if !saw {
        return Err("endpoint host did not resolve".into());
    }
    Ok(())
}

fn is_blocked_name(host: &str) -> bool {
    let host = host.to_ascii_lowercase();
    host == "metadata.google.internal"
        || host.ends_with(".local")
        || host.ends_with(".internal")
        || host.ends_with(".localhost")
}

fn ip_blocked(ip: std::net::IpAddr) -> bool {
    match ip {
        std::net::IpAddr::V4(v4) => {
            let [a, b, _, _] = v4.octets();
            v4.is_private()
                || v4.is_loopback()
                || v4.is_link_local()
                || v4.is_unspecified()
                || v4.is_broadcast()
                || v4.is_multicast()
                || a == 0
                || (a == 100 && (b & 0b1100_0000) == 64)
                || (a == 192 && b == 0 && v4.octets()[2] == 2)
                || (a == 198 && b == 51 && v4.octets()[2] == 100)
                || (a == 203 && b == 0 && v4.octets()[2] == 113)
        }
        std::net::IpAddr::V6(v6) => {
            v6.is_loopback()
                || v6.is_unspecified()
                || v6.is_multicast()
                || (v6.segments()[0] & 0xffc0) == 0xfe80
                || (v6.segments()[0] & 0xfe00) == 0xfc00
        }
    }
}

fn send_json(url: &str, headers: &[(&str, &str)], body: &Value) -> Result<String, ConnectorError> {
    require_https_or_loopback(url)?;
    for (_, value) in headers {
        header_value_ok(value)?;
    }
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(8))
        .timeout_read(Duration::from_secs(25))
        .timeout_write(Duration::from_secs(8))
        .redirects(0)
        .build();
    let mut request = agent.request("POST", url);
    for (name, value) in headers {
        request = request.set(name, value);
    }
    let response = request
        .send_json(body.clone())
        .map_err(|err| ConnectorError::new(format!("vendor request failed: {err}")))?;
    let text = response
        .into_string()
        .map_err(|err| ConnectorError::new(format!("vendor response: {err}")))?;
    Ok(redact_secrets(&tail(&text)))
}

fn tail(text: &str) -> String {
    text.chars().take(1500).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn credentials_absent() -> BTreeMap<String, Option<String>> {
        [
            "COPILOT_GITHUB_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GEN_AUDIO_CONNECTOR_LIVE",
            "GEN_AUDIO_CLAUDE_CODE_CLI",
            "CLAUDE_CODE_BIN",
        ]
        .into_iter()
        .map(|name| (name.to_string(), None))
        .collect()
    }

    #[test]
    fn seven_connectors_and_vendor_mocks_do_not_pretend_to_be_models() {
        let reports = health_all();
        assert_eq!(reports.len(), 7);
        let _env = bind_env(credentials_absent());
        for id in ["copilot", "claude", "gpt", "gemini"] {
            let health = health_one(id).unwrap();
            assert_eq!(health.mode, Mode::Mock);
            let done = complete_one(id, "status please").unwrap();
            assert!(done.mock);
            assert!(done.text.contains("not model output"));
        }
        assert_eq!(health_one("mcp").unwrap().mode, Mode::Local);
    }

    #[test]
    fn registry_rejects_duplicate_plugin_ids_and_identity_mismatches() {
        let mut registry = ConnectorRegistry::new();
        registry.register("mcp", local_mcp).unwrap();
        assert!(registry.register("mcp", local_mcp).is_err());
        assert!(registry.register("wrong", local_mcp).is_err());
        assert!(default_registry().unwrap().validate().is_ok());
        let ids: Vec<&str> = default_registry().unwrap().ids().collect();
        assert_eq!(ids.len(), CONNECTOR_IDS.len());
        assert!(CONNECTOR_IDS.iter().all(|expected| ids.contains(expected)));
    }

    #[test]
    fn empty_and_whitespace_credentials_match_call_semantics() {
        for value in [String::new(), "  \t".into()] {
            let mut env = credentials_absent();
            env.insert("ANTHROPIC_API_KEY".into(), Some(value.clone()));
            env.insert("OPENAI_API_KEY".into(), Some(value.clone()));
            env.insert("GEMINI_API_KEY".into(), Some(value));
            let _env = bind_env(env);
            for id in ["claude", "gpt", "gemini"] {
                let health = health_one(id).unwrap();
                assert_eq!(health.mode, Mode::Mock, "{id}");
                assert!(!health.authenticated);
                let done = complete_one(id, "hello").unwrap();
                assert!(done.mock);
            }
        }
    }

    #[test]
    fn claude_cli_health_tracks_switch_and_binary_not_api_key() {
        let mut env = credentials_absent();
        env.insert("GEN_AUDIO_CLAUDE_CODE_CLI".into(), Some("1".into()));
        env.insert(
            "CLAUDE_CODE_BIN".into(),
            Some("definitely-missing-claude-binary".into()),
        );
        env.insert("ANTHROPIC_API_KEY".into(), Some("sk-ant-present".into()));
        let _env = bind_env(env);
        let health = health_one("claude").unwrap();
        assert_eq!(health.mode, Mode::Misconfigured);
        assert!(health.detail.contains("CLI"));
        assert!(health.detail.contains("not used"));
        assert!(!health.authenticated);
    }

    #[test]
    fn token_present_does_not_send() {
        let mut env = credentials_absent();
        env.insert("OPENAI_API_KEY".into(), Some("sk-testtoken123456".into()));
        let _env = bind_env(env);
        let health = health_one("gpt").unwrap();
        assert_eq!(health.mode, Mode::TokenPresent);
        assert!(!health.authenticated);
        let done = complete_one("gpt", "hello").unwrap();
        assert!(done.mock);
        assert!(!done.text.contains("sk-test"));
    }

    #[test]
    fn claude_cli_requires_live_switch_but_ignores_api_key_health() {
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("target")
            .join(format!("claude-health-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let name = if cfg!(windows) {
            "claude.exe"
        } else {
            "claude"
        };
        let bin = root.join(name);
        std::fs::write(&bin, b"test fixture").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = std::fs::metadata(&bin).unwrap().permissions();
            permissions.set_mode(0o700);
            std::fs::set_permissions(&bin, permissions).unwrap();
        }
        let bin = bin.canonicalize().unwrap();
        let mut env = credentials_absent();
        env.insert("GEN_AUDIO_CLAUDE_CODE_CLI".into(), Some(" 1 ".into()));
        env.insert(
            "CLAUDE_CODE_BIN".into(),
            Some(bin.to_string_lossy().into_owned()),
        );
        env.insert("ANTHROPIC_API_KEY".into(), Some(String::new()));
        let _env = bind_env(env);
        let health = health_one("claude").unwrap();
        assert_eq!(health.mode, Mode::Configured);
        assert!(health
            .detail
            .contains("disabled until GEN_AUDIO_CONNECTOR_LIVE=1"));
        let done = complete_one("claude", "hello").unwrap();
        assert!(done.mock);
        assert_eq!(done.model, "claude-code-cli");
        drop(_env);
        std::fs::remove_file(&bin).unwrap();
        std::fs::remove_dir(&root).unwrap();
    }

    #[test]
    fn live_health_is_configured_but_not_claimed_authenticated() {
        let mut env = credentials_absent();
        env.insert("OPENAI_API_KEY".into(), Some("sk-testtoken123456".into()));
        env.insert("GEN_AUDIO_CONNECTOR_LIVE".into(), Some("1".into()));
        let _env = bind_env(env);
        let health = health_one("gpt").unwrap();
        assert_eq!(health.mode, Mode::Configured);
        assert!(!health.authenticated);
        assert!(health.detail.contains("not verified"));
    }

    #[test]
    fn chat_body_and_endpoint_rules() {
        let body = chat_body("gpt-4o-mini", "hi");
        assert_eq!(body["messages"][0]["content"], "hi");
        assert!(require_https_or_loopback("http://example.com").is_err());
        assert!(require_https_or_loopback("http://127.0.0.1.evil.com").is_err());
        assert!(require_https_or_loopback("http://localhost.attacker.com").is_err());
        assert!(require_https_or_loopback("https://user:token@api.openai.com/v1").is_err());
        assert!(require_https_or_loopback("https://169.254.169.254/latest").is_err());
        assert!(require_https_or_loopback("http://127.0.0.1:9/v1").is_ok());
        assert!(parse_endpoint("https://api.openai.com/v1").is_ok());
        assert!(check_model("../etc").is_err());
        assert!(check_model("gemini/flash").is_err());
        let args = claude_code_args("claude");
        assert_eq!(args[1], "-p");
        assert!(!args
            .iter()
            .any(|arg| arg.contains("ping") || arg.contains("sh -c")));
    }
}
