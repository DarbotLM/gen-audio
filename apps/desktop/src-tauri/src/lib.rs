use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use gen_audio_mcp::http;

pub mod sidecar_log;
use serde::Serialize;
#[cfg(debug_assertions)]
use serde_json::Value;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, WindowEvent};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// Hide console for console-subsystem children (MCP sidecar is CUI).
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Serialize)]
struct McpRuntime {
    addr: String,
    token: Option<String>,
    handshake_ok: bool,
    detail: String,
    mode: String,
}

struct SidecarChild(Mutex<Option<Child>>);

#[tauri::command]
fn connector_statuses() -> Vec<gen_audio_connectors::HealthReport> {
    gen_audio_connectors::health_all()
}

/// Dev/test-only commands (E1 addendum): they serve the labeled fixture deck
/// and run Python improve on the fixture tone. Compiled, and so registered,
/// only under debug_assertions; the release exe that `tauri build` makes has
/// neither (see `invoke_handler`).
pub const DEV_ONLY_COMMANDS: [&str; 2] = ["viewport_example", "run_fixture_improve"];

#[cfg(debug_assertions)]
#[tauri::command]
fn viewport_example() -> Value {
    serde_json::from_str(include_str!(
        "../../../../schemas/examples/viewport.example.json"
    ))
    .expect("example viewport is valid json")
}

#[cfg(debug_assertions)]
#[tauri::command]
fn run_fixture_improve() -> Result<Value, String> {
    use gen_audio_core::bridge::{self, PythonTool};
    use gen_audio_core::fixture::write_fixture_tone;
    use serde_json::json;
    let server = gen_audio_mcp::Server::boot();
    write_fixture_tone(&server.scratch)?;
    let repo = server.repo.ok_or("repository root not found")?;
    let plan = bridge::plan(
        PythonTool::Improve,
        &repo,
        &server.scratch,
        &json!({"input": "fixture-tone.wav", "output": "fixture-24k.wav"}),
    )?;
    let ran = bridge::run_plan(&plan)?;
    Ok(json!({
        "ok": ran.get("ok").cloned().unwrap_or(Value::Bool(false)),
        "code": ran.get("code").cloned().unwrap_or(Value::Null),
        "synthesizedSpeech": false,
        "fixture": true
    }))
}

#[tauri::command]
fn mcp_status(state: tauri::State<'_, Mutex<McpRuntime>>) -> Result<McpRuntime, String> {
    state
        .lock()
        .map(|guard| guard.clone())
        .map_err(|_| "mcp status lock".into())
}

fn preferred_addr() -> String {
    preferred_addr_from(std::env::var("GEN_AUDIO_MCP_ADDR").ok().as_deref())
}

fn preferred_addr_from(configured: Option<&str>) -> String {
    const DEFAULT: &str = "127.0.0.1:8765";
    let Some(raw) = configured.map(str::trim).filter(|value| !value.is_empty()) else {
        return DEFAULT.into();
    };
    let Ok(socket) = raw.parse::<std::net::SocketAddr>() else {
        return DEFAULT.into();
    };
    if socket.ip() != std::net::Ipv4Addr::LOCALHOST || !(8765..=8770).contains(&socket.port()) {
        return DEFAULT.into();
    }
    socket.to_string()
}

fn sidecar_binary() -> Option<PathBuf> {
    #[cfg(test)]
    if let Some(path) = std::env::var_os("GEN_AUDIO_DESKTOP_TEST_SIDECAR") {
        return Some(PathBuf::from(path));
    }
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    ["gen-audio-mcp.exe", "gen-audio-mcp"]
        .into_iter()
        .map(|name| dir.join(name))
        .find(|path| path.is_file())
}

fn runtime(
    addr: String,
    token: Option<String>,
    handshake_ok: bool,
    detail: impl Into<String>,
    mode: &str,
) -> McpRuntime {
    McpRuntime {
        addr,
        token,
        handshake_ok,
        detail: detail.into(),
        mode: mode.into(),
    }
}

