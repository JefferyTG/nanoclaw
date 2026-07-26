"""生图工具 ``generate_image``。

用 AI 图像生成 API（OpenAI 兼容的 images/generations 协议）把文字描述变成图片，
并支持基于已有图片的图生图（img2img）。

设计要点（见开发计划 image_gen_dev_plan.md）：
- 工具**始终注册**（不受 base_model_multimodal 影响）。未配置 ``image_gen_model``
  时仍注册，但返回"未配置生图模型"的友好提示给主模型，由主模型用文字继续回答。
- **单工具同时支持文生图与图生图**：``prompt`` 必填；``image_id`` / ``image_url``
  可传单个或**多个**（数组），任意一个提供即走图生图，都不提供则文生图。``image_id``
  复用 ask_image 已有的会话内图片标识（用户上传或之前生成的图都能引用），``image_url``
  接受公网链接。图生图的 ``image`` 始终以**数组**形式传递，支持一次基于多张源图生成。
- 具体用哪个服务、哪个模型（图生图模型缺省回落到通用 model，文/图生图可共用）、
  图生图请求体怎么拼，完全由 ``image_gen_model`` 配置决定（api_key / base_url /
  model / img2img_model / img2img），代码不绑定任何具体服务商或模型名。
- 源图传输（默认 ``encoding=auto``，按每张源图自动选择，无需手动配置）：本地图
  （image_id 经 ImageStore、file_path 经工作区边界校验）读字节转 **base64 内联**，
  不暴露 localhost URL（外部 API 取不到本地地址）；公网图（image_url）直接发链接。
  也可显式配 ``base64`` / ``url`` 强制统一。多张源图（三路可混用、均支持数组）各自
  拼装后放进同一个 ``image`` 数组。file_path 与 read_file / ask_image 共用同一套
  工作区边界隔离，越界路径会被拦截。
- 图片字节**下载后落本地 ImageStore**（与 ask_image 同目录，随 /clear 清理）。
- 显示：**新增 ``image`` 流事件**——生图成功后通过 ``stream_sink`` 推给网页端，
  在对话气泡里内联显示（非网页渠道 stream_sink 为 None，退化为仅落盘 + 文本结果）。
- 超时防护：**工具内 HTTP 超时（默认 120s，可配 image_gen_model.timeout_sec）+ 429/5xx
  有限重试**；超时/限流返回文本给主模型优雅收尾，不抛异常、不拖垮整轮。
- HTTP 客户端用 **httpx**（项目已依赖）：直接打图像端点，精确控制 timeout 与重试。
"""

import asyncio
import base64
import binascii
import mimetypes
import os
import re

import httpx

from agent.tools.base import Tool


# 生图接口路径（拼在 base_url 之后；base_url 由用户配置，不写死任何服务商）
_GEN_PATH = "/images/generations"

# 重试：429 等 8s，5xx 等 5s，最多 3 次
_MAX_ATTEMPTS = 3
_RETRY_429_WAIT = 8
_RETRY_5XX_WAIT = 5


