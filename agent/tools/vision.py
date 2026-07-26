"""视觉工具 ``ask_image``。

当基础模型是纯文本（如 DeepSeek）时，把图片从基础模型的上下文里抽走、换成占位符，
并注册本工具。基础模型在需要理解/回答关于图片的问题时调用本工具：工具把图片
（base64 data URL）+ 用户问题（含基础模型补充的上下文）一并交给配置的**多模态模型**
解读，把回答回填给基础模型，由基础模型结合上下文汇总回复用户。

关键点（见开发计划 image_vision_dev_plan.md）：
- 多模态模型未配置时，仍注册本工具，但返回"看不见图片"给基础模型，由它继续回答
  用户的**文字部分**（绝不从系统层短路，避免丢掉用户的文字提问）。
- 图片以 base64 data URL 内嵌调用，无需公网 hosting；省 token 靠"只发精炼问题+图、
  不灌整段历史"。
- 支持单张或多张（image_id 可为字符串或数组），多张可一次性对比。
- 跨轮引用：image_id 在历史消息里有记录，图片字节落盘存活，重启后仍可取。
- 本地读图：除消息附带的 image_id 外，也支持 file_path 直接读工作区内的本地图片
  文件（与 read_file 相同的工作区边界校验，路径穿越会被拦截）。
"""

import base64
import mimetypes
import os

from agent.tools.base import Tool
from providers.openai_compat import OpenAICompatProvider


