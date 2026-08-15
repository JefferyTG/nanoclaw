"""DSH 会话编排工具：调用本机 DeepSeek Harness（dsh）的 Agent 执行编码任务。

DshSessionTool 封装 DeepSeek Harness web profile 的 HTTP RPC API
（默认 http://127.0.0.1:3080/api），让宿主 Agent 像"项目经理"一样与 DSH 的
编码 Agent 多轮对话：

- action=list    列出本机 DSH 会话（按项目 cwd 优先排序），找到"上次干到哪"
- action=prompt  给 DSH Agent 发一条消息（不传 session_id 时自动新建会话，
                  cwd=项目工作区）；同一 session_id 反复调用即多轮对话，
                  DSH 侧会话持久、记得上下文
- action=read    增量读取 DSH 的回复：传 before_seq 只返回 seq 大于该值的
                  新内容（工具无状态，增量游标由调用方持有）
- action=cancel  打断 DSH 当前回合（方向错了时兜底）

协议事实（2026-08-14 对运行中的 DSH web 实测）：
- 信封：POST /api/<method>，body {"type":"client-request","rpcId":"<uuid>",
  "method":"...","payload":{...}}；响应 result.ok / result.value / result.error
- session.history 返回 value.events[]，assistant/message 文本位于
  event.data.message.content[].text，回合结束有 turn/end 事件，事件带单调 seq

安全边界：DSH web 默认绑定 127.0.0.1 且无认证（本机专用）；DSH Agent 运行在
workspace-write 沙箱 + 审批 fail-closed（无应答者时需审批的操作会被拒绝），
因此派活应限定在项目目录内。
"""

import json
import os
from typing import Optional
from urllib.parse import urljoin

import httpx

from agent.tools.base import Tool


