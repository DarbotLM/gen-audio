//! A small Agent Client Protocol (ACP) agent.
//!
//! ACP itself is session-scoped: `session/new` returns a `sessionId`. That is
//! not an MCP session. This process keeps the map only in memory for the
//! stdio lifetime and does not pin a client to a Serve node.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use gen_audio_connectors::health_all;
use serde_json::{json, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionSemantics {
    Durable,
    ProcessLifetime,
    Stateless,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Session {
    pub cwd: String,
    pub agents: Vec<String>,
    pub voice: String,
    /// Library asset uids bound to this session (max 8). Uids only, never paths.
    pub assets: Vec<String>,
}

/// Storage contract used by ACP. The protocol-facing agent never reaches into a
/// concrete map, so a durable implementation can replace the in-memory default.
pub trait SessionStore: Send + Sync {
    fn semantics(&self) -> SessionSemantics;
    /// Atomically allocate a collision-free session id and insert the session.
    /// Durable stores must make this operation atomic in their backing store.
    fn create(&self, prefix: &str, session: Session) -> Result<String, String>;
    fn put(&self, id: String, session: Session) -> Result<(), String>;
    fn get(&self, id: &str) -> Result<Option<Session>, String>;
    fn delete(&self, id: &str) -> Result<bool, String>;
}

#[derive(Default)]
struct SessionState {
    sessions: HashMap<String, Session>,
    next: u64,
}

pub struct InMemorySessionStore {
    state: Mutex<SessionState>,
}

impl Default for InMemorySessionStore {
    fn default() -> Self {
        Self {
            state: Mutex::new(SessionState {
                sessions: HashMap::new(),
                next: 1,
            }),
        }
    }
}

impl SessionStore for InMemorySessionStore {
    fn semantics(&self) -> SessionSemantics {
        SessionSemantics::ProcessLifetime
    }

    fn create(&self, prefix: &str, session: Session) -> Result<String, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "session store lock poisoned")?;
        loop {
            let candidate = format!("{prefix}-{}", state.next);
            state.next = state
                .next
                .checked_add(1)
                .ok_or("session id space exhausted")?;
            if let std::collections::hash_map::Entry::Vacant(entry) =
                state.sessions.entry(candidate.clone())
            {
                entry.insert(session);
                return Ok(candidate);
            }
        }
    }

    fn put(&self, id: String, session: Session) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "session store lock poisoned")?;
        let stored = state
            .sessions
            .get_mut(&id)
            .ok_or_else(|| format!("cannot update unknown sessionId {id}"))?;
        *stored = session;
        Ok(())
    }

    fn get(&self, id: &str) -> Result<Option<Session>, String> {
        Ok(self
            .state
            .lock()
            .map_err(|_| "session store lock poisoned")?
            .sessions
            .get(id)
            .cloned())
    }

    fn delete(&self, id: &str) -> Result<bool, String> {
        Ok(self
            .state
            .lock()
            .map_err(|_| "session store lock poisoned")?
            .sessions
            .remove(id)
            .is_some())
    }
}

#[derive(Clone, Debug)]
pub struct ToolCall {
    pub name: String,
    pub arguments: Value,
}

pub enum IntentPlan {
    Health,
    Tools(Vec<ToolCall>),
}

pub trait IntentPlugin: Send + Sync {
    fn id(&self) -> &'static str;
    fn supports(&self, prompt: &str) -> bool;
    fn plan(&self, prompt: &str, session: &Session) -> Result<IntentPlan, String>;
}

pub struct IntentRegistry {
    plugins: Vec<Box<dyn IntentPlugin>>,
}

impl IntentRegistry {
    pub fn new() -> Self {
        Self {
            plugins: Vec::new(),
        }
    }

    pub fn register(&mut self, plugin: Box<dyn IntentPlugin>) -> Result<(), String> {
        if self.plugins.iter().any(|item| item.id() == plugin.id()) {
            return Err(format!("duplicate ACP intent plugin {}", plugin.id()));
        }
        self.plugins.push(plugin);
        Ok(())
    }

    fn plan(&self, prompt: &str, session: &Session) -> Result<IntentPlan, String> {
        let matches: Vec<&dyn IntentPlugin> = self
            .plugins
            .iter()
            .map(Box::as_ref)
            .filter(|plugin| plugin.supports(prompt))
            .collect();
        if matches.is_empty() {
            return Err("unsupported intent: no registered ACP plugin accepts this prompt".into());
        }
        if matches.len() > 1 {
            let ids = matches
                .iter()
                .map(|plugin| plugin.id())
                .collect::<Vec<_>>()
                .join(", ");
            return Err(format!(
                "ambiguous intent: multiple ACP plugins matched: {ids}"
            ));
        }
        matches[0].plan(prompt, session)
    }
}

impl Default for IntentRegistry {
    fn default() -> Self {
        let mut registry = Self::new();
        registry
            .register(Box::new(HealthIntent))
            .expect("health plugin is unique");
        registry
            .register(Box::new(ProductIntent))
            .expect("product plugin is unique");
        registry
    }
}

pub struct Agent {
    sessions: Arc<dyn SessionStore>,
    intents: IntentRegistry,
}