fn wait_for_handshake(addr: &str, token: &str, boot_nonce: &str, child: &mut Child) -> bool {
    let started = Instant::now();
    while started.elapsed() < Duration::from_secs(8) {
        if !matches!(child.try_wait(), Ok(None)) {
            return false;
        }
        if http::initialize_owned_handshake(addr, token, boot_nonce).is_ok() {
            return matches!(child.try_wait(), Ok(None));
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}

fn hide_console(cmd: &mut Command) {
    #[cfg(windows)]
    {
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let _ = cmd;
}

/// Hidden helper process with every stdio stream closed (reg add).
#[cfg(windows)]
fn spawn_hidden(mut cmd: Command) -> std::io::Result<Child> {
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    hide_console(&mut cmd);
    cmd.spawn()
}

/// The MCP sidecar, hidden, with its stderr (one line per rejected request,
/// startup errors) drained into the rotating gen-audio-mcp.log in the app log
/// dir instead of thrown away (see sidecar_log).
fn spawn_sidecar(mut cmd: Command, log_dir: Option<PathBuf>) -> std::io::Result<Child> {
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    hide_console(&mut cmd);
    let mut child = cmd.spawn()?;
    if let Some(stderr) = child.stderr.take() {
        sidecar_log::spawn_pump(stderr, log_dir);
    }
    Ok(child)
}

fn boot_mcp(log_dir: Option<PathBuf>) -> (McpRuntime, Option<Child>) {
    gen_audio_core::paths::reap_stale_mcp_addr();
    let addr = preferred_addr();
    if let Err(err) = http::ensure_bind_allowed(&addr) {
        return (runtime(addr, None, false, err.to_string(), "failed"), None);
    }
    let token = match http::generate_token() {
        Ok(token) => token,
        Err(err) => return (runtime(addr, None, false, err.to_string(), "failed"), None),
    };
    let boot_nonce = match http::generate_token() {
        Ok(nonce) => nonce,
        Err(err) => return (runtime(addr, None, false, err.to_string(), "failed"), None),
    };

    // Reserve only our chosen port. The reservation is released immediately
    // before spawn; the boot nonce still proves that a racing listener cannot
    // masquerade as the child.
    if let (Some(bin), Some((reservation, sidecar_addr))) =
        (sidecar_binary(), reserve_sidecar_addr(&addr))
    {
        drop(reservation);
        let mut cmd = Command::new(&bin);
        cmd.args(["--http", &sidecar_addr])
            .env("GEN_AUDIO_MCP_TOKEN", &token)
            .env("GEN_AUDIO_MCP_BOOT_NONCE", &boot_nonce);
        match spawn_sidecar(cmd, log_dir) {
            Ok(mut child) => {
                if wait_for_handshake(&sidecar_addr, &token, &boot_nonce, &mut child) {
                    let owner_pid = child.id();
                    match gen_audio_core::paths::publish_mcp_connection_for_owner(
                        &sidecar_addr,
                        &token,
                        owner_pid,
                    ) {
                        Ok(()) => {
                            return (
                                runtime(
                                    sidecar_addr,
                                    Some(token),
                                    true,
                                    "authenticated initialize ok (owned sidecar)",
                                    "sidecar",
                                ),
                                Some(child),
                            );
                        }
                        Err(err) => {
                            eprintln!("gen-audio: MCP descriptor publish failed: {err}");
                        }
                    }
                }
                let owner_pid = child.id();
                if let Err(cleanup) = stop_sidecar(&mut child) {
                    eprintln!("gen-audio: failed sidecar cleanup: {cleanup}");
                }
                let _ = gen_audio_core::paths::delete_mcp_addr_for_owner(owner_pid);
            }
            Err(err) => {
                // The in-process listener still starts below. The sidecar
                // error has to be visible or a missing binary looks like a
                // successful boot.
                eprintln!("gen-audio: MCP sidecar failed to start: {err}");
            }
        }
    }

    // A racing foreign listener may have seen the failed child's bearer
    // token. The owned in-process fallback always receives fresh credentials.
    let token = match http::generate_token() {
        Ok(token) => token,
        Err(err) => return (runtime(addr, None, false, err.to_string(), "failed"), None),
    };
    let boot_nonce = match http::generate_token() {
        Ok(nonce) => nonce,
        Err(err) => return (runtime(addr, None, false, err.to_string(), "failed"), None),
    };
    match bind_loopback_range(&addr, &token, &boot_nonce) {
        Ok(bound) => {
            let text = bound.to_string();
            match http::initialize_owned_handshake(&text, &token, &boot_nonce) {
                Ok(_) => {}
                Err(err) => {
                    return (runtime(text, None, false, err, "in-process"), None);
                }
            }
            if let Err(err) = gen_audio_core::paths::publish_mcp_connection(&text, &token) {
                return (
                    runtime(
                        text,
                        None,
                        false,
                        format!("MCP descriptor publish failed: {err}"),
                        "in-process",
                    ),
                    None,
                );
            }
            let detail = if text == addr {
                "initialize ok (in-process)".to_string()
            } else {
                format!("{addr} was busy; bound {text}. Clients must use this address.")
            };
            (
                runtime(
                    text,
                    Some(token),
                    true,
                    format!("authenticated {detail}"),
                    "in-process",
                ),
                None,
            )
        }
        Err(err) => (runtime(addr, None, false, err.to_string(), "failed"), None),
    }
}

#[cfg(windows)]
fn register_login_autostart() {
    if !autostart_enabled(std::env::var("GEN_AUDIO_AUTOSTART").ok().as_deref()) {
        return;
    }
    let Ok(exe) = std::env::current_exe() else {
        return;
    };
    let value = format!("\"{}\"", exe.display());
    let mut cmd = Command::new("reg");
    cmd.args([
        "add",
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        "/v",
        "DarbotGenAudio",
        "/t",
        "REG_SZ",
        "/d",
        &value,
        "/f",
    ]);
    let _ = spawn_hidden(cmd).and_then(|mut child| child.wait());
}

#[cfg(not(windows))]
fn register_login_autostart() {}

#[cfg(any(windows, test))]
fn autostart_enabled(value: Option<&str>) -> bool {
    value == Some("1")
}

fn stop_sidecar(child: &mut Child) -> Result<(), String> {
    let kill = match child.try_wait().map_err(|err| err.to_string())? {
        Some(_) => Ok(()),
        None => child.kill().map_err(|err| err.to_string()),
    };
    let wait = child.wait().map_err(|err| err.to_string());
    kill.and(wait.map(|_| ()))
}

fn loopback_candidates(preferred: &str) -> Vec<String> {
    let mut candidates = Vec::new();
    if !preferred.is_empty() {
        candidates.push(preferred.to_string());
    }
    let start = preferred
        .parse::<std::net::SocketAddr>()
        .map(|socket| socket.port())
        .unwrap_or(8765);
    let end = 8770;
    for port in start..=end {
        let addr = format!("127.0.0.1:{port}");
        if !candidates.iter().any(|item| item == &addr) {
            candidates.push(addr);
        }
    }
    candidates
}

fn reserve_sidecar_addr(preferred: &str) -> Option<(std::net::TcpListener, String)> {
    for candidate in loopback_candidates(preferred) {
        if let Ok(listener) = std::net::TcpListener::bind(&candidate) {
            let addr = listener.local_addr().ok()?.to_string();
            return Some((listener, addr));
        }
    }
    None
}

fn bind_loopback_range(
    preferred: &str,
    token: &str,
    boot_nonce: &str,
) -> std::io::Result<std::net::SocketAddr> {
    let start = preferred
        .parse::<std::net::SocketAddr>()
        .map(|socket| socket.port())
        .unwrap_or(8765);
    let end = 8770;
    let mut last = std::io::Error::other(format!(
        "no configured loopback port in {start}-{end} was free"
    ));
    for addr in loopback_candidates(preferred) {
        match http::spawn_owned_loopback(&addr, token, boot_nonce) {
            Ok(bound) => return Ok(bound),
            Err(err) => last = err,
        }
    }
    Err(last)
}

fn request_quit(app: &tauri::AppHandle) {
    cleanup_runtime(app);
    app.exit(0);
}

fn cleanup_runtime(app: &tauri::AppHandle) {
    let mut owner_pid = std::process::id();
    if let Some(state) = app.try_state::<SidecarChild>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.as_mut() {
                owner_pid = child.id();
                if let Err(err) = stop_sidecar(child) {
                    eprintln!("gen-audio: MCP sidecar cleanup warning: {err}");
                }
            }
        }
    }
    if let Err(err) = gen_audio_core::paths::delete_mcp_addr_for_owner(owner_pid) {
        eprintln!("gen-audio: MCP descriptor cleanup warning: {err}");
    }
}

fn focus_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// The IPC commands this build registers. Debug builds add DEV_ONLY_COMMANDS;
/// release builds cannot, because those functions are not compiled there.
#[cfg(debug_assertions)]
fn invoke_handler() -> impl Fn(tauri::ipc::Invoke) -> bool + Send + Sync + 'static {
    tauri::generate_handler![
        connector_statuses,
        viewport_example,
        run_fixture_improve,
        mcp_status
    ]
}

