"""视频生成工具 ``create_video`` / ``query_video``。

配置驱动的多服务商适配层：把视频服务描述成 ``video_providers`` 配置，代码里不出现
任何 ``if provider == ...`` 分支、不绑定任何厂商。每个 provider 用配置描述
「创建端点、查询方式、响应字段映射、参数映射」，工具纯读配置拼请求、解析响应
（见 config.json 的 ``video_provider`` / ``video_providers`` 结构）。

与生图不同，视频生成是**异步任务**：``create_video`` 只负责提交任务并**立即返回**
video_id，绝不轮询；用户稍后再问「视频好了吗」，模型再调用 ``query_video`` 查询结果。

设计要点（配置 schema 见需求单；兼容旧 video_gen_model 结构）：
- 两个工具**始终注册**（不受任何开关影响）。未配置时仍注册，但返回"未配置视频模型"
  的友好提示给主模型，由主模型用文字继续回答。
- 配置驱动：``video_provider`` 指定当前启用哪个 provider（缺省 "agnes"）；
  ``video_providers`` 描述各 provider 的 api_key/base_url/model/timeout_sec/download
  与 create/query/fields。**向后兼容**：没有新结构时，用 ``video_gen_model`` 的
  api_key/base_url/model/timeout_sec/download 填充 agnes 项，create/query/fields 走
  默认 schema（等价旧行为）。api_key 为空依次兜底 ``image_gen_model.api_key`` →
  环境变量 ``VIDEO_GEN_API_KEY``；base_url 为空兜底 ``image_gen_model.base_url``。
- ``create_video``：按 ``create.method/path`` 拼 URL（拼在 base_url 之后），body 里
  model 从配置读、其余参数照旧支持 width/height/num_frames/frame_rate/seed/
  negative_prompt/image。返回 video_id / 状态 / 预计时长（seconds 字段）并提示用户
  "预计 X 分钟后完成，稍后问我视频好了吗"。**立即返回，绝不轮询。**
- ``query_video``：按 ``query.method/path`` 拼 URL 查询状态。以 "/" 开头的绝对路径
  从网关根（scheme://netloc）拼起——agnes 的查询端点 ``/agnesapi`` 位于网关**根路径**
  （不是 /v1/ 下）；``id_in=query`` 时加 ``?<id_param>=<id>`` 查询参数，
  ``id_in=path`` 时替换 ``id_placeholder``（缺省 "{id}"，对应 kling）。completed 时
  **立即**把直链（按 fields.url 取，取不到再试 fields.url_fallback）下载落盘到
  VideoStore，返回本地引用；queued/in_progress 返回当前进度与"还没好"提示；failed
  透传 error 信息。``download=false`` 时不落盘、仅返回直链。查询端点 404 时兜底走
  ``{base_url}{create.path}/<TASK_ID>``（旧版兼容，404 才触发）。
- 响应字段映射统一走 fields 配置（支持"点路径 + 数组下标"，如 ``metadata.url``、
  ``result.videos[0].url``）。status 归一化：completed/success/done/succeed 视为完成
  （kling 历史版本返回 "succeed"）；queued/in_progress/processing/running 视为生成中；
  failed/error 视为失败。
- 超时防护：两个工具 ``execution_timeout_sec`` 均设 30s（秒级 HTTP，不需要长预算）；
  内部预算 25s，留 5s 给 async context manager 清理资源。429/5xx/短暂网络错误在预算
  内有限重试；错误文案安全（不泄漏 api_key）。
- HTTP 客户端用 **httpx**（项目已依赖）：直接打视频端点，精确控制 timeout 与重试。
"""

import asyncio
import math
import os
import re
import urllib.parse

import httpx

from agent.tools.base import Tool


# 默认 schema：兼容旧 video_gen_model 结构（无 video_providers 时的 agnes 兜底）。
# create.path 拼在 base_url 之后；query.path 以 "/" 开头时从网关根拼起。
_DEFAULT_CREATE = {"method": "POST", "path": "/videos"}
_DEFAULT_QUERY = {
    "method": "GET",
    "path": "/agnesapi",
    "id_in": "query",
    "id_param": "video_id",
}
_DEFAULT_FIELDS = {
    "task_id": "video_id",
    "status": "status",
    "progress": "progress",
    "seconds": "seconds",
    "size": "size",
    "url": "metadata.url",
    "url_fallback": "url",
}

