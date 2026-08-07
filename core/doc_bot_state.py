"""文档评论机器人的落盘状态：处理过哪些评论、哪些改动提案在等确认。

必须落盘：飞书的事件会重投，而重投的判据只有 comment_id/reply_id；进程重启后
内存里的去重表就没了，一条老评论能被回上好几遍——评论对全体协作者可见，刷屏
比不回更糟。提案同理，用户可能过一会儿才回「确认」。

不 import 任何飞书 SDK，纯文件 + 标准库，方便脱机跑单测。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

# 提案超过这个时间没被确认就作废：过了一天，文档大概率已经变了
PENDING_TTL_SECONDS = 24 * 3600
# 去重表只留最近这些条，免得文件无限长
MAX_SEEN = 2000


@dataclass
class EditProposal:
    """一条等待确认的改动。确认时要原样比对 old_text，对不上就不落笔。"""

    file_token: str = ""
    file_type: str = "docx"
    comment_id: str = ""
    block_id: str = ""
    old_text: str = ""
    new_text: str = ""
    # 追加到文末（找不到可改的块时的退路），此时 block_id 为空
    append: bool = False
    why: str = ""
    created_at: float = field(default_factory=time.time)

    def expired(self, now: Optional[float] = None, ttl: float = PENDING_TTL_SECONDS) -> bool:
        return (now or time.time()) - float(self.created_at or 0) > ttl


class DocBotState:
    """
    seen（已处理的评论/回复）+ pending（等确认的改动提案），带文件锁。

    每次操作都重新读盘：BloomBox、CLI、机器人可能同时在跑，内存缓存会互相覆盖。
    状态量很小（几 KB），读盘的代价远小于把评论回重的代价。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")

    # ---------- io ----------
    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            fh.close()

    def _read(self) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            # 状态文件损坏就当空的重来：这里存的是可再生的运行状态，不是用户数据
            return {"seen": [], "pending": {}}
        if not isinstance(data, dict):
            return {"seen": [], "pending": {}}
        data.setdefault("seen", [])
        data.setdefault("pending", {})
        return data

    def _write(self, data: Dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- seen ----------
    def check_and_mark(self, key: str) -> bool:
        """第一次见到这条评论/回复返回 True；重投返回 False。"""
        if not key:
            return True
        with self._lock():
            data = self._read()
            seen: List[str] = [str(x) for x in data.get("seen") or []]
            if key in seen:
                return False
            seen.append(key)
            data["seen"] = seen[-MAX_SEEN:]
            self._write(data)
        return True

    # ---------- pending ----------
    def put_pending(self, proposal: EditProposal) -> None:
        with self._lock():
            data = self._read()
            pending = data.get("pending") or {}
            pending[proposal.comment_id] = asdict(proposal)
            data["pending"] = self._drop_expired(pending)
            self._write(data)

    def take_pending(self, comment_id: str) -> Optional[EditProposal]:
        """取出并删除。执行只有一次机会，留着会被「确认」两遍。"""
        if not comment_id:
            return None
        with self._lock():
            data = self._read()
            pending = self._drop_expired(data.get("pending") or {})
            raw = pending.pop(comment_id, None)
            data["pending"] = pending
            self._write(data)
        if not raw:
            return None
        try:
            return EditProposal(**raw)
        except TypeError:
            # 字段改过版本，老提案直接作废好过按错的字段落笔
            return None

    def peek_pending(self, comment_id: str) -> Optional[EditProposal]:
        with self._lock():
            data = self._read()
            raw = (self._drop_expired(data.get("pending") or {})).get(comment_id)
        if not raw:
            return None
        try:
            return EditProposal(**raw)
        except TypeError:
            return None

    @staticmethod
    def _drop_expired(pending: Dict, now: Optional[float] = None) -> Dict:
        now = now or time.time()
        out = {}
        for key, raw in (pending or {}).items():
            created = float((raw or {}).get("created_at") or 0)
            if now - created <= PENDING_TTL_SECONDS:
                out[key] = raw
        return out


def default_state_path() -> Path:
    from .paths import default_persist_dir

    return Path(default_persist_dir()) / "doc_bot_state.json"
