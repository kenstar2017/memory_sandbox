"""大模型适配器：仅在沙箱无解时调用。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from .config import LLMConfig

ProgressCallback = Callable[[str], None]


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
            "提示: 可用「记住：问 => 答」写入长时记忆；"
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
            "不要创建仓库、不要开 PR、不要修改远程代码；只输出文字答案。\n\n"
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
    """解析本机 Cursor agent CLI 路径。"""
    explicit = (config.agent_bin or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else explicit
    for name in ("agent", "cursor-agent"):
        found = shutil.which(name)
        if found:
            return found
    return None


def resolve_llm_cwd(config: LLMConfig) -> str:
    return (config.cwd or os.getenv("CURSOR_CWD", "") or os.getcwd()).strip() or os.getcwd()


def describe_cursor_llm(config: LLMConfig) -> str:
    """供 CLI 启动时展示回退目标。"""
    runtime = (config.runtime or "local").lower().strip()
    if runtime == "cloud":
        return "cursor/cloud（无仓库，不能读本机盘）"
    cwd = resolve_llm_cwd(config)
    mode = (config.agent_mode or "").strip() or "full"
    return f"cursor/local workspace={cwd} mode={mode}"


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
        self.agent_mode = (config.agent_mode or "").strip()
        self.agent_force = bool(config.agent_force)
        self.agent_bin = resolve_agent_bin(config)

    def generate(
        self,
        prompt: str,
        context: str = "",
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        if not self.agent_bin:
            return (
                "[LLM Error] 未找到本机 Cursor agent CLI（agent / cursor-agent）。\n"
                "请安装 Cursor Agent 并确保在 PATH 中，或在 config 设置 llm.agent_bin。\n"
                "若只要 Cloud 无仓库问答，将 llm.runtime 设为 cloud（仍不能读本机盘）。"
            )
        if not os.path.isdir(self.cwd):
            return f"[LLM Error] workspace 目录不存在: {self.cwd}"

        text = (
            "你是开发助手。请基于当前工作区磁盘上的真实文件回答；"
            "简洁、可落地。默认只读分析，不要修改文件、不要开 PR。\n\n"
        )
        if context:
            text += f"近期上下文:\n{context}\n\n"
        text += f"用户问题:\n{prompt}"

        cmd: List[str] = [
            self.agent_bin,
            "-p",
            "--output-format",
            "text",
            "--trust",
            "--workspace",
            self.cwd,
        ]
        if self.agent_mode:
            cmd.extend(["--mode", self.agent_mode])
        if self.agent_force:
            cmd.append("--force")
        if self.model:
            cmd.extend(["--model", self.model])
        if self.api_key:
            cmd.extend(["--api-key", self.api_key])
        cmd.append(text)

        mode_hint = self.agent_mode or "full"
        _emit(
            on_progress,
            f"Cursor Local Agent：workspace={self.cwd} mode={mode_hint}（可读本机盘）…",
        )
        env = os.environ.copy()
        if self.api_key:
            env["CURSOR_API_KEY"] = self.api_key

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(30, self.timeout),
                cwd=self.cwd,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return f"[LLM Error] Cursor Local Agent 超时（>{self.timeout}s），workspace={self.cwd}"
        except FileNotFoundError:
            return f"[LLM Error] 无法执行 agent：{self.agent_bin}"
        except Exception as e:
            return f"[LLM Error] Cursor Local Agent: {e}"

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        elapsed = time.time() - t0
        if proc.returncode != 0:
            detail = err or out or f"exit={proc.returncode}"
            return f"[LLM Error] Cursor Local Agent 失败（{elapsed:.0f}s）: {detail[:800]}"
        if not out:
            return f"[LLM Error] Cursor Local Agent 无输出（{elapsed:.0f}s）: {(err or '')[:400]}"
        _emit(on_progress, f"Cursor Local Agent：完成（{elapsed:.0f}s）")
        return out


def build_llm(config: LLMConfig) -> Optional[BaseLLM]:
    if not config.enabled:
        return None
    provider = (config.provider or "mock").lower()
    if provider == "mock":
        return MockLLM()
    if provider in {"openai", "openai_compatible"}:
        return OpenAICompatibleLLM(config)
    if provider in {"cursor", "cursor_cloud", "cursor-agent"}:
        runtime = (config.runtime or "local").lower().strip()
        # 显式 cloud provider 名或 runtime=cloud → 无仓库 Cloud REST
        if provider == "cursor_cloud" or runtime == "cloud":
            return CursorCloudLLM(config)
        # 默认 local：本机 agent CLI 读盘；找不到二进制时在 generate 报错，不静默回落 Cloud
        return CursorLocalAgentLLM(config)
    raise ValueError(f"未知 llm.provider: {config.provider}")
