#!/usr/bin/env python3
"""记忆沙箱 Mac GUI：双击 .app 即可运行。"""

from __future__ import annotations

import json
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

# 开发态保证可导入
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.config import load_config
from core.paths import app_support_dir, default_config_path, default_persist_dir, is_frozen


SOURCE_LABEL = {
    "working": "工作记忆",
    "long_term": "长时记忆",
    "procedural": "程序性记忆",
    "llm": "大模型",
    "command": "指令",
    "sensory_reject": "感觉记忆",
}


class MemorySandboxApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("记忆沙箱 Memory Sandbox")
        self.geometry("820x620")
        self.minsize(640, 480)
        self.configure(bg="#f6f3ee")

        self._busy = False
        self.sandbox = self._init_sandbox()
        self._build_ui()
        self._append_system(
            "优先检索本地三级记忆，缺失时才调用大模型。\n"
            "指令示例：记住：问 => 答 | 忘记刚才内容 | 查看记忆状态 | 切换场景：dev | 帮助"
        )
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _init_sandbox(self) -> MemorySandbox:
        cfg_path = str(default_config_path())
        cfg = load_config(cfg_path)
        # 打包后记忆写入用户目录，避免 .app 只读
        cfg.long_term.persist_dir = str(default_persist_dir())
        return MemorySandbox(config=cfg, config_path=cfg_path)

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass

        header = tk.Frame(self, bg="#1f2a24", height=64)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        title = tk.Label(
            header,
            text="记忆沙箱",
            font=("PingFang SC", 20, "bold"),
            fg="#f4efe6",
            bg="#1f2a24",
        )
        title.pack(side=tk.LEFT, padx=20, pady=14)

        subtitle = tk.Label(
            header,
            text="Sensory · Working · Long-Term",
            font=("Avenir Next", 11),
            fg="#9fb3a6",
            bg="#1f2a24",
        )
        subtitle.pack(side=tk.LEFT, padx=(0, 12), pady=18)

        toolbar = tk.Frame(self, bg="#f6f3ee")
        toolbar.pack(fill=tk.X, padx=16, pady=(12, 0))

        ttk.Button(toolbar, text="记忆状态", command=self.on_status).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="清空工作记忆", command=self.on_clear_working).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="写入开发种子", command=self.on_seed).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="打开数据目录", command=self.on_open_data).pack(side=tk.RIGHT)

        self.chat = scrolledtext.ScrolledText(
            self,
            wrap=tk.WORD,
            font=("PingFang SC", 13),
            bg="#fffaf3",
            fg="#1f2a24",
            insertbackground="#1f2a24",
            relief=tk.FLAT,
            padx=14,
            pady=12,
        )
        self.chat.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)
        self.chat.configure(state=tk.DISABLED)
        self.chat.tag_configure("user", foreground="#0f4c3a", font=("PingFang SC", 13, "bold"))
        self.chat.tag_configure("bot", foreground="#1f2a24")
        self.chat.tag_configure("meta", foreground="#6d7a72", font=("Avenir Next", 10))
        self.chat.tag_configure("sys", foreground="#7a6a4f", font=("PingFang SC", 12))

        bottom = tk.Frame(self, bg="#f6f3ee")
        bottom.pack(fill=tk.X, padx=16, pady=(0, 16))

        self.input = tk.Text(
            bottom,
            height=3,
            font=("PingFang SC", 13),
            bg="white",
            fg="#1f2a24",
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=0,
            padx=10,
            pady=8,
        )
        self.input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.input.bind("<Command-Return>", self.on_send)
        self.input.bind("<Control-Return>", self.on_send)

        send_btn = ttk.Button(bottom, text="发送 ⌘↩", command=self.on_send)
        send_btn.pack(side=tk.LEFT, padx=(10, 0), ipady=18)

        self.status = tk.Label(
            self,
            text=self._status_text(),
            anchor="w",
            font=("Avenir Next", 10),
            fg="#5c685f",
            bg="#eae4da",
            padx=16,
            pady=6,
        )
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

    def _status_text(self) -> str:
        st = self.sandbox.status()
        return (
            f"工作记忆 {st['working']['size']}/{st['working']['max_size']} · "
            f"长时记忆 {st['long_term']['declarative_count']} 条 · "
            f"场景 {st['working']['scene']} · "
            f"数据 {app_support_dir()}"
        )

    def _append(self, text: str, tag: str):
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, text + "\n", tag)
        self.chat.see(tk.END)
        self.chat.configure(state=tk.DISABLED)

    def _append_system(self, text: str):
        self._append(text, "sys")

    def on_send(self, event=None):
        if self._busy:
            return "break"
        text = self.input.get("1.0", tk.END).strip()
        if not text:
            return "break"
        self.input.delete("1.0", tk.END)
        self._append(f"你：{text}", "user")
        self._busy = True
        self.status.configure(text="思考中…")

        def worker():
            try:
                result = self.sandbox.chat(text)
            except Exception as e:
                result = None
                err = str(e)
                self.after(0, lambda: self._on_error(err))
                return
            self.after(0, lambda: self._on_result(result))

        threading.Thread(target=worker, daemon=True).start()
        return "break"

    def _on_result(self, result):
        self._busy = False
        label = SOURCE_LABEL.get(result.source, result.source)
        self._append(f"沙箱：{result.answer}", "bot")
        self._append(f"← 来源：{label} ({result.source})\n", "meta")
        self.status.configure(text=self._status_text())

    def _on_error(self, err: str):
        self._busy = False
        self._append(f"错误：{err}", "meta")
        self.status.configure(text=self._status_text())
        messagebox.showerror("记忆沙箱", err)

    def on_status(self):
        data = self.sandbox.status()
        self._append(json.dumps(data, ensure_ascii=False, indent=2), "meta")
        self.status.configure(text=self._status_text())

    def on_clear_working(self):
        self.sandbox.working.clear()
        self._append_system("工作记忆已清空。")
        self.status.configure(text=self._status_text())

    def on_seed(self):
        samples = [
            ("如何启动本地前端", "在项目根目录执行 pnpm install && pnpm start，注意检查 .npmrc 私源配置。"),
            ("agency 项目怎么跑", "进入 live_web_agency，执行 pnpm install，再 pnpm start；e2e 用 agency-e2e。"),
            ("切换开发环境要注意什么", "确认当前 Node/pnpm 版本、hosts/代理、环境变量（.env）以及对应业务的 mock 开关。"),
            ("记忆沙箱怎么减少 token", "优先把高频问答用「记住：问 => 答」写入长时记忆；重复问题会直接命中沙箱，不走大模型。"),
            ("git 提交规范", "使用简洁祈使句说明 why；不要自动 push；不要改 git config。"),
        ]
        for q, a in samples:
            self.sandbox.remember(q, a, scene="dev")
        self.sandbox.working.set_scene("dev")
        self._append_system(f"已写入 {len(samples)} 条开发场景记忆，当前场景: dev")
        self.status.configure(text=self._status_text())

    def on_open_data(self):
        path = app_support_dir()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(path)])
        else:
            messagebox.showinfo("数据目录", str(path))


def main():
    # 打包态避免部分环境 cwd 异常
    if is_frozen():
        try:
            import os
            os.chdir(app_support_dir())
        except Exception:
            pass
    app = MemorySandboxApp()
    app.mainloop()


if __name__ == "__main__":
    main()