#[cfg(not(debug_assertions))]
fn invoke_handler() -> impl Fn(tauri::ipc::Invoke) -> bool + Send + Sync + 'static {
    tauri::generate_handler![connector_statuses, mcp_status]
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Placeholder until setup boots MCP (second instance exits in the plugin
    // before setup, so it never starts a second sidecar or tray).
    let pending = runtime(preferred_addr(), None, false, "starting", "starting");

    let mut builder = tauri::Builder::default();

    #[cfg(desktop)]
    {
        // Must be first so a second launch focuses the existing window and exits
        // before other plugins / setup can open another tray.
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // Second launch with --quit invokes the same path as tray Quit (sidecar stop + exit).
            if args.iter().any(|a| a == "--quit") {
                request_quit(app);
            } else {
                focus_main_window(app);
            }
        }));
    }

    let app = builder
        .manage(Mutex::new(pending))
        .manage(SidecarChild(Mutex::new(None)))
        .invoke_handler(invoke_handler())
        .setup(move |app| {
            let (mcp, child) = boot_mcp(app.path().app_log_dir().ok());
            if mcp.handshake_ok {
                register_login_autostart();
            }
            let tooltip = format!("Gen-Audio MCP {} ({})", mcp.addr, mcp.mode);
            if let Ok(mut guard) = app.state::<Mutex<McpRuntime>>().lock() {
                *guard = mcp;
            }
            if let Ok(mut guard) = app.state::<SidecarChild>().0.lock() {
                *guard = child;
            }

            let show = MenuItem::with_id(app, "show", "Show Gen-Audio", true, None::<&str>)?;
            let status = MenuItem::with_id(app, "mcp", tooltip.clone(), false, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &status, &quit])?;
            let icon = tray_rgba();
            let _ = TrayIconBuilder::with_id("gen-audio")
                .tooltip(tooltip)
                .icon(icon)
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => focus_main_window(app),
                    "quit" => request_quit(app),
                    _ => {}
                })
                .build(app);

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Product: X hides to tray; Quit is tray-only. Do this even if tray
                // build failed so a second launch can still focus/show via single-instance.
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while running Gen-Audio");

    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            cleanup_runtime(app);
        }
    });
}

