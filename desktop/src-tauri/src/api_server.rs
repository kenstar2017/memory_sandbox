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

/// /api/health 里我们关心的字段。
#[derive(Debug, Clone, Default)]
pub struct Health {
  pub ok: bool,
  /// true = 无 Web 界面的后端；缺字段视为旧版完整 UI
  pub api_only: bool,
  pub build: String,
  pub features: Vec<String>,
  /// app_web.py + core/*.py 的内容指纹；旧后端没有这个字段
  pub code_stamp: String,
}

/// 读 /api/health。
pub fn probe_health(host: &str, port: u16) -> Option<Health> {
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
  Some(parse_health(json))
}

pub fn parse_health(json: &str) -> Health {
  let Ok(v) = serde_json::from_str::<serde_json::Value>(json.trim()) else {
    return Health::default();
  };
  Health {
    ok: v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false),
    api_only: v.get("api_only").and_then(|x| x.as_bool()).unwrap_or(false),
    build: v
      .get("build")
      .and_then(|x| x.as_str())
      .unwrap_or_default()
      .to_string(),
    features: v
      .get("features")
      .and_then(|x| x.as_array())
      .map(|a| {
        a.iter()
          .filter_map(|x| x.as_str().map(|s| s.to_string()))
          .collect()
      })
      .unwrap_or_default(),
    code_stamp: v
      .get("code_stamp")
      .and_then(|x| x.as_str())
      .unwrap_or_default()
      .to_string(),
  }
}

/// 随包 app_web.py + core/*.py 的内容指纹，和后端自报的 code_stamp 比对。
///
/// UI_FEATURES 只在加了新接口时才动，改 core/ 里的行为不会动它，于是旧后端自称
/// 健康、行为却是上个版本的（踩过两次：新接口 404、context_pack 少了记忆 id）。
///
/// 算法必须与 app_web.py::compute_code_stamp 逐字节一致：
/// 文件序列为 "app_web.py" + 按名排序的 "core/*.py"（不递归、跳过非 .py，
/// 所以 __pycache__ 天然被排除）；每个文件产出一行 "<相对路径>:<内容 sha256 十六进制>\n"；
/// 把这些行拼起来再 sha256，取前 12 个十六进制字符。
/// 任一文件读不到就返回空串——半份指纹不能用来判谁旧。
/// 两侧各有一个对同一组样例文件断言 ca0f047bc734 的测试守着。
pub fn expected_code_stamp(root: &Path) -> String {
  use sha2::{Digest, Sha256};

  let mut names = vec!["app_web.py".to_string()];
  let mut core: Vec<String> = match std::fs::read_dir(root.join("core")) {
    Ok(entries) => entries
      .filter_map(|e| e.ok())
      .filter_map(|e| e.file_name().into_string().ok())
      .filter(|n| n.ends_with(".py"))
      .map(|n| format!("core/{n}"))
      .collect(),
    Err(_) => return String::new(),
  };
  core.sort();
  names.extend(core);

  let mut lines = String::new();
  for rel in &names {
    let Ok(data) = std::fs::read(root.join(rel)) else {
      return String::new();
    };
    lines.push_str(&format!("{rel}:{:x}\n", Sha256::digest(&data)));
  }
  format!("{:x}", Sha256::digest(lines.as_bytes()))
    .chars()
    .take(12)
    .collect()
}

/// 后端跑的代码是否和随包源码不一致。任一侧算不出指纹就当「无意见」，
/// 宁可放过也不要每次启动都误杀一个好后端。
pub fn code_is_stale(health: &Health, expected: &str) -> bool {
  !expected.is_empty() && !health.code_stamp.is_empty() && health.code_stamp != expected
}

/// 随包 app_web.py 里声明的 UI_FEATURES，作为「后端够不够新」的基准。
///
/// 不解析源码的话，就只能靠手工维护版本号；漏了就会像这次一样：
/// 旧后端自称健康，新接口全 404。
pub fn expected_features(script: &Path) -> Vec<String> {
  let Ok(src) = std::fs::read_to_string(script) else {
    return Vec::new();
  };
  let Some(after) = src.split_once("UI_FEATURES = (") else {
    return Vec::new();
  };
  let Some((body, _)) = after.1.split_once(')') else {
    return Vec::new();
  };
  // 元组里夹着中文注释，而注释里的逗号是全角的，按 ',' 切会把整段注释连着后面那个
  // 特性名切成一个 chunk——那玩意儿永远不在后端的 features 里，于是每次启动都判「旧后端」
  // 并把好好的进程换掉。先按行剥掉 # 之后的内容。
  let cleaned: String = body
    .lines()
    .map(|line| line.split('#').next().unwrap_or(""))
    .collect::<Vec<_>>()
    .join("\n");
  cleaned
    .split(',')
    .filter_map(|s| {
      let t = s.trim().trim_matches('"').trim_matches('\'').trim();
      if t.is_empty() {
        None
      } else {
        Some(t.to_string())
      }
    })
    .collect()
}

