use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Read},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::Manager;

const ALLOWED_METHODS: &[&str] = &[
    "get_symbols",
    "get_settings",
    "save_settings",
    "get_backend_status",
    "get_dashboard_data",
    "get_decision_workspace",
    "get_edge_lab",
    "create_journal_entry",
    "update_journal_entry",
    "delete_journal_entry",
    "get_journal_entries",
    "get_weekly_journal_review",
    "get_trace_dates",
    "get_trace_data",
    "build_one_off_profile",
    "get_one_off_profiles",
    "get_one_off_profile",
    "get_trade_setups",
    "get_market_overview",
    "get_runtime_info",
    "get_events",
    "start_collector",
    "stop_collector",
    "start_ninjatrader",
    "stop_ninjatrader",
];

#[derive(Deserialize)]
struct Ready {
    #[serde(rename = "type")]
    kind: String,
    port: u16,
    token: String,
    data_dir: PathBuf,
}

fn parse_ready(line: &str) -> Result<Ready, String> {
    let ready: Ready = serde_json::from_str(line).map_err(|_| {
        "The local analytics service returned an invalid startup response.".to_owned()
    })?;
    if ready.kind != "ready"
        || ready.port == 0
        || ready.token.len() < 24
        || ready
            .token
            .bytes()
            .any(|b| !b.is_ascii_alphanumeric() && b != b'_' && b != b'-')
        || !ready.data_dir.is_absolute()
    {
        return Err("The local analytics service returned an unsafe startup response.".to_owned());
    }
    Ok(ready)
}

fn validate_request(method: &str, args: &[Value]) -> Result<(), String> {
    if !ALLOWED_METHODS.contains(&method) {
        return Err("This analytics operation is not available.".to_owned());
    }
    if args.len() > 16 {
        return Err("Too many arguments for an analytics operation.".to_owned());
    }
    Ok(())
}

struct Backend {
    client: reqwest::Client,
    endpoint: String,
    token: String,
    child: Mutex<Option<Child>>,
    stopping: AtomicBool,
    #[cfg(windows)]
    _job: std::os::windows::io::OwnedHandle,
}

