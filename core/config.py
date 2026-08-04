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


def _user_config_path(config_path: Optional[str] = None) -> Path:
    """优先写 Application Support 用户配置，避免改仓库内 config.yaml。"""
    from .paths import app_support_dir

    path = app_support_dir() / "config.yaml"
    if config_path:
        candidate = Path(config_path)
        if candidate.is_file() and "Application Support" in str(candidate):
            path = candidate
    return path


def persist_llm_agent_settings(
    config_path: Optional[str],
    agent_mode: str,
    agent_force: bool,
) -> str:
    """
    把 agent_mode / agent_force 合并写入用户配置（Application Support），
    避免改仓库内 config.yaml。返回实际写入路径。
    """
    path = _user_config_path(config_path)
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


# Web/CLI 可调的长时检索字段（含说明文案）
RETRIEVAL_SETTING_SPECS = [
    {
        "key": "similarity_threshold",
        "label": "命中阈值",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "step": 0.01,
        "help": "综合分超过此值才算「命中」并直接复用答案。越高越严格，越低越容易命中旧记忆。",
    },
    {
        "key": "top_k",
        "label": "召回条数",
        "type": "int",
        "min": 1,
        "max": 20,
        "step": 1,
        "help": "每次检索最多返回几条候选。一般 3 即可；排查时可临时调高。",
    },
    {
        "key": "bm25_enabled",
        "label": "启用 BM25",
        "type": "bool",
        "help": "打开后，检索会同时看「语义向量 + 关键词重叠 + BM25 文本相关度」。关闭则只用向量和关键词。",
    },
    {
        "key": "vector_weight",
        "label": "语义向量权重",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "help": "看意思是否相近（本地哈希向量）。口语换说法、近义表达时更有用。建议与关键词、BM25 权重之和约为 1。",
    },
    {
        "key": "keyword_weight",
        "label": "关键词权重",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "help": "看分词/关键词是否对得上。专有名词、命令、路径等精确词较依赖这项。",
    },
    {
        "key": "bm25_weight",
        "label": "BM25 权重",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "help": "经典全文检索打分，对问句里的实词匹配更敏感。适合文档标题、术语较多的记忆。",
    },
    {
        "key": "aging_enabled",
        "label": "启用陈旧降权",
        "type": "bool",
        "help": "很久没命中的记忆，检索时分数会略降，避免老结论抢在新知识前面。",
    },
    {
        "key": "aging_days",
        "label": "陈旧天数",
        "type": "float",
        "min": 1.0,
        "max": 3650.0,
        "step": 1.0,
        "help": "超过这么多天未命中，开始按「陈旧」降权；归档陈旧记忆也参考这个天数。",
    },
    {
        "key": "aging_decay",
        "label": "陈旧降权幅度",
        "type": "float",
        "min": 0.0,
        "max": 0.8,
        "step": 0.01,
        "help": "最旧记忆最多再扣掉的分数比例。例如 0.15 ≈ 最多扣 15%。",
    },
    {
        "key": "reinforce_boost",
        "label": "重复命中加成",
        "type": "float",
        "min": 0.0,
        "max": 0.5,
        "step": 0.01,
        "help": "同一条记忆被反复命中时，略微提高它的排序权重，让常用结论更稳。",
    },
]


def persist_long_term_settings(
    config_path: Optional[str],
    updates: Dict[str, Any],
) -> str:
    """把 long_term 检索相关字段合并写入用户配置，返回路径。"""
    allowed = {s["key"] for s in RETRIEVAL_SETTING_SPECS}
    path = _user_config_path(config_path)
    raw: Dict[str, Any] = {}
    if path.is_file():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    lt = dict(raw.get("long_term") or {})
    for k, v in (updates or {}).items():
        if k in allowed:
            lt[k] = v
    raw["long_term"] = lt
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
    # 混合检索权重（vector + keyword + bm25，建议和为 1）
    bm25_enabled: bool = True
    vector_weight: float = 0.55
    keyword_weight: float = 0.20
    bm25_weight: float = 0.25
    # 很久未命中时检索降权；归档命令用同一天数阈值
    aging_enabled: bool = True
    aging_days: float = 90.0
    aging_min_hits: int = 0
    aging_decay: float = 0.15  # 最高再扣 15% 分数


@dataclass
class EmbeddingConfig:
    dim: int = 256  # 改维度后需 reoptimize 刷新向量


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
    # cursor local：是否传 --approve-mcps（批准 Cursor mcp.json；飞书读文档请用下方 feishu 配置）
    approve_mcps: bool = False


@dataclass
class FeishuConfig:
    """记忆沙箱内置飞书读文档（OpenAPI，不依赖 Cursor/Trae MCP）。"""

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    # 由 OAuth 动态获取，勿指望在飞书管理后台查看明文
    user_access_token: str = ""
    refresh_token: str = ""
    user_token_expires_at: int = 0
    # OAuth 回调，需与开放平台「重定向 URL」一致
    redirect_uri: str = "http://127.0.0.1:18765/feishu/callback"
    oauth_scope: str = (
        "offline_access docs:document.content:read wiki:wiki:readonly "
        "wiki:node:read wiki:node:update docx:document:create "
        "docx:document:readonly docx:document:write_only"
    )
    # 文档域名（如 bytedance.larkoffice.com），用于把 document_id 拼成可点链接；
    # api_base 是 open.feishu.cn，推不出企业实际域名，所以单独配
    doc_host: str = ""
    # 开放平台 API 根；国内一般 https://open.feishu.cn
    api_base: str = "https://open.feishu.cn"
    timeout: float = 30.0
    # 注入 LLM 前的正文最大字符数
    max_chars: int = 80000


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
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        return cls(
            sensory=SensoryConfig(**_filter_kwargs(SensoryConfig, data.get("sensory"))),
            working=WorkingConfig(**_filter_kwargs(WorkingConfig, data.get("working"))),
            long_term=LongTermConfig(**_filter_kwargs(LongTermConfig, data.get("long_term"))),
            embedding=EmbeddingConfig(**_filter_kwargs(EmbeddingConfig, data.get("embedding"))),
            llm=LLMConfig(**_filter_kwargs(LLMConfig, data.get("llm"))),
            feishu=FeishuConfig(**_filter_kwargs(FeishuConfig, data.get("feishu"))),
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
