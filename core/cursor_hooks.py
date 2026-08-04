"""把记忆沙箱的 Cursor hook 门禁装到用户机器上。

被 CLI、Web/BloomBox API、安装脚本共用，逻辑只此一份。

设计约束：
- **必须合并**进 ~/.cursor/hooks.json，不能覆盖：用户很可能有自己的 hook。
- 只按脚本文件名认领自己的条目，所以换 Python 路径、改 matcher 都不会留下重复条目。
- hook 脚本只用标准库，装到 ~/.cursor/hooks/ 后与本仓库彻底解耦：
  仓库删了、BloomBox 卸载了，hook 仍能跑（跑不动也只是失败放过）。
- 用内容哈希判断「需要升级」，不依赖手工维护版本号。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import is_frozen, resource_root

SOURCE_DIR_NAME = "cursor_hooks"
STATE_DIR_NAME = "memory-sandbox-hook-state"
MANIFEST_NAME = ".memory-sandbox-hooks.json"

# 事件 → 脚本与配置。改这里就等于改产物，装过的机器会被识别为「需升级」。
HOOK_SPECS: Dict[str, Dict[str, Any]] = {
    "sessionStart": {"script": "memory-session-context.py"},
    "preToolUse": {
        "script": "memory-require-prepare.py",
        "matcher": "Write|Delete|Task|MCP:",
    },
    "postToolUse": {"script": "memory-mark.py", "matcher": "MCP:"},
    "stop": {"script": "memory-ensure-remember.py", "loop_limit": 1},
}

SCRIPT_NAMES = tuple(spec["script"] for spec in HOOK_SPECS.values())


@dataclass
class HooksStatus:
    installed: bool = False
    up_to_date: bool = False
    hooks_json: str = ""
    hooks_dir: str = ""
    python: str = ""
    source_dir: str = ""
    installed_at: str = ""
    missing_scripts: List[str] = field(default_factory=list)
    stale_scripts: List[str] = field(default_factory=list)
    missing_events: List[str] = field(default_factory=list)
    # 用户自己的 hook 条目数：用来向用户证明「我们没动你的配置」
    foreign_entries: int = 0
    available: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "up_to_date": self.up_to_date,
            "hooks_json": self.hooks_json,
            "hooks_dir": self.hooks_dir,
            "python": self.python,
            "source_dir": self.source_dir,
            "installed_at": self.installed_at,
            "missing_scripts": list(self.missing_scripts),
            "stale_scripts": list(self.stale_scripts),
            "missing_events": list(self.missing_events),
            "foreign_entries": self.foreign_entries,
            "available": self.available,
            "error": self.error,
        }


@dataclass
class InstallResult:
    ok: bool = False
    action: str = ""
    hooks_json: str = ""
    hooks_dir: str = ""
    python: str = ""
    scripts: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    backup: str = ""
    kept_foreign: int = 0
    message: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "hooks_json": self.hooks_json,
            "hooks_dir": self.hooks_dir,
            "python": self.python,
            "scripts": list(self.scripts),
            "events": list(self.events),
            "backup": self.backup,
            "kept_foreign": self.kept_foreign,
            "message": self.message,
            "error": self.error,
        }


def cursor_dir() -> Path:
    override = os.environ.get("MEMORY_SANDBOX_CURSOR_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cursor"


def hooks_dir() -> Path:
    return cursor_dir() / "hooks"


def hooks_json_path() -> Path:
    return cursor_dir() / "hooks.json"


def state_dir() -> Path:
    return cursor_dir() / STATE_DIR_NAME


def source_dir() -> Optional[Path]:
    """hook 脚本的来源目录：开发态在仓库根，BloomBox 打包态在 resources/api 下。"""
    override = os.environ.get("MEMORY_SANDBOX_HOOKS_SRC")
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(resource_root() / SOURCE_DIR_NAME)
    candidates.append(Path(__file__).resolve().parent.parent / SOURCE_DIR_NAME)
    for path in candidates:
        if all((path / name).is_file() for name in SCRIPT_NAMES):
            return path
    return None


def resolve_python() -> str:
    """给 hooks.json 里的命令挑一个解释器。

    hook 只用标准库，所以任何 python3 都行；关键是给绝对路径，别指望 PATH。
    """
    override = os.environ.get("MEMORY_SANDBOX_HOOK_PYTHON") or os.environ.get(
        "BLOOMBOX_PYTHON"
    )
    if override and Path(override).exists():
        return str(Path(override))

    # 打包态 sys.executable 是 app 自己，不是解释器，拿来跑脚本会把整个 app 再起一遍
    if not is_frozen():
        exe = Path(sys.executable)
        if exe.exists() and "python" in exe.name.lower():
            return str(exe)

    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ):
        if Path(path).exists():
            return path
    return "python3"


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _command(python: str, script: Path) -> str:
    return f"{shlex.quote(python)} {shlex.quote(str(script))}"


def _is_ours(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    command = str(entry.get("command") or "")
    return any(name in command for name in SCRIPT_NAMES)


def _load_hooks_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 用户配置坏了：不当回事地覆盖会毁掉他的东西，交给上层报错
        raise ValueError(f"{path} 不是合法 JSON，请先修好再安装")
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层不是对象，无法合并")
    return data


def _count_foreign(hooks: Dict[str, Any]) -> int:
    total = 0
    for entries in hooks.values():
        if isinstance(entries, list):
            total += sum(1 for e in entries if not _is_ours(e))
    return total


def _read_manifest() -> Dict[str, Any]:
    path = hooks_dir() / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def status() -> HooksStatus:
    st = HooksStatus(
        hooks_json=str(hooks_json_path()),
        hooks_dir=str(hooks_dir()),
        python=resolve_python(),
    )

    src = source_dir()
    if src is None:
        st.available = False
        st.error = "找不到 hook 源文件（cursor_hooks/），无法安装或校验"
    else:
        st.source_dir = str(src)

    target = hooks_dir()
    for name in SCRIPT_NAMES:
        installed = target / name
        if not installed.is_file():
            st.missing_scripts.append(name)
        elif src is not None and _sha256(installed) != _sha256(src / name):
            st.stale_scripts.append(name)

    try:
        data = _load_hooks_json(hooks_json_path())
    except ValueError as exc:
        st.error = str(exc)
        return st

    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    st.foreign_entries = _count_foreign(hooks)
    for event in HOOK_SPECS:
        entries = hooks.get(event)
        if not isinstance(entries, list) or not any(_is_ours(e) for e in entries):
            st.missing_events.append(event)

    st.installed = not st.missing_scripts and not st.missing_events
    st.up_to_date = st.installed and not st.stale_scripts
    manifest = _read_manifest()
    st.installed_at = str(manifest.get("installed_at") or "")
    return st


def install(python: Optional[str] = None) -> InstallResult:
    """把脚本拷到 ~/.cursor/hooks/ 并把条目合并进 hooks.json。可反复执行。"""
    result = InstallResult(
        action="install",
        hooks_json=str(hooks_json_path()),
        hooks_dir=str(hooks_dir()),
    )

    src = source_dir()
    if src is None:
        result.error = "找不到 hook 源文件（cursor_hooks/），无法安装"
        return result

    interpreter = python or resolve_python()
    result.python = interpreter

    try:
        data = _load_hooks_json(hooks_json_path())
    except ValueError as exc:
        result.error = str(exc)
        return result

    try:
        target = hooks_dir()
        target.mkdir(parents=True, exist_ok=True)
        for name in SCRIPT_NAMES:
            dest = target / name
            shutil.copyfile(src / name, dest)
            dest.chmod(0o755)
            result.scripts.append(str(dest))

        backup = _backup(hooks_json_path())
        if backup:
            result.backup = str(backup)

        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        # 先清掉自己在所有事件下的旧条目，避免改了事件/matcher 后残留重复
        for event in list(hooks.keys()):
            entries = hooks.get(event)
            if isinstance(entries, list):
                hooks[event] = [e for e in entries if not _is_ours(e)]

        for event, spec in HOOK_SPECS.items():
            entry: Dict[str, Any] = {
                "command": _command(interpreter, target / spec["script"])
            }
            if spec.get("matcher"):
                entry["matcher"] = spec["matcher"]
            if spec.get("loop_limit") is not None:
                entry["loop_limit"] = spec["loop_limit"]
            entries = hooks.get(event)
            if not isinstance(entries, list):
                # 该事件的值不是数组，本就是 Cursor 认不了的配置，只能重建；
                # 原文件已备份，用户要找回来也有据
                entries = []
                hooks[event] = entries
            entries.append(entry)
            result.events.append(event)

        hooks = {k: v for k, v in hooks.items() if v}
        data["hooks"] = hooks
        # Cursor 只认 schema version 1；用户已经写了别的值就别乱改
        data.setdefault("version", 1)
        result.kept_foreign = _count_foreign(hooks)

        _write_json(hooks_json_path(), data)
        _write_json(
            target / MANIFEST_NAME,
            {
                "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "python": interpreter,
                "source_dir": str(src),
                "scripts": list(SCRIPT_NAMES),
                "events": list(HOOK_SPECS.keys()),
            },
        )
    except OSError as exc:
        result.error = f"写入失败：{exc}"
        return result

    result.ok = True
    result.message = (
        f"已装到 {result.hooks_json}（保留你原有 {result.kept_foreign} 条 hook）。"
        "Cursor 存盘即重载，sessionStart 注入要新开对话才生效。"
    )
    return result


def uninstall() -> InstallResult:
    """撤掉自己的条目与脚本，用户其它 hook 原样保留。"""
    result = InstallResult(
        action="uninstall",
        hooks_json=str(hooks_json_path()),
        hooks_dir=str(hooks_dir()),
    )

    try:
        data = _load_hooks_json(hooks_json_path())
    except ValueError as exc:
        result.error = str(exc)
        return result

    try:
        hooks = data.get("hooks")
        if isinstance(hooks, dict):
            for event in list(hooks.keys()):
                entries = hooks.get(event)
                if not isinstance(entries, list):
                    continue
                kept = [e for e in entries if not _is_ours(e)]
                if len(kept) != len(entries):
                    result.events.append(event)
                if kept:
                    hooks[event] = kept
                else:
                    hooks.pop(event, None)
            result.kept_foreign = _count_foreign(hooks)
            data["hooks"] = hooks
            if hooks_json_path().is_file():
                backup = _backup(hooks_json_path())
                if backup:
                    result.backup = str(backup)
                _write_json(hooks_json_path(), data)

        target = hooks_dir()
        for name in list(SCRIPT_NAMES) + [MANIFEST_NAME]:
            path = target / name
            if path.is_file():
                path.unlink()
                result.scripts.append(str(path))
        shutil.rmtree(state_dir(), ignore_errors=True)
    except OSError as exc:
        result.error = f"清理失败：{exc}"
        return result

    result.ok = True
    result.message = f"已移除；保留你原有 {result.kept_foreign} 条 hook。"
    return result


def _backup(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copyfile(path, backup)
    except OSError:
        return None
    return backup


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