class GenerateImageTool(Tool):
    """根据文字描述生成图片；可选基于已有图片做图生图，并返回图片在本会话的引用。"""

    name = "generate_image"
    description = (
        "根据文字描述生成一张图片。当用户明确要求'画图 / 生图 / 画一张…'时调用本工具。"
        "prompt 为画面描述（中文即可）；size 可选，形如 '1024x1024' / '1024x768' / "
        "'768x1024'，不填默认 1024x1024。生成成功后图片会直接在对话里显示，"
        "也可在本会话内继续引用。若想基于某张（或多张）已有图片改造（图生图），可额外传入"
        "图生图源图（三路任选其一或混用，均支持单张字符串或多张数组）："
        "① image_id——本会话已有图片的标识（见 ask_image 的 image_id，用户上传或之前生成的图均可）；"
        "② image_url——公网图片链接；"
        "③ file_path——工作区内本地图片文件路径（相对工作区或绝对路径，与 read_file / ask_image 一致）。"
        "都不传则按文字全新生成；图生图的源图会以数组形式传给生图服务。"
        "若未配置生图模型，会告知无法生图，由你用文字说明。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "画面描述（中文即可，尽量具体）",
            },
            "size": {
                "type": "string",
                "description": (
                    "可选尺寸，形如 '1024x1024' / '1024x768' / '768x1024'，"
                    "不填默认 1024x1024"
                ),
            },
            "image_id": {
                "type": ["string", "array"],
                "description": (
                    "可选，图生图源图标识：本会话已有图片的 image_id"
                    "（用户上传或之前生成的图）；与 image_url / file_path 可同时提供，支持单张字符串或多张数组"
                ),
            },
            "image_url": {
                "type": ["string", "array"],
                "description": "可选，图生图源图公网 URL；与 image_id / file_path 可同时提供，支持单张字符串或多张数组",
            },
            "file_path": {
                "type": ["string", "array"],
                "description": "可选，图生图源图工作区内本地图片文件路径（相对工作区或绝对路径）；与 image_id / image_url 可同时提供，支持单张字符串或多张数组",
            },
        },
        "required": ["prompt"],
    }

    def __init__(self, image_store, config) -> None:
        """初始化。

        参数：
            image_store: ``ImageStore`` 实例，用于按 session_key 落盘生成的图片、读取源图。
            config: 共享的 ``NanoClawConfig`` 实例（实时读取 image_gen_model，含
                timeout_sec / img2img_model / img2img，支持网页改配置后新会话即时生效）。
            具体服务地址与模型名由配置决定，本工具不绑定任何服务商。
        """
        self.image_store = image_store
        self.config = config
        # 本地读图的边界根目录：与 read_file / ask_image 等文件系统工具保持一致的工作区隔离
        self.workspace = os.path.abspath(getattr(config, "workspace", "."))

    # —— 配置读取 ——
    def _gen_cfg(self) -> dict:
        cfg = getattr(self.config, "image_gen_model", None) or {}
        if not isinstance(cfg, dict):
            return {}
        return cfg

    def _configured(self) -> bool:
        c = self._gen_cfg()
        # api_key / base_url / model 三者皆需由用户配置，缺一不可
        return bool(c.get("api_key") and c.get("base_url") and c.get("model"))

    def _timeout(self) -> float:
        """生图 HTTP 超时（秒）：取自 image_gen_model.timeout_sec，缺省回落 120。"""
        c = self._gen_cfg()
        t = c.get("timeout_sec")
        if isinstance(t, (int, float)) and t > 0:
            return float(t)
        return 120.0

    def _img2img_model(self) -> str:
        """图生图模型：优先 img2img_model，缺省回落到通用 model。"""
        c = self._gen_cfg()
        return (c.get("img2img_model") or "").strip() or (c.get("model") or "")

    def _img2img_cfg(self) -> dict:
        """图生图请求体装配配置，缺失项给安全默认（不依赖 config 是否写了 img2img）。"""
        c = self._gen_cfg()
        raw = c.get("img2img") or {}
        if not isinstance(raw, dict):
            raw = {}
        strength = raw.get("strength")
        return {
            "image_field": raw.get("image_field") or "image",
            "image_location": raw.get("image_location") or "body",
            "encoding": raw.get("encoding") or "auto",
            "strength_field": raw.get("strength_field") or "",
            "strength": strength if isinstance(strength, (int, float)) else None,
            "tags": raw.get("tags") or [],
            "as_array": bool(raw.get("as_array", True)),
        }

    @staticmethod
    def _parse_size(size) -> tuple:
        """把 size 参数解析成 (width, height)；格式非法回落默认 1024x1024。"""
        if isinstance(size, str):
            m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", size)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                if 0 < w <= 4096 and 0 < h <= 4096:
                    return w, h
        return 1024, 1024

    @staticmethod
    def _ext_mime_from_content_type(ct: str):
        """由 Content-Type 推断扩展名与 mime。"""
        ct = (ct or "").split(";")[0].strip().lower()
        mapping = {
            "image/png": ("png", "image/png"),
            "image/jpeg": ("jpg", "image/jpeg"),
            "image/jpg": ("jpg", "image/jpeg"),
            "image/webp": ("webp", "image/webp"),
            "image/gif": ("gif", "image/gif"),
            "image/bmp": ("bmp", "image/bmp"),
        }
        return mapping.get(ct, ("png", "image/png"))

    # —— 源图解析（图生图，支持多张）——
    @staticmethod
    def _as_list(value):
        """把单值/数组参数统一为字符串列表；None 返回空列表（与 ask_image 一致）。"""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(i) for i in value]
        return [str(value)]

    def _safe_local_path(self, user_path: str):
        """把本地路径解析为绝对路径，越出工作区返回 None（与 read_file / ask_image 同构）。"""
        target = os.path.abspath(os.path.join(self.workspace, user_path))
        if target == self.workspace or target.startswith(self.workspace + os.sep):
            return target
        return None

    def _resolve_sources(self, image_ids, image_urls, file_paths, session_key):
        """解析图生图源图（三路：image_id / image_url / file_path，支持多张混用），返回 (sources, err)。

        sources 为列表，每个元素 ``{"bytes":..., "url":..., "mime":...}``：
        - image_ids：本会话 ImageStore 里的 image_id 列表，本地图读字节（base64 内联用）。
        - image_urls：公网链接列表，直发用。
        - file_paths：工作区内本地图片文件，经工作区边界校验后读字节（base64 内联用）。
        - 三者都为空 → 返回 ([], None) 表示文生图。
        """
        ids = self._as_list(image_ids)
        urls = self._as_list(image_urls)
        paths = self._as_list(file_paths)
        if not ids and not urls and not paths:
            return [], None
        if ids and (not session_key or self.image_store is None):
            return None, "需要提供 session_key 才能按 image_id 读取源图"
        sources = []
        for iid in ids:
            ref = self.image_store.resolve(session_key, iid)
            if ref is None:
                return None, (
                    f"未找到 image_id={iid} 对应的源图"
                    f"（可能已被 /clear 或会话清理）"
                )
            try:
                with open(ref.path, "rb") as f:
                    raw = f.read()
            except Exception as exc:  # noqa: BLE001
                return None, f"读取源图 image_id={iid} 失败：{exc}"
            sources.append({"bytes": raw, "url": None, "mime": ref.mime})
        for u in urls:
            sources.append({"bytes": None, "url": u, "mime": None})
        for p in paths:
            absolute = self._safe_local_path(p)
            if absolute is None:
                return None, f"文件 {p} 越过工作区边界，已被拦截"
            if not os.path.isfile(absolute):
                return None, f"文件 {p} 不存在"
            mime = mimetypes.guess_type(absolute)[0]
            if not mime or not mime.startswith("image/"):
                return None, f"文件 {p} 不是可识别的图片文件（mime={mime}）"
            try:
                with open(absolute, "rb") as f:
                    raw = f.read()
            except Exception as exc:  # noqa: BLE001
                return None, f"读取源图文件 {p} 失败：{exc}"
            sources.append({"bytes": raw, "url": None, "mime": mime})
        return sources, None

    def _build_img2img_payload(self, prompt, width, height, sources,
                               size_provided) -> tuple:
        """装配图生图请求体，返回 (payload, err)。

        按 image_gen_model.img2img 配置拼装：源图编码（base64/url）、键名、位置
        （body 顶层 / extra_body 嵌套）、是否数组、强度、标签。``image`` 默认以**数组**
        形式传递（支持多图，Agnes 即如此）；size 仅在用户显式传入时才带，避免与源图
        尺寸冲突（多数服务商图生图沿用源图尺寸）。
        """
        cfg = self._img2img_cfg()
        vals = []
        for s in sources:
            enc = cfg["encoding"]
            if enc == "auto":
                # 按源图类型自动选：本地字节→base64 内联，公网链接→直发 URL
                if s["bytes"] is not None:
                    enc = "base64"
                elif s["url"]:
                    enc = "url"
                else:
                    return None, "图生图源图既缺少本地字节也缺少公网 URL"
            if enc == "url":
                if not s["url"]:
                    return None, "图生图配置为 url 编码，但某张源图是本地图、缺少可访问的 URL"
                vals.append(s["url"])
            else:  # base64 内联
                if not s["bytes"]:
                    return None, "图生图配置为 base64 编码，但未能读取到某张本地源图字节"
                b64 = base64.b64encode(s["bytes"]).decode("ascii")
                vals.append(f"data:{s['mime']};base64,{b64}" if s["mime"] else b64)
        # 非数组模式（极少数服务商要求标量）：只取第一张，无源图给空串
        if not cfg["as_array"]:
            vals = vals[0] if vals else ""

        core = {
            "model": self._img2img_model(),
            "prompt": prompt,
        }
        if size_provided:
            core["size"] = f"{width}x{height}"
        extra = {cfg["image_field"]: vals}
        if cfg["strength_field"] and cfg["strength"] is not None:
            extra[cfg["strength_field"]] = cfg["strength"]
        if cfg["tags"]:
            extra["tags"] = cfg["tags"]

        if cfg["image_location"] == "extra_body":
            core["extra_body"] = extra
        else:
            core.update(extra)
        return core, None

    # —— 主流程 ——
    async def execute(
        self,
        prompt,
        size=None,
        image_id=None,
        image_url=None,
        file_path=None,
        session_key=None,
        stream_sink=None,
        _generated_ids=None,
    ) -> str:
        """执行：调用配置的图像生成服务生图（文生图或图生图），落盘，推 image 事件，返回文本。"""
        if not self._configured():
            return (
                "⚠️ 当前未配置生图模型（image_gen_model）。我暂时无法生成图片。"
                "如果你需要画图，请先在 config.json 的 image_gen_model 中填好"
                " api_key / base_url / model（或设置环境变量 IMAGE_GEN_API_KEY），"
                "我会立刻为你生成；现在我先就你文字里的其它需求作答。"
            )

        c = self._gen_cfg()
        api_key = c["api_key"]
        base_url = str(c["base_url"]).rstrip("/")
        model = c["model"]  # 文生图模型

        width, height = self._parse_size(size)
        size_provided = bool(size)

        # 解析源图（三路：image_id / image_url / file_path，支持多张混用）→ 决定文生图 / 图生图
        sources, src_err = self._resolve_sources(
            image_id, image_url, file_path, session_key
        )
        if src_err:
            return f"⚠️ {src_err}"
        is_img2img = bool(sources)

        if is_img2img:
            payload, perr = self._build_img2img_payload(
                prompt, width, height, sources, size_provided
            )
            if perr:
                return f"⚠️ {perr}"
        else:
            payload = {
                "model": model,
                "prompt": prompt,
                "size": f"{width}x{height}",
            }

        url = base_url + _GEN_PATH
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self._timeout())

        # —— 调生图服务：超时 + 429/5xx 有限重试，失败优雅收尾 ——
        raw = None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for attempt in range(_MAX_ATTEMPTS):
                    try:
                        resp = await client.post(url, json=payload, headers=headers)
                    except (httpx.TimeoutException, httpx.RequestError) as exc:
                        if attempt == _MAX_ATTEMPTS - 1:
                            return (
                                f"⚠️ 生图请求失败（{type(exc).__name__}），已重试 "
                                f"{_MAX_ATTEMPTS} 次仍未成功。你可以稍后再试，或检查网络。"
                            )
                        await asyncio.sleep(min(2 * (attempt + 1), 10))
                        continue
                    if resp.status_code == 429:
                        if attempt == _MAX_ATTEMPTS - 1:
                            return "⚠️ 生图接口限流（429），重试后仍未成功，请稍后再试。"
                        await asyncio.sleep(_RETRY_429_WAIT)
                        continue
                    if resp.status_code >= 500:
                        if attempt == _MAX_ATTEMPTS - 1:
                            return (
                                f"⚠️ 生图接口服务端错误（{resp.status_code}），"
                                f"重试 {_MAX_ATTEMPTS} 次仍未成功，请稍后再试。"
                            )
                        await asyncio.sleep(_RETRY_5XX_WAIT)
                        continue
                    break
                else:
                    return "⚠️ 生图失败：未知原因（重试耗尽）。"

                if resp.status_code >= 400:
                    return (
                        f"⚠️ 生图失败：接口返回 {resp.status_code}。"
                        f"{_safe_err_text(resp) or '无更多错误信息。'}"
                    )

                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    return "⚠️ 生图失败：接口返回的不是合法 JSON。"

                item = (data.get("data") or [None])[0]
                if not isinstance(item, dict):
                    return "⚠️ 生图失败：返回结构异常（缺少 data[0]）。"
                img_url = item.get("url")
                b64 = item.get("b64_json")
                if img_url:
                    try:
                        dl = await client.get(img_url)
                        dl.raise_for_status()
                        raw = dl.content
                        ct = dl.headers.get("content-type", "")
                        ext, mime = self._ext_mime_from_content_type(ct)
                    except Exception as exc:  # noqa: BLE001
                        return f"⚠️ 生图成功但图片下载失败：{exc}。"
                elif b64:
                    try:
                        raw = base64.b64decode(b64)
                        ext, mime = "png", "image/png"
                    except (binascii.Error, ValueError) as exc:
                        return f"⚠️ 生图成功但图片解码失败：{exc}。"
                else:
                    return "⚠️ 生图失败：返回中既没有 url 也没有 b64_json。"
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 生图过程出错：{exc}。"

        if not raw:
            return "⚠️ 生图失败：未获取到图片字节。"

        # —— 落盘到 ImageStore ——
        if not session_key or self.image_store is None:
            return "⚠️ 生图成功，但无法确定所属会话（缺少 session_key），无法保存图片。"
        try:
            ref = self.image_store.save(session_key, raw, ext, mime)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 生图成功，但保存图片失败：{exc}。"

        # —— 推 image 流事件 ——
        if stream_sink is not None:
            try:
                await stream_sink({
                    "type": "image",
                    "key": session_key,
                    "id": ref.id,
                    "url": f"/image?key={session_key}&id={ref.id}",
                    "mime": ref.mime,
                })
            except Exception:  # noqa: BLE001
                pass

        if isinstance(_generated_ids, list):
            _generated_ids.append(ref.id)

        kind = "图生图" if is_img2img else "文生图"
        return (
            f"✅ 已{kind}生成图片（image_id={ref.id}，{width}x{height}，{mime}）。"
            f"图片已保存到本会话，可直接在对话中查看或继续引用。"
        )


def _safe_err_text(resp: httpx.Response) -> str:
    """从错误响应里尽量取出可读信息（截断避免过长）。"""
    try:
        body = resp.text or ""
    except Exception:  # noqa: BLE001
        return ""
    body = body.strip()
    if len(body) > 300:
        body = body[:300] + "…"
    return body
