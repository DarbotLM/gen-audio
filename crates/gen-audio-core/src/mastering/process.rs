//! Small bounded, shell-free process runner used only by mastering.

use std::io::Read;
use std::process::{Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

pub(super) struct Limits {
    pub deadline: Duration,
    pub stdout: usize,
    pub stderr: usize,
}

#[derive(Debug)]
pub(super) struct Output {
    pub status: ExitStatus,
    pub _stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

static ACTIVE: AtomicUsize = AtomicUsize::new(0);
const CAPACITY: usize = 4;

struct Permit;

impl Drop for Permit {
    fn drop(&mut self) {
        ACTIVE.fetch_sub(1, Ordering::AcqRel);
    }
}

enum Event {
    Stdout(Result<Vec<u8>, String>),
    Stderr(Result<Vec<u8>, String>),
}

pub(super) fn run(command: &mut Command, limits: Limits) -> Result<Output, String> {
    let started = Instant::now();
    ACTIVE
        .try_update(Ordering::AcqRel, Ordering::Acquire, |active| {
            (active < CAPACITY).then_some(active + 1)
        })
        .map_err(|_| "FFmpeg process capacity exhausted".to_string())?;
    let _permit = Permit;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    tree::configure(command);
    let mut child = command
        .spawn()
        .map_err(|_| "FFmpeg is unavailable or could not be started".to_string())?;
    let process_tree = match tree::Tree::attach(&child) {
        Ok(tree) => tree,
        Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err("FFmpeg process containment failed".into());
        }
    };
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let (send, receive) = mpsc::channel();
    let out_send = send.clone();
    let out_limit = limits.stdout;
    let out_thread = thread::spawn(move || {
        let _ = out_send.send(Event::Stdout(read_bounded(stdout, out_limit, "stdout")));
    });
    let err_limit = limits.stderr;
    let err_thread = thread::spawn(move || {
        let _ = send.send(Event::Stderr(read_bounded(stderr, err_limit, "stderr")));
    });

    let mut stdout = None;
    let mut stderr = None;
    let mut status: Option<ExitStatus> = None;
    let mut result = None;
    while result.is_none() {
        if started.elapsed() >= limits.deadline {
            result = Some(Err("FFmpeg deadline exceeded".to_string()));
            break;
        }
        match receive.recv_timeout(Duration::from_millis(10)) {
            Ok(Event::Stdout(Ok(bytes))) => stdout = Some(bytes),
            Ok(Event::Stderr(Ok(bytes))) => stderr = Some(bytes),
            Ok(Event::Stdout(Err(error))) | Ok(Event::Stderr(Err(error))) => {
                result = Some(Err(error));
                break;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                thread::sleep(Duration::from_millis(1));
            }
        }
        if status.is_none() {
            match child.try_wait() {
                Ok(Some(found)) => {
                    status = Some(found);
                    process_tree.terminate();
                }
                Ok(None) => {}
                Err(_) => {
                    result = Some(Err("FFmpeg process wait failed".into()));
                }
            }
        }
        if let (Some(status), Some(stdout), Some(stderr)) =
            (status, stdout.as_ref(), stderr.as_ref())
        {
            result = Some(Ok(Output {
                status,
                _stdout: stdout.clone(),
                stderr: stderr.clone(),
            }));
        }
    }

    process_tree.terminate();
    if result.as_ref().is_some_and(Result::is_err) {
        let _ = child.kill();
    }
    let waited = child.wait();
    let out_joined = out_thread.join();
    let err_joined = err_thread.join();
    if waited.is_err() || out_joined.is_err() || err_joined.is_err() {
        return Err("FFmpeg process reap failed".into());
    }
    result.expect("bounded process loop always resolves")
}

fn read_bounded(mut pipe: impl Read, cap: usize, stream: &'static str) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    let mut block = [0u8; 8192];
    loop {
        match pipe.read(&mut block) {
            Ok(0) => return Ok(bytes),
            Ok(count) => {
                if count > cap.saturating_sub(bytes.len()) {
                    return Err(format!("FFmpeg {stream} output limit exceeded"));
                }
                bytes.extend_from_slice(&block[..count]);
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err("FFmpeg output read failed".into()),
        }
    }
}

#[cfg(unix)]
mod tree {
    use std::os::unix::process::CommandExt;

    unsafe extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }

    pub(super) fn configure(command: &mut std::process::Command) {
        command.process_group(0);
    }

    pub(super) struct Tree(i32);

    impl Tree {
        pub(super) fn attach(child: &std::process::Child) -> Result<Self, String> {
            i32::try_from(child.id())
                .map(Self)
                .map_err(|_| "process id is not representable".into())
        }

        pub(super) fn terminate(&self) {
            unsafe {
                let _ = kill(-self.0, 9);
            }
        }
    }
}

#[cfg(windows)]
mod tree {
    use std::ffi::c_void;
    use std::os::windows::io::AsRawHandle;
    use std::os::windows::process::CommandExt;

    type Handle = *mut c_void;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        fn TerminateJobObject(job: Handle, code: u32) -> i32;
        fn CloseHandle(handle: Handle) -> i32;
        fn SetInformationJobObject(
            job: Handle,
            class: i32,
            information: *const c_void,
            length: u32,
        ) -> i32;
    }
    #[link(name = "ntdll")]
    unsafe extern "system" {
        fn NtResumeProcess(process: Handle) -> i32;
    }

    #[repr(C)]
    struct BasicLimits {
        process_time: i64,
        job_time: i64,
        flags: u32,
        minimum_working_set: usize,
        maximum_working_set: usize,
        active_processes: u32,
        affinity: usize,
        priority: u32,
        scheduling: u32,
    }

    #[repr(C)]
    struct ExtendedLimits {
        basic: BasicLimits,
        io: [u64; 6],
        process_memory: usize,
        job_memory: usize,
        peak_process_memory: usize,
        peak_job_memory: usize,
    }

    pub(super) fn configure(command: &mut std::process::Command) {
        command.creation_flags(0x0800_0004);
    }

    pub(super) struct Tree(Handle);

    unsafe impl Send for Tree {}

    impl Tree {
        pub(super) fn attach(child: &std::process::Child) -> Result<Self, String> {
            let job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if job.is_null() {
                return Err("could not create FFmpeg job".into());
            }
            let tree = Self(job);
            let mut limits: ExtendedLimits = unsafe { std::mem::zeroed() };
            limits.basic.flags = 0x0000_2000;
            let configured = unsafe {
                SetInformationJobObject(
                    job,
                    9,
                    &limits as *const _ as *const c_void,
                    std::mem::size_of::<ExtendedLimits>() as u32,
                )
            };
            if configured == 0
                || unsafe { AssignProcessToJobObject(job, child.as_raw_handle()) } == 0
                || unsafe { NtResumeProcess(child.as_raw_handle()) } < 0
            {
                return Err("could not contain FFmpeg process".into());
            }
            Ok(tree)
        }

        pub(super) fn terminate(&self) {
            unsafe {
                let _ = TerminateJobObject(self.0, 1);
            }
        }
    }

    impl Drop for Tree {
        fn drop(&mut self) {
            self.terminate();
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }
}
