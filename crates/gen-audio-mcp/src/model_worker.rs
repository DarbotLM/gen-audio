use std::time::Duration;

use gen_audio_core::redact::redact_secrets;
use serde_json::{json, Value};
use ureq::http::Uri;

/// Response ceiling from the native worker wire contract.
const MAX_RESPONSE_BYTES: u64 = 4 * 1024 * 1024;

fn endpoint(engine: &str) -> Result<String, String> {
    let (key, port) = match engine {
        "vibevoice" => ("GEN_AUDIO_VIBEVOICE_WORKER_URL", 8996),
        "personaplex" => ("GEN_AUDIO_PERSONAPLEX_WORKER_URL", 8997),
        _ => return Err("engine must be vibevoice or personaplex".into()),
    };
    let base = std::env::var(key)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| format!("http://127.0.0.1:{port}"));
    loopback_base(&base).ok_or_else(|| format!("{key} must be a loopback http URL without a path"))
}

/// Parse the URL instead of matching a prefix: `http://127.0.0.1:1@host/` names `host`.
fn loopback_base(value: &str) -> Option<String> {
    let base = value.trim().trim_end_matches('/');
    let uri: Uri = base.parse().ok()?;
    let authority = uri.authority()?;
    let loopback = matches!(uri.host()?, "127.0.0.1" | "localhost" | "[::1]");
    let plain = matches!(uri.scheme_str(), Some("http" | "https"))
        && !authority.as_str().contains('@')
        && matches!(uri.path(), "" | "/")
        && uri.query().is_none();
    (loopback && plain).then(|| base.to_string())
}

pub fn call(action: &str, args: &Value) -> Result<Value, String> {
    let engine = args
        .get("engine")
        .and_then(Value::as_str)
        .ok_or("engine is required")?;
    let base = endpoint(engine)?;
    let (method, path, timeout) = match action {
        "status" => ("GET", "/v1/status", 10),
        "preflight" => ("GET", "/v1/preflight", 120),
        "load" => ("POST", "/v1/load", 1_800),
        "unload" => ("POST", "/v1/unload", 120),
        "generate" => ("POST", "/v1/generate", 1_800),
        _ => return Err("unknown model-worker action".into()),
    };
    // Status codes stay as responses: the worker explains refusals in its JSON body.
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_connect(Some(Duration::from_secs(5)))
        .timeout_recv_response(Some(Duration::from_secs(timeout)))
        .timeout_recv_body(Some(Duration::from_secs(timeout)))
        .max_redirects(0)
        .proxy(None)
        .http_status_as_error(false)
        .build()
        .into();
    let result = if method == "GET" {
        agent.get(&(base + path)).call()
    } else {
        let payload = if action == "generate" {
            args.clone()
        } else {
            json!({})
        };
        agent.post(&(base + path)).send_json(payload)
    };
    let mut response = match result {
        Ok(response) => response,
        Err(error) => {
            return Ok(failure(
                engine,
                "worker_unavailable",
                &redact_secrets(&error.to_string()),
            ))
        }
    };
    let status = response.status();
    let body = response
        .body_mut()
        .with_config()
        .limit(MAX_RESPONSE_BYTES)
        .read_to_string();
    let parsed = body
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .filter(Value::is_object);
    Ok(match parsed {
        // An HTTP error must never read as success, whatever the body claims.
        Some(mut value) if !status.is_success() => {
            value["ok"] = json!(false);
            value["synthesizedSpeech"] = json!(false);
            value["httpStatus"] = json!(status.as_u16());
            value
        }
        Some(value) => value,
        None if !status.is_success() => failure(
            engine,
            "worker_http_error",
            &format!("model worker returned HTTP {}", status.as_u16()),
        ),
        None => failure(
            engine,
            "worker_bad_response",
            "model worker response was not a JSON object within 4 MiB",
        ),
    })
}

fn failure(engine: &str, code: &str, message: &str) -> Value {
    json!({
        "ok": false,
        "synthesizedSpeech": false,
        "engine": engine,
        "errors": [{"code": code, "message": message}]
    })
}

#[cfg(test)]
mod tests {
    use super::{call, endpoint, loopback_base};
    use serde_json::json;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::Mutex;

    static ENV: Mutex<()> = Mutex::new(());

    #[test]
    fn accepts_only_loopback_bases() {
        for good in [
            "http://127.0.0.1:8996",
            "http://localhost:8996/",
            "http://[::1]:8996",
        ] {
            assert!(loopback_base(good).is_some(), "{good}");
        }
        for bad in [
            "http://example.com:8996",
            "http://127.0.0.1:8996@example.com",
            "http://127.0.0.1:80@example.com:8996/",
            "http://127.0.0.1.example.com:8996",
            "http://localhost:8996/other",
            "file:///tmp/worker",
            "not a url",
        ] {
            assert!(loopback_base(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn rejects_unknown_engines() {
        assert!(endpoint("kokoro_onnx").is_err());
        assert!(call("status", &json!({})).is_err());
    }

    fn reply_once(status: &'static str, body: &'static str) -> serde_json::Value {
        let _guard = ENV.lock().unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buffer = [0_u8; 4096];
            let _ = stream.read(&mut buffer).unwrap();
            write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        });
        std::env::set_var("GEN_AUDIO_PERSONAPLEX_WORKER_URL", url);
        let value = call("status", &json!({"engine": "personaplex"})).unwrap();
        std::env::remove_var("GEN_AUDIO_PERSONAPLEX_WORKER_URL");
        server.join().unwrap();
        value
    }

    #[test]
    fn keeps_worker_error_body_and_never_reports_it_as_success() {
        let value = reply_once(
            "409 Conflict",
            r#"{"ok":true,"errors":[{"code":"busy","message":"generating"}]}"#,
        );
        assert_eq!(value["ok"], false);
        assert_eq!(value["httpStatus"], 409);
        assert_eq!(value["errors"][0]["code"], "busy");
    }

    #[test]
    fn passes_status_through_and_refuses_non_json() {
        let value = reply_once("200 OK", r#"{"ok":true,"loaded":false}"#);
        assert_eq!(value["loaded"], false);
        let bad = reply_once("200 OK", "[]");
        assert_eq!(bad["errors"][0]["code"], "worker_bad_response");
    }
}