impl Agent {
    pub fn new() -> Self {
        Self::with_session_store(Arc::new(InMemorySessionStore::default()))
    }

    pub fn with_session_store(sessions: Arc<dyn SessionStore>) -> Self {
        Self {
            sessions,
            intents: IntentRegistry::default(),
        }
    }

    pub fn register_intent_plugin(&mut self, plugin: Box<dyn IntentPlugin>) -> Result<(), String> {
        self.intents.register(plugin)
    }

    pub fn session_semantics(&self) -> SessionSemantics {
        self.sessions.semantics()
    }
}

impl Default for Agent {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug)]
pub struct AcpOutput {
    pub response: Option<Value>,
    pub notifications: Vec<Value>,
}

pub fn handle(agent: &Agent, message: Value) -> Result<AcpOutput, String> {
    let obj = message.as_object().ok_or("request must be an object")?;
    let id = obj.get("id").cloned();
    if obj.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return Err("jsonrpc must be 2.0".into());
    }
    let method = obj.get("method").and_then(Value::as_str).unwrap_or("");
    let params = obj.get("params").cloned().unwrap_or(json!({}));
    let notification = matches!(id, None | Some(Value::Null));
    let (result, notes) = match method {
        "initialize" => (initialize(agent, &params)?, Vec::new()),
        "session/new" => (session_new(agent, &params)?, Vec::new()),
        "session/prompt" => session_prompt(agent, &params)?,
        "session/cancel" => {
            let _ = session_cancel(agent, &params)?;
            (json!({}), Vec::new())
        }
        _ => return Err(format!("method not found: {method}")),
    };
    if notification {
        return Ok(AcpOutput {
            response: None,
            notifications: notes,
        });
    }
    Ok(AcpOutput {
        response: Some(json!({"jsonrpc":"2.0","id": id, "result": result})),
        notifications: notes,
    })
}

fn initialize(agent: &Agent, params: &Value) -> Result<Value, String> {
    let version = params
        .get("protocolVersion")
        .and_then(Value::as_u64)
        .unwrap_or(1);
    let session_semantics = match agent.session_semantics() {
        SessionSemantics::Durable => "durable",
        SessionSemantics::ProcessLifetime => "process_lifetime_in_memory",
        SessionSemantics::Stateless => "stateless",
    };
    if version != 1 {
        return Err(format!("unsupported ACP protocolVersion {version}"));
    }
    Ok(json!({
        "protocolVersion": 1,
        "agentCapabilities": {
            "loadSession": false,
            "sessionSemantics": session_semantics,
            "promptCapabilities": {"image": false, "audio": false, "embeddedContext": false}
        },
        "agentInfo": {"name": "gen-audio", "version": "0.1.0"},
        "authMethods": []
    }))
}

fn session_new(agent: &Agent, params: &Value) -> Result<Value, String> {
    let cwd = params.get("cwd").and_then(Value::as_str).unwrap_or("");
    if cwd.is_empty() || cwd.chars().count() > 512 {
        return Err("session/new requires cwd".into());
    }
    // ACP requires cwd. This agent does not open it and does not read files from it.
    let agents = params
        .get("agents")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let voice = params
        .get("voice")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if !agents.is_empty() || !voice.is_empty() {
        gen_audio_mcp::control::validate_track(&agents, &voice).map_err(|(_, message)| message)?;
    }
    let agent_ids: Vec<String> = agents
        .iter()
        .filter_map(|value| value.as_str().map(str::to_string))
        .collect();
    let assets = session_assets(params.get("assets"))?;
    let id = agent.sessions.create(
        "gen-audio-session",
        Session {
            cwd: cwd.to_string(),
            agents: agent_ids.clone(),
            voice: voice.clone(),
            assets: assets.clone(),
        },
    )?;
    Ok(json!({"sessionId": id, "agents": agent_ids, "voice": voice, "assets": assets}))
}

/// `session/new` `assets`: up to 8 distinct ga1 uids that resolve in the library catalog.
fn session_assets(value: Option<&Value>) -> Result<Vec<String>, String> {
    use gen_audio_core::asset_catalog::{require, MAX_SESSION_ASSETS};
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let items = value
        .as_array()
        .ok_or("assets must be an array of asset uids")?;
    if items.len() > MAX_SESSION_ASSETS {
        return Err(format!("assets is capped at {MAX_SESSION_ASSETS} uids"));
    }
    let mut out: Vec<String> = Vec::new();
    for item in items {
        let uid = item.as_str().ok_or("asset uid must be a string")?;
        require(uid).map_err(|error| error.rpc().1)?;
        if out.iter().any(|seen| seen == uid) {
            return Err(format!("duplicate asset {uid}"));
        }
        out.push(uid.to_string());
    }
    Ok(out)
}

