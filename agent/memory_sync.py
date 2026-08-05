"""记忆跨会话同步：全局 revision / 变更日志 / 补丁与快照消息（TASK-004）。

本模块是「快照 + 版本补丁」机制的数据与格式层，不依赖模型、不引入新工具：

- ``MemoryChangeLog``：全局记忆变更日志（``workspace/memory/changelog.jsonl``）
  的唯一事实源。每写一次 USER.md / MEMORY.md 递增一个全局 revision；
  daily/ 永不记录（也就不可能触发补丁）。
- ``build_patch_message``：把落后期间的变更条目渲染成一条
  ``<memory_patch revision="N">`` 的 system 消息。diff 超限（>20 行）时只给
  「大改，最新内容见对应文件，可 read_file 查看全文」提示，不逐行列明细。
- ``build_snapshot_message``：累积补丁过多时重建的完整记忆快照
  ``<memory_snapshot revision="N">``，用于替换历史里累积的旧补丁。
- ``diff_lines`` / ``estimate_text_tokens``：行级 diff 与极简 token 估算。

角色统一用 ``system``（DeepSeek 不支持 developer，已核实；OpenAI 兼容 API
允许多条 system 消息，项目已有压缩摘要先例）。

revision 是系统内部字段，补丁文本不教模型解析数字——模型只需知道
「这是记忆更新，新信息覆盖旧快照」。
"""

import difflib
import json
import os
import re
from datetime import datetime

# 单条变更 diff 超过该行数时，补丁只给「大改」提示而不逐行列明细
BIG_CHANGE_LINES = 20

# 粗略 token 估算用：匹配 CJK 表意文字及中文常用标点/全角符号
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def diff_lines(old_text: str, new_text: str) -> tuple[list[str], list[str]]:
    """行级 diff，返回 ``(added_lines, removed_lines)``。

    用 difflib（stdlib）计算；``autojunk=False`` 保证重复行较多的记忆文件
    也能得到可读的 diff。新增行/删除行均保持出现顺序。
    """
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    added: list[str] = []
    removed: list[str] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new_lines[j1:j2])
        if tag in ("delete", "replace"):
            removed.extend(old_lines[i1:i2])
    return added, removed