/// 跑着的后端缺哪些本版本已有的能力。
pub fn missing_features(health: &Health, expected: &[String]) -> Vec<String> {
  expected
    .iter()
    .filter(|f| !health.features.iter().any(|h| h == *f))
    .cloned()
    .collect()
}

pub fn health_ok(host: &str, port: u16) -> bool {
  matches!(probe_health(host, port), Some(h) if h.ok && h.api_only)
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
  let found_root = resolve_api_root(resource_dir);
  let expected = found_root
    .as_ref()
    .map(|r| expected_features(&r.join("app_web.py")))
    .unwrap_or_default();
  let expected_stamp = found_root
    .as_ref()
    .map(|r| expected_code_stamp(r))
    .unwrap_or_default();

  // 只复用 api_only 后端；完整 Web UI 进程会开浏览器，BloomBox 要关掉再起 --api-only
  match probe_health(DEFAULT_HOST, DEFAULT_PORT) {
    Some(h) if h.ok && h.api_only => {
      let mut lacking = missing_features(&h, &expected);
      if lacking.is_empty() && code_is_stale(&h, &expected_stamp) {
        // 特性名齐全但代码不是这一份：让下面走「换掉」的分支，理由写清楚
        lacking.push(format!(
          "代码指纹 {} != {}",
          if h.code_stamp.is_empty() {
            "?".to_string()
          } else {
            h.code_stamp.clone()
          },
          expected_stamp
        ));
      }
      if lacking.is_empty() {
        info!("API-only already running on :{DEFAULT_PORT}, reuse");
        *state.owned.lock().map_err(|e| e.to_string())? = false;
        return Ok(format!(
          "已复用本机 API-only http://{DEFAULT_HOST}:{DEFAULT_PORT}"
        ));
      }
      if found_root.is_none() {
        // 起不了新的，只能凑合用旧的，但要留下线索：新接口会 404
        warn!("stale API-only on :{DEFAULT_PORT} but no api root to restart from");
        *state.owned.lock().map_err(|e| e.to_string())? = false;
        return Ok(format!(
          "已复用本机 API-only http://{DEFAULT_HOST}:{DEFAULT_PORT}（旧版 build {}，缺 {}）",
          h.build,
          lacking.join(",")
        ));
      }
      // 旧后端自称健康，新接口却全 404，前端只会看到一句 Load failed，必须换掉
      warn!(
        "stale API-only on :{DEFAULT_PORT} (build={}, missing={:?}), restarting",
        h.build, lacking
      );
      shutdown_and_wait();
    }
    Some(h) if h.ok => {
      warn!("port {DEFAULT_PORT} has full Web UI; shutting down for API-only");
      shutdown_and_wait();
    }
    _ => {}
  }

  let root = found_root.ok_or_else(|| {
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

/// 请退出并等端口真的空出来，免得紧接着的 spawn 撞上还没退干净的旧进程。
fn shutdown_and_wait() {
  shutdown_via_http();
  for _ in 0..20 {
    thread::sleep(Duration::from_millis(150));
    if probe_health(DEFAULT_HOST, DEFAULT_PORT).is_none() {
      break;
    }
  }
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

#[cfg(test)]
mod tests {
  use super::*;

  fn health_with(features: &[&str]) -> Health {
    Health {
      ok: true,
      api_only: true,
      build: "x".into(),
      features: features.iter().map(|s| s.to_string()).collect(),
      code_stamp: String::new(),
    }
  }

  /// 与 Python 侧共享的契约样例：改动任一边的算法都会让两边测试之一挂掉。
  /// 见 tests/test_api_web.py::CodeStampTests::test_matches_rust_contract_sample
  const SAMPLE_STAMP: &str = "ca0f047bc734";

  fn sample_tree(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("bb-stamp-{tag}-{}", std::process::id()));
    std::fs::create_dir_all(dir.join("core")).unwrap();
    std::fs::write(dir.join("app_web.py"), b"print(1)\n").unwrap();
    std::fs::write(dir.join("core/a.py"), b"A\n").unwrap();
    std::fs::write(dir.join("core/b.py"), b"B\n").unwrap();
    dir
  }

  #[test]
  fn parses_health_fields() {
    let h = parse_health(
      r#"{"ok": true, "build": "20260804", "features": ["cors", "cursor_hooks"], "api_only": true}"#,
    );
    assert!(h.ok && h.api_only);
    assert_eq!(h.build, "20260804");
    assert_eq!(h.features, vec!["cors", "cursor_hooks"]);
  }

  #[test]
  fn missing_api_only_field_means_old_full_ui() {
    let h = parse_health(r#"{"ok": true, "features": []}"#);
    assert!(h.ok);
    assert!(!h.api_only);
  }

  #[test]
  fn garbage_body_is_not_ok() {
    assert!(!parse_health("<html>404</html>").ok);
  }

  #[test]
  fn detects_backend_missing_new_feature() {
    // 这次的真实故障：旧后端自称健康，但没有 cursor_hooks 接口
    let expected = vec!["cors".to_string(), "cursor_hooks".to_string()];
    let lacking = missing_features(&health_with(&["cors"]), &expected);
    assert_eq!(lacking, vec!["cursor_hooks"]);
  }

  #[test]
  fn up_to_date_backend_lacks_nothing() {
    let expected = vec!["cors".to_string()];
    assert!(missing_features(&health_with(&["cors", "extra"]), &expected).is_empty());
  }

  #[test]
  fn unknown_expected_features_never_force_restart() {
    // 读不到随包脚本时 expected 为空，不能因此把好后端判成旧的
    assert!(missing_features(&health_with(&[]), &[]).is_empty());
  }

  #[test]
  fn reads_expected_features_from_script() {
    let dir = std::env::temp_dir().join(format!("bb-feat-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let script = dir.join("app_web.py");
    std::fs::write(
      &script,
      "PORT = 1\nUI_FEATURES = (\"chat_stream\", \"cors\", \"cursor_hooks\")\nX = 2\n",
    )
    .unwrap();
    assert_eq!(
      expected_features(&script),
      vec!["chat_stream", "cors", "cursor_hooks"]
    );
    std::fs::remove_dir_all(&dir).ok();
  }

  #[test]
  fn comments_inside_the_tuple_are_not_features() {
    // 真实的 app_web.py 就是这么写的：多行 + 中文注释（注释里是全角逗号）
    let dir = std::env::temp_dir().join(format!("bb-feat-c-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let script = dir.join("app_web.py");
    std::fs::write(
      &script,
      "UI_FEATURES = (\n    \"cors\",\n    # 说明一句，带个全角逗号\n    # 再来一行\n    \"code_stamp\",\n)\n",
    )
    .unwrap();
    assert_eq!(expected_features(&script), vec!["cors", "code_stamp"]);
    std::fs::remove_dir_all(&dir).ok();
  }

  #[test]
  fn missing_script_yields_no_expectations() {
    assert!(expected_features(Path::new("/nope/app_web.py")).is_empty());
  }

  #[test]
  fn code_stamp_matches_python_contract() {
    let dir = sample_tree("contract");
    assert_eq!(expected_code_stamp(&dir), SAMPLE_STAMP);
    std::fs::remove_dir_all(&dir).ok();
  }

  #[test]
  fn pycache_does_not_affect_stamp() {
    // 打包时 rsync 排除了 __pycache__，但开发目录里有；两边指纹必须一样
    let dir = sample_tree("pyc");
    std::fs::create_dir_all(dir.join("core/__pycache__")).unwrap();
    std::fs::write(dir.join("core/__pycache__/a.cpython-39.pyc"), b"junk").unwrap();
    assert_eq!(expected_code_stamp(&dir), SAMPLE_STAMP);
    std::fs::remove_dir_all(&dir).ok();
  }

  #[test]
  fn editing_a_core_file_changes_stamp() {
    let dir = sample_tree("edit");
    std::fs::write(dir.join("core/b.py"), b"B2\n").unwrap();
    assert_ne!(expected_code_stamp(&dir), SAMPLE_STAMP);
    std::fs::remove_dir_all(&dir).ok();
  }

  #[test]
  fn incomplete_tree_yields_no_stamp() {
    assert!(expected_code_stamp(Path::new("/nope/api")).is_empty());
  }

  #[test]
  fn stale_code_detected_only_when_both_sides_known() {
    let mut h = health_with(&["cors"]);
    // 旧后端没有 code_stamp 字段：不能因此判它旧（会每次启动都误杀）
    assert!(!code_is_stale(&h, "aaaaaaaaaaaa"));
    h.code_stamp = "aaaaaaaaaaaa".into();
    assert!(!code_is_stale(&h, "aaaaaaaaaaaa"));
    assert!(!code_is_stale(&h, ""), "读不到随包源码时不下判断");
    assert!(code_is_stale(&h, "bbbbbbbbbbbb"));
  }
}