fn session_prompt(agent: &Agent, params: &Value) -> Result<(Value, Vec<Value>), String> {
    let session_id = params
        .get("sessionId")
        .and_then(Value::as_str)
        .ok_or("session/prompt requires sessionId")?;
    let mut session = agent.sessions.get(session_id)?.ok_or("unknown sessionId")?;
    let prompt = prompt_text(params.get("prompt"))?;
    match agent.intents.plan(&prompt, &session)? {
        IntentPlan::Health => {
            let reports = health_all();
            let failed = reports
                .iter()
                .filter(|item| item.mode == gen_audio_connectors::Mode::Misconfigured)
                .count();
            let lines: Vec<String> = reports
                .iter()
                .map(|item| {
                    format!(
                        "{}={:?}(authenticated={})",
                        item.connector_id, item.mode, item.authenticated
                    )
                })
                .collect();
            let summary = format!(
                "Gen-Audio connector health (local configuration report, not a vendor authentication probe): {}",
                lines.join(", ")
            );
            let note = json!({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": summary}
                }}
            });
            return Ok((
                json!({
                    "stopReason": "end_turn",
                    "status": if failed == 0 { "ok" } else { "failed" },
                    "failed": failed
                }),
                vec![note],
            ));
        }
        IntentPlan::Tools(planned) => {
            let calls = planned;
            let mut changed = false;
            for call in &calls {
                if call.name == "ui_set_sidepane" || call.name == "ui_generate" {
                    if let Some(list) = call.arguments.get("agents").and_then(Value::as_array) {
                        session.agents = list
                            .iter()
                            .filter_map(|value| value.as_str().map(str::to_string))
                            .collect();
                        changed = true;
                    }
                    if let Some(voice) = call.arguments.get("voice").and_then(Value::as_str) {
                        session.voice = voice.to_string();
                        changed = true;
                    }
                }
            }
            if changed {
                agent.sessions.put(session_id.to_string(), session)?;
            }
            let mut notes = Vec::new();
            let mut summary = String::new();
            let mut completed = 0usize;
            let mut failed = Vec::new();
            for (index, call) in calls.iter().enumerate() {
                let tool_id = format!("gen-audio-tool-{}", index + 1);
                notes.push(json!({
                    "jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": session_id, "update": {
                            "sessionUpdate": "tool_call", "toolCallId": tool_id,
                            "title": call.name, "kind": "other", "status": "pending"
                        }
                    }
                }));
                match mcp_tool(&call.name, call.arguments.clone()) {
                    Ok(outcome) => {
                        if !summary.is_empty() {
                            summary.push('\n');
                        }
                        summary.push_str(&outcome.text);
                        for warning in &outcome.warnings {
                            summary.push('\n');
                            summary.push_str(warning);
                        }
                        if let Some(reason) =
                            explicit_tool_failure(&outcome.payload, outcome.is_error)
                        {
                            failed.push(json!({
                                "toolCallId": tool_id,
                                "tool": call.name,
                                "error": reason
                            }));
                            notes.push(json!({
                                "jsonrpc": "2.0", "method": "session/update", "params": {
                                    "sessionId": session_id, "update": {
                                        "sessionUpdate": "tool_call_update", "toolCallId": tool_id,
                                        "status": "failed", "content": [{"type": "text", "text": failed.last().unwrap()["error"]}]
                                    }
                                }
                            }));
                        } else {
                            completed += 1;
                            notes.push(json!({
                                "jsonrpc": "2.0", "method": "session/update", "params": {
                                    "sessionId": session_id, "update": {
                                        "sessionUpdate": "tool_call_update", "toolCallId": tool_id,
                                        "status": "completed", "content": [{"type": "text", "text": outcome.text}]
                                    }
                                }
                            }));
                        }
                    }
                    Err(err) => {
                        if !summary.is_empty() {
                            summary.push('\n');
                        }
                        summary.push_str(&err);
                        failed
                            .push(json!({"toolCallId": tool_id, "tool": call.name, "error": err}));
                        notes.push(json!({
                            "jsonrpc": "2.0", "method": "session/update", "params": {
                                "sessionId": session_id, "update": {
                                    "sessionUpdate": "tool_call_update", "toolCallId": tool_id,
                                    "status": "failed", "content": [{"type": "text", "text": failed.last().unwrap()["error"]}]
                                }
                            }
                        }));
                    }
                }
            }
            summary.push_str(" synthesizedSpeech is false. This agent does not invent a podcast.");
            notes.push(json!({
                "jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id, "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": summary}
                    }
                }
            }));
            let status = if failed.is_empty() {
                "ok"
            } else if completed == 0 {
                "failed"
            } else {
                "partial"
            };
            Ok((
                json!({
                    "stopReason": "end_turn", "status": status, "success": failed.is_empty(),
                    "completedToolCalls": completed, "failedToolCalls": failed
                }),
                notes,
            ))
        }
    }
}

fn explicit_tool_failure(value: &Value, is_error: bool) -> Option<String> {
    if is_error || value.get("ok").and_then(Value::as_bool) == Some(false) {
        let detail = value
            .get("reason")
            .or_else(|| value.get("error"))
            .and_then(Value::as_str)
            .unwrap_or(if is_error {
                "tool returned isError=true"
            } else {
                "tool returned ok=false"
            });
        return Some(detail.to_string());
    }
    if value.get("isError").and_then(Value::as_bool) == Some(true) {
        return Some("tool returned isError=true".into());
    }
    None
}

#[derive(Debug)]
struct ToolOutcome {
    payload: Value,
    text: String,
    is_error: bool,
    warnings: Vec<String>,
}

