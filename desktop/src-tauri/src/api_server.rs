//! 随 BloomBox 拉起 / 退出 Python API（app_web.py --api-only）。

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use log::{info, warn};

pub const DEFAULT_HOST: &str = "127.0.0.1";
pub const DEFAULT_PORT: u16 = 8765;

pub struct ApiServerState {
  pub child: Mutex<Option<Child>>,
  /// true = 本进程拉起的，退出时要杀；false = 复用已有服务
  pub owned: Mutex<bool>,
}

impl Default for ApiServerState {
  fn default() -> Self {
    Self {
      child: Mutex::new(None),
      owned: Mutex::new(false),
    }
  }
}

/// 读 /api/health。返回 (ok, api_only)。
pub fn probe_health(host: &str, port: u16) -> Option<(bool, bool)> {
  let Ok(addr) = format!("{host}:{port}").parse::<SocketAddr>() else {
    return None;
  };
  let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(400)) else {
    return None;
  };
  let _ = stream.set_read_timeout(Some(Duration::from_millis(600)));
  let _ = stream.set_write_timeout(Some(Duration::from_millis(600)));
  let req = format!(
    "GET /api/health HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
  );
  if stream.write_all(req.as_bytes()).is_err() {
    return None;
  }
  let mut buf = Vec::new();
  let _ = stream.read_to_end(&mut buf);
  let body = String::from_utf8_lossy(&buf);
  // 跳过 HTTP 头
  let json = body.split("\r\n\r\n").nth(1).unwrap_or(&body);
  let ok = json.contains("\"ok\": true") || json.contains("\"ok\":true");
  if !ok {
    return Some((false, false));
  }
  // 仅 api_only=true 才算「无 Web 界面」的后端；缺字段视为旧版完整 UI
  let api_only = json.contains("\"api_only\": true") || json.contains("\"api_only\":true");
  Some((true, api_only))
}

pub fn health_ok(host: &str, port: u16) -> bool {
  matches!(probe_health(host, port), Some((true, true)))
}

fn find_python() -> Option<PathBuf> {
  if let Ok(p) = std::env::var("BLOOMBOX_PYTHON") {
    let path = PathBuf::from(p);
    if path.exists() {
      return Some(path);
    }
  }
  for name in ["python3", "python"] {
    if let Ok(out) = Command::new("/usr/bin/which").arg(name).output() {
      if out.status.success() {
        let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if !s.is_empty() {
          return Some(PathBuf::from(s));
        }
      }
    }
  }
  for p in [
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
  ] {
    let path = PathBuf::from(p);
    if path.exists() {
      return Some(path);
    }
  }
  None
}

/// 开发：仓库根；发布：Resources 下的 api/；可用 BLOOMBOX_API_ROOT 覆盖。
pub fn resolve_api_root(resource_dir: Option<PathBuf>) -> Option<PathBuf> {
  if let Ok(p) = std::env::var("BLOOMBOX_API_ROOT") {
    let path = PathBuf::from(p);
    if path.join("app_web.py").exists() {
      return Some(path);
    }
  }
  let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
  let repo = manifest.join("../..");
  if let Ok(repo) = repo.canonicalize() {
    if repo.join("app_web.py").exists() {
      return Some(repo);
    }
  }
  if let Some(res) = resource_dir {
    for c in [
      res.join("api"),
      res.join("resources").join("api"),
      res.clone(),
    ] {
      if c.join("app_web.py").exists() {
        return Some(c);
      }
    }
  }
  None
}

