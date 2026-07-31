"""配置加载与默认值。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional, Type, TypeVar

import yaml

T = TypeVar("T")

# UI / CLI 用的 Agent 模式名 → (llm.agent_mode, llm.agent_force)
# ask/plan 只读；agent=全工具（可写，force=true）
_AGENT_MODE_MAP = {
    "ask": ("ask", False),
    "plan": ("plan", False),
    "agent": ("", True),
    "full": ("", True),
}


def normalize_agent_ui_mode(value: str) -> str:
    """规范为 ask | plan | agent。"""
    v = (value or "").strip().lower()
    if v in {"", "full", "write", "yolo"}:
        return "agent"
    if v in {"ask", "readonly", "read"}:
        return "ask"
    if v in {"plan"}:
        return "plan"
    if v in {"agent"}:
        return "agent"
    raise ValueError("Agent 模式应为 ask（只读）| plan（规划）| agent（可写全工具）")


def agent_ui_mode_from_config(cfg: "LLMConfig") -> str:
    mode = (cfg.agent_mode or "").strip().lower()
    if mode in {"ask", "plan"}:
        return mode
    return "agent"


def apply_agent_ui_mode(cfg: "LLMConfig", ui_mode: str) -> str:
    """写入 LLMConfig，返回规范化后的 ask|plan|agent。"""
    ui = normalize_agent_ui_mode(ui_mode)
    agent_mode, agent_force = _AGENT_MODE_MAP[ui]
    cfg.agent_mode = agent_mode
    cfg.agent_force = agent_force
    return ui


def persist_llm_agent_settings(
    config_path: Optional[str],
    agent_mode: str,
    agent_force: bool,
) -> str:
    """
    把 agent_mode / agent_force 合并写入用户配置（Application Support），
    避免改仓库内 config.yaml。返回实际写入路径。
    """
    from .paths import app_support_dir

    path = app_support_dir() / "config.yaml"
    # 若当前已加载的就是用户配置，仍写同一路径
    if config_path:
        candidate = Path(config_path)
        if candidate.is_file() and "Application Support" in str(candidate):
            path = candidate

    raw: Dict[str, Any] = {}
    if path.is_file():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    llm = dict(raw.get("llm") or {})
    llm["agent_mode"] = agent_mode
    llm["agent_force"] = bool(agent_force)
    raw["llm"] = llm
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
    return str(path)


def _filter_kwargs(cls: Type[T], data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """只保留 dataclass 已声明字段，忽略未知键，避免用户配置多字段导致启动失败。"""
    if not data:
        return {}
    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in allowed}


@dataclass
class SensoryConfig:
    ttl: float = 3.0


@dataclass
class WorkingConfig:
    chunk_size: int = 7
    idle_clear_seconds: float = 600.0


@dataclass
class LongTermConfig:
    similarity_threshold: float = 0.70
    top_k: int = 3
    persist_dir: str = "data/memory"
    reinforce_boost: float = 0.05


@dataclass
class EmbeddingConfig:
    dim: int = 256


@dataclass
class LLMConfig:
    enabled: bool = True
    provider: str = "mock"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 600
    # cursor：工作目录；空则用进程 cwd（在项目根启动 CLI 即作用该项目）
    cwd: str = ""
    # cursor：local=本机 agent CLI 可读盘；cloud=Cloud 无仓库（看不到本机文件）
    runtime: str = "local"
    # cursor local：agent 可执行文件；空则 PATH 查找 agent / cursor-agent
    agent_bin: str = ""
    # cursor local：ask=只读 | plan=只读规划 | 空=全工具（建议配合 agent_force）
    agent_mode: str = "ask"
    # cursor local：是否传 --force（可写/跑命令，慎用）
    agent_force: bool = False


@dataclass
class SandboxConfig:
    default_scene: str = "general"


@dataclass
class AppConfig:
    sensory: SensoryConfig = field(default_factory=SensoryConfig)
    working: WorkingConfig = field(default_factory=WorkingConfig)
    long_term: LongTermConfig = field(default_factory=LongTermConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        return cls(
            sensory=SensoryConfig(**_filter_kwargs(SensoryConfig, data.get("sensory"))),
            working=WorkingConfig(**_filter_kwargs(WorkingConfig, data.get("working"))),
            long_term=LongTermConfig(**_filter_kwargs(LongTermConfig, data.get("long_term"))),
            embedding=EmbeddingConfig(**_filter_kwargs(EmbeddingConfig, data.get("embedding"))),
            llm=LLMConfig(**_filter_kwargs(LLMConfig, data.get("llm"))),
            sandbox=SandboxConfig(**_filter_kwargs(SandboxConfig, data.get("sandbox"))),
        )


def load_config(path: Optional[str] = None) -> AppConfig:
    """加载 YAML 配置；路径不存在时返回默认配置。"""
    if path is None:
        # 优先项目根目录 config.yaml
        candidates = [
            Path.cwd() / "config.yaml",
            Path(__file__).resolve().parent.parent / "config.yaml",
        ]
        for p in candidates:
            if p.is_file():
                path = str(p)
                break
    if not path or not Path(path).is_file():
        return AppConfig()

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.from_dict(raw)