class AskImageTool(Tool):
    """把图片交给视觉模型解读，并返回其回答。"""

    name = "ask_image"
    description = (
        "需要理解或回答关于图片内容的问题时调用本工具，把图片交给视觉模型解读并返回其回答。"
        "两种图片来源（至少提供其一，可混用）："
        "① image_id——用户消息中占位符里的图片标识；"
        "② file_path——工作区内本地图片文件的路径（如用户要求你直接看某个本地图片文件时）。"
        "两者均支持单张字符串或多张数组。"
        "question 应是关于图片的问题，可包含必要的上下文。"
        "若未配置视觉模型，会告知无法看图，由你仅基于用户文字作答。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_id": {
                "type": ["string", "array"],
                "description": "图片标识（来自用户消息占位符），支持单张字符串或多张数组",
            },
            "file_path": {
                "type": ["string", "array"],
                "description": "工作区内本地图片文件路径（相对工作区或绝对路径），支持单张字符串或多张数组",
            },
            "question": {
                "type": "string",
                "description": "关于图片的问题，应包含必要的上下文",
            },
        },
        "required": ["question"],
    }

    def __init__(self, image_store, config) -> None:
        """初始化。

        参数：
            image_store: ``ImageStore`` 实例，用于按 session_key + id 取回图片。
            config: 共享的 ``NanoClawConfig`` 实例（实时读取 multimodal_model，
                支持网页改配置后新会话即时生效）。
        """
        self.image_store = image_store
        self.config = config
        # 本地读图的边界根目录：与 read_file 等文件系统工具保持一致的工作区隔离
        self.workspace = os.path.abspath(getattr(config, "workspace", "."))
        # 视觉 Provider 懒构造（仅首次调用且已配置时），避免无谓建连
        self._vision_provider = None

    def _mm_configured(self) -> bool:
        mm = getattr(self.config, "multimodal_model", None) or {}
        return bool(mm.get("api_key") and mm.get("base_url") and mm.get("model"))

    def _get_provider(self) -> OpenAICompatProvider:
        if self._vision_provider is None:
            mm = self.config.multimodal_model
            self._vision_provider = OpenAICompatProvider(
                mm["api_key"], mm["base_url"], mm["model"]
            )
        return self._vision_provider

    @staticmethod
    def _as_list(value) -> list:
        """把单值/数组参数统一为字符串列表；None 返回空列表。"""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(i) for i in value]
        return [str(value)]

    def _safe_local_path(self, user_path: str):
        """把本地路径解析为绝对路径，越出工作区返回 None（同 read_file 的边界规则）。"""
        target = os.path.abspath(os.path.join(self.workspace, user_path))
        if target == self.workspace or target.startswith(self.workspace + os.sep):
            return target
        return None

    @staticmethod
    def _file_to_image_part(path: str, label: str):
        """读取图片文件并转成多模态 image_url part；失败返回文本说明 part。"""
        mime = mimetypes.guess_type(path)[0]
        if not mime or not mime.startswith("image/"):
            return {"type": "text", "text": f"[{label} 不是可识别的图片文件（mime={mime}）]"}
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            return {"type": "text", "text": f"[{label} 读取失败：{exc}]"}
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }

    def _build_content(self, ids: list, paths: list, question: str, session_key) -> list:
        """构造发给视觉模型的 user content（多模态 list 形式）。

        图片来源两路合并：
        - ids：消息附带图片，经 ImageStore 按 session_key 落盘取回；
        - paths：本地图片文件，经工作区边界校验后直接读取。
        """
        content = [{"type": "text", "text": question}]
        for iid in ids:
            ref = self.image_store.resolve(session_key, iid) if session_key else None
            if ref is None or not os.path.exists(ref.path):
                content.append({
                    "type": "text",
                    "text": f"[图片 image_id={iid} 未找到或已过期，可能已被 /clear 或会话清理]",
                })
                continue
            try:
                with open(ref.path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
            except Exception as exc:  # noqa: BLE001
                content.append({
                    "type": "text",
                    "text": f"[图片 image_id={iid} 读取失败：{exc}]",
                })
                continue
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{ref.mime};base64,{b64}"},
            })
        for p in paths:
            absolute = self._safe_local_path(p)
            if absolute is None:
                content.append({
                    "type": "text",
                    "text": f"[文件 {p} 越过工作区边界，已被拦截]",
                })
                continue
            if not os.path.isfile(absolute):
                content.append({
                    "type": "text",
                    "text": f"[文件 {p} 不存在]",
                })
                continue
            content.append(self._file_to_image_part(absolute, f"文件 {p}"))
        return content

    async def execute(self, question, image_id=None, file_path=None, session_key=None) -> str:
        """执行：把图片 + 问题交给视觉模型，返回其回答。

        参数：
            question: 关于图片的问题（含上下文）。
            image_id: 消息附带图片的标识，单张字符串或多张数组（与 file_path 至少给一个）。
            file_path: 工作区内本地图片文件路径，单张字符串或多张数组。
            session_key: 当前会话标识（由 AgentLoop 在工具执行时注入），
                用于按会话找回 image_id 对应的图片；仅用 file_path 时可缺失。
        """
        ids = self._as_list(image_id)
        paths = self._as_list(file_path)

        if not ids and not paths:
            return "错误：image_id 与 file_path 至少需要提供一个。"

        # 未配置多模态模型：告知看不见图片，由基础模型仅基于文字作答
        if not self._mm_configured():
            targets = ", ".join(ids + paths)
            return (
                "⚠️ 当前未配置视觉模型，我暂时看不见这张图片"
                f"（{targets}）。我将仅根据你文字里的问题作答。"
            )

        # 仅当需要按 image_id 找图时才依赖 session_key；纯本地路径读图不需要
        if ids and not session_key:
            return "无法定位图片所属会话（缺少 session_key），无法读取消息附带的图片。"

        content = self._build_content(ids, paths, question, session_key)
        try:
            provider = self._get_provider()
            resp = await provider.chat([{"role": "user", "content": content}])
        except Exception as exc:  # noqa: BLE001 - 视觉模型调用失败不应拖垮主循环
            return f"视觉模型调用失败：{exc}"

        if resp.finish_reason == "error":
            return f"视觉模型返回错误：{resp.content or '未知错误'}"
        return resp.content or "视觉模型未返回内容"