impl Backend {
    fn start(app: &tauri::AppHandle) -> Result<Self, String> {
        let demo = std::env::var("PUBLICGEX_DEMO").is_ok_and(|value| value == "1");
        let data_dir = if cfg!(debug_assertions) {
            project_root().join(if demo { ".demo-data" } else { "data" })
        } else {
            let base = app
                .path()
                .app_local_data_dir()
                .map_err(|error| error.to_string())?;
            if demo {
                base.join(".demo-data")
            } else {
                base.join("data")
            }
        };
        fs::create_dir_all(&data_dir)
            .map_err(|error| format!("Cannot create the app data folder: {error}"))?;
        let stderr = OpenOptions::new()
            .create(true)
            .append(true)
            .open(data_dir.join("backend.log"))
            .map_err(|error| format!("Cannot create the backend log: {error}"))?;

        let mut command = backend_command()?;
        command.args(["--data-dir", &data_dir.to_string_lossy(), "--port", "0"]);
        if demo {
            command.arg("--demo");
        }
        command
            .current_dir(&data_dir)
            .env_remove("PYTHONPATH")
            .env_remove("PYTHONHOME")
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(stderr));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("Cannot start local analytics: {error}"))?;
        #[cfg(windows)]
        let job = match attach_process_job(&child) {
            Ok(job) => job,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        let stdout = child
            .stdout
            .take()
            .ok_or("The local analytics startup pipe is unavailable.")?;
        let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let mut line = String::new();
            let result = BufReader::new(stdout)
                .take(65_536)
                .read_line(&mut line)
                .map_err(|_| "Cannot read the local analytics startup response.".to_owned())
                .and_then(|_| parse_ready(&line));
            let _ = sender.send(result);
        });
        let ready = receiver
            .recv_timeout(Duration::from_secs(60))
            .map_err(|_| {
                "Local analytics did not become ready. Check backend.log in the app data folder."
                    .to_owned()
            })
            .and_then(|result| result);
        let ready = match ready {
            Ok(ready) => ready,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        if ready.data_dir.canonicalize().ok() != data_dir.canonicalize().ok() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("The local analytics service opened an unexpected data folder.".to_owned());
        }
        let client = match reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(180))
            .build()
        {
            Ok(client) => client,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "Cannot initialize local analytics transport: {error}"
                ));
            }
        };
        Ok(Self {
            client,
            endpoint: format!("http://127.0.0.1:{}/rpc", ready.port),
            token: ready.token,
            child: Mutex::new(Some(child)),
            stopping: AtomicBool::new(false),
            #[cfg(windows)]
            _job: job,
        })
    }

    async fn request(
        &self,
        method: &str,
        args: Vec<Value>,
        timeout: Duration,
    ) -> Result<Value, String> {
        let response = self.client.post(&self.endpoint)
            .bearer_auth(&self.token).timeout(timeout)
            .json(&json!({"method": method, "args": args}))
            .send().await.map_err(|error| {
                if error.is_timeout() { "The analytics request timed out. Try again after the current collection finishes.".to_owned() }
                else { "The local analytics service is unavailable. Restart the dashboard to reconnect.".to_owned() }
            })?;
        let status = response.status();
        let value: Value = response
            .json()
            .await
            .map_err(|_| "The local analytics service returned an invalid response.".to_owned())?;
        if let Some(error) = value.get("error") {
            return Err(error
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| error.to_string()));
        }
        if !status.is_success() {
            return Err(format!("The local analytics request failed ({status})."));
        }
        value
            .get("result")
            .cloned()
            .ok_or_else(|| "The analytics response did not contain a result.".to_owned())
    }

    async fn stop(&self) {
        let _ = self
            .request("shutdown", Vec::new(), Duration::from_secs(3))
            .await;
        let deadline = Instant::now() + Duration::from_secs(12);
        loop {
            let finished = {
                let mut guard = self.child.lock().unwrap_or_else(|error| error.into_inner());
                match guard.as_mut() {
                    Some(child) => child.try_wait().ok().flatten().is_some(),
                    None => true,
                }
            };
            if finished || Instant::now() >= deadline {
                break;
            }
            tokio::time::sleep(Duration::from_millis(80)).await;
        }
        self.kill_child();
    }

    fn kill_child(&self) {
        let mut guard = self.child.lock().unwrap_or_else(|error| error.into_inner());
        if let Some(mut child) = guard.take() {
            // Closing stdin also asks the Python service to stop if its HTTP loop failed.
            child.stdin.take();
            if child.try_wait().ok().flatten().is_none() {
                let _ = child.kill();
            }
            let _ = child.wait();
        }
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.kill_child();
    }
}

