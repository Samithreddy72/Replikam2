#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{process::{Child, Command, Stdio}, sync::Mutex, time::Duration};
use serde_json::{json, Value};
use tauri::Manager;
use tauri_plugin_updater::UpdaterExt;

struct Engine {
    child: Mutex<Option<Child>>,
    base: String,
    token: String,
    client: reqwest::Client,
    startup_error: Option<String>,
    mutations: tokio::sync::Mutex<()>,
}

// A fixed API surface, never an arbitrary URL or command supplied by the webview.
fn allowed(method: &str, path: &str) -> bool {
    match method {
        "GET" => matches!(path, "/api/state" | "/api/devices" | "/api/bridges" | "/api/audio/diagnostics" | "/api/desktop/status"),
        "POST" => matches!(path, "/api/support-report" | "/api/preflight" | "/api/signin-request" | "/api/signin-redeem" | "/api/signout" | "/api/remember" | "/api/unlock" | "/api/golive" | "/api/stop" | "/api/return" | "/api/return-tuning" | "/api/audio/recover" | "/api/microphone" | "/api/desktop/configure"),
        _ => false,
    }
}

#[tauri::command]
async fn engine_request(engine: tauri::State<'_, Engine>, method: String, path: String, body: Option<Value>) -> Result<Value, String> {
    let _mutation = if method == "POST" { Some(engine.mutations.lock().await) } else { None };
    if !allowed(&method, &path) { return Err("Unsupported engine request".into()); }
    if let Some(error) = &engine.startup_error { return Err(error.clone()); }
    {
        let mut guard = engine.child.lock().map_err(|_| "Engine lock unavailable")?;
        if let Some(child) = guard.as_mut() {
            if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                return Err(format!("The media engine exited ({status}). Close any older NetBridge app and restart Studio. See desktop-engine.log in ~/.netbridge-source/logs."));
            }
        }
    }
    let request = engine.client.request(method.parse::<reqwest::Method>().map_err(|e| e.to_string())?, format!("{}{}", engine.base, path))
        .header("X-NetBridge-Token", &engine.token);
    let request = if method == "POST" { request.json(&body.unwrap_or(json!({}))) } else { request };
    let response = request.send().await.map_err(|_| "Media engine is starting or unavailable. Retry in a moment.".to_string())?;
    let status = response.status();
    let value: Value = response.json().await.map_err(|_| "Invalid media engine response")?;
    if !status.is_success() {
        return Err(value.get("_error").or(value.get("error")).and_then(Value::as_str).unwrap_or("Engine request failed").to_string());
    }
    Ok(value)
}

impl Engine {
    fn launch(app: &tauri::App) -> Self {
        let token = uuid::Uuid::new_v4().to_string();
        let port = std::net::TcpListener::bind("127.0.0.1:0").and_then(|s| s.local_addr()).map(|a| a.port());
        let mut engine = Self {child: Mutex::new(None), base: String::new(), token, client: reqwest::Client::builder().timeout(Duration::from_secs(100)).no_proxy().build().expect("HTTP client"), startup_error: None, mutations: tokio::sync::Mutex::new(())};
        let result = (|| -> Result<Child, Box<dyn std::error::Error>> {
            let port = port?;
            engine.base = format!("http://127.0.0.1:{port}");
            let mut command;
            if cfg!(debug_assertions) {
                let source = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../netbridge-source");
                let python = std::env::var_os("NB_PYTHON").map(std::path::PathBuf::from).unwrap_or_else(|| source.join(if cfg!(windows) {".venv/Scripts/python.exe"} else {".venv/bin/python"}));
                command = Command::new(python);
                command.arg(source.join("desktop_engine.py")).env("NB_MEDIA_DIR", source.join("_bundle/runtime"));
            } else {
                let root = app.path().resource_dir()?.join("engine");
                command = Command::new(root.join(if cfg!(windows) {"NetBridgeEngine.exe"} else {"NetBridgeEngine"}));
                command.env("NB_MEDIA_DIR", root.join("runtime"));
            }
            #[cfg(windows)] {
                use std::os::windows::process::CommandExt;
                command.creation_flags(0x08000000);
            }
            let logs = app.path().home_dir()?.join(".netbridge-source/logs");
            std::fs::create_dir_all(&logs)?;
            let log = std::fs::File::create(logs.join("desktop-engine.log"))?;
            command.env("NB_DESKTOP_TOKEN", &engine.token).env("NB_DESKTOP_PORT", port.to_string()).env("PYTHONUNBUFFERED", "1")
                .stdin(Stdio::null()).stdout(log.try_clone()?).stderr(log).spawn().map_err(Into::into)
        })();
        match result { Ok(child) => *engine.child.lock().unwrap() = Some(child), Err(e) => engine.startup_error = Some(format!("Unable to start the bundled media engine: {e}")) }
        engine
    }
    fn stop(&self) {
        let _ = reqwest::blocking::Client::builder().timeout(Duration::from_secs(2)).no_proxy().build().and_then(|c| c.post(format!("{}/api/desktop/quit", self.base)).header("X-NetBridge-Token", &self.token).json(&json!({})).send());
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                for _ in 0..300 {
                    if matches!(child.try_wait(), Ok(Some(_))) { return; }
                    std::thread::sleep(Duration::from_millis(100));
                }
                let _ = child.kill(); let _ = child.wait();
            }
        }
    }
}