fn normalize_tool_result(value: Value, warnings: Vec<String>) -> Result<ToolOutcome, String> {
    if let Some(content) = value.get("content") {
        let text = content
            .get(0)
            .and_then(|item| item.get("text"))
            .and_then(Value::as_str)
            .ok_or("MCP tool result has no text content")?;
        let payload: Value = serde_json::from_str(text)
            .map_err(|err| format!("MCP tool result text is not JSON: {err}"))?;
        return Ok(ToolOutcome {
            payload,
            text: text.to_string(),
            is_error: value
                .get("isError")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            warnings,
        });
    }
    Ok(ToolOutcome {
        text: value.to_string(),
        payload: value,
        is_error: false,
        warnings,
    })
}

fn control_tool(name: &str) -> bool {
    matches!(
        name,
        "ui_navigate"
            | "ui_select_tile"
            | "ui_flip"
            | "ui_playback"
            | "ui_set_sidepane"
            | "ui_generate"
            | "ui_compare"
            | "library_rename"
            | "viewport_get"
            | "card_export"
    )
}

fn desktop_addr() -> Result<Option<String>, String> {
    let raw = raw_desktop_addr();
    let Some(addr) = raw else {
        return Ok(None);
    };
    ensure_loopback_target(&addr)?;
    Ok(Some(addr))
}

fn read_configured_addr() -> Option<String> {
    if let Ok(addr) = std::env::var("GEN_AUDIO_MCP_ADDR") {
        let addr = addr.trim();
        if !addr.is_empty() {
            return Some(addr.to_string());
        }
    }
    gen_audio_core::paths::read_mcp_addr()
}

fn raw_desktop_addr() -> Option<String> {
    #[cfg(test)]
    {
        return DESKTOP_ADDR.with(|slot| slot.borrow().clone());
    }
    #[cfg(not(test))]
    read_configured_addr()
}

#[cfg(test)]
fn mcp_addr_env_lock() -> std::sync::MutexGuard<'static, ()> {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    LOCK.lock().unwrap_or_else(|poison| poison.into_inner())
}

fn ensure_loopback_target(addr: &str) -> Result<(), String> {
    gen_audio_mcp::http::ensure_bind_allowed(addr).map_err(|err| err.to_string())?;
    let socket: std::net::SocketAddr = addr
        .parse()
        .map_err(|err| format!("desktop address is not a socket address: {err}"))?;
    if !socket.ip().is_loopback() {
        return Err("ACP refuses a non-loopback desktop address".into());
    }
    Ok(())
}

/// UI actions go to the desktop's bound MCP listener.
/// An in-process server has a different control bus, so it is not a fallback.
fn mcp_tool(name: &str, args: Value) -> Result<ToolOutcome, String> {
    if let Some(addr) = desktop_addr()? {
        return normalize_tool_result(
            gen_audio_mcp::http::tools_call_result(&addr, name, &args)?,
            Vec::new(),
        );
    }
    if control_tool(name) {
        return Err(
            "desktop control endpoint is not bound; refusing to apply this action on an isolated bus"
                .into(),
        );
    }
    let server = gen_audio_mcp::Server::boot();
    let result = gen_audio_mcp::call_tool(&server, &json!({"name": name, "arguments": args}))
        .map_err(|err| err.message);
    let cleanup = std::fs::remove_dir_all(&server.scratch.dir);
    match (result, cleanup) {
        (Ok(value), Ok(())) => normalize_tool_result(value, Vec::new()),
        (Ok(value), Err(err)) => {
            normalize_tool_result(value, vec![format!("scratch cleanup failed: {err}")])
        }
        (Err(err), Ok(())) => Err(err),
        (Err(err), Err(cleanup)) => Err(format!("{err}; scratch cleanup also failed: {cleanup}")),
    }
}

#[cfg(test)]
thread_local! {
    static DESKTOP_ADDR: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
fn bind_desktop_for_test(addr: Option<String>) {
    DESKTOP_ADDR.with(|slot| *slot.borrow_mut() = addr);
}

struct HealthIntent;
struct ProductIntent;

impl IntentPlugin for HealthIntent {
    fn id(&self) -> &'static str {
        "health"
    }
    fn supports(&self, prompt: &str) -> bool {
        matches!(
            prompt.trim().to_ascii_lowercase().as_str(),
            "health" | "status"
        )
    }
    fn plan(&self, _prompt: &str, _session: &Session) -> Result<IntentPlan, String> {
        Ok(IntentPlan::Health)
    }
}

impl IntentPlugin for ProductIntent {
    fn id(&self) -> &'static str {
        "product"
    }
    fn supports(&self, prompt: &str) -> bool {
        prompt.split_whitespace().any(is_verb)
    }
    fn plan(&self, prompt: &str, session: &Session) -> Result<IntentPlan, String> {
        let calls = product_plan(prompt, session);
        if calls.is_empty() {
            return Err(
                "unsupported intent: recognized plugin but the prompt has no executable action"
                    .into(),
            );
        }
        Ok(IntentPlan::Tools(
            calls
                .into_iter()
                .map(|(name, arguments)| ToolCall { name, arguments })
                .collect(),
        ))
    }
}