# 两个工具的单次执行总预算：Registry 兜底 30s，内部预算留 5s 清理余量。
_EXECUTION_TIMEOUT_SEC = 30.0
_CLEANUP_GRACE_SECONDS = 5.0
_INTERNAL_BUDGET_SEC = _EXECUTION_TIMEOUT_SEC - _CLEANUP_GRACE_SECONDS  # 25.0

# 重试：429 等 3s，5xx 等 2s，最多 3 次（视频工具预算短，退避取小）
_MAX_ATTEMPTS = 3
_RETRY_429_WAIT = 3
_RETRY_5XX_WAIT = 2

# num_frames 约束：<= 441 且满足 8n + 1（官方文档）
_MAX_NUM_FRAMES = 441

# 预算耗尽/请求超时的统一文案（内部错误，不泄漏任何请求细节）
_TIMEOUT_MSG = "视频请求超时：已完成资源释放，请稍后重试。"


# 下载时从 Content-Type / URL 推断扩展名与 mime
_MIME_BY_EXT = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
}


def _get_field(obj, path):
    """按「点路径 + 数组下标」从嵌套结构取值；缺字段返回 None。

    路径形如 ``metadata.url``、``result.videos[0].url``：用 "." 分隔层级，支持
    ``[N]`` 下标访问 list。配置里 ``fields`` 各键的值即这类路径；``path`` 为空串
    或 None 时一律返回 None（对应 url_fallback 留空表示"没有第二来源"）。
    """
    if obj is None or not path:
        return None
    current = obj
    for token in str(path).split("."):
        if current is None:
            return None
        # "videos[0]" → ["videos", "0", ""]；偶数位是键名、奇数位是数组下标
        for i, part in enumerate(re.split(r"\[(\d+)\]", token)):
            if i % 2 == 0:
                if not part:
                    continue
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    return None
            else:
                if isinstance(current, (list, tuple)) and part.isdigit():
                    idx = int(part)
                    if idx < len(current):
                        current = current[idx]
                    else:
                        return None
                else:
                    return None
    return current


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


