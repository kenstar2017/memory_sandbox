"""工作记忆层：滑动窗口上下文，本地匹配与轻量推理。"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .rules import RuleEngine
from .utils import cosine_similarity, extract_keywords, keyword_overlap

# 失败/鉴权类答复：不可复用（删长时后仍会卡在工作记忆）
_NON_REUSABLE_ANS = re.compile(
    r"(?:"
    r"^\[(?:MockLLM|LLM Error)\]"
    r"|读不到该文档"
    r"|Invalid access token"
    r"|99991668|99991672"
    r"|Access denied\..*wiki"
    r"|飞书文档：.*失败"
    r"|未配置飞书"
    r"|tenant_access_token:.*失败"
    r"|user_access_token:.*失败"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def is_non_reusable_answer(answer: str) -> bool:
    text = (answer or "").strip()
    if not text:
        return True
    return bool(_NON_REUSABLE_ANS.search(text))


class WorkingMemory:
    """
    模拟人类短时工作台：
    - 固定容量滑动窗口（7±2）
    - 本地规则 / 上下文推理
    - 空闲超时清空
    """

    def __init__(
        self,
        chunk_size: int = 7,
        idle_clear_seconds: float = 600.0,
        rule_engine: Optional[RuleEngine] = None,
    ):
        self.max_size = chunk_size
        self.idle_clear_seconds = idle_clear_seconds
        self.window: List[Dict[str, Any]] = []
        self.rule_engine = rule_engine or RuleEngine()
        self._last_active = time.time()
        self.scene: str = "general"
        # 临时推理状态（如连续计算的当前值）
        self.scratch: Dict[str, Any] = {}

    def touch(self) -> None:
        self._last_active = time.time()

    def _maybe_idle_clear(self) -> None:
        if time.time() - self._last_active > self.idle_clear_seconds:
            self.window.clear()
            self.scratch.clear()

    def add_context(
        self,
        text: str,
        keywords: Optional[List[str]] = None,
        vec: Optional[List[float]] = None,
        role: str = "user",
        meta: Optional[dict] = None,
    ) -> None:
        self._maybe_idle_clear()
        self.touch()
        item = {
            "text": text,
            "kw": keywords or extract_keywords(text),
            "vec": vec,
            "role": role,
            "meta": meta or {},
            "ts": time.time(),
        }
        self.window.append(item)
        if len(self.window) > self.max_size:
            self.window.pop(0)

    def local_match(self, user_input: str, user_vec: Optional[List[float]] = None) -> Optional[str]:
        """
        本地匹配流程：
        1. 规则引擎（计算/FAQ/短句）
        2. 上下文关联（追问、延续）
        无答案返回 None
        """
        self._maybe_idle_clear()
        self.touch()

        rule_ans = self.rule_engine.match(
            user_input,
            context=self.window,
            scratch=self.scratch,
        )
        if rule_ans:
            if re_fullmatch_number(rule_ans):
                self.scratch["last_number"] = float(rule_ans)
            return rule_ans

        ctx_ans = self.context_reason(user_input, user_vec)
        if ctx_ans:
            return ctx_ans
        return None

    def context_reason(
        self,
        user_input: str,
        user_vec: Optional[List[float]] = None,
    ) -> Optional[str]:
        """上下文关联：重复问题、高度相似的上一轮问答直接复用。"""
        if not self.window:
            return None

        # 「刚才说了什么 / 上一句是什么」——先于复用逻辑
        normalized = user_input.strip().rstrip("？?")
        if normalized in {"刚才说了什么", "上一句是什么", "刚才问了什么", "上下文"}:
            if not self.window:
                return "工作记忆为空。"
            lines = []
            for item in self.window[-4:]:
                role = "用户" if item.get("role") == "user" else "助手"
                lines.append(f"{role}: {item.get('text')}")
            return "\n".join(lines)

        # 飞书链接：凭证/正文可能变化，不复用工作记忆旧答（否则删长时后仍假命中）
        try:
            from .feishu import extract_feishu_urls

            if extract_feishu_urls(user_input):
                return None
        except Exception:
            pass

        # 重复提问：与历史 user 高度重合
        user_kw = extract_keywords(user_input)
        best_score = 0.0
        best_answer = None
        pending_user = None

        for item in self.window:
            if item.get("role") == "user":
                pending_user = item
                continue
            if item.get("role") != "assistant" or pending_user is None:
                continue

            ans = str(item.get("text") or "")
            if is_non_reusable_answer(ans):
                continue

            score = keyword_overlap(user_kw, pending_user.get("kw") or [])
            if user_vec and pending_user.get("vec"):
                score = max(score, cosine_similarity(user_vec, pending_user["vec"]))

            # 完全相同文本
            if pending_user.get("text", "").strip() == user_input.strip():
                score = 1.0

            if score > best_score:
                best_score = score
                best_answer = ans

        # 工作记忆内高相似重复问
        if best_score >= 0.92 and best_answer:
            return best_answer

        return None

    def get_all_vectors(self) -> List[List[float]]:
        return [item["vec"] for item in self.window if item.get("vec")]

    def recent_context_text(self, n: int = 4) -> str:
        parts = []
        for item in self.window[-n:]:
            role = item.get("role", "user")
            parts.append(f"{role}: {item.get('text', '')}")
        return "\n".join(parts)

    def clear(self) -> None:
        self.window.clear()
        self.scratch.clear()
        self.touch()

    def forget(self, keyword: Optional[str] = None) -> int:
        if keyword is None:
            n = len(self.window)
            self.clear()
            return n
        needle = keyword.strip().lower()
        before = len(self.window)
        self.window = [
            item for item in self.window
            if needle not in str(item.get("text", "")).lower()
        ]
        self.touch()
        return before - len(self.window)

    def set_scene(self, scene: str) -> None:
        self.scene = scene.strip() or "general"
        self.touch()

    def stats(self) -> dict:
        self._maybe_idle_clear()
        return {
            "size": len(self.window),
            "max_size": self.max_size,
            "scene": self.scene,
            "scratch_keys": list(self.scratch.keys()),
        }


def re_fullmatch_number(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", (s or "").strip()))