fn is_verb(token: &str) -> bool {
    matches!(
        token.to_ascii_lowercase().as_str(),
        "agent"
            | "agents"
            | "persona"
            | "personas"
            | "voice"
            | "navigate"
            | "slide"
            | "select"
            | "play"
            | "pause"
            | "seek"
            | "profile"
            | "rename"
            | "generate"
            | "list"
            | "flip"
            | "harvest"
    )
}

fn product_plan(prompt: &str, track: &Session) -> Vec<(String, Value)> {
    let tokens: Vec<String> = prompt.split_whitespace().map(str::to_string).collect();
    let mut calls = Vec::new();
    let mut agents: Vec<String> = Vec::new();
    let mut voice: Option<String> = None;
    let mut generate = false;
    let mut index = 0;
    while index < tokens.len() {
        let verb = tokens[index].to_ascii_lowercase();
        match verb.as_str() {
            "agent" | "agents" | "persona" | "personas" => {
                index += 1;
                while index < tokens.len() && !is_verb(&tokens[index]) {
                    agents.push(tokens[index].to_ascii_lowercase());
                    index += 1;
                }
            }
            "voice" => {
                index += 1;
                if index < tokens.len() && !is_verb(&tokens[index]) {
                    voice = Some(tokens[index].to_ascii_lowercase());
                    index += 1;
                }
            }
            "navigate" | "slide" => {
                index += 1;
                if index < tokens.len() && !is_verb(&tokens[index]) {
                    calls.push((
                        "ui_navigate".into(),
                        json!({"slide": tokens[index].to_ascii_lowercase()}),
                    ));
                    index += 1;
                }
            }
            "select" => {
                index += 1;
                if index < tokens.len() && !is_verb(&tokens[index]) {
                    calls.push(("ui_select_tile".into(), json!({"tileId": tokens[index]})));
                    index += 1;
                }
            }
            "play" | "pause" => {
                let action = verb;
                index += 1;
                if index < tokens.len() && !is_verb(&tokens[index]) {
                    calls.push((
                        "ui_playback".into(),
                        json!({"tileId": tokens[index], "action": action}),
                    ));
                    index += 1;
                }
            }
            "seek" => {
                index += 1;
                if index + 1 < tokens.len() {
                    let seconds: f64 = tokens[index + 1].parse().unwrap_or(-1.0);
                    calls.push((
                        "ui_playback".into(),
                        json!({"tileId": tokens[index], "action": "seek", "seconds": seconds}),
                    ));
                    index += 2;
                }
            }
            "profile" => {
                index += 1;
                if index < tokens.len() && !is_verb(&tokens[index]) {
                    calls.push((
                        "voice_profile_get".into(),
                        json!({"personaId": tokens[index].to_ascii_lowercase()}),
                    ));
                    index += 1;
                }
            }
            "rename" => {
                index += 1;
                if index < tokens.len() {
                    let clip = tokens[index].clone();
                    index += 1;
                    let mut args = json!({"clipId": clip});
                    while index < tokens.len() && tokens[index].contains('=') {
                        if let Some((key, value)) = tokens[index].split_once('=') {
                            if key == "semantic" {
                                args["semanticName"] = json!(value);
                            } else if key == "face" {
                                args["faceName"] = json!(value);
                            }
                        }
                        index += 1;
                    }
                    calls.push(("library_rename".into(), args));
                }
            }
            "generate" => {
                generate = true;
                index += 1;
            }
            "flip" => {
                index += 1;
                if index < tokens.len() && !is_verb(&tokens[index]) {
                    let tile = tokens[index].clone();
                    index += 1;
                    let mut args = json!({"tileId": tile, "flipped": true});
                    if index < tokens.len() && !is_verb(&tokens[index]) {
                        args["personaId"] = json!(tokens[index].to_ascii_lowercase());
                        index += 1;
                    }
                    calls.push(("ui_flip".into(), args));
                }
            }
            "harvest" => {
                index += 1;
                if index < tokens.len() && !is_verb(&tokens[index]) {
                    let clip = tokens[index].clone();
                    index += 1;
                    let mut args = json!({"clipId": clip, "apply": false});
                    if index < tokens.len() && !is_verb(&tokens[index]) {
                        args["personaId"] = json!(tokens[index].to_ascii_lowercase());
                        args["apply"] = json!(true);
                        index += 1;
                    }
                    calls.push(("library_harvest".into(), args));
                }
            }
            "list" => {
                calls.push(("voice_profile_list".into(), json!({})));
                index += 1;
            }
            _ => index += 1,
        }
    }
    let fallback_agents = if track.agents.is_empty() {
        vec!["alice".to_string()]
    } else {
        track.agents.clone()
    };
    let fallback_voice = if track.voice.is_empty() {
        "kokoro_onnx".to_string()
    } else {
        track.voice.clone()
    };
    if !agents.is_empty() || voice.is_some() {
        let agents = if agents.is_empty() {
            fallback_agents
        } else {
            agents
        };
        let voice = voice.unwrap_or(fallback_voice);
        let args = json!({"agents": agents, "voice": voice});
        if generate {
            calls.insert(0, ("ui_generate".into(), args));
        } else {
            calls.insert(0, ("ui_set_sidepane".into(), args));
        }
    } else if generate {
        calls.insert(
            0,
            (
                "ui_generate".into(),
                json!({"agents": fallback_agents, "voice": fallback_voice}),
            ),
        );
    }
    if generate {
        let voice_id = calls
            .iter()
            .find(|(name, _)| name == "ui_generate")
            .and_then(|(_, args)| args.get("voice").and_then(Value::as_str))
            .unwrap_or("");
        let adapter = gen_audio_core::catalog::voice_model(voice_id)
            .map(|model| model.synth_adapter)
            .unwrap_or(false);
        if adapter {
            calls.push((
                "synth".into(),
                json!({"output": "request.wav", "script": "examples/podcast_script_sample.txt"}),
            ));
        }
    }
    calls
}