class _VideoToolMixin:
    """create_video / query_video 共享的配置解析与 HTTP 请求逻辑。"""

    # Registry 对两个工具的兜底单次执行超时：秒级 HTTP，不需要长预算
    execution_timeout_sec = _EXECUTION_TIMEOUT_SEC

    def __init__(self, video_store, config, *, client_factory=None) -> None:
        """初始化。

        参数：
            video_store: ``VideoStore`` 实例，用于按 session_key 落盘下载的视频。
            config: 共享的 ``NanoClawConfig`` 实例（实时读取 video_provider /
                video_providers，兼容旧 video_gen_model；支持网页改配置后新会话
                即时生效）。
            具体服务地址、端点、字段映射与模型名全部由配置决定，本工具不绑定任何
            服务商（配置见 config.json 的 video_providers）。
        """
        self.video_store = video_store
        self.config = config
        # 仅测试时注入；生产环境每次执行都创建独立 client，便于取消时确定关闭连接。
        self._client_factory = client_factory or httpx.AsyncClient

    # —— 配置读取（配置驱动 + 旧结构兜底）——
    def _provider_cfg(self) -> dict:
        """当前启用 provider 的生效配置（含旧 video_gen_model 兜底）。

        优先读 ``config.video_provider`` + ``config.video_providers``；没有新结构
        时用 ``video_gen_model`` 的 api_key/base_url/model/timeout_sec/download
        填充 agnes 项，create/query/fields 用默认 schema（等价旧行为）。返回 dict
        含 provider/api_key/base_url/model/timeout_sec/download/create/query/fields，
        各字段可为空（是否配置好由 ``_configured`` 判断）。
        """
        provider = (
            str(getattr(self.config, "video_provider", None) or "").strip() or "agnes"
        )
        providers = getattr(self.config, "video_providers", None) or {}
        if not isinstance(providers, dict):
            providers = {}
        raw = providers.get(provider)
        if not isinstance(raw, dict) or not raw:
            # 旧结构兜底：用 video_gen_model 填充 agnes 项
            vgm = getattr(self.config, "video_gen_model", None) or {}
            raw = dict(vgm) if isinstance(vgm, dict) else {}

        create = dict(_DEFAULT_CREATE)
        if isinstance(raw.get("create"), dict):
            create.update({k: v for k, v in raw["create"].items() if v is not None})
        query = dict(_DEFAULT_QUERY)
        if isinstance(raw.get("query"), dict):
            query.update({k: v for k, v in raw["query"].items() if v is not None})
        fields = dict(_DEFAULT_FIELDS)
        if isinstance(raw.get("fields"), dict):
            fields.update({k: v for k, v in raw["fields"].items() if v is not None})

        return {
            "provider": provider,
            "api_key": str(raw.get("api_key") or "").strip(),
            "base_url": str(raw.get("base_url") or "").strip(),
            "model": str(raw.get("model") or "").strip(),
            "timeout_sec": raw.get("timeout_sec"),
            "download": raw.get("download", True),
            "create": create,
            "query": query,
            "fields": fields,
        }

    def _base_url(self) -> str:
        """创建接口的 base_url；为空时兜底 image_gen_model.base_url。"""
        url = self._provider_cfg()["base_url"]
        if url:
            return url
        img = getattr(self.config, "image_gen_model", None) or {}
        if isinstance(img, dict):
            url = str(img.get("base_url") or "").strip().rstrip("/")
        return url

    def _api_key(self) -> str:
        """api_key 解析：provider → image_gen_model → 环境变量。"""
        key = self._provider_cfg()["api_key"]
        if key:
            return key
        img = getattr(self.config, "image_gen_model", None) or {}
        if isinstance(img, dict):
            key = str(img.get("api_key") or "").strip()
        if key:
            return key
        return str(os.environ.get("VIDEO_GEN_API_KEY") or "").strip()

    def _model(self) -> str:
        return self._provider_cfg()["model"]

    def _timeout(self) -> float:
        """单次 HTTP 请求超时（秒）：取自配置，缺省回落 30。"""
        t = self._provider_cfg()["timeout_sec"]
        if isinstance(t, (int, float)) and t > 0:
            return float(t)
        return 30.0

    def _download_enabled(self) -> bool:
        """是否把完成的视频落盘（provider.download，缺省 true）。"""
        return bool(self._provider_cfg()["download"])

    def _configured(self) -> bool:
        # api_key（含兜底）、base_url、model 三者齐备才算配置好；model 不内置默认名
        return bool(self._api_key() and self._base_url() and self._model())

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

    def _create_cfg(self) -> dict:
        return self._provider_cfg()["create"]

    def _query_cfg(self) -> dict:
        return self._provider_cfg()["query"]

    def _fields(self) -> dict:
        return self._provider_cfg()["fields"]

    def _create_url(self) -> str:
        """创建接口 URL：base_url + create.path。"""
        path = str(self._create_cfg().get("path") or "").strip()
        base = self._base_url()
        if not path:
            return base
        return base.rstrip("/") + "/" + path.lstrip("/")

    def _query_url(self, video_id: str) -> str:
        """拼查询接口 URL（含 id_in=path 时的占位符替换）。

        以 "/" 开头的绝对路径从网关根（scheme://netloc）拼起——agnes 的查询端点
        /agnesapi 位于网关**根路径**而非 base_url 的 /v1/ 下；相对路径则拼在
        base_url 之后。``id_in=path`` 时把 id_placeholder（缺省 "{id}"）替换成
        video_id；``id_in=query`` 时不带查询参数（由调用方用 params 传）。
        """
        q = self._query_cfg()
        base = self._base_url()
        path = str(q.get("path") or "").strip()
        if not path:
            url = base
        elif path.startswith("/"):
            parts = urllib.parse.urlsplit(base.rstrip("/"))
            url = f"{parts.scheme}://{parts.netloc}{path}"
        else:
            url = base.rstrip("/") + "/" + path.lstrip("/")
        if q.get("id_in") == "path":
            placeholder = str(q.get("id_placeholder") or "{id}")
            url = url.replace(placeholder, str(video_id))
        return url

    # —— 预算与退避 ——
    @staticmethod
    def _remaining(deadline: float) -> float:
        return deadline - asyncio.get_running_loop().time()

    @classmethod
    async def _sleep_with_deadline(cls, seconds: float, deadline: float) -> bool:
        """在剩余预算内退避；取消必须原样向上传播。"""
        remaining = cls._remaining(deadline)
        if remaining <= 0:
            return False
        await asyncio.sleep(min(seconds, remaining))
        return cls._remaining(deadline) > 0

    async def _run(self, work, *args) -> str:
        """在 25s 预算内执行 ``work(client, deadline, *args) -> str``。

        内部预算比 Registry 的 execution_timeout_sec 略早，确保 async with
        有机会关闭客户端和连接；超时/取消统一转成安全文案。
        """
        try:
            async with asyncio.timeout(_INTERNAL_BUDGET_SEC):
                async with self._client_factory() as client:
                    deadline = (
                        asyncio.get_running_loop().time() + _INTERNAL_BUDGET_SEC
                    )
                    return await work(client, deadline, *args)
        except asyncio.TimeoutError:
            return "⚠️ 视频任务超时：已完成资源释放，请稍后重试。"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 视频处理出错：{exc}。"

    # —— 带重试的请求 ——
    async def _request(self, client, deadline, method, url, *,
                       json_body=None, params=None):
        """发一次带有限重试的请求，返回 ``(data, err)``。

        成功时 data 为解析后的 JSON dict、err 为 None；失败时反之，err 为安全
        文案（不泄漏 api_key）。429/5xx/短暂网络错误在预算内重试。
        """
        resp = None
        for attempt in range(_MAX_ATTEMPTS):
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return None, _TIMEOUT_MSG
            try:
                resp = await client.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=self._auth_headers(),
                    timeout=httpx.Timeout(min(self._timeout(), remaining)),
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                if attempt == _MAX_ATTEMPTS - 1:
                    return None, (
                        f"视频请求失败（{type(exc).__name__}），已重试 "
                        f"{_MAX_ATTEMPTS} 次仍未成功。你可以稍后再试，或检查网络。"
                    )
                if not await self._sleep_with_deadline(
                    min(2 * (attempt + 1), 5), deadline
                ):
                    return None, _TIMEOUT_MSG
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                status = resp.status_code
                await resp.aclose()
                resp = None
                if attempt == _MAX_ATTEMPTS - 1:
                    if status == 429:
                        return None, "视频接口限流（429），重试后仍未成功，请稍后再试。"
                    return None, (
                        f"视频接口服务端错误（{status}），重试 {_MAX_ATTEMPTS} 次"
                        f"仍未成功，请稍后再试。"
                    )
                wait = _RETRY_429_WAIT if status == 429 else _RETRY_5XX_WAIT
                if not await self._sleep_with_deadline(wait, deadline):
                    return None, _TIMEOUT_MSG
                continue
            break
        if resp is None:
            return None, "视频请求失败：未知原因（重试耗尽）。"

        if resp.status_code >= 400:
            text = _safe_err_text(resp)
            await resp.aclose()
            return None, f"视频接口返回 {resp.status_code}。{text or '无更多错误信息。'}"

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            await resp.aclose()
            return None, "视频接口返回的不是合法 JSON。"
        await resp.aclose()
        if not isinstance(data, dict):
            return None, "视频接口返回结构异常（不是 JSON 对象）。"
        return data, None

    @staticmethod
    def _payload_obj(data: dict) -> dict:
        """兼容顶层对象与 ``{"data": {...}}`` 嵌套包装两种响应结构。"""
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner
        return data