fn updates_configured(app: &tauri::AppHandle) -> bool {
    app.config().plugins.0.get("updater").map(|c| {
        c.get("pubkey").and_then(Value::as_str).map(|v| !v.is_empty()).unwrap_or(false)
            && c.get("endpoints").and_then(Value::as_array).map(|v| !v.is_empty()).unwrap_or(false)
    }).unwrap_or(false)
}

#[tauri::command]
async fn check_update(app: tauri::AppHandle) -> Result<Value, String> {
    if !updates_configured(&app) { return Ok(json!({"configured":false})); }
    let update = app.updater().map_err(|e|e.to_string())?.check().await.map_err(|e|e.to_string())?;
    Ok(match update {
        Some(u) => json!({"configured":true,"version":u.version,"notes":u.body}),
        None => json!({"configured":true,"version":null}),
    })
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle, engine: tauri::State<'_, Engine>) -> Result<(), String> {
    if !updates_configured(&app) { return Err("A signed update channel has not been configured".into()); }
    // Hold the same lock used by Go live, so a session cannot start during installation.
    let _mutation = engine.mutations.lock().await;
    let state: Value = engine.client.get(format!("{}/api/state",engine.base)).header("X-NetBridge-Token",&engine.token)
        .send().await.map_err(|e|e.to_string())?.error_for_status().map_err(|e|e.to_string())?.json().await.map_err(|e|e.to_string())?;
    if state.get("live").and_then(Value::as_bool).unwrap_or(true) || state.get("wanted").and_then(Value::as_bool).unwrap_or(true) {
        return Err("End the session before installing an update".into());
    }
    if let Some(update)=app.updater().map_err(|e|e.to_string())?.check().await.map_err(|e|e.to_string())? {
        // Tauri verifies the artifact signature before replacing the complete app.
        update.download_and_install(|_,_|{},||{}).await.map_err(|e|e.to_string())?;
        let handle=app.clone();
        tauri::async_runtime::spawn_blocking(move||handle.state::<Engine>().stop()).await.map_err(|e|e.to_string())?;
        app.restart();
    }
    Ok(())
}

#[tauri::command]
fn export_report(app: tauri::AppHandle, report: Value) -> Result<String,String> {
    // Export only the intentionally small schema, never raw state or logs.
    let fields=["generated_at","desktop_version","engine_version","live","delivery","return_on","return_gain","return_jitter_ms","audio_backend","audio_running"];
    let safe: serde_json::Map<String,Value>=fields.iter().filter_map(|key|report.get(*key).map(|value|((*key).to_string(),value.clone()))).collect();
    let path=app.path().download_dir().map_err(|e|e.to_string())?.join(format!("netbridge-support-{}.json",uuid::Uuid::new_v4()));
    std::fs::write(&path,serde_json::to_vec_pretty(&safe).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    Ok(path.to_string_lossy().into_owned())
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| { if let Some(window) = app.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); } }))
         .setup(|app| {
            use tauri::{menu::{Menu, MenuItem}, tray::TrayIconBuilder};
            let open = MenuItem::with_id(app,"open","Open NetBridge",true,None::<&str>)?;
            let quit = MenuItem::with_id(app,"quit","Quit NetBridge…",true,None::<&str>)?;
            let menu = Menu::with_items(app,&[&open,&quit])?;
            let mut tray = TrayIconBuilder::new().menu(&menu).tooltip("NetBridge — open the app for live status")
                .on_menu_event(|app,event| {
                    if let Some(window)=app.get_webview_window("main") {
                        let _=window.show(); let _=window.set_focus();
                        if event.id.as_ref()=="quit" { let _=window.close(); }
                    }
                });
            if let Some(icon)=app.default_window_icon(){tray=tray.icon(icon.clone());}
            tray.build(app)?;
            let engine = Engine::launch(app); app.manage(engine); Ok(())
        })
        .invoke_handler(tauri::generate_handler![engine_request, check_update, install_update, export_report])
        .build(tauri::generate_context!()).expect("Unable to start NetBridge Studio");
    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event { app.state::<Engine>().stop(); }
    });
}

#[cfg(test)]
mod tests {
    use super::allowed;
    #[test]
    fn rejects_arbitrary_urls_and_mutations() {
        assert!(allowed("POST", "/api/golive"));
        for path in ["https://example.com", "/api/desktop/quit", "/api/stop?x=1", "/api/../admin", "/api/audio/capture"] { assert!(!allowed("POST", path)); }
        assert!(!allowed("GET", "/api/stop"));
        assert!(!allowed("DELETE", "/api/state"));
    }
}
