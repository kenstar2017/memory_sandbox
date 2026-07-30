"""CLI 人机交互：TTY 着色、单行 spinner、阶段进度（stderr）与答案排版（stdout）。"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional, TextIO


# ANSI（NO_COLOR / 非 TTY 时自动关闭）
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"
_RED = "\033[31m"

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

_SOURCE_STYLE = {
    "working": (_GREEN, "工作记忆"),
    "long_term": (_CYAN, "长时记忆"),
    "procedural": (_CYAN, "程序性"),
    "llm": (_MAGENTA, "LLM"),
    "miss": (_YELLOW, "未命中"),
    "sensory_reject": (_RED, "无效输入"),
    "command": (_BLUE, "指令"),
}


def _use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except Exception:
        return False


class CliUi:
    """交互模式 / ask 共用的终端 UI。"""

    def __init__(self, err: TextIO = sys.stderr, out: TextIO = sys.stdout):
        self.err = err
        self.out = out
        self.color = _use_color(err)
        self._spinner_stop: Optional[threading.Event] = None
        self._spinner_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._current = ""
        self._t0 = 0.0

    def _c(self, code: str, text: str) -> str:
        if not self.color:
            return text
        return f"{code}{text}{_RESET}"

    def _write_err(self, text: str, end: str = "\n") -> None:
        self.err.write(text + end)
        self.err.flush()

    def stop_spinner(self, final: Optional[str] = None, ok: bool = True) -> None:
        with self._lock:
            if self._spinner_stop is not None:
                self._spinner_stop.set()
            th = self._spinner_thread
        if th and th.is_alive():
            th.join(timeout=0.5)
        with self._lock:
            self._spinner_stop = None
            self._spinner_thread = None
            msg = final if final is not None else self._current
            self._current = ""
        if not msg:
            return
        # 清掉 spinner 行再打印完成态
        if self.color and self.err.isatty():
            self.err.write("\r\033[K")
        mark = self._c(_GREEN, "✓") if ok else self._c(_YELLOW, "·")
        elapsed = ""
        if self._t0:
            elapsed = self._c(_DIM, f"  {time.time() - self._t0:.0f}s")
        self._write_err(f"  {mark} {msg}{elapsed}")

    def _spin_loop(self, stop: threading.Event) -> None:
        i = 0
        while not stop.is_set():
            with self._lock:
                msg = self._current
            if not msg:
                break
            frame = _SPINNER[i % len(_SPINNER)]
            i += 1
            elapsed = time.time() - self._t0 if self._t0 else 0
            line = f"  {self._c(_CYAN, frame)} {msg}{self._c(_DIM, f'  {elapsed:.0f}s')}"
            if self.color and self.err.isatty():
                self.err.write("\r\033[K" + line)
            else:
                # 非 TTY：偶尔打印一行心跳，避免刷屏
                if i == 1 or i % 8 == 0:
                    self._write_err(line)
                stop.wait(0.35)
                continue
            self.err.flush()
            stop.wait(0.08)

    def progress(self, msg: str) -> None:
        """on_progress 回调：短阶段逐行记录；长阶段单行 spinner（同阶段只更新文案）。"""
        msg = (msg or "").strip()
        if not msg:
            return
        short = _shorten_progress(msg)
        long_stage = _is_long_stage(msg)

        with self._lock:
            spinning = self._spinner_thread is not None
            prev = self._current
            if not self._t0:
                self._t0 = time.time()

        # 长等待结束文案：立刻打勾收起 spinner
        if spinning and ("完成" in msg or "已返回" in msg):
            self.stop_spinner(final=short, ok=True)
            return

        # 长等待中的心跳：只更新 spinner 文案
        if spinning and long_stage:
            with self._lock:
                self._current = short
            return

        if spinning and prev:
            self.stop_spinner(final=prev, ok=True)

        if long_stage:
            with self._lock:
                self._current = short
                stop = threading.Event()
                self._spinner_stop = stop
                th = threading.Thread(target=self._spin_loop, args=(stop,), daemon=True)
                self._spinner_thread = th
                th.start()
            return

        mark = self._c(_DIM, "·")
        self._write_err(f"  {mark} {short}")
        with self._lock:
            self._current = ""

    def begin_turn(self) -> None:
        self.stop_spinner(final=None)
        with self._lock:
            self._t0 = time.time()
            self._current = ""

    def end_turn(self) -> None:
        with self._lock:
            prev = self._current
        if prev:
            self.stop_spinner(final=prev, ok=True)
        else:
            self.stop_spinner(final=None)
        with self._lock:
            self._t0 = 0.0

    def banner(
        self,
        *,
        mode: str,
        persist_dir: str,
        llm_line: Optional[str] = None,
    ) -> None:
        title = self._c(_BOLD + _CYAN, "记忆沙箱")
        self._write_err("")
        self._write_err(self._c(_DIM, "╭─ ") + title + self._c(_DIM, " ────────────────────────────────"))
        self._write_err(f"│  {self._c(_DIM, '模式')}  {mode}")
        self._write_err(f"│  {self._c(_DIM, '记忆')}  {persist_dir}")
        if llm_line:
            self._write_err(f"│  {self._c(_DIM, 'LLM')}   {llm_line}")
        self._write_err(
            self._c(_DIM, "│  指令  切换Agent模式：ask|plan|agent · 记住：问 => 答 · 帮助 · quit")
        )
        self._write_err(self._c(_DIM, "╰────────────────────────────────────────────"))
        self._write_err(self._c(_DIM, "  进度在下方滚动；答案在回复区。长等待会显示旋转指示。"))

    def prompt_label(self) -> str:
        return self._c(_BOLD + _CYAN, "你") + self._c(_DIM, " › ")

    def print_result(self, result, as_json: bool = False) -> None:
        self.end_turn()
        if as_json:
            import json

            print(
                json.dumps(
                    {
                        "answer": result.answer,
                        "source": result.source,
                        "meta": result.meta,
                        "hit_local": result.source not in ("miss", "llm", "sensory_reject"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=self.out,
            )
            return

        color, label = _SOURCE_STYLE.get(result.source, (_DIM, result.source))
        badge = self._c(color, f"● {label}")
        self._write_err(self._c(_DIM, "─── ") + badge + self._c(_DIM, " ───"))

        answer = (result.answer or "").rstrip()
        if not answer:
            if result.source == "miss":
                print(self._c(_YELLOW, "(无本地命中)"), file=self.out)
            else:
                print("", file=self.out)
            return

        # 答案略缩进，和进度区分开；立刻 flush，避免与 stderr 进度交错
        for line in answer.splitlines() or [answer]:
            print(f"  {line}", file=self.out)
        print("", file=self.out)
        try:
            self.out.flush()
        except Exception:
            pass

    def bye(self) -> None:
        self.end_turn()
        self._write_err(self._c(_DIM, "再见。"))


def _is_long_stage(msg: str) -> bool:
    keys = (
        "Cursor Local",
        "Cursor Cloud",
        "Cursor SDK",
        "回退沙箱 LLM",
        "请求模型",
        "MockLLM",
        "思考",
        "Agent",
    )
    return any(k in msg for k in keys)


def _shorten_progress(msg: str) -> str:
    """去掉冗余前缀，界面更干净。"""
    m = msg
    for prefix in ("Cursor Local Agent：", "Cursor Cloud：", "Cursor SDK："):
        if m.startswith(prefix):
            m = m[len(prefix) :]
            break
    # 统一省略号
    m = m.replace("…", "").strip()
    if len(m) > 88:
        m = m[:85] + "…"
    return m