#[cfg(windows)]
fn attach_process_job(child: &Child) -> Result<std::os::windows::io::OwnedHandle, String> {
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    // A job contains only this newly spawned backend and its descendants. Closing
    // its handle on native exit prevents orphaned collector/PyInstaller children.
    unsafe {
        let raw = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if raw.is_null() {
            return Err("Cannot create the analytics process owner.".to_owned());
        }
        let job = OwnedHandle::from_raw_handle(raw);
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = SetInformationJobObject(
            job.as_raw_handle(),
            JobObjectExtendedLimitInformation,
            (&info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if configured == 0
            || AssignProcessToJobObject(job.as_raw_handle(), child.as_raw_handle()) == 0
        {
            return Err("Cannot safely supervise the analytics process tree.".to_owned());
        }
        Ok(job)
    }
}

fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("project parent")
        .to_path_buf()
}

fn backend_command() -> Result<Command, String> {
    if cfg!(debug_assertions) {
        let root = project_root();
        let python = if cfg!(windows) {
            root.join(".venv/Scripts/python.exe")
        } else {
            root.join(".venv/bin/python")
        };
        if !python.is_file() {
            return Err("The project Python environment is missing. Run the setup command in the README first.".to_owned());
        }
        let mut command = Command::new(python);
        command.arg(root.join("backend/service.py"));
        Ok(command)
    } else {
        let filename = if cfg!(windows) {
            "publicgex-backend.exe"
        } else {
            "publicgex-backend"
        };
        let binary = std::env::current_exe()
            .map_err(|error| error.to_string())?
            .with_file_name(filename);
        if !binary.is_file() {
            return Err(
                "The bundled analytics service is missing. Reinstall PublicGex Dashboard."
                    .to_owned(),
            );
        }
        Ok(Command::new(binary))
    }
}

struct BackendState(Arc<Backend>);

#[tauri::command]
async fn backend_request(
    state: tauri::State<'_, BackendState>,
    method: String,
    args: Vec<Value>,
) -> Result<Value, String> {
    validate_request(&method, &args)?;
    if state.0.stopping.load(Ordering::Acquire) {
        return Err("The dashboard is closing.".to_owned());
    }
    let timeout = if method == "build_one_off_profile" {
        Duration::from_secs(180)
    } else {
        Duration::from_secs(30)
    };
    state.0.request(&method, args, timeout).await
}

pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let backend = Backend::start(app.handle()).map_err(std::io::Error::other)?;
            app.manage(BackendState(Arc::new(backend)));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_request])
        .build(tauri::generate_context!())
        .expect("PublicGex Dashboard could not start");
    app.run(|handle, event| match event {
        tauri::RunEvent::ExitRequested { api, .. } => {
            if let Some(state) = handle.try_state::<BackendState>() {
                if !state.0.stopping.swap(true, Ordering::AcqRel) {
                    api.prevent_exit();
                    let backend = Arc::clone(&state.0);
                    let app_handle = handle.clone();
                    tauri::async_runtime::spawn(async move {
                        backend.stop().await;
                        app_handle.exit(0);
                    });
                }
            }
        }
        tauri::RunEvent::Exit => {
            if let Some(state) = handle.try_state::<BackendState>() {
                state.0.kill_child();
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_invalid_startup_messages_without_echoing_tokens() {
        for line in [
            "not json",
            r#"{"type":"ready","port":0,"token":"private-value","data_dir":"relative"}"#,
        ] {
            let error = parse_ready(line).err().expect("invalid handshake rejected");
            assert!(!error.contains("private-value"));
        }
    }

    #[test]
    fn accepts_random_loopback_port_and_absolute_data_directory() {
        let line = json!({"type":"ready", "port":49152, "token":"abcdefghijklmnopqrstuvwxyz012345", "data_dir":std::env::temp_dir()}).to_string();
        assert_eq!(parse_ready(&line).unwrap().port, 49152);
    }

    #[test]
    fn frontend_cannot_request_shutdown_or_arbitrary_functions() {
        assert!(validate_request("get_market_overview", &[]).is_ok());
        assert!(validate_request("shutdown", &[]).is_err());
        assert!(validate_request("__import__", &[]).is_err());
        assert!(validate_request("get_symbols", &vec![Value::Null; 17]).is_err());
    }

    #[cfg(windows)]
    #[test]
    fn dropping_process_owner_terminates_only_its_child() {
        use std::os::windows::process::CommandExt;
        let mut child = Command::new("ping.exe")
            .args(["-n", "30", "127.0.0.1"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(0x08000000)
            .spawn()
            .expect("local test process");
        let job = attach_process_job(&child).expect("job owns test process");
        assert!(child.try_wait().unwrap().is_none());
        drop(job);
        let deadline = Instant::now() + Duration::from_secs(3);
        while child.try_wait().unwrap().is_none() {
            if Instant::now() >= deadline {
                let _ = child.kill();
                panic!("dropping the job did not stop its child");
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        let _ = child.wait();
    }
}
