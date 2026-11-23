use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::thread;
use std::time::Duration;

fn measurement(final_output: bool) -> &'static str {
    if final_output {
        r#"{"input_i":"-16.00","input_tp":"-1.50","input_lra":"4.00","input_thresh":"-26.00","output_i":"-16.00","output_tp":"-1.50","output_lra":"4.00","output_thresh":"-26.00","normalization_type":"linear","target_offset":"0.00"}"#
    } else {
        r#"{"input_i":"-26.00","input_tp":"-20.00","input_lra":"2.50","input_thresh":"-36.00","output_i":"-16.00","output_tp":"-10.00","output_lra":"2.50","output_thresh":"-26.00","normalization_type":"linear","target_offset":"0.10"}"#
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.as_slice() == ["hang"] {
        thread::sleep(Duration::from_secs(60));
        return;
    }
    if args.as_slice() == ["flood-stderr"] {
        let block = vec![b'x'; 8192];
        let mut stderr = std::io::stderr().lock();
        for _ in 0..16 {
            let _ = stderr.write_all(&block);
            let _ = stderr.flush();
        }
        return;
    }
    let executable = std::env::current_exe()
        .ok()
        .and_then(|path| {
            path.file_name()
                .map(|name| name.to_string_lossy().into_owned())
        })
        .unwrap_or_default();
    if executable.contains("fake-ffmpeg-fail") {
        eprintln!(r#"private command failure: C:\private\secret ffmpeg -i unavailable.wav"#);
        std::process::exit(7);
    }
    let Some(input_index) = args.iter().position(|arg| arg == "-i") else {
        eprintln!("missing -i");
        std::process::exit(2);
    };
    let Some(input) = args.get(input_index + 1) else {
        eprintln!("missing input");
        std::process::exit(2);
    };
    let is_null = args.windows(3).any(|window| window == ["-f", "null", "-"]);
    let final_output =
        Path::new(input).file_name().and_then(|name| name.to_str()) == Some("render.wav");
    eprintln!("{}", measurement(final_output));
    if is_null {
        return;
    }
    let Some(output) = args.last() else {
        std::process::exit(3);
    };
    if !args.iter().any(|arg| arg == "-n") {
        std::process::exit(4);
    }
    let mut bytes = std::fs::read(input).expect("read fixture WAV");
    if bytes.len() > 44 {
        bytes[44] ^= 1;
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output)
        .expect("create no-clobber output");
    file.write_all(&bytes).expect("write fixture WAV");
    file.sync_all().expect("sync fixture WAV");
}
