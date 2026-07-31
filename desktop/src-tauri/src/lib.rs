mod api_server;

use api_server::ApiServerState;
use tauri::{Manager, RunEvent};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .manage(ApiServerState::default())
    .setup(|app| {
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      } else {
        let _ = app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        );
      }

      let resource_dir = app.path().resource_dir().ok();
      let state = app.state::<ApiServerState>();
      match api_server::start_api_server(&state, resource_dir) {
        Ok(msg) => {
          log::info!("{msg}");
        }
        Err(err) => {
          log::error!("API start failed: {err}");
          // 不阻止窗口打开；前端会显示未连接
        }
      }
      Ok(())
    })
    .build(tauri::generate_context!())
    .expect("error while building BloomBox")
    .run(|app_handle, event| {
      if let RunEvent::Exit = event {
        let state = app_handle.state::<ApiServerState>();
        api_server::stop_api_server(&state);
      }
    });
}
