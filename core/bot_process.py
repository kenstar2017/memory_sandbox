"""启停飞书机器人进程（供 BloomBox / Web UI 的按钮调用）。

为什么要有这一层：机器人是个常驻进程，原先只能自己开一个终端跑 `python3 feishu_bot.py`，
关掉终端就没了，也看不出「到底在不在跑」。这里把它托管起来——用 pidfile 记住是谁，
日志重定向到文件，状态可查。

两个刻意的选择：

- **起出来的进程是脱离会话的**（`start_new_session=True`）。BloomBox 退出、API 服务重启，
  机器人都照跑不误；否则每次关窗口机器人就掉线，等于没托管。代价是要靠 pidfile 找回它。
- **不是我们起的也认**：用户可能自己在终端里跑着一个。状态里会如实报出来，
  按「停止」也停得掉——不然界面显示「未运行」而实际上有一个在收消息，比不做还糟。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .paths import app_support_dir, default_config_path, is_frozen, resource_root

SCRIPT_NAME = "feishu_bot.py"
# 日志超过这个大小就在下次启动时截断：机器人跑几个月，日志不能无限涨
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_TAIL_LINES = 40
# 启动后等一下再看死没死：SDK 没装、配置缺失这类错误一秒内就退了，
# 与其让界面显示「已启动」再莫名其妙消失，不如当场报出来
START_PROBE_SECONDS = 1.5
STOP_WAIT_SECONDS = 6.0


def pid_path() -> Path:
    return app_support_dir() / "feishu_bot.pid"


def log_path() -> Path:
    logs = app_support_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "feishu_bot.log"


def script_path() -> Path:
    """打包后 feishu_bot.py 与 app_web.py 同级躺在 Resources/api 里。"""
    return resource_root() / SCRIPT_NAME


def python_bin() -> str:
    """
    优先用跑着本进程的解释器：机器人和 API 服务共用一套依赖，换个解释器就可能少包。
    冻结成单文件时 sys.executable 是应用本体，只能退回 PATH 上的 python3。
    """
    if not is_frozen():
        return sys.executable or "python3"
    import shutil

    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return "python3"


@dataclass
class BotStatus:
    running: bool = False
    pid: int = 0
    # True = pidfile 里记着的那个（BloomBox 起的）；False = 在别处起的，扫出来的
    owned: bool = False
    started_at: str = ""
    available: bool = False  # 找得到 feishu_bot.py
    sdk_installed: bool = False
    configured: bool = False  # app_id / app_secret 齐了
    allow_count: int = 0
    doc_bot_enabled: bool = False
    script: str = ""
    python: str = ""
    log: str = ""
    log_tail: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BotActionResult:
    ok: bool
    message: str
    status: BotStatus = field(default_factory=BotStatus)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "status": self.status.to_dict()}


# ---------- 进程探测 ----------

# 本进程起的那个机器人。留着句柄只为收尸：杀掉后不 wait 就是个僵尸，
# 一直挂在 API 服务名下。别拿它当「在不在跑」的唯一依据——
# API 服务重启后句柄就没了，机器人还活着，那时得靠 pidfile 找回来。
_CHILD: Optional[subprocess.Popen] = None


def _process_command(pid: int) -> str:
    """取进程的命令行；取不到返回空串（进程没了或没权限）。"""
    try:
        out = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:  # noqa: BLE001 - ps 不在或超时都当查不到
        return ""
    return (out.stdout or "").strip()


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    child = _CHILD
    if child is not None and child.pid == pid:
        # 是本进程起的：必须用 poll() 判活并顺手收尸。被杀掉的子进程在父进程
        # 调 wait 之前是僵尸，kill(pid, 0) 照样成功，只看信号会一直以为它还在跑
        return child.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但不属于当前用户，当它活着（后面停的时候会如实报错）
        return True
    except OSError:
        return False
    return True


def _is_bot(pid: int) -> bool:
    """
    确认这个 pid 真是机器人。

    pid 会被系统回收：只凭 pidfile 里的数字去 kill，很可能杀掉一个刚好复用了
    这个号的无辜进程。所以每次都比对一下命令行。
    """
    if not _alive(pid):
        return False
    cmd = _process_command(pid)
    if not cmd:
        # ps 查不到就别硬猜，宁可当它不是
        return False
    return SCRIPT_NAME in cmd


def _read_pidfile() -> Tuple[int, str]:
    try:
        data = json.loads(pid_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0, ""
    if not isinstance(data, dict):
        return 0, ""
    try:
        return int(data.get("pid") or 0), str(data.get("started_at") or "")
    except (TypeError, ValueError):
        return 0, ""


def _write_pidfile(pid: int) -> str:
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        pid_path().write_text(
            json.dumps({"pid": pid, "started_at": started}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return started


def _clear_pidfile() -> None:
    try:
        pid_path().unlink()
    except (FileNotFoundError, OSError):
        pass


def _scan_running() -> List[int]:
    """扫本机还有没有别处起的机器人（用户自己开终端跑的那种）。"""
    try:
        out = subprocess.run(
            # pgrep -f 收的是正则，点号得转义，否则 feishu_botXpy 也算数
            ["/usr/bin/pgrep", "-f", SCRIPT_NAME.replace(".", r"\.")],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:  # noqa: BLE001
        return []
    pids: List[int] = []
    for line in (out.stdout or "").splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        # pgrep -f 会匹配到自己（命令行里带着这个词），排掉
        if pid == os.getpid():
            continue
        if _is_bot(pid):
            pids.append(pid)
    return pids


def _find_running() -> Tuple[int, bool, str]:
    """返回 (pid, 是否我们起的, 启动时间)。没跑返回 (0, False, "")。"""
    pid, started = _read_pidfile()
    if pid and _is_bot(pid):
        return pid, True, started
    if pid:
        # pidfile 是陈的：进程早没了，或号被别人复用了
        _clear_pidfile()
    found = _scan_running()
    if found:
        return found[0], False, ""
    return 0, False, ""


def _tail_log(lines: int = LOG_TAIL_LINES) -> str:
    path = log_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            # 日志最多两兆，直接读完再切比倒着找换行简单，也不会切坏多字节字符
            content = f.read()
    except (FileNotFoundError, OSError):
        return ""
    rows = content.splitlines()
    return "\n".join(rows[-lines:]).strip()


def _config_summary() -> Tuple[bool, int, bool, str]:
    """(凭证齐了吗, 白名单人数, 文档评论机器人开了吗, 出错原因)。"""
    try:
        from .config import load_config

        cfg = load_config(str(default_config_path()))
    except Exception as e:  # noqa: BLE001 - 配置坏了不该让整个状态接口 500
        return False, 0, False, f"读配置失败：{e}"
    feishu = getattr(cfg, "feishu", None)
    if feishu is None:
        return False, 0, False, ""
    app_id = (getattr(feishu, "app_id", "") or os.environ.get("FEISHU_APP_ID") or "").strip()
    secret = (
        getattr(feishu, "app_secret", "") or os.environ.get("FEISHU_APP_SECRET") or ""
    ).strip()
    allow = [x for x in (getattr(feishu, "bot_allow_open_ids", []) or []) if str(x).strip()]
    return bool(app_id and secret), len(allow), bool(getattr(feishu, "doc_bot_enabled", False)), ""


def _sdk_installed() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("lark_oapi") is not None
    except (ImportError, ValueError):
        return False


def status() -> BotStatus:
    pid, owned, started = _find_running()
    configured, allow_count, doc_bot, err = _config_summary()
    script = script_path()
    return BotStatus(
        running=bool(pid),
        pid=pid,
        owned=owned,
        started_at=started,
        available=script.is_file(),
        sdk_installed=_sdk_installed(),
        configured=configured,
        allow_count=allow_count,
        doc_bot_enabled=doc_bot,
        script=str(script),
        python=python_bin(),
        log=str(log_path()),
        log_tail=_tail_log(),
        error=err,
    )


# ---------- 启停 ----------


def start() -> BotActionResult:
    global _CHILD
    st = status()
    if st.running:
        return BotActionResult(
            True, f"机器人已经在跑了（PID {st.pid}），不用重复启动。", st
        )
    if not st.available:
        return BotActionResult(
            False,
            f"找不到 {SCRIPT_NAME}（{st.script}）。开发环境请在仓库根目录启动 API；"
            "安装包缺这个文件说明打包时漏了，重新 npm run sync-api 再打。",
            st,
        )
    if not st.sdk_installed:
        return BotActionResult(
            False, "缺少 lark-oapi（长连接的握手是私有协议，没法手写）：pip install lark-oapi", st
        )
    if not st.configured:
        return BotActionResult(
            False, "还没配 feishu.app_id / app_secret，先填好配置再启动。", st
        )

    path = log_path()
    try:
        # 只在启动时截断，运行中不动文件，免得写日志的句柄指到被删的 inode
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.unlink()
    except OSError:
        pass

    try:
        handle = open(path, "a", encoding="utf-8")
    except OSError as e:
        return BotActionResult(False, f"打不开日志文件 {path}：{e}", st)

    try:
        handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} BloomBox 启动机器人 ===\n")
        handle.flush()
        proc = subprocess.Popen(  # noqa: S603 - 参数全是本地常量，没有用户输入
            # -u 不能省：stdout 重定向到文件就变成块缓冲，机器人是常驻进程、几乎不退出，
            # 于是「收到消息」这类日志会卡在缓冲区里几小时不落盘。排障时看到的是一份
            # 停在启动那一刻的日志，比没有日志更误导人
            [python_bin(), "-u", str(script_path())],
            cwd=str(resource_root()),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            # 自成会话：BloomBox 退出、API 服务重启都不会把机器人带走
            start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001
        handle.close()
        return BotActionResult(False, f"启动失败：{e}", status())
    finally:
        try:
            handle.close()
        except OSError:
            pass

    _CHILD = proc
    time.sleep(START_PROBE_SECONDS)
    if proc.poll() is not None:
        # 一秒就退了，八成是配置或依赖问题，把日志尾巴带上，别让人去翻文件
        _CHILD = None
        st = status()
        tail = st.log_tail or "（日志是空的）"
        return BotActionResult(
            False, f"机器人启动后立刻退出了（退出码 {proc.returncode}）：\n{tail}", st
        )

    started = _write_pidfile(proc.pid)
    st = status()
    st.started_at = st.started_at or started
    return BotActionResult(True, f"机器人已启动（PID {proc.pid}），日志在 {log_path()}", st)


def _reap(pid: int) -> None:
    global _CHILD
    if _CHILD is not None and _CHILD.pid == pid:
        try:
            _CHILD.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        _CHILD = None


def _terminate(pid: int) -> str:
    """停一个进程。返回 "" 表示停掉了，"denied" 没权限，"alive" 是真赖着不走。"""
    try:
        _signal_bot(pid, signal.SIGTERM)
    except ProcessLookupError:
        _reap(pid)
        return ""
    except PermissionError:
        return "denied"

    deadline = time.time() + STOP_WAIT_SECONDS
    while time.time() < deadline:
        if not _alive(pid):
            break
        time.sleep(0.2)
    else:
        # 长连接偶尔会卡在收包上，SIGTERM 叫不动就来硬的
        try:
            _signal_bot(pid, signal.SIGKILL)
            time.sleep(0.3)
        except OSError:
            pass

    _reap(pid)
    # 用 _is_bot 而不是 _alive 收尾：被杀掉但还没被父进程收走的僵尸，
    # kill(pid, 0) 照样成功，ps 里却只剩个 (Python)——那不算没停掉
    return "alive" if _is_bot(pid) else ""


def stop() -> BotActionResult:
    pid, owned, _started = _find_running()
    if not pid:
        _clear_pidfile()
        return BotActionResult(True, "机器人本来就没在跑。", status())

    # 连着扫一遍：手工在终端起过一个、BloomBox 又起了一个的情况真会发生，
    # 那时同一条消息会被回两遍。既然按了「停止」，就把看得见的都停掉，
    # 否则界面报「已停止」而飞书那边照样有人应
    targets = [pid] + [p for p in _scan_running() if p != pid]
    denied: List[int] = []
    stubborn: List[int] = []
    for target in targets:
        outcome = _terminate(target)
        if outcome == "denied":
            denied.append(target)
        elif outcome == "alive":
            stubborn.append(target)

    _clear_pidfile()
    st = status()
    if denied:
        return BotActionResult(
            False,
            f"没权限停掉 PID {'、'.join(str(p) for p in denied)}"
            "（多半是别的用户起的），请手动 kill。",
            st,
        )
    if stubborn:
        return BotActionResult(
            False, f"没能停掉 PID {'、'.join(str(p) for p in stubborn)}，请手动处理。", st
        )

    extra = f"，另外还停掉了 {len(targets) - 1} 个重复实例" if len(targets) > 1 else ""
    where = "" if owned else "（这个是在 BloomBox 之外启动的）"
    return BotActionResult(True, f"机器人已停止{where}{extra}。", st)


def restart() -> BotActionResult:
    stopped = stop()
    if not stopped.ok:
        return stopped
    return start()


def _signal_bot(pid: int, sig: int) -> None:
    """
    给机器人发信号。

    尽量按**进程组**发：机器人跑 agent 时会拉起子进程，只杀父进程会留下孤儿。
    只有在它自己就是组长时才这么做——从终端起的进程若与别人同组，
    整组杀过去会连累用户的 shell。
    """
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = -1
    if pgid == pid:
        os.killpg(pgid, sig)
        return
    os.kill(pid, sig)


def tail_log(lines: int = LOG_TAIL_LINES) -> str:
    return _tail_log(lines)