class CreateVideoTool(_VideoToolMixin, Tool):
    """创建视频生成任务（异步任务式，立即返回 video_id，绝不轮询）。"""

    name = "create_video"
    description = (
        "根据文字描述创建一个视频生成任务（异步任务式）。当用户明确要求"
        "'生成视频 / 做一段视频 / 视频生成'时调用。"
        "**注意：本工具创建任务后立即返回 video_id，不会等待、不会轮询、不会在本工具内"
        "等到视频完成**——请把 video_id 与预计完成时间告知用户，并让用户稍后问"
        "'视频好了吗'，届时再调用 query_video 查询并下载结果。"
        "若未配置视频模型，会告知无法生成，由你用文字说明。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "视频画面描述（中文即可，尽量具体：主体+动作+场景+镜头运动+光线+风格）",
            },
            "image": {
                "type": "string",
                "description": "可选，图生视频：源图片的公网 URL，直接传给视频服务",
            },
            "width": {
                "type": "integer",
                "description": "可选，视频宽度（默认 1152；服务端会标准化到最近档位）",
            },
            "height": {
                "type": "integer",
                "description": "可选，视频高度（默认 768；服务端会标准化到最近档位）",
            },
            "num_frames": {
                "type": "integer",
                "description": (
                    "可选，视频帧数：必须 <= 441 且满足 8n+1（如 81≈3秒 / 121≈5秒 / "
                    "241≈10秒 / 441≈18秒，按 24fps）"
                ),
            },
            "frame_rate": {
                "type": "number",
                "description": "可选，视频帧率（1~60，推荐 24 或 30）",
            },
            "seed": {
                "type": "integer",
                "description": "可选，随机种子，用于生成可复现结果",
            },
            "negative_prompt": {
                "type": "string",
                "description": "可选，反向提示词，描述需要避免的内容",
            },
        },
        "required": ["prompt"],
    }

    # —— 参数校验与请求体装配 ——
    @staticmethod
    def _build_create_payload(prompt, image, width, height, num_frames,
                              frame_rate, seed, negative_prompt, model):
        payload = {"model": model, "prompt": prompt}
        if image is not None:
            image = str(image).strip()
            if not image:
                return None, "image 不能为空字符串"
            payload["image"] = image
        if width is not None:
            if not (isinstance(width, int) and 0 < width <= 4096):
                return None, "width 必须是 1~4096 之间的整数"
            payload["width"] = width
        if height is not None:
            if not (isinstance(height, int) and 0 < height <= 4096):
                return None, "height 必须是 1~4096 之间的整数"
            payload["height"] = height
        if num_frames is not None:
            if not (
                isinstance(num_frames, int)
                and 1 <= num_frames <= _MAX_NUM_FRAMES
                and (num_frames - 1) % 8 == 0
            ):
                return None, (
                    f"num_frames 必须是 ≤{_MAX_NUM_FRAMES} 且满足 8n+1 的整数"
                    "（如 81/121/241/321/401/441）"
                )
            payload["num_frames"] = num_frames
        if frame_rate is not None:
            if not (
                isinstance(frame_rate, (int, float))
                and 1 <= frame_rate <= 60
            ):
                return None, "frame_rate 必须在 1~60 之间"
            payload["frame_rate"] = frame_rate
        if seed is not None:
            if not isinstance(seed, int):
                return None, "seed 必须是整数"
            payload["seed"] = seed
        if negative_prompt is not None:
            negative_prompt = str(negative_prompt).strip()
            if negative_prompt:
                payload["negative_prompt"] = negative_prompt
        return payload, None

    async def execute(
        self,
        prompt=None,
        image=None,
        width=None,
        height=None,
        num_frames=None,
        frame_rate=None,
        seed=None,
        negative_prompt=None,
        session_key=None,
    ) -> str:
        """执行：创建视频任务并**立即返回** video_id（绝不轮询）。"""
        if not self._configured():
            return (
                "⚠️ 当前未配置视频生成模型（video_gen_model）。我暂时无法生成视频。"
                "如果你需要生成视频，请先在 config.json 的 video_gen_model 中填好"
                " api_key / base_url / model（api_key 为空时会自动复用 image_gen_model 的"
                " key，或设置环境变量 VIDEO_GEN_API_KEY）；现在我先就你文字里的"
                "其它需求作答。"
            )
        if not prompt or not str(prompt).strip():
            return "⚠️ 缺少视频画面描述 prompt，无法创建视频任务。"

        payload, perr = self._build_create_payload(
            prompt, image, width, height, num_frames, frame_rate, seed,
            negative_prompt, self._model(),
        )
        if perr:
            return f"⚠️ {perr}"
        return await self._run(self._create_work, payload)

    async def _create_work(self, client, deadline, payload) -> str:
        create = self._create_cfg()
        method = str(create.get("method") or "POST").upper()
        url = self._create_url()
        data, err = await self._request(client, deadline, method, url,
                                        json_body=payload)
        if err:
            return f"⚠️ {err}"

        body = self._payload_obj(data)
        # video_id 优先按 fields.task_id 路径取（如 agnes 的 "video_id"），取不到再
        # 兜底常见键名 video_id/id，兼容不同服务商创建响应的差异。
        video_id = _get_field(body, self._fields().get("task_id"))
        if not video_id:
            video_id = body.get("video_id") or body.get("id") or ""
        status = str(body.get("status") or "queued").strip() or "queued"
        progress = body.get("progress")
        seconds = body.get("seconds")
        size = body.get("size")
        task_id = body.get("task_id") or ""

        if not video_id:
            return "⚠️ 视频任务创建失败：接口未返回 video_id，请稍后重试。"

        seconds_text = ""
        if isinstance(seconds, (int, float)) and seconds > 0:
            seconds_text = f"约 {seconds} 秒"
        elif isinstance(seconds, str):
            try:
                seconds_num = float(seconds)
                if seconds_num > 0:
                    seconds_text = f"约 {seconds_num:g} 秒"
            except ValueError:
                pass
        size_text = f"，{size}" if size else ""
        progress_text = (
            f"，进度 {progress}%" if isinstance(progress, (int, float)) else ""
        )

        # 用 seconds（视频时长）粗估完成时间，避免用户干等
        try:
            seconds_num = float(seconds)
            minutes = max(1, math.ceil(seconds_num / 60.0))
        except (TypeError, ValueError):
            minutes = None

        lines = [
            "✅ 已创建视频生成任务，**立即返回，无需等待**：",
            f"- video_id：{video_id}",
            f"- 状态：{status}{progress_text}",
        ]
        if seconds_text:
            lines.append(f"- 预计时长：{seconds_text}{size_text}")
        if task_id:
            lines.append(f"- task_id：{task_id}")
        if minutes:
            lines.append(
                f"预计约 {minutes} 分钟后完成。请稍后问我「视频好了吗」，"
                "我再帮你查询并下载。"
            )
        else:
            lines.append(
                "生成通常需要几分钟，请稍后问我「视频好了吗」，我再帮你查询并下载。"
            )
        return "\n".join(lines)