fn session_cancel(agent: &Agent, params: &Value) -> Result<Value, String> {
    let session_id = params
        .get("sessionId")
        .and_then(Value::as_str)
        .ok_or("session/cancel requires sessionId")?;
    agent.sessions.delete(session_id)?;
    Ok(json!({}))
}

fn prompt_text(prompt: Option<&Value>) -> Result<String, String> {
    let blocks = prompt
        .and_then(Value::as_array)
        .ok_or("prompt must be an array of content blocks")?;
    let mut text = String::new();
    for block in blocks {
        if block.get("type").and_then(Value::as_str) == Some("text") {
            if let Some(piece) = block.get("text").and_then(Value::as_str) {
                text.push_str(piece);
            }
        }
    }
    if text.chars().count() > 8_000 {
        return Err("prompt is too long".into());
    }
    Ok(text)
}

pub fn smoke() -> Result<String, String> {
    let agent = Agent::new();
    let init = handle(
        &agent,
        json!({
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": false, "writeTextFile": false}, "terminal": false},
                "clientInfo": {"name": "gen-audio-smoke", "version": "0.1.0"}
            }
        }),
    )?;
    let version = init
        .response
        .as_ref()
        .and_then(|value| value["result"]["protocolVersion"].as_u64())
        .ok_or("initialize missing protocolVersion")?;
    if version != 1 {
        return Err("unexpected protocol version".into());
    }
    let created = handle(
        &agent,
        json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": ".", "mcpServers": []}
        }),
    )?;
    let session_id = created.response.as_ref().unwrap()["result"]["sessionId"]
        .as_str()
        .unwrap()
        .to_string();
    let prompted = handle(
        &agent,
        json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "health"}]
            }
        }),
    )?;
    let stop = prompted.response.as_ref().unwrap()["result"]["stopReason"].as_str();
    if stop != Some("end_turn") {
        return Err("prompt did not end the turn".into());
    }
    if prompted.notifications.is_empty() {
        return Err("prompt produced no session/update".into());
    }
    Ok("acp smoke ok".into())
}

pub fn stdio_loop(agent: &Agent) {
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let message: Value = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(_) => {
                let _ = writeln!(
                    stdout,
                    "{}",
                    json!({"jsonrpc":"2.0","id": null, "error": {"code": -32700, "message": "parse error"}})
                );
                let _ = stdout.flush();
                continue;
            }
        };
        let request_id = message.get("id").cloned().unwrap_or(Value::Null);
        match handle(agent, message) {
            Ok(output) => {
                for note in output.notifications {
                    let _ = writeln!(stdout, "{note}");
                }
                if let Some(response) = output.response {
                    let _ = writeln!(stdout, "{response}");
                }
                let _ = stdout.flush();
            }
            Err(err) => {
                let id = request_id;
                if id.is_null() {
                    continue;
                }
                let _ = writeln!(
                    stdout,
                    "{}",
                    json!({"jsonrpc":"2.0","id": id, "error": {"code": -32603, "message": err}})
                );
                let _ = stdout.flush();
            }
        }
    }
}

use std::io::{BufRead, Write};

#[cfg(test)]
mod tests {
    use super::*;

    use gen_audio_core::bridge::NoKokoroEnv;

    #[test]
    fn tool_results_normalize_inner_payloads_and_failures() {
        let direct =
            normalize_tool_result(json!({"ok": false, "reason": "refused"}), Vec::new()).unwrap();
        assert_eq!(direct.payload["ok"], false);
        assert_eq!(
            explicit_tool_failure(&direct.payload, direct.is_error).as_deref(),
            Some("refused")
        );

        let enveloped = normalize_tool_result(
            json!({
                "content": [{"type": "text", "text": "{\"ok\":false,\"reason\":\"refused\"}"}],
                "isError": true
            }),
            Vec::new(),
        )
        .unwrap();
        assert_eq!(enveloped.payload["ok"], false);
        assert!(enveloped.is_error);
        assert_eq!(
            explicit_tool_failure(&enveloped.payload, enveloped.is_error).as_deref(),
            Some("refused")
        );

        let outer_error = normalize_tool_result(
            json!({
                "content": [{"type": "text", "text": "{\"ok\":true}"}],
                "isError": true
            }),
            Vec::new(),
        )
        .unwrap();
        assert!(explicit_tool_failure(&outer_error.payload, outer_error.is_error).is_some());

        let direct_ok =
            normalize_tool_result(json!({"ok": true, "queued": 4}), Vec::new()).unwrap();
        assert_eq!(direct_ok.text, "{\"ok\":true,\"queued\":4}");
        assert!(explicit_tool_failure(&direct_ok.payload, direct_ok.is_error).is_none());
    }

