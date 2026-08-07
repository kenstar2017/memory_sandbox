"""工作记忆内置规则引擎：四则运算、固定问答、指令类、短句应答。"""

from __future__ import annotations

import ast
import operator
import re
from typing import Callable, Dict, List, Optional, Tuple


_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval_arith(expr: str) -> Optional[float]:
    """安全求值简单算术表达式。"""
    try:
        node = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def _eval(n):
        if isinstance(n, ast.Expression):
            return _eval(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        # py3.9 兼容
        if hasattr(ast, "Num") and isinstance(n, ast.Num):
            return n.n
        if isinstance(n, ast.BinOp) and type(n.op) in _SAFE_OPS:
            return _SAFE_OPS[type(n.op)](_eval(n.left), _eval(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _SAFE_OPS:
            return _SAFE_OPS[type(n.op)](_eval(n.operand))
        raise ValueError("unsafe")

    try:
        return float(_eval(node))
    except Exception:
        return None


class RuleEngine:
    """轻量规则引擎：命中则直接返回答案，无需大模型。"""

    def __init__(self):
        self._faq: Dict[str, str] = {
            "你好": "你好！我是记忆沙箱助手，优先用本地记忆回答。",
            "hello": "Hello! Memory sandbox is ready.",
            "你是谁": "我是本地记忆沙箱（Sensory / Working / Long-Term），优先检索记忆，缺失时才调用大模型。",
            "帮助": (
                "可用指令：\n"
                "- 写记忆说人话就行：记一下 <内容> / 把 <内容> 存到记忆库 / <内容>，记下来\n"
                "  想分开问答就写 记住：<问> => <答>；只说「记一下这个」则记上一轮对话\n"
                "- 忘记刚才内容 / 忘记：<关键词>\n"
                "- 删除记忆：<问题或id>\n"
                "- 清空工作记忆\n"
                "- 清空长时记忆 / 清空全部记忆（需再发「确认清空…」；可「确认…并备份」）\n"
                "- 备份长时记忆（连同知识库）/ 查看长时备份 / 恢复长时记忆备份\n"
                "- 清理低访问记忆 [天数，默认30]\n"
                "- 优化已有记忆（给旧条目补别名/刷新向量）\n"
                "- 查看短时记忆 / 查看长时记忆 / 查看全部记忆\n"
                "- 查看记忆状态\n"
                "- 飞书登录（浏览器 OAuth 获取 user_access_token，管理后台无明文）\n"
                "- 切换场景：<场景名>"
            ),
            "help": "Commands: remember / forget / backup long-term / confirm clear / list / status.",
        }
        self._custom_handlers: List[Tuple[re.Pattern, Callable[[re.Match, str], Optional[str]]]] = []

    def add_faq(self, question: str, answer: str) -> None:
        self._faq[question.strip().lower()] = answer

    def match(
        self,
        user_input: str,
        context: Optional[List[dict]] = None,
        scratch: Optional[dict] = None,
    ) -> Optional[str]:
        text = user_input.strip()
        if not text:
            return None

        # 1) 固定 FAQ
        key = text.lower().rstrip("？?!.。！")
        if key in self._faq:
            return self._faq[key]

        # 2) 算术
        arith = self._try_arith(text)
        if arith is not None:
            return arith

        # 3) 上下文延续计算：再加2 / 再乘3
        ctx = self._try_context_math(text, context or [], scratch or {})
        if ctx is not None:
            return ctx

        # 4) 自定义 handler
        for pattern, handler in self._custom_handlers:
            m = pattern.search(text)
            if m:
                ans = handler(m, text)
                if ans is not None:
                    return ans

        # 5) 短句应答
        short = self._short_reply(text)
        if short is not None:
            return short

        return None

    def _try_arith(self, text: str) -> Optional[str]:
        # 匹配：计算 1+2*3 / 1+1等于多少 / 纯表达式
        patterns = [
            r"^(?:计算|算一下|算下|求)\s*(.+)$",
            r"^(.+?)(?:等于多少|是多少|等于几|\s*=\s*\??)\s*$",
            r"^([0-9\.\s\+\-\*\/\%\(\)]+)$",
        ]
        for p in patterns:
            m = re.match(p, text, re.IGNORECASE)
            if not m:
                continue
            expr = m.group(1).strip()
            expr = expr.replace("×", "*").replace("÷", "/").replace("ｘ", "*")
            expr = re.sub(r"[^0-9\.\+\-\*\/\%\(\)\s]", "", expr)
            if not re.search(r"\d", expr):
                continue
            val = _safe_eval_arith(expr)
            if val is not None:
                if abs(val - int(val)) < 1e-9:
                    return str(int(val))
                return str(round(val, 6))
        return None

    def _try_context_math(
        self,
        text: str,
        context: List[dict],
        scratch: Optional[dict] = None,
    ) -> Optional[str]:
        m = re.match(r"^(?:再|然后)?\s*(加|减|乘|除以|除)\s*(-?\d+(?:\.\d+)?)\s*$", text)
        if not m:
            return None
        op_word, num_s = m.group(1), m.group(2)
        num = float(num_s)

        # 优先工作记忆 scratch，其次上下文中的数字结果
        last_num = None
        if scratch and "last_number" in scratch:
            try:
                last_num = float(scratch["last_number"])
            except (TypeError, ValueError):
                last_num = None

        if last_num is None:
            for item in reversed(context):
                t = str(item.get("text", ""))
                if item.get("role") == "assistant":
                    mm = re.fullmatch(r"-?\d+(?:\.\d+)?", t.strip())
                    if mm:
                        last_num = float(mm.group(0))
                        break
                nums = re.findall(r"-?\d+(?:\.\d+)?", t)
                if nums and ("+" in t or "-" in t or "*" in t or "/" in t or "加" in t):
                    last_num = float(nums[-1])
                    break
        if last_num is None:
            return None

        ops = {
            "加": last_num + num,
            "减": last_num - num,
            "乘": last_num * num,
            "除": last_num / num if num != 0 else None,
            "除以": last_num / num if num != 0 else None,
        }
        result = ops.get(op_word)
        if result is None:
            return "除数不能为 0"
        if abs(result - int(result)) < 1e-9:
            return str(int(result))
        return str(round(result, 6))

    def _short_reply(self, text: str) -> Optional[str]:
        if text in {"谢谢", "感谢", "thanks", "thank you"}:
            return "不客气。"
        if text in {"再见", "拜拜", "bye"}:
            return "再见，记忆已保留到长时库。"
        if re.fullmatch(r"[哈嘿呵]+", text):
            return "哈哈。"
        return None
