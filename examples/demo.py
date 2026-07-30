#!/usr/bin/env python3
"""演示三级记忆链路：规则命中 / 长时命中 / 大模型回退 / 记忆巩固。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.config import AppConfig, LLMConfig, LongTermConfig


def run():
    # 使用临时目录，避免污染正式记忆库
    cfg = AppConfig(
        long_term=LongTermConfig(
            persist_dir=str(ROOT / "data" / "demo_memory"),
            similarity_threshold=0.65,
            top_k=3,
        ),
        llm=LLMConfig(enabled=True, provider="mock"),
    )
    sb = MemorySandbox(config=cfg)
    sb.working.set_scene("demo")

    cases = [
        ("", "感觉记忆拦截空输入"),
        ("1+1", "工作记忆规则：算术"),
        ("再加2", "工作记忆上下文延续计算"),
        ("记住：本地 mock 端口 => 默认 3001，改 hosts 后重启代理", "主动写入长时记忆"),
        ("本地 mock 端口是多少", "长时记忆命中"),
        ("本地 mock 端口", "长时记忆近似命中"),
        ("量子引力和咖啡有什么关系", "沙箱无解 → MockLLM"),
        ("查看记忆状态", "指令：状态"),
        ("忘记刚才内容", "指令：遗忘工作记忆"),
    ]

    print("=" * 60)
    print("记忆沙箱 Demo")
    print("=" * 60)
    for text, title in cases:
        print(f"\n--- {title} ---")
        print(f"你> {text!r}")
        result = sb.chat(text)
        print(f"沙箱[{result.source}]> {result.answer}")

    print("\n完成。演示数据目录:", cfg.long_term.persist_dir)


if __name__ == "__main__":
    run()
