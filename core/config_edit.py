"""在界面里看/改生效的那份 config.yaml。

两条硬要求决定了这里的写法：

1. **配置里有密钥**（app_secret、user_access_token、llm.api_key）。界面上直接摊开
   一屏密钥不合适，所以读的时候按键名把值换成 `********`；保存时再把没被动过的
   掩码换回原值。用户想改密钥就把掩码整个替换成新值，照样能改。
2. **不能把文件改坏**。这份文件同时存着飞书 token，写坏了要重新授权。所以走
   文本级替换而不是 YAML 反序列化再 dump（那会吃掉注释），保存前先解析 + 用
   load_config 试载一遍，再原子替换，并留一份备份。
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from .paths import default_config_path

MASK = "********"
# 按键名判断哪些值要遮：宁可多遮一个，也不要把 token 摊在界面上
SECRET_KEY_RE = re.compile(r"(secret|token|api_key|apikey|password|passwd|credential)", re.I)
# 但键名里带 token 的不一定是密钥：user_token_expires_at 是个时间戳，
# 遮起来只会让人看不到「什么时候要重新授权」
_NOT_SECRET_RE = re.compile(r"(_expires_at|_at|_path|_dir|_file|_url|_enabled|_ttl)$", re.I)
# 空值不遮：遮了反而看不出「这里还没填」，而且用户想填的时候得先删掉掩码
_EMPTY_VALUES = {"", "''", '""', "null", "~", "{}", "[]"}

_TOP_KEY_RE = re.compile(r"^([A-Za-z_][\w.-]*):\s*(.*)$")
_SUB_KEY_RE = re.compile(r"^(\s+)([A-Za-z_][\w.-]*):\s*(.*)$")


@dataclass
class ConfigView:
    path: str = ""
    text: str = ""
    exists: bool = False
    masked: List[str] = field(default_factory=list)
    mask: str = MASK
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SaveResult:
    ok: bool = False
    message: str = ""
    path: str = ""
    backup: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def config_path() -> Path:
    return Path(default_config_path())


def _strip_comment(value: str) -> Tuple[str, str]:
    """
    切出「值」和「行尾注释」。

    只认前面有空白的 `#`：`model: "a#b"` 里的井号是值的一部分，不是注释。
    """
    m = re.search(r"\s+#", value)
    if not m:
        return value.strip(), ""
    return value[: m.start()].strip(), value[m.start() :]


def _is_secret(key: str, value: str) -> bool:
    if not SECRET_KEY_RE.search(key) or _NOT_SECRET_RE.search(key):
        return False
    if value in _EMPTY_VALUES:
        return False
    # 数字和布尔当不了密钥：过期时间戳、开关之类的照常显示
    if value.lower() in ("true", "false"):
        return False
    try:
        float(value)
    except ValueError:
        return True
    return False


def _walk(text: str):
    """逐行产出 (行号, 缩进, 完整键路径, 值, 行尾注释)；不是键值行的不产出。"""
    section = ""
    for i, line in enumerate(text.splitlines()):
        top = _TOP_KEY_RE.match(line)
        if top:
            section = top.group(1)
            value, comment = _strip_comment(top.group(2))
            yield i, "", section, value, comment
            continue
        sub = _SUB_KEY_RE.match(line)
        if sub:
            indent, key, rest = sub.groups()
            value, comment = _strip_comment(rest)
            path = f"{section}.{key}" if section else key
            yield i, indent, path, value, comment


def mask_secrets(text: str) -> Tuple[str, List[str]]:
    """把密钥类字段的值换成掩码，返回 (文本, 被遮的键路径)。"""
    lines = text.splitlines()
    masked: List[str] = []
    for idx, indent, path, value, comment in _walk(text):
        key = path.rsplit(".", 1)[-1]
        if not _is_secret(key, value):
            continue
        # 掩码要加引号：YAML 里 * 开头是别名，裸的 ******** 会让整份配置解析失败，
        # 编辑器里也就变成一屏红字
        lines[idx] = f"{indent}{key}: '{MASK}'{comment}"
        masked.append(path)
    out = "\n".join(lines)
    if text.endswith("\n"):
        out += "\n"
    return out, masked


def _secret_values(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for _idx, _indent, path, value, _comment in _walk(text):
        key = path.rsplit(".", 1)[-1]
        if _is_secret(key, value):
            out[path] = value
    return out


def restore_secrets(new_text: str, current_text: str) -> Tuple[str, List[str]]:
    """
    把用户没动过的掩码换回真值。

    返回 (文本, 还原不了的键)。还原不了说明用户在一个原本没有值的字段上写了掩码，
    那只能报错——真按字面量存下去，等于用一串星号当密钥。
    """
    known = _secret_values(current_text)
    lines = new_text.splitlines()
    unresolved: List[str] = []
    for idx, indent, path, value, comment in _walk(new_text):
        if value.strip("'\"") != MASK:
            continue
        key = path.rsplit(".", 1)[-1]
        original = known.get(path)
        if original is None:
            unresolved.append(path)
            continue
        lines[idx] = f"{indent}{key}: {original}{comment}"
    out = "\n".join(lines)
    if new_text.endswith("\n"):
        out += "\n"
    return out, unresolved


def validate(text: str) -> str:
    """能不能当配置用？返回空串表示可以，否则是给人看的错误说明。"""
    try:
        import yaml
    except ImportError:  # pragma: no cover - 依赖缺失时不拦着保存
        return ""
    try:
        data = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001 - yaml 的异常类型很杂，一律当格式错
        return f"YAML 格式有误：{e}"
    if data is None:
        return "配置是空的，至少要有一个顶层小节"
    if not isinstance(data, dict):
        return "配置最外层必须是键值对"

    # 再用真正的加载器试一次，能挡住「某个小节写成了字符串/列表」这类结构错。
    # 挡不住标量类型写错（load_config 不校验类型，top_k 写成中文也照收），
    # 那种只能等用到时才暴露
    tmp = None
    try:
        from .config import load_config

        fd, tmp = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        load_config(tmp)
    except Exception as e:  # noqa: BLE001
        return f"配置读不出来：{e}"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return ""


def read_view() -> ConfigView:
    path = config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConfigView(path=str(path), text="", exists=False)
    except OSError as e:
        return ConfigView(path=str(path), exists=False, error=f"读不了配置文件：{e}")
    masked_text, masked = mask_secrets(text)
    return ConfigView(path=str(path), text=masked_text, exists=True, masked=masked)


def save(new_text: str) -> SaveResult:
    path = config_path()
    if not (new_text or "").strip():
        return SaveResult(False, "", str(path), error="内容是空的，没保存")

    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    except OSError as e:
        return SaveResult(False, "", str(path), error=f"读不了原配置：{e}")

    text, unresolved = restore_secrets(new_text, current)
    if unresolved:
        return SaveResult(
            False,
            "",
            str(path),
            error=(
                "这些字段原本没有值，却写着掩码 "
                + MASK
                + "：" + "、".join(unresolved)
                + "。请填真实值或留空。"
            ),
        )

    err = validate(text)
    if err:
        return SaveResult(False, "", str(path), error=err)
    if text == current:
        return SaveResult(True, "内容没有变化，未写入。", str(path))

    backup = ""
    if current:
        backup = str(path) + ".bak-edit"
        try:
            Path(backup).write_text(current, encoding="utf-8")
        except OSError:
            backup = ""

    # 原子替换：这份文件里存着飞书 token，写一半崩了就得重新授权
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        return SaveResult(False, "", str(path), error=f"写入失败：{e}")

    msg = "配置已保存。进程启动时才读配置，BloomBox 与飞书机器人重启后生效。"
    if backup:
        msg += f"\n改动前那份备份在 {backup}"
    return SaveResult(True, msg, str(path), backup=backup)
