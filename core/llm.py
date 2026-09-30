"""大模型适配器：仅在沙箱无解时调用。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from .config import LLMConfig

ProgressCallback = Callable[[str], None]

# provider 别名：build_llm 与各处 describe 共用，避免多处硬编码字符串集合
CURSOR_PROVIDERS = {"cursor", "cursor_cloud", "cursor-agent"}
TRAE_PROVIDERS = {"trae", "trae_cli", "trae-cli", "traecli", "coco"}

# 规范名 → 界面标签。界面下拉直接渲染这里的内容，避免前端再写一份标签跟着漂。
# 顶栏那个下拉挤在主题和几个按钮中间，标签一律短写，语义靠 title 提示补
PROVIDER_LABELS = {
    "cursor": "Cursor Agent",
    "trae": "Trae CLI",
    "cursor_cloud": "Cursor Cloud",
    "openai_compatible": "OpenAI 网关",
    "mock": "离线占位",
}

# 界面下拉里给出的切换项；cursor_cloud / openai_compatible 需要额外配密钥或网关，
# 不摆在导航里，但仍可在 config 里写、也仍能通过接口切
PROVIDER_CHOICES = ("cursor", "trae", "mock")


def normalize_llm_provider(value: str) -> str:
    """把别名规范成 build_llm 认的名字；未知名字当场报错，别等回退那一刻才炸。"""
    v = (value or "").strip().lower()
    if not v:
        raise ValueError("provider 不能为空")
    if v in TRAE_PROVIDERS:
        return "trae"
    if v == "cursor_cloud":
        return "cursor_cloud"
    if v in CURSOR_PROVIDERS:
        return "cursor"
    if v in {"openai", "openai_compatible"}:
        return "openai_compatible"
    if v == "mock":
        return "mock"
    raise ValueError(f"未知 llm.provider: {value}")


def provider_label(value: str) -> str:
    try:
        name = normalize_llm_provider(value)
    except ValueError:
        return value or "未设置"
    return PROVIDER_LABELS.get(name, name)


def llm_provider_options(current: str = "") -> List[Dict[str, str]]:
    """界面下拉选项。当前 provider 不在常规选项里（如配了 OpenAI 网关）时补进去，
    否则下拉会显示成别的值，让人以为已经切走了。"""
    options = [{"value": v, "label": PROVIDER_LABELS[v]} for v in PROVIDER_CHOICES]
    try:
        name = normalize_llm_provider(current)
    except ValueError:
        return options
    if name not in PROVIDER_CHOICES:
        options.insert(0, {"value": name, "label": PROVIDER_LABELS.get(name, name)})
    return options

# 记忆沙箱回退 Agent：禁止推远程；推送由用户本机自行决定
_GIT_BAN = (
    "【硬性约束】禁止执行任何 git push / git push --force / gh pr create 等推送或发布远程操作；"
    "禁止要求用户登录 GitHub 来替你完成推送。"
    "若只需同步远程，用一句话提示用户自行在本机终端 push，不要代为执行。"
    "本地 git status / diff / log 只读查询可以。"
)


def is_read_only_agent_mode(agent_mode: str) -> bool:
    return (agent_mode or "").strip().lower() in {"ask", "plan"}


def build_local_agent_prompt(agent_mode: str, prompt: str, context: str = "") -> str:
    """本机 Agent CLI（Cursor / Trae）共用的提示词。"""
    if is_read_only_agent_mode(agent_mode):
        text = (
            "你是开发助手。请基于当前工作区磁盘上的真实文件回答；"
            "简洁、可落地。当前为只读/规划模式：不要修改文件、不要开 PR、不要 commit。\n"
            f"{_GIT_BAN}\n\n"
        )
    else:
        text = (
            "你是开发助手。请基于当前工作区磁盘上的真实文件回答；"
            "简洁、可落地。当前为 Agent 全工具模式：可按需改本地文件；"
            "不要自动 git commit，除非用户明确要求提交。\n"
            f"{_GIT_BAN}\n\n"
        )
    if context:
        text += f"近期上下文:\n{context}\n\n"
    text += f"用户问题:\n{prompt}"
    return text


def _emit(on_progress: Optional[ProgressCallback], message: str) -> None:
    if on_progress:
        try:
            on_progress(message)
        except Exception:
            pass


class BaseLLM(ABC):
    @abstractmethod
    def generate(
        self,
        prompt: str,
        context: str = "",
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        raise NotImplementedError


class MockLLM(BaseLLM):
    """本地占位：无外部 API 时返回提示，便于离线开发。"""

    def generate(
        self,
        prompt: str,
        context: str = "",
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        _emit(on_progress, "MockLLM：未配置真实模型，返回占位提示")
        hint = (
            "[MockLLM] 沙箱三层记忆均未命中。\n"
            f"问题: {prompt}\n"
        )
        if context:
            hint += f"近期上下文:\n{context}\n"
        hint += (
            "提示: 说「记一下 <内容>」即可写入长时记忆；"
            "或在 config 将 llm.provider 设为 cursor / openai_compatible 并配置 API。"
        )
        return hint


class OpenAICompatibleLLM(BaseLLM):
    """兼容 OpenAI Chat Completions 的 HTTP 调用（标准库实现）。"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.base_url = (config.base_url or os.getenv("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = config.api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = config.model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = config.timeout or 60
        if not self.base_url:
            raise ValueError("openai_compatible 需要配置 llm.base_url 或环境变量 OPENAI_BASE_URL")

    def generate(
        self,
        prompt: str,
        context: str = "",
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        system = (
            "你是本地开发助手。优先给出可落地的简洁回答。"
            "若提供了上下文，请结合上下文作答。"
        )
        user_content = prompt
        if context:
            user_content = f"上下文:\n{context}\n\n用户问题:\n{prompt}"

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        _emit(on_progress, f"正在请求模型 {self.model}（超时 {self.timeout}s）…")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            _emit(on_progress, "模型响应已返回")
            return body["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            return f"[LLM Error] HTTP {e.code}: {detail}"
        except Exception as e:
            return f"[LLM Error] {e}"


class CursorCloudLLM(BaseLLM):
    """
    通过 Cursor Cloud Agents API 调用 Cursor 模型（crsr_ API Key）。
    使用无仓库 Agent，适合「记忆未命中 → 问模型」的问答回退。
    文档：https://cursor.com/docs/cloud-agent/api/endpoints
    """

    TERMINAL = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}

    def __init__(self, config: LLMConfig):
        self.config = config
        self.api_key = (
            (config.api_key or "").strip()
            or os.getenv("CURSOR_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        self.base_url = (
            (config.base_url or "").strip()
            or os.getenv("CURSOR_API_BASE", "https://api.cursor.com")
        ).rstrip("/")
        self.model = (config.model or os.getenv("CURSOR_MODEL", "")).strip()
        self.timeout = int(config.timeout or 300)
        if not self.api_key:
            raise ValueError("cursor provider 需要 llm.api_key 或环境变量 CURSOR_API_KEY")

    def _auth_headers(self) -> Dict[str, str]:
        # Cloud Agents API 同时接受 Bearer 与 Basic(api_key:)
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "memory-sandbox/0.1",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = self._auth_headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or min(120, self.timeout)) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e

    def generate(
        self,
        prompt: str,
        context: str = "",
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        text = (
            "你是开发助手。请简洁、可落地地回答用户问题。"
            "不要创建仓库、不要开 PR、不要 git push、不要修改远程代码；只输出文字答案。\n\n"
        )
        if context:
            text += f"近期上下文:\n{context}\n\n"
        text += f"用户问题:\n{prompt}"

        body: Dict[str, Any] = {
            "prompt": {"text": text},
            "name": "memory-sandbox-fallback",
        }
        if self.model:
            body["model"] = {"id": self.model}

        # 创建接口可能同步等到首轮 run 结束，超时需覆盖整段等待
        create_timeout = max(120, self.timeout)
        model_hint = self.model or "默认模型"
        _emit(
            on_progress,
            f"Cursor Cloud：正在创建 Agent（模型 {model_hint}，此步常需 30～120s，请稍候）…",
        )
        t0 = time.time()
        try:
            created = self._request("POST", "/v1/agents", body, timeout=create_timeout)
        except Exception as e:
            return f"[LLM Error] 创建 Cursor Agent 失败: {e}"

        agent = created.get("agent") or {}
        run = created.get("run") or {}
        agent_id = agent.get("id") or ""
        run_id = run.get("id") or agent.get("latestRunId") or ""
        if not agent_id or not run_id:
            return f"[LLM Error] Cursor API 响应缺少 agent/run id: {json.dumps(created, ensure_ascii=False)[:400]}"

        last_status = str(run.get("status") or "CREATING")
        result_text = (run.get("result") or "").strip()
        _emit(
            on_progress,
            f"Cursor Cloud：Agent 已创建（{time.time() - t0:.0f}s），run 状态={last_status}",
        )
        try:
            # 创建响应里 run 已结束时可直接用
            if last_status in self.TERMINAL:
                if last_status != "FINISHED":
                    return (
                        f"[LLM Error] Cursor run {last_status}"
                        + (f": {result_text}" if result_text else "")
                    )
                if result_text:
                    _emit(on_progress, "Cursor Cloud：模型已完成，正在整理答案…")
                    return result_text

            deadline = time.time() + max(30, self.timeout)
            poll_n = 0
            prev_status = last_status
            while time.time() < deadline:
                info = self._request(
                    "GET",
                    f"/v1/agents/{agent_id}/runs/{run_id}",
                    timeout=60,
                )
                last_status = str(info.get("status") or "")
                poll_n += 1
                elapsed = time.time() - t0
                # 状态变化必报；否则约每 3 次轮询报一次心跳
                if poll_n == 1 or last_status != prev_status or poll_n % 3 == 0:
                    _emit(
                        on_progress,
                        f"Cursor Cloud：模型思考中… status={last_status or '?'}（已等待 {elapsed:.0f}s）",
                    )
                prev_status = last_status
                if last_status in self.TERMINAL:
                    result_text = (info.get("result") or "").strip()
                    if last_status != "FINISHED":
                        return (
                            f"[LLM Error] Cursor run {last_status}"
                            + (f": {result_text}" if result_text else "")
                        )
                    if not result_text:
                        return "[LLM Error] Cursor run 已结束但无 result 文本"
                    _emit(on_progress, f"Cursor Cloud：完成（总耗时 {elapsed:.0f}s）")
                    return result_text
                time.sleep(1.5)
            return f"[LLM Error] Cursor run 超时（>{self.timeout}s），最后状态={last_status}"
        finally:
            # 尽力归档，避免堆积无仓库 Agent
            try:
                _emit(on_progress, "Cursor Cloud：归档临时 Agent…")
                self._request("POST", f"/v1/agents/{agent_id}/archive", payload={}, timeout=15)
            except Exception:
                pass


def resolve_agent_bin(config: LLMConfig) -> Optional[str]:
    """解析本机 Cursor agent CLI 路径。

    Web/DMG 等 GUI 启动时常没有 shell 的 PATH（~/.local/bin 不在其中），
    因此除 PATH 查找外，再探测常见安装位置。
    """
    explicit = (config.agent_bin or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else explicit
    for name in ("agent", "cursor-agent"):
        found = shutil.which(name)
        if found:
            return found
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "agent"),
        os.path.join(home, ".local", "bin", "cursor-agent"),
        "/usr/local/bin/agent",
        "/usr/local/bin/cursor-agent",
        "/opt/homebrew/bin/agent",
        "/opt/homebrew/bin/cursor-agent",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def resolve_llm_cwd(config: LLMConfig) -> str:
    explicit = (config.cwd or os.getenv("CURSOR_CWD", "")).strip()
    if explicit:
        return explicit
    cwd = (os.getcwd() or "").strip()
    # 打包 App 常 chdir 到 Application Support/MemorySandbox，不适合做 Agent 工作区
    norm = cwd.replace("\\", "/")
    if "Application Support/MemorySandbox" in norm:
        home = os.path.expanduser("~")
        docs = os.path.join(home, "Documents")
        return docs if os.path.isdir(docs) else home
    return cwd or os.path.expanduser("~")


def describe_cursor_llm(config: LLMConfig) -> str:
    """供 CLI 启动时展示回退目标。"""
    from .config import agent_ui_mode_from_config

    runtime = (config.runtime or "local").lower().strip()
    if runtime == "cloud":
        return "cursor/cloud（无仓库，不能读本机盘）"
    cwd = resolve_llm_cwd(config)
    mode = agent_ui_mode_from_config(config)
    force = " force" if config.agent_force and mode == "agent" else ""
    return f"cursor/local workspace={cwd} mode={mode}{force}"


class CursorLocalAgentLLM(BaseLLM):
    """
    通过本机 Cursor `agent` CLI（--workspace）读本地盘。
    默认 --mode ask 只读；找不到 agent 时 generate 返回明确错误，不静默回落 Cloud。
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self.api_key = (
            (config.api_key or "").strip()
            or os.getenv("CURSOR_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        self.model = (config.model or os.getenv("CURSOR_MODEL", "")).strip()
        self.cwd = resolve_llm_cwd(config)
        self.timeout = int(config.timeout or 600)
        self.agent_bin = resolve_agent_bin(config)

    def generate(
        self,
        prompt: str,
        context: str = "",
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        # 每次从 config 读取，支持运行时切换 ask/plan/agent
        agent_mode = (self.config.agent_mode or "").strip()
        agent_force = bool(self.config.agent_force)
        approve_mcps = bool(getattr(self.config, "approve_mcps", True))
        self.cwd = resolve_llm_cwd(self.config)
        self.agent_bin = resolve_agent_bin(self.config) or self.agent_bin

        if not self.agent_bin:
            return (
                "[LLM Error] 未找到本机 Cursor agent CLI（agent / cursor-agent）。\n"
                "请安装 Cursor Agent 并确保在 PATH 中，或在 config 设置 llm.agent_bin。\n"
                "若只要 Cloud 无仓库问答，将 llm.runtime 设为 cloud（仍不能读本机盘）。"
            )
        if not os.path.isdir(self.cwd):
            return f"[LLM Error] workspace 目录不存在: {self.cwd}"

        text = build_local_agent_prompt(agent_mode, prompt, context)

        cmd: List[str] = [
            self.agent_bin,
            "-p",
            "--output-format",
            "text",
            "--trust",
            "--workspace",
            self.cwd,
        ]
        if agent_mode:
            cmd.extend(["--mode", agent_mode])
        if agent_force:
            cmd.append("--force")
        if approve_mcps:
            # 使 ~/.cursor/mcp.json 中的飞书等 MCP 在无交互 -p 模式下可用
            cmd.append("--approve-mcps")
        if self.model:
            cmd.extend(["--model", self.model])
        if self.api_key:
            cmd.extend(["--api-key", self.api_key])
        cmd.append(text)

        mode_hint = agent_mode or "agent"
        self.timeout = int(self.config.timeout or self.timeout or 600)
        _emit(
            on_progress,
            f"Cursor Local Agent：workspace={self.cwd} mode={mode_hint}"
            f"{' force' if agent_force else ''}"
            f"{' approve-mcps' if approve_mcps else ''}"
            f" timeout={self.timeout}s…",
        )
        env = os.environ.copy()
        if self.api_key:
            env["CURSOR_API_KEY"] = self.api_key
        # 告诉用户级 hook：这是记忆沙箱自己拉起来的嵌套 agent，读写两侧门禁都别管它。
        # 不加这个标记，它会被 stop 门禁逼着自己 memory_remember 一条，而调用方
        # （评论机器人 / IM 机器人）随后还会写一条，一次问答落两条互相打架的记忆。
        env["MEMORY_SANDBOX_NESTED"] = "1"

        t0 = time.time()
        # 用 Popen 以便超时时 kill 并尽量拿到已缓冲的 stdout/stderr
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.cwd,
                env=env,
            )
        except FileNotFoundError:
            return f"[LLM Error] 无法执行 agent：{self.agent_bin}"
        except Exception as e:
            return (
                f"[LLM Error] Cursor Local Agent 启动失败: {type(e).__name__}: {e}\n"
                f"agent_bin={self.agent_bin}\nworkspace={self.cwd}"
            )

        try:
            out, err = proc.communicate(timeout=max(30, self.timeout))
        except subprocess.TimeoutExpired as e:
            _emit(on_progress, f"Cursor Local Agent：超时，正在终止进程（>{self.timeout}s）…")
            try:
                proc.kill()
            except Exception:
                pass
            try:
                out2, err2 = proc.communicate(timeout=5)
            except Exception:
                out2, err2 = "", ""
            # TimeoutExpired 可能已带部分输出
            partial_out = ""
            partial_err = ""
            if isinstance(getattr(e, "stdout", None), str):
                partial_out = e.stdout
            if isinstance(getattr(e, "stderr", None), str):
                partial_err = e.stderr
            out = (partial_out or out2 or "").strip()
            err = (partial_err or err2 or "").strip()
            elapsed = time.time() - t0
            lines = [
                f"[LLM Error] Cursor Local Agent 超时（>{self.timeout}s，实际约 {elapsed:.0f}s）",
                f"exception: subprocess.TimeoutExpired",
                f"workspace={self.cwd}",
                f"agent_bin={self.agent_bin}",
                f"mode={mode_hint} force={agent_force}",
                f"returncode={proc.returncode}",
                "说明：Agent 在时限内未结束。常见原因：飞书/外网需登录或很慢、任务过重、timeout 过短。",
                "处理：在用户 config 提高 llm.timeout（如 900）；llm.cwd 设为项目目录；确认能访问该文档。",
            ]
            if err:
                lines.append("—— stderr ——\n" + err[:2000])
            if out:
                lines.append("—— stdout（部分） ——\n" + out[:2000])
            if not err and not out:
                lines.append("—— 无 stdout/stderr（进程可能一直阻塞且未刷缓冲）——")
            msg = "\n".join(lines)
            _emit(on_progress, "Cursor Local Agent：已超时并暴露诊断信息")
            return msg
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            return (
                f"[LLM Error] Cursor Local Agent 异常: {type(e).__name__}: {e}\n"
                f"workspace={self.cwd}\nagent_bin={self.agent_bin}"
            )

        out = (out or "").strip()
        err = (err or "").strip()
        elapsed = time.time() - t0
        if proc.returncode != 0:
            detail = err or out or f"（无输出）"
            return (
                f"[LLM Error] Cursor Local Agent 失败（{elapsed:.0f}s）\n"
                f"exception: nonzero exit\n"
                f"returncode={proc.returncode}\n"
                f"workspace={self.cwd}\n"
                f"agent_bin={self.agent_bin}\n"
                f"mode={mode_hint}\n"
                f"—— stderr/stdout ——\n{detail[:2000]}"
            )
        if not out:
            return (
                f"[LLM Error] Cursor Local Agent 无输出（{elapsed:.0f}s）\n"
                f"returncode={proc.returncode}\n"
                f"workspace={self.cwd}\n"
                f"—— stderr ——\n{(err or '（空）')[:1500]}"
            )
        _emit(on_progress, f"Cursor Local Agent：完成（{elapsed:.0f}s）")
        return out


TRAE_BIN_NAMES = ("coco", "traecli", "traex")


def resolve_trae_bin(config: LLMConfig) -> Optional[str]:
    """解析本机 Trae CLI 路径（coco 通常是 traex 的软链）。

    与 resolve_agent_bin 同理：GUI 启动时 ~/.local/bin 常不在 PATH 里，需要显式探测。
    """
    explicit = (config.trae_bin or "").strip()
    if explicit:
        return explicit
    for name in TRAE_BIN_NAMES:
        found = shutil.which(name)
        if found:
            return found
    home = os.path.expanduser("~")
    prefixes = (
        os.path.join(home, ".local", "bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    )
    for prefix in prefixes:
        for name in TRAE_BIN_NAMES:
            path = os.path.join(prefix, name)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None


def describe_trae_llm(config: LLMConfig) -> str:
    """供 CLI 启动时展示回退目标。"""
    from .config import agent_ui_mode_from_config

    cwd = resolve_llm_cwd(config)
    mode = agent_ui_mode_from_config(config)
    bin_path = resolve_trae_bin(config)
    bin_hint = os.path.basename(bin_path) if bin_path else "未找到"
    return f"trae/local workspace={cwd} mode={mode} bin={bin_hint}"


def build_trae_cmd(
    bin_path: str,
    prompt_text: str,
    cwd: str,
    agent_mode: str,
    model: str = "",
    last_message_file: str = "",
) -> List[str]:
    """拼 `traecli exec` 命令行。

    注意 Trae CLI 与 Cursor agent 的差异（照抄 Cursor 那套会静默跑错）：
    - 非交互是 `exec` 子命令；`-p` 在 traecli 里是 `--profile`，不是 prompt
    - 工作目录用 `-C/--cd`，没有 `--workspace`
    - 只读/可写由 `-s/--sandbox` 控制，而不是 `--mode` + `--force`
    - prompt 是位置参数，必须放在所有选项之后
    """
    cmd = [
        bin_path,
        "exec",
        "--color",
        "never",
        # 回退问答的 cwd 常是 ~/Documents 这类非 git 目录，不跳过检查会直接退出
        "--skip-git-repo-check",
        # 一次性问答，不必留会话文件
        "--ephemeral",
        "-C",
        cwd,
        "-s",
        "read-only" if is_read_only_agent_mode(agent_mode) else "workspace-write",
    ]
    if model:
        cmd.extend(["-m", model])
    if last_message_file:
        # stdout 混着进度与工具日志，终答单独落文件最干净（也不必解析 --json 的 JSONL）
        cmd.extend(["-o", last_message_file])
    cmd.append(prompt_text)
    return cmd


class TraeLocalAgentLLM(BaseLLM):
    """
    通过本机 Trae CLI（coco / traecli）的 exec 子命令读本地盘。
    Cursor 额度用完时的替代回退：Trae 走自己的登录态（coco login），不吃 Cursor 配额。
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self.model = (config.model or os.getenv("TRAE_MODEL", "")).strip()
        self.cwd = resolve_llm_cwd(config)
        self.timeout = int(config.timeout or 600)
        self.trae_bin = resolve_trae_bin(config)

    def generate(
        self,
        prompt: str,
        context: str = "",
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        # 每次从 config 读，支持运行时切 ask/agent 与工作目录
        agent_mode = (self.config.agent_mode or "").strip().lower()
        self.cwd = resolve_llm_cwd(self.config)
        self.trae_bin = resolve_trae_bin(self.config) or self.trae_bin
        self.timeout = int(self.config.timeout or self.timeout or 600)
        self.model = (self.config.model or self.model or "").strip()

        if not self.trae_bin:
            return (
                "[LLM Error] 未找到本机 Trae CLI（coco / traecli / traex）。\n"
                "请安装 TraeCode CLI 并确保在 PATH 中，或在 config 设置 llm.trae_bin。"
            )
        if not os.path.isdir(self.cwd):
            return f"[LLM Error] workspace 目录不存在: {self.cwd}"

        text = build_local_agent_prompt(agent_mode, prompt, context)
        mode_hint = "read-only" if is_read_only_agent_mode(agent_mode) else "workspace-write"
        _emit(
            on_progress,
            f"Trae Local Agent：workspace={self.cwd} sandbox={mode_hint}"
            f" model={self.model or '默认'} timeout={self.timeout}s…",
        )

        fd, last_msg_path = tempfile.mkstemp(prefix="memory-sandbox-trae-", suffix=".txt")
        os.close(fd)
        cmd = build_trae_cmd(
            self.trae_bin,
            text,
            self.cwd,
            agent_mode,
            model=self.model,
            last_message_file=last_msg_path,
        )
        env = os.environ.copy()
        # 同 Cursor 分支：告诉用户级 hook 这是沙箱自己拉起的嵌套 agent，两侧门禁都别管它
        env["MEMORY_SANDBOX_NESTED"] = "1"

        t0 = time.time()
        try:
            code, out, err, timed_out = _run_cli_capture(cmd, self.cwd, env, self.timeout)
        except FileNotFoundError:
            os.unlink(last_msg_path)
            return f"[LLM Error] 无法执行 Trae CLI：{self.trae_bin}"
        except Exception as e:
            os.unlink(last_msg_path)
            return (
                f"[LLM Error] Trae Local Agent 启动失败: {type(e).__name__}: {e}\n"
                f"trae_bin={self.trae_bin}\nworkspace={self.cwd}"
            )

        elapsed = time.time() - t0
        try:
            with open(last_msg_path, "r", encoding="utf-8") as f:
                answer = f.read().strip()
        except Exception:
            answer = ""
        finally:
            try:
                os.unlink(last_msg_path)
            except Exception:
                pass

        if timed_out:
            _emit(on_progress, "Trae Local Agent：已超时并暴露诊断信息")
            lines = [
                f"[LLM Error] Trae Local Agent 超时（>{self.timeout}s，实际约 {elapsed:.0f}s）",
                f"workspace={self.cwd}",
                f"trae_bin={self.trae_bin}",
                f"sandbox={mode_hint}",
                "说明：Agent 在时限内未结束。常见原因：任务过重、外网/登录很慢、timeout 过短。",
                "处理：在用户 config 提高 llm.timeout（如 900）；llm.cwd 设为项目目录。",
            ]
            if answer:
                lines.append("—— 已写出的部分终答 ——\n" + answer[:2000])
            if err:
                lines.append("—— stderr ——\n" + err[:2000])
            return "\n".join(lines)

        if code != 0:
            detail = (err or out or "（无输出）")[:2000]
            return (
                f"[LLM Error] Trae Local Agent 失败（{elapsed:.0f}s）\n"
                f"returncode={code}\n"
                f"workspace={self.cwd}\n"
                f"trae_bin={self.trae_bin}\n"
                f"sandbox={mode_hint}\n"
                "若提示未登录/鉴权失败，先在终端跑一次 coco login。\n"
                f"—— stderr/stdout ——\n{detail}"
            )

        if not answer:
            # 极少数情况下 -o 没落盘（如模型只调工具没给终答），退回 stdout
            answer = (out or "").strip()
        if not answer:
            return (
                f"[LLM Error] Trae Local Agent 无输出（{elapsed:.0f}s）\n"
                f"returncode={code}\n"
                f"workspace={self.cwd}\n"
                f"—— stderr ——\n{(err or '（空）')[:1500]}"
            )
        _emit(on_progress, f"Trae Local Agent：完成（{elapsed:.0f}s）")
        return answer


def _run_cli_capture(
    cmd: List[str],
    cwd: str,
    env: Dict[str, str],
    timeout: int,
) -> "tuple[int, str, str, bool]":
    """跑子进程并捕获输出；超时则 kill 并尽量取回已缓冲的内容。

    返回 (returncode, stdout, stderr, timed_out)。
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
    )
    try:
        out, err = proc.communicate(timeout=max(30, timeout))
        return proc.returncode, (out or "").strip(), (err or "").strip(), False
    except subprocess.TimeoutExpired as e:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out2, err2 = proc.communicate(timeout=5)
        except Exception:
            out2, err2 = "", ""
        partial_out = e.stdout if isinstance(getattr(e, "stdout", None), str) else ""
        partial_err = e.stderr if isinstance(getattr(e, "stderr", None), str) else ""
        return (
            proc.returncode,
            (partial_out or out2 or "").strip(),
            (partial_err or err2 or "").strip(),
            True,
        )


def describe_llm_target(config: LLMConfig, include_runtime: bool = False) -> str:
    """各 provider 的统一展示口径，供 CLI banner 与沙箱进度提示共用。"""
    provider = (config.provider or "").lower().strip()
    if provider in CURSOR_PROVIDERS:
        return describe_cursor_llm(config)
    if provider in TRAE_PROVIDERS:
        return describe_trae_llm(config)
    hint = f"provider={config.provider or 'llm'}"
    runtime = (config.runtime or "").strip()
    if include_runtime and runtime:
        hint += f" runtime={runtime}"
    return hint


def build_llm(config: LLMConfig) -> Optional[BaseLLM]:
    if not config.enabled:
        return None
    provider = (config.provider or "mock").lower()
    if provider == "mock":
        return MockLLM()
    if provider in {"openai", "openai_compatible"}:
        return OpenAICompatibleLLM(config)
    if provider in TRAE_PROVIDERS:
        # Trae 只有本机 CLI 这一条路（无 Cloud REST），找不到二进制时在 generate 报错
        return TraeLocalAgentLLM(config)
    if provider in CURSOR_PROVIDERS:
        runtime = (config.runtime or "local").lower().strip()
        # 显式 cloud provider 名或 runtime=cloud → 无仓库 Cloud REST
        if provider == "cursor_cloud" or runtime == "cloud":
            return CursorCloudLLM(config)
        # 默认 local：本机 agent CLI 读盘；找不到二进制时在 generate 报错，不静默回落 Cloud
        return CursorLocalAgentLLM(config)
    raise ValueError(f"未知 llm.provider: {config.provider}")
