"""配置加载与默认值。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional, Type, TypeVar

import yaml

T = TypeVar("T")


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
    timeout: int = 60
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