def estimate_text_tokens(text: str) -> int:
    """极简 token 估算（与 agent.memory 同款启发式，非精确 tokenizer）。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    non_cjk = len(text) - cjk
    return int(cjk * 1.5 + non_cjk * 0.25) + 1


class MemoryChangeLog:
    """``workspace/memory/changelog.jsonl`` 的读写：全局记忆 revision 事实源。

    每行一条：``{revision, file, operation, added_lines, removed_lines, timestamp}``。
    当前全局 revision = 日志最后一条的 revision；文件不存在视为 0。
    只同步 USER.md / MEMORY.md 两个文件；daily/ 永不写入本日志。
    """

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)
        self.path = os.path.join(
            self.workspace, "workspace", "memory", "changelog.jsonl"
        )

    def read_all(self) -> list[dict]:
        """读取全部变更条目（按 revision 升序）。单行损坏跳过，不阻塞。"""
        if not os.path.exists(self.path):
            return []
        entries: list[dict] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def current_revision(self) -> int:
        """当前全局 revision；日志为空时为 0。"""
        entries = self.read_all()
        return int(entries[-1].get("revision") or 0) if entries else 0

    def append(
        self,
        file: str,
        operation: str,
        added_lines: list[str],
        removed_lines: list[str],
    ) -> int:
        """追加一条变更日志并返回新 revision（= 当前 + 1）。"""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        revision = self.current_revision() + 1
        entry = {
            "revision": revision,
            "file": file,
            "operation": operation,
            "added_lines": list(added_lines or []),
            "removed_lines": list(removed_lines or []),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return revision

    def entries_after(self, after_revision: int) -> list[dict]:
        """返回 revision 严格大于 ``after_revision`` 的变更条目（升序）。"""
        return [
            entry for entry in self.read_all()
            if int(entry.get("revision") or 0) > after_revision
        ]


def is_patch_message(msg: dict) -> bool:
    """判断一条消息是否为系统生成的记忆补丁。"""
    content = (msg or {}).get("content") or ""
    return content.startswith("<memory_patch")


def is_snapshot_message(msg: dict) -> bool:
    """判断一条消息是否为重建后的完整记忆快照。"""
    content = (msg or {}).get("content") or ""
    return content.startswith("<memory_snapshot")


def _entry_diff_lines(entry: dict) -> int:
    return len(entry.get("added_lines") or []) + len(entry.get("removed_lines") or [])


def _fmt_diff_line(line: str) -> str:
    """渲染 diff 行：避免记忆文件内容本身以 '- ' 开头时出现 '- - xxx' 双连字符。"""
    text = str(line)
    return text if text.startswith("- ") else f"- {text}"


def build_patch_message(entries: list[dict]) -> dict:
    """把落后期间的变更条目渲染成一条 ``<memory_patch>`` system 消息。

    - 一条补丁可覆盖多个文件/多次变更；revision 属性取覆盖的最新 revision。
    - 单条变更 diff 超过 ``BIG_CHANGE_LINES`` 行时，只给「大改」提示，
      不逐行列明细（省 token，让模型按需 read_file 看全文）。
    - 不调用模型：内容全部来自 changelog 的行级 diff。
    """
    latest_revision = int(entries[-1]["revision"])
    parts = [f'<memory_patch revision="{latest_revision}">']
    for entry in entries:
        rel = entry.get("file") or ""
        added = entry.get("added_lines") or []
        removed = entry.get("removed_lines") or []
        if not added and not removed:
            continue  # 空 diff 条目（防御性）：不生成内容
        parts.append(f"文件：{rel}")
        if _entry_diff_lines(entry) > BIG_CHANGE_LINES:
            parts.append(
                f"变更：大改（本次变更超过 {BIG_CHANGE_LINES} 行），"
                f"最新内容见 {rel}，可 read_file 查看全文。"
            )
        else:
            if removed:
                parts.append("变更内容（删除）：")
                parts.extend(_fmt_diff_line(line) for line in removed)
            if added:
                parts.append("变更内容（新增）：")
                parts.extend(_fmt_diff_line(line) for line in added)
        parts.append("")
    while parts and parts[-1] == "":
        parts.pop()
    parts.append("该内容覆盖旧记忆中的冲突信息。")
    parts.append("这是认知更新，无需将补丁内容回写记忆文件（文件内容由写入方维护）。")
    parts.append("</memory_patch>")
    return {"role": "system", "content": "\n".join(parts)}


def estimate_patch_tokens(entries: list[dict]) -> int:
    """估算一条补丁消息的 token 开销（用于「补丁总量超阈值→重建快照」判断）。"""
    return estimate_text_tokens(build_patch_message(entries)["content"])


def build_snapshot_message(
    user_text: str, memory_text: str, revision: int
) -> dict:
    """构建一条新的完整记忆快照 system 消息（重建快照时替换旧补丁们）。

    快照是「旧快照 + 所有补丁」等价物：直接从当前文件全文生成，内容完整、
    与最新全局 revision 对齐，且无需调用模型。
    """
    content = (
        f'<memory_snapshot revision="{revision}">\n'
        "以下为记忆文件的最新完整内容，覆盖本提示词【用户信息】与"
        "【长期记忆】中的旧快照：\n"
        "\n【USER.md】\n" + (user_text or "（空）") +
        "\n\n【MEMORY.md】\n" + (memory_text or "（空）") +
        "\n</memory_snapshot>"
    )
    return {"role": "system", "content": content}


def read_memory_files(workspace: str) -> tuple[str, str]:
    """读取 USER.md / MEMORY.md 全文（用于重建快照）；读失败返回空串。"""
    memory_dir = os.path.join(os.path.abspath(workspace), "workspace", "memory")

    def _read(name: str) -> str:
        path = os.path.join(memory_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    return _read("USER.md"), _read("MEMORY.md")