#[cfg(test)]
mod sidecar_boot {
    use super::{boot_mcp, stop_sidecar, wait_for_handshake};
    use std::fs;
    use std::io::Read;
    use std::path::PathBuf;
    use std::process::{Command, Output, Stdio};
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    const TEST_NAME: &str = "sidecar_boot::sidecar_spawn_failure_is_logged_and_boot_continues";

    fn fake_sidecar_path() -> PathBuf {
        std::env::var_os("GEN_AUDIO_DESKTOP_TEST_SIDECAR")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                std::env::current_exe()
                    .expect("current_exe")
                    .parent()
                    .expect("exe dir")
                    .join("gen-audio-mcp")
            })
    }

    /// Runs `cmd` with stdout and stderr captured (stdin null), but never
    /// waits past `deadline`. A child still running then is killed and
    /// reaped; a child that exited while some other process still holds its
    /// stdout/stderr pipe (an inherited handle) is reported as that. Either
    /// way the test fails in seconds with what was printed so far, instead of
    /// `Command::output()` blocking for as long as the pipe stays open.
    fn output_within(mut cmd: Command, deadline: Duration) -> Result<Output, String> {
        let mut child = cmd
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|err| format!("spawn: {err}"))?;
        let started = Instant::now();
        let (out_tx, out_rx) = mpsc::channel();
        let (err_tx, err_rx) = mpsc::channel();
        let mut out = child.stdout.take().expect("stdout pipe");
        let mut err = child.stderr.take().expect("stderr pipe");
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = out.read_to_end(&mut buf);
            let _ = out_tx.send(buf);
        });
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = err.read_to_end(&mut buf);
            let _ = err_tx.send(buf);
        });
        let status = loop {
            if let Some(status) = child.try_wait().map_err(|err| format!("try_wait: {err}"))? {
                break status;
            }
            if started.elapsed() >= deadline {
                let pid = child.id();
                let _ = child.kill();
                let _ = child.wait();
                let stdout = out_rx
                    .recv_timeout(Duration::from_secs(2))
                    .unwrap_or_default();
                let stderr = err_rx
                    .recv_timeout(Duration::from_secs(2))
                    .unwrap_or_default();
                return Err(format!(
                    "child pid {pid} did not exit within {deadline:?}; killed and reaped\nstdout:\n{}\nstderr:\n{}",
                    String::from_utf8_lossy(&stdout),
                    String::from_utf8_lossy(&stderr)
                ));
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        let remaining = |started: Instant| {
            deadline
                .saturating_sub(started.elapsed())
                .max(Duration::from_secs(2))
        };
        let stdout = out_rx.recv_timeout(remaining(started)).map_err(|_| {
            format!("child exited ({status:?}) but its stdout pipe stayed open past {deadline:?}: another process inherited it")
        })?;
        let stderr = err_rx.recv_timeout(remaining(started)).map_err(|_| {
            format!("child exited ({status:?}) but its stderr pipe stayed open past {deadline:?}: another process inherited it")
        })?;
        Ok(Output {
            status,
            stdout,
            stderr,
        })
    }
    /// Stand-in for a boot child that never exits. Runs only when spawned.
    #[test]
    #[ignore = "spawned by output_within_kills_a_child_that_outlives_the_deadline"]
    fn stalled_child() {
        if std::env::var("GEN_AUDIO_DESKTOP_STALL_CHILD").is_err() {
            return;
        }
        std::thread::sleep(Duration::from_secs(20));
    }

    /// DD-8: the sidecar boot test hung for ~8 minutes in one scripts/test.ps1
    /// run (artifacts/test-logs/20261123-103041-dd6b) because the re-exec used
    /// `Command::output()`, which has no deadline. A stalled child must fail
    /// the test in seconds, killed and reaped, never hang the gate.
    #[test]
    fn output_within_kills_a_child_that_outlives_the_deadline() {
        let mut cmd = Command::new(std::env::current_exe().expect("current_exe"));
        cmd.args([
            "--exact",
            "sidecar_boot::stalled_child",
            "--ignored",
            "--nocapture",
        ])
        .env("GEN_AUDIO_DESKTOP_STALL_CHILD", "1");
        let started = Instant::now();
        let result = output_within(cmd, Duration::from_secs(2));
        let elapsed = started.elapsed();
        assert!(
            elapsed < Duration::from_secs(10),
            "a stalled child held the test for {elapsed:?}"
        );
        let err = result.expect_err("a stalled child must be an error, not a pass");
        assert!(err.contains("did not exit within"), "{err}");
    }

    #[test]
    fn sidecar_spawn_failure_is_logged_and_boot_continues() {
        if std::env::var("GEN_AUDIO_DESKTOP_BOOT_MCP").ok().as_deref() == Some("1") {
            let fake = fake_sidecar_path();
            if fake.is_file() {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    let mode = fs::metadata(&fake).expect("fake meta").permissions().mode();
                    if mode & 0o111 != 0 {
                        eprintln!(
                            "gen-audio: refusing to replace executable {}",
                            fake.display()
                        );
                        std::process::exit(2);
                    }
                }
                let _ = fs::remove_file(&fake);
            }
            fs::write(&fake, b"not an executable sidecar").expect("write fake sidecar");
            let (runtime, child) = boot_mcp(None);
            let _ = fs::remove_file(&fake);
            println!("mode={}", runtime.mode);
            println!("handshake={}", runtime.handshake_ok);
            println!("child={}", child.is_some());
            let _ = std::io::Write::flush(&mut std::io::stdout());
            let ok = runtime.mode == "in-process" && child.is_none();
            std::process::exit(if ok { 0 } else { 3 });
        }

        let work =
            std::env::temp_dir().join(format!("gen-audio-desktop-boot-{}", std::process::id()));
        let _ = fs::remove_dir_all(&work);
        fs::create_dir_all(&work).expect("work dir");
        let addr_file = work.join("mcp.addr");
        let mut cmd = Command::new(std::env::current_exe().expect("current_exe"));
        cmd.args(["--exact", TEST_NAME, "--nocapture"])
            .env("GEN_AUDIO_DESKTOP_BOOT_MCP", "1")
            .env(
                "GEN_AUDIO_DESKTOP_TEST_SIDECAR",
                work.join("invalid-sidecar.exe"),
            )
            .env("GEN_AUDIO_MCP_ADDR", "127.0.0.1:0")
            .env("GEN_AUDIO_WORK_DIR", &work)
            .env("GEN_AUDIO_MCP_ADDR_FILE", &addr_file);
        let output = output_within(cmd, Duration::from_secs(60));
        let _ = fs::remove_dir_all(&work);
        let output = output.expect("re-exec the desktop test harness");
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            output.status.success(),
            "child status {:?}\nstdout:\n{stdout}\nstderr:\n{stderr}",
            output.status
        );
        assert!(
            stderr.contains("gen-audio: MCP sidecar failed to start:"),
            "spawn failure was not logged\nstderr:\n{stderr}"
        );
        assert!(
            stdout.contains("mode=in-process"),
            "boot did not continue in-process\nstdout:\n{stdout}\nstderr:\n{stderr}"
        );
        assert!(
            stdout.contains("handshake=true"),
            "in-process listener did not handshake\nstdout:\n{stdout}\nstderr:\n{stderr}"
        );
        assert!(
            stdout.contains("child=false"),
            "a failed spawn still returned a child\nstdout:\n{stdout}"
        );
    }

    #[test]
    fn child_exit_fails_handshake_promptly_and_is_reaped() {
        let mut child = Command::new(std::env::current_exe().expect("current_exe"))
            .arg("--definitely-not-a-libtest-option")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn exiting child");
        let started = std::time::Instant::now();
        let token = gen_audio_mcp::http::generate_token().unwrap();
        let nonce = gen_audio_mcp::http::generate_token().unwrap();
        assert!(!wait_for_handshake(
            "127.0.0.1:1",
            &token,
            &nonce,
            &mut child,
        ));
        assert!(
            started.elapsed() < std::time::Duration::from_secs(2),
            "an exited sidecar was not rejected promptly"
        );
        stop_sidecar(&mut child).expect("reap exited child");
        assert!(
            child.try_wait().unwrap().is_some(),
            "exited sidecar was not reaped"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::{autostart_enabled, preferred_addr_from};

    #[test]
    fn desktop_mcp_addr_stays_inside_the_csp_port_range() {
        assert_eq!(
            preferred_addr_from(Some("127.0.0.1:8769")),
            "127.0.0.1:8769"
        );
        for raw in [
            "127.0.0.1:8764",
            "127.0.0.1:8771",
            "127.0.0.1:9000",
            "[::1]:8765",
            "example.com:8765",
            "not-an-address",
        ] {
            assert_eq!(preferred_addr_from(Some(raw)), "127.0.0.1:8765");
        }
    }

    #[test]
    fn login_autostart_requires_exact_explicit_opt_in() {
        for value in [None, Some("0"), Some("true"), Some(""), Some("yes")] {
            assert!(!autostart_enabled(value), "{value:?}");
        }
        assert!(autostart_enabled(Some("1")));
    }
}

fn tray_rgba() -> tauri::image::Image<'static> {
    let mut rgba = vec![0u8; 32 * 32 * 4];
    for pixel in rgba.chunks_mut(4) {
        pixel.copy_from_slice(&[24, 168, 154, 255]);
    }
    tauri::image::Image::new_owned(rgba, 32, 32)
}