pub fn start_api_server(
  state: &ApiServerState,
  resource_dir: Option<PathBuf>,
) -> Result<String, String> {
  // 只复用 api_only 后端；完整 Web UI 进程会开浏览器，BloomBox 要关掉再起 --api-only
  match probe_health(DEFAULT_HOST, DEFAULT_PORT) {
    Some((true, true)) => {
      info!("API-only already running on :{DEFAULT_PORT}, reuse");
      *state.owned.lock().map_err(|e| e.to_string())? = false;
      return Ok(format!(
        "已复用本机 API-only http://{DEFAULT_HOST}:{DEFAULT_PORT}"
      ));
    }
    Some((true, false)) => {
      warn!("port {DEFAULT_PORT} has full Web UI; shutting down for API-only");
      shutdown_via_http();
      for _ in 0..20 {
        thread::sleep(Duration::from_millis(150));
        if probe_health(DEFAULT_HOST, DEFAULT_PORT).is_none() {
          break;
        }
      }
    }
    _ => {}
  }

  let root = resolve_api_root(resource_dir).ok_or_else(|| {
    "未找到 app_web.py。请设置 BLOOMBOX_API_ROOT，或在开发态从仓库运行 BloomBox。".to_string()
  })?;
  let script = root.join("app_web.py");
  let python = find_python().ok_or_else(|| {
    "未找到 python3。请安装 Python 3，或设置 BLOOMBOX_PYTHON=/path/to/python3".to_string()
  })?;

  info!(
    "starting API-only (no browser UI): {:?} {:?} --api-only (cwd={:?})",
    python, script, root
  );

  let mut cmd = Command::new(&python);
  cmd.arg(&script)
    .arg("--api-only")
    .current_dir(&root)
    .env("MS_API_ONLY", "1")
    .stdin(Stdio::null())
    .stdout(Stdio::null())
    .stderr(Stdio::null());

  #[cfg(unix)]
  {
    use std::os::unix::process::CommandExt;
    cmd.process_group(0);
  }

  let child = cmd.spawn().map_err(|e| {
    format!("启动 Python API 失败: {e}（python={python:?} script={script:?}）")
  })?;

  {
    let mut guard = state.child.lock().map_err(|e| e.to_string())?;
    *guard = Some(child);
  }
  *state.owned.lock().map_err(|e| e.to_string())? = true;

  for _ in 0..40 {
    thread::sleep(Duration::from_millis(250));
    if health_ok(DEFAULT_HOST, DEFAULT_PORT) {
      return Ok(format!(
        "已启动 API http://{DEFAULT_HOST}:{DEFAULT_PORT}（{}）",
        root.display()
      ));
    }
    if let Ok(mut g) = state.child.lock() {
      if let Some(c) = g.as_mut() {
        if let Ok(Some(status)) = c.try_wait() {
          *g = None;
          return Err(format!(
            "API 进程提前退出（{status}）。请检查 Python 依赖：pip install -r requirements.txt"
          ));
        }
      }
    }
  }

  Err(format!(
    "API 启动超时（未在 {DEFAULT_PORT} 端口就绪）。请检查 Python 环境与依赖。"
  ))
}

pub fn stop_api_server(state: &ApiServerState) {
  let owned = state.owned.lock().map(|g| *g).unwrap_or(false);
  if !owned {
    return;
  }
  if let Ok(mut guard) = state.child.lock() {
    if let Some(mut child) = guard.take() {
      info!("stopping owned API process pid={}", child.id());
      let _ = child.kill();
      let _ = child.wait();
    }
  }
  shutdown_via_http();
}

fn shutdown_via_http() {
  let Ok(addr) = format!("{DEFAULT_HOST}:{DEFAULT_PORT}").parse::<SocketAddr>() else {
    return;
  };
  let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(300)) else {
    return;
  };
  let req = concat!(
    "POST /api/shutdown HTTP/1.1\r\n",
    "Host: 127.0.0.1:8765\r\n",
    "Content-Type: application/json\r\n",
    "Content-Length: 2\r\n",
    "Connection: close\r\n\r\n",
    "{}"
  );
  let _ = stream.write_all(req.as_bytes());
}

#[allow(dead_code)]
pub fn api_root_exists(path: &Path) -> bool {
  path.join("app_web.py").exists()
}

#[allow(dead_code)]
pub fn warn_if_missing(msg: &str) {
  warn!("{msg}");
}