    #[test]
    fn handshake_smoke() {
        assert_eq!(smoke().unwrap(), "acp smoke ok");
    }

    #[test]
    fn shared_session_store_allocates_without_overwriting_ids() {
        let store = Arc::new(InMemorySessionStore::default());
        let first_agent = Agent::with_session_store(store.clone());
        let second_agent = Agent::with_session_store(store.clone());
        let first = handle(
            &first_agent,
            json!({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"first"}}),
        )
        .unwrap();
        let second = handle(
            &second_agent,
            json!({"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"second"}}),
        )
        .unwrap();
        let first_id = first.response.as_ref().unwrap()["result"]["sessionId"]
            .as_str()
            .unwrap();
        let second_id = second.response.as_ref().unwrap()["result"]["sessionId"]
            .as_str()
            .unwrap();
        assert_ne!(first_id, second_id);
        assert_eq!(store.get(first_id).unwrap().unwrap().cwd, "first");
        assert_eq!(store.get(second_id).unwrap().unwrap().cwd, "second");
    }

    #[test]
    fn cwd_is_not_opened_and_errors_keep_the_request_id() {
        let agent = Agent::new();
        let created = handle(
            &agent,
            json!({"jsonrpc":"2.0","id":7,"method":"session/new","params":{"cwd":"/this/path/is/not/opened"}}),
        )
        .unwrap();
        assert!(created.response.unwrap()["result"]["sessionId"]
            .as_str()
            .unwrap()
            .starts_with("gen-audio-session-"));
        let missing = handle(
            &agent,
            json!({"jsonrpc":"2.0","id":8,"method":"session/load","params":{}}),
        )
        .unwrap_err();
        assert!(missing.contains("method not found"));
        let bad = handle(
            &agent,
            json!({"jsonrpc":"1.0","id":9,"method":"initialize","params":{"protocolVersion":1}}),
        )
        .unwrap_err();
        assert!(bad.contains("2.0"));
    }

    #[test]
    fn unsupported_intents_are_errors_and_plugin_ids_are_unique() {
        struct Never;
        impl IntentPlugin for Never {
            fn id(&self) -> &'static str {
                "health"
            }
            fn supports(&self, _prompt: &str) -> bool {
                false
            }
            fn plan(&self, _prompt: &str, _session: &Session) -> Result<IntentPlan, String> {
                unreachable!()
            }
        }
        let mut agent = Agent::new();
        assert!(agent.register_intent_plugin(Box::new(Never)).is_err());
        let created = handle(
            &agent,
            json!({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"."}}),
        )
        .unwrap();
        let session_id = created.response.unwrap()["result"]["sessionId"]
            .as_str()
            .unwrap()
            .to_string();
        let error = handle(
            &agent,
            json!({"jsonrpc":"2.0","id":2,"method":"session/prompt","params":{
                "sessionId": session_id, "prompt":[{"type":"text","text":"frobnicate this"}]
            }}),
        )
        .unwrap_err();
        assert!(error.contains("unsupported intent"), "{error}");
    }

    #[test]
    fn prompt_calls_the_voice_profile_tool() {
        let agent = Agent::new();
        let created = handle(
            &agent,
            json!({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"."}}),
        )
        .unwrap();
        let session_id = created.response.unwrap()["result"]["sessionId"]
            .as_str()
            .unwrap()
            .to_string();
        let prompted = handle(
            &agent,
            json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "profile alice"}]
                }
            }),
        )
        .unwrap();
        let blob = serde_json::to_string(&prompted.notifications).unwrap();
        assert!(blob.contains("voice_profile_get"), "{blob}");
        assert!(blob.contains("af_heart"), "{blob}");
        assert!(blob.contains("tool_call"), "{blob}");
        assert!(blob.contains("synthesizedSpeech"), "{blob}");
        assert!(!blob.contains("\"synthesizedSpeech\":true"));
    }

    #[test]
    fn session_persona_generate_calls_synth_and_does_not_invent_speech() {
        let _env = NoKokoroEnv::new();
        let agent = Agent::new();
        let created = handle(
            &agent,
            json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {"cwd": ".", "agents": ["alice"], "voice": "kokoro_onnx"}
            }),
        )
        .unwrap();
        let result = created.response.unwrap();
        assert_eq!(result["result"]["agents"][0], "alice");
        assert_eq!(result["result"]["voice"], "kokoro_onnx");
        let session_id = result["result"]["sessionId"].as_str().unwrap().to_string();
        let prompted = handle(
            &agent,
            json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "generate"}]
                }
            }),
        )
        .unwrap();
        assert_eq!(
            prompted.response.as_ref().unwrap()["result"]["success"],
            false
        );
        assert_eq!(
            prompted.response.as_ref().unwrap()["result"]["status"],
            "failed"
        );
        assert!(
            !prompted.response.as_ref().unwrap()["result"]["failedToolCalls"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        let blob = serde_json::to_string(&prompted.notifications).unwrap();
        assert!(blob.contains("ui_generate"), "{blob}");
        assert!(blob.contains("synth"), "{blob}");
        assert!(blob.contains("No speech was invented"), "{blob}");
        assert!(blob.contains("tool_call"), "{blob}");
        assert!(!blob.contains("\"synthesizedSpeech\":true"), "{blob}");
        assert!(
            blob.contains("GEN_AUDIO_KOKORO_MODEL and GEN_AUDIO_KOKORO_VOICES are unset"),
            "{blob}"
        );
        let rejected = handle(
            &agent,
            json!({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/new",
                "params": {"cwd": ".", "agents": ["copilot"], "voice": "af_heart"}
            }),
        )
        .unwrap_err();
        assert!(
            rejected.contains("connector") || rejected.contains("af_heart"),
            "{rejected}"
        );
    }

    #[test]
    fn desktop_target_must_be_loopback() {
        bind_desktop_for_test(None);
        let _guard = mcp_addr_env_lock();
        let saved = std::env::var("GEN_AUDIO_MCP_ADDR").ok();
        std::env::set_var("GEN_AUDIO_MCP_ADDR", "10.1.8.70:8765");
        assert_eq!(desktop_addr().unwrap(), None);
        let err = ensure_loopback_target(&read_configured_addr().expect("env addr")).unwrap_err();
        assert!(
            err.contains("loopback") || err.contains("non-loopback"),
            "{err}"
        );
        match saved {
            Some(value) => std::env::set_var("GEN_AUDIO_MCP_ADDR", value),
            None => std::env::remove_var("GEN_AUDIO_MCP_ADDR"),
        }
    }

    #[test]
    fn session_new_binds_library_asset_uids() {
        let agent = Agent::new();
        let clip = gen_audio_core::asset_catalog::uid_for_legacy("audio_clip", "lib-misaki-kokoro")
            .unwrap();
        let out = handle(
            &agent,
            json!({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":".","assets":[clip, "ga:cube_ihdr:biaxxmnibxtcur7nxdu3ffvna4"]}}),
        )
        .unwrap();
        let result = &out.response.unwrap()["result"];
        assert_eq!(result["assets"][0], clip);
        let session = result["sessionId"].as_str().unwrap();
        assert_eq!(
            agent.sessions.get(session).unwrap().unwrap().assets.len(),
            2
        );
        for bad in [
            json!(["ga:cube_ihdr:aaaaaaaaaaaaaaaaaaaaaaaaaa"]),
            json!(["lib-misaki-kokoro"]),
            json!([clip, clip]),
            json!(vec![clip; 9]),
            json!("ga:cube_ihdr:biaxxmnibxtcur7nxdu3ffvna4"),
        ] {
            let err = handle(
                &agent,
                json!({"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":".","assets":bad}}),
            );
            assert!(err.is_err(), "{bad} should be rejected");
        }
    }

    #[test]
    fn t14_navigate_reaches_the_bound_desktop_bus() {
        let addr = gen_audio_mcp::http::spawn_loopback("127.0.0.1:0").unwrap();
        let addr = addr.to_string();
        bind_desktop_for_test(Some(addr.clone()));
        let before = gen_audio_mcp::http::get_control(&addr, 0).unwrap();
        let cursor = before["cursor"].as_u64().unwrap_or(0);
        let agent = Agent::new();
        let created = handle(
            &agent,
            json!({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"."}}),
        )
        .unwrap();
        let session_id = created.response.unwrap()["result"]["sessionId"]
            .as_str()
            .unwrap()
            .to_string();
        let prompted = handle(
            &agent,
            json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "navigate spatial"}]
                }
            }),
        )
        .unwrap();
        bind_desktop_for_test(None);
        let blob = serde_json::to_string(&prompted.notifications).unwrap();
        assert!(blob.contains("ui_navigate"), "{blob}");
        assert!(!blob.contains("isolated bus"), "{blob}");
        let after = gen_audio_mcp::http::get_control(&addr, cursor).unwrap();
        let events = after["events"].as_array().cloned().unwrap_or_default();
        assert!(
            events.iter().any(|event| {
                event["op"] == "navigate" && event["args"]["slide"] == "slide:spatial"
            }),
            "{after}"
        );
        let view = gen_audio_mcp::http::tools_call(&addr, "viewport_get", &json!({})).unwrap();
        assert_eq!(view["ui"]["slide"], "slide:spatial");
    }

    #[test]
    fn ui_navigate_without_a_desktop_does_not_touch_an_isolated_bus() {
        bind_desktop_for_test(None);
        let marker = format!("isolated-{}", std::process::id());
        let err =
            mcp_tool("ui_navigate", json!({"slide": "spatial", "tileId": marker})).unwrap_err();
        assert!(err.contains("not bound"), "{err}");
        let delta = gen_audio_mcp::control::since(0);
        assert!(
            delta
                .events
                .iter()
                .all(|event| event["args"]["tileId"] != marker),
            "isolated ACP published onto the local bus"
        );
    }
}