class QueryVideoTool(_VideoToolMixin, Tool):
    """查询视频任务状态；completed 时立即下载落盘到 VideoStore。"""

    name = "query_video"
    description = (
        "查询视频生成任务的状态并获取结果。当用户问'视频好了吗 / 视频生成好了吗 / "
        "查一下视频进度'时调用。参数 video_id 必填（来自 create_video 的返回值）。"
        "completed 时我会立即把视频下载保存到本会话；尚未完成时返回当前进度并提示"
        "还没好，让用户稍后再问；失败时返回失败原因。若未配置视频模型，会告知无法查询。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "video_id": {
                "type": "string",
                "description": "视频任务 ID（create_video 返回的 video_id）",
            },
        },
        "required": ["video_id"],
    }

    async def execute(self, video_id=None, session_key=None) -> str:
        """执行：查询任务状态；completed 时立即下载落盘。"""
        if not self._configured():
            return (
                "⚠️ 当前未配置视频模型（video_gen_model）。我暂时无法查询视频任务。"
                "如果你需要查询视频，请先在 config.json 的 video_gen_model 中填好"
                " api_key / base_url / model（api_key 为空时会自动复用 image_gen_model 的"
                " key，或设置环境变量 VIDEO_GEN_API_KEY）。"
            )
        if not video_id or not str(video_id).strip():
            return "⚠️ 缺少 video_id 参数，无法查询视频任务。"
        return await self._run(self._query_work, str(video_id).strip(), session_key)

    async def _query_work(self, client, deadline, video_id, session_key) -> str:
        q = self._query_cfg()
        method = str(q.get("method") or "GET").upper()
        url = self._query_url(video_id)
        params = {}
        if q.get("id_in") == "query":
            params = {str(q.get("id_param") or "video_id"): video_id}
        data, err = await self._request(client, deadline, method, url, params=params)

        # 查询端点 404 时兜底旧版接口 GET {base_url}{create.path}/<TASK_ID>
        # （task_id 与 video_id 在多数响应中相同，可复用）
        if err is not None and ("404" in err or "未找到" in err):
            legacy_url = self._create_url() + "/" + video_id
            data, err = await self._request(
                client, deadline, "GET", legacy_url
            )
        if err:
            return f"⚠️ {err}"

        body = self._payload_obj(data)
        fields = self._fields()
        status = str(_get_field(body, fields.get("status")) or "").strip().lower()
        progress = _get_field(body, fields.get("progress"))
        progress_text = (
            f"进度 {progress}%"
            if isinstance(progress, (int, float)) and progress > 0
            else ""
        )

        if status in ("completed", "success", "done", "succeed"):
            return await self._download_completed(
                client, deadline, body, video_id, session_key
            )

        if status in ("queued", "in_progress", "processing", "running"):
            prefix = "⏳ 视频还在生成中"
            if progress_text:
                prefix += f"（{progress_text}）"
            return (
                f"{prefix}，还没好。请稍后再问我「视频好了吗」，我会帮你查询并下载。"
            )

        if status in ("failed", "error"):
            err_text = body.get("error") or body.get("message") or ""
            if isinstance(err_text, dict):
                err_text = err_text.get("message") or err_text.get("error") or ""
            return f"❌ 视频生成失败：{err_text or '无更多错误信息'}。"

        return (
            f"⏳ 视频任务状态未知（status={status or '空'}"
            f"{('，' + progress_text) if progress_text else ''}）。"
            f"请稍后再问我「视频好了吗」。"
        )

    async def _download_completed(self, client, deadline, body, video_id,
                                  session_key) -> str:
        """status=completed：立即把直链下载落盘（签名 URL 有时效）。

        直链按 fields.url 路径取；取不到时依次尝试 fields.url_fallback（可为空串
        表示没有第二来源）。agnes 默认 url=metadata.url、url_fallback=url。
        """
        fields = self._fields()
        video_url = _get_field(body, fields.get("url")) or ""
        if not video_url:
            video_url = _get_field(body, fields.get("url_fallback")) or ""

        if not video_url:
            return "⚠️ 视频已生成，但接口未返回视频直链（metadata.url / url）。"

        if not self._download_enabled():
            return (
                f"✅ 视频已生成完成！直链（有效期短，请尽快使用）：{video_url}"
            )

        if not session_key or self.video_store is None:
            return (
                f"⚠️ 视频已生成，但无法确定所属会话（缺少 session_key），无法保存。"
                f"直链（有效期短，请尽快使用）：{video_url}"
            )

        dl = None
        try:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return "⚠️ 视频任务超时：已完成资源释放，请稍后重试。"
            dl = await client.get(
                video_url,
                timeout=httpx.Timeout(min(self._timeout(), remaining)),
            )
            dl.raise_for_status()
            raw = dl.content
            ct = dl.headers.get("content-type", "")
            ext, mime = self._ext_mime_from_content_type(ct, video_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 视频已生成，但下载失败：{exc}。"
        finally:
            if dl is not None:
                await dl.aclose()

        try:
            ref = self.video_store.save(session_key, raw, ext, mime)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 视频已生成，但保存失败：{exc}。"

        size_mb = len(raw) / (1024 * 1024)
        return (
            f"✅ 视频已生成完成，并已下载保存到本会话（直链有时效，已立即落盘）：\n"
            f"- video_id：{video_id}\n"
            f"- 本地文件：{ref.path}\n"
            f"- 大小：约 {size_mb:.1f} MB"
        )

    @staticmethod
    def _ext_mime_from_content_type(ct: str, url: str):
        """由 Content-Type / URL 推断视频扩展名与 mime。"""
        ct = (ct or "").split(";")[0].strip().lower()
        mapping = {
            "video/mp4": ("mp4", "video/mp4"),
            "video/quicktime": ("mov", "video/quicktime"),
            "video/webm": ("webm", "video/webm"),
            "video/x-matroska": ("mkv", "video/x-matroska"),
            "video/mpeg": ("mpg", "video/mpeg"),
            "video/avi": ("avi", "video/x-msvideo"),
            "video/x-msvideo": ("avi", "video/x-msvideo"),
        }
        if ct in mapping:
            return mapping[ct]
        ext = _ext_from_url(url)
        return ext, _MIME_BY_EXT.get(ext, "video/mp4")


def _ext_from_url(url: str) -> str:
    """从直链 URL 路径里取扩展名；取不到回落 mp4。"""
    path = urllib.parse.urlparse(str(url or "")).path
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", path)
    return m.group(1).lower() if m else "mp4"