class DshSessionTool(Tool):
    """与 DeepSeek Harness Agent 对话式协作的会话工具。

    适用场景：
        - 用户要求用 dsh / DeepSeek Harness 开发代码、修 bug、审查、跑测试
        - 需要把编码任务委托给独立 CLI 编程 Agent 并多轮纠偏（替代无头单发）
    不适用场景：
        - 简单问答（宿主 Agent 自己答更快）
        - DSH web 未运行（工具会给出可读报错，先 `dsh web`）
    """

    # ---- Tool 抽象属性 ----
    name = "dsh_session"
    description = (
        "调用本机 DeepSeek Harness（dsh）的编码 Agent 并多轮对话。"
        "action=list 查看已有 DSH 会话；action=prompt 派活/追问（同一 session_id "
        "反复调用即对话，DSH 记住上下文；不传 session_id 会自动新建项目会话）；"
        "action=read 增量读取 DSH 回复（传 before_seq 只取新内容）；"
        "action=cancel 打断当前回合。当用户要求用 dsh/DeepSeek Harness 开发、"
        "或需要独立编程 Agent 写代码/改 bug/跑测试/做代码审查时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "prompt", "read", "cancel"],
                "description": "操作：list 列会话 / prompt 发消息 / read 读回复 / cancel 取消",
            },
            "session_id": {
                "type": "string",
                "description": "DSH 会话 ID（prompt/read/cancel 使用；prompt 缺省时自动新建）",
            },
            "message": {
                "type": "string",
                "description": "发给 DSH Agent 的指令（action=prompt 时必填）",
            },
            "before_seq": {
                "type": "integer",
                "description": "增量读取起点（action=read 使用，只返回 seq 大于该值的新内容；首次可省略）",
            },
        },
        "required": ["action"],
    }

    # ---- 内部常量 ----
    DEFAULT_BASE_URL = "http://127.0.0.1:3080"
    RPC_TIMEOUT_SEC = 20.0

    def __init__(
        self,
        workspace: str,
        base_url: Optional[str] = None,
        client_factory=None,
    ) -> None:
        """构造工具。

        Args:
            workspace: 项目工作区路径；新建 DSH 会话时作为 cwd 传入。
            base_url: DSH web 的 API 根地址；缺省读环境变量 DSH_API_BASE，
                再缺省 127.0.0.1:3080。
            client_factory: 测试注入用；接受 **kwargs 返回 httpx.AsyncClient。
        """
        self.workspace = os.path.abspath(workspace)
        # 注意：DSH 的 session.create 校验 cwd 必须是绝对路径（相对路径会报
        # "session header cwd must be an absolute path"）。config.workspace
        # 默认是相对路径 "."，必须在此规范化，不能依赖调用方传绝对路径。
        self.base_url = (
            (base_url or os.environ.get("DSH_API_BASE") or self.DEFAULT_BASE_URL)
            .rstrip("/")
        )
        self._client_factory = client_factory or httpx.AsyncClient

    # ---- Tool 执行入口 ----
    async def execute(self, **kwargs) -> str:
        action = str(kwargs.get("action") or "").strip()
        session_id = kwargs.get("session_id")
        message = kwargs.get("message")
        before_seq = kwargs.get("before_seq")

        if action not in ("list", "prompt", "read", "cancel"):
            return (
                f"未知 action：{action!r}。可选：list（列会话）/ prompt（派活或追问，"
                "需 message）/ read（读回复，可带 before_seq）/ cancel（取消，需 session_id）"
            )
        try:
            if action == "list":
                return await self._list_sessions()
            if action == "prompt":
                return await self._prompt(session_id, message)
            if action == "read":
                return await self._read(session_id, before_seq)
            return await self._cancel(session_id)
        except httpx.ConnectError as exc:
            return (
                f"DSH 服务未连接（{self.base_url}）：请先运行 `dsh web` 再试。"
                f"（{exc.__class__.__name__}）"
            )
        except Exception as exc:  # 与 ExecTool 一致：错误转可读字符串，不让主链路炸
            return f"DSH 调用失败：{exc}"

    # ---- RPC 底层 ----
    async def _rpc(self, method: str, payload: dict) -> dict:
        """执行一次 DSH RPC 调用，返回业务结果（value）或抛异常。"""
        import uuid

        body = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": payload,
        }
        url = urljoin(f"{self.base_url}/", f"api/{method}")
        async with self._client_factory(timeout=self.RPC_TIMEOUT_SEC) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
        result = data.get("result") or {}
        if not result.get("ok"):
            err = result.get("error") or {}
            code = err.get("code", "unknown")
            msg = err.get("message", "未知错误")
            raise RuntimeError(f"DSH 返回错误 [{code}] {msg}")
        return result.get("value") or {}

    # ---- 各 action ----
    async def _list_sessions(self) -> str:
        value = await self._rpc("session.list", {})
        items = value.get("items") or []
        if not items:
            return "DSH 目前没有会话。可用 action=prompt 直接新建（cwd=项目目录）。"

        def sort_key(item: dict):
            # 当前项目目录的会话排最前（running 优先），其次其他项目（按时间倒序）
            same_project = 0 if (item.get("cwd") or "").startswith(self.workspace) else 1
            return (same_project, 0 if item.get("running") else 1, -(item.get("updatedAt") or 0))

        lines = ["DSH 会话列表（本项目的排前面）："]
        for item in sorted(items, key=sort_key):
            proj = (item.get("projections") or {}).get("values") or {}
            title = proj.get("title") or "(无标题)"
            sid = item.get("sessionId") or ""
            short = sid.split("-")[0][:8]
            state = "运行中" if item.get("running") else "空闲"
            lines.append(
                f"- [{state}] {short}… 标题：{title} ｜ cwd：{item.get('cwd')}"
            )
        lines.append(
            "提示：把完整 session_id 传给 prompt/read/cancel 继续对话；"
            "action=prompt 不传 session_id 会新建会话。"
        )
        return "\n".join(lines)

    async def _prompt(self, session_id, message) -> str:
        if not message or not str(message).strip():
            return "派活失败：action=prompt 必须带 message（要发给 DSH Agent 的指令）。"
        message = str(message).strip()
        # 缺省会话：自动新建（cwd=项目工作区）
        if not session_id:
            created = await self._rpc(
                "session.create", {"cwd": self.workspace}
            )
            session_id = created.get("sessionId")
            if not session_id:
                return "派活失败：DSH 未返回新会话 ID。"
            prefix = f"已自动新建会话 {session_id}"
        else:
            prefix = f"使用会话 {session_id}"
        value = await self._rpc(
            "session.prompt",
            {
                "sessionId": str(session_id),
                "mode": "queue",
                "content": [{"type": "text", "text": message}],
            },
        )
        if not value.get("accepted"):
            return f"派活失败：DSH 未接受消息（{value}）。"
        return (
            f"{prefix}，指令已派发（accepted）。\n"
            "DSH Agent 正在干活；稍后用 action=read 传同一 session_id 轮询结果。"
        )

    async def _read(self, session_id, before_seq) -> str:
        if not session_id:
            return "读取失败：action=read 必须带 session_id。"
        # 注意：DSH 的 beforeSeq 参数是"往前翻页"语义（取 seq < beforeSeq 的旧事件），
        # 不是增量起点。增量读取拿最新一页后在本地按 seq 过滤（before_seq 仅作过滤起点）。
        value = await self._rpc("session.history", {"sessionId": str(session_id)})
        events = value.get("events") or []
        if not events:
            return "该会话还没有任何事件（可能刚创建，等 DSH Agent 启动）。"

        last_seq = max((e.get("event") or {}).get("seq", 0) for e in events)
        start = int(before_seq) if before_seq is not None else 0

        # 收集 seq > start 的完整 assistant 文本（chunk 已在持久层折叠为 message）
        replies: list[tuple[int, str]] = []
        for entry in events:
            ev = entry.get("event") or {}
            if ev.get("type") != "assistant/message":
                continue
            seq = ev.get("seq", 0)
            if seq <= start:
                continue
            data = ev.get("data") or {}
            msg = data.get("message", data)
            text = "".join(
                part.get("text", "")
                for part in (msg.get("content") or [])
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if text.strip():
                replies.append((seq, text.strip()))

        # 回合是否已结束（turn/end 事件存在）
        has_turn_end = any(
            (e.get("event") or {}).get("type") == "turn/end" for e in events
        )

        # 检测挂起的审批请求：DSH 审批策略为 ask 且应答者缺失/无人应答时，
        # 回合会卡在 approval/asked 上（宿主 Agent 无法通过 API 批准）。
        approvals = [
            (e.get("event") or {}).get("data") or {}
            for e in events
            if (e.get("event") or {}).get("type") == "approval/asked"
        ]
        if approvals and not has_turn_end:
            a = approvals[-1]
            reason = str(a.get("reason") or "")
            tool_name = str(a.get("toolName") or "?")
            return (
                f"⚠️ DSH 正在等待权限审批（工具 {tool_name}），已挂起：{reason}\n"
                "宿主 Agent 无法批准 DSH 的审批请求（/api 无审批方法）。处理方式：\n"
                "1) 让用户在 DSH Web 界面（127.0.0.1:3080）处理审批，或\n"
                "2) 用 action=cancel 取消当前回合，重新派活时明确限定在项目目录内，或\n"
                "3) 在 DSH 侧把审批策略改为 never（越界操作确定性拒绝，不挂起）。"
            )

        if not replies:
            if has_turn_end:
                return (
                    f"DSH 回合已完成但无新回复（起点 seq={start}，最新 seq={last_seq}）。\n"
                    f"下次读取仍可用 before_seq={last_seq}。"
                )
            return (
                f"DSH 还在干活，暂无新回复（起点 seq={start}，最新 seq={last_seq}）。\n"
                f"稍后再用 action=read 传 before_seq={last_seq} 继续轮询。"
            )

        seq, text = replies[-1]
        state = "已完成" if has_turn_end else "运行中"
        return (
            f"【DSH Agent 回复】（事件 seq={seq}，状态：{state}）\n{text}\n"
            f"---\n下次增量读取请传 before_seq={last_seq}。"
        )

    async def _cancel(self, session_id) -> str:
        if not session_id:
            return "取消失败：action=cancel 必须带 session_id。"
        value = await self._rpc("session.cancel", {"sessionId": str(session_id)})
        return f"已请求取消会话 {session_id} 的当前回合：{value}"
