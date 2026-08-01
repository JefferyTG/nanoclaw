"""网页渠道：在同局域网内提供网页界面，让用户登录后跟 NanoClaw 聊天、修改配置。

实现要点：
- 用 aiohttp 在后台线程里起一个 HTTP/WebSocket 服务（与网关共用同一套 bus/Gateway，
  聊天逻辑 100% 复用 CLI/飞书那套）；
- ``GET /`` 返回单页前端（webui/index.html）；``WS /ws`` 做实时聊天；
- ``GET/POST /api/config`` 让网页读写本实例配置（写回该文件夹的 config.json）；
- 多会话命令（/new /sessions /switch /clear）与 CLI/飞书对称；
- 每个浏览器连接分配独立 conn_id，作为会话键的一部分（web:<conn_id>:<序号>），
  因此多个标签页/多个用户互不干扰；
- 开放免登录：不校验密码，仅靠 conn_id 区分连接（局域网信任环境）。

注意：本渠道只是「把浏览器消息投进 bus」的另一个 Channel，与 CLI/飞书平级。
所谓「多实例」由用户把项目复制到不同文件夹、各自设不同 web_port 实现，本文件不负责。
"""

import asyncio
import hashlib
import json
import logging
import os
import threading
import uuid

from aiohttp import web

from bus.queue import InboundMessage, OutboundMessage
from channels.base import Channel
from voice.asr.base import ASRError
from voice.tts.base import TTSError

logger = logging.getLogger("nanoclaw.web")

# 所有 HTTP 响应都禁止缓存，确保浏览器每次都拉取最新前端（避免流式上线后
# 旧页面把 {"event":...} 帧当纯文本显示成「一堆 JSON」的缓存陷阱）。
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}

# 浏览器录音由本地 ASR 服务即时转写，不落盘。服务通常提供自己的上限；此值
# 仅在注入的兼容服务没有声明 max_audio_bytes 时作为安全回退。
_DEFAULT_ASR_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# 网页可编辑的配置字段白名单（GET 返回、POST 接受均限定在此范围内）
_CONFIG_FIELDS = (
    "api_key", "base_url", "model", "subagent_model", "workspace", "timezone",
    "max_iterations", "identity_file", "feishu_app_id", "feishu_app_secret",
    "web_host", "web_port", "turn_timeout_sec",
    "multimodal_model", "base_model_multimodal",
    "image_gen_model",
)


class WebChannel(Channel):
    """网页渠道（name 固定为 ``"web"``），在后台线程跑 aiohttp 服务。"""

    def __init__(self, name: str, bus, host: str, port: int, config, config_path: str,
                 session_manager=None, image_store=None, asr_service=None,
                 tts_service=None) -> None:
        super().__init__(name=name, bus=bus)
        self.host = host
        self.port = port
        self.config = config              # 共享的 NanoClawConfig 实例（网页可就地修改）
        self.config_path = config_path    # 本实例 config.json 路径（用于持久化）
        self.session_manager = session_manager  # 会话持久化管理器（侧边栏读写用，可空）
        self.image_store = image_store    # 图片存储（落盘/解析/清理），可空
        # 语音转写服务；由 composition root 注入。None 表示本实例未启用 ASR。
        # 服务属于主 asyncio loop，不能直接在本文件的 aiohttp 后台 loop 调用。
        self.asr_service = asr_service
        # 文字转语音服务同样属于主 asyncio loop。None 表示 TTS 未启用；
        # 这不会影响既有文本 WebSocket 聊天。
        self.tts_service = tts_service
        self._loop = None                 # 网关主事件循环（跨线程投递用）
        self._web_loop = None             # aiohttp 所在事件循环（后台线程）
        self._thread = None               # 后台守护线程
        self._conns: dict = {}            # conn_id -> WebSocket
        self._sessions: dict = {}         # conn_id -> {"seq": int, "current_key": str}
        self._lock = threading.Lock()     # 保护 _conns / _sessions 的跨线程访问
        self._clear_callback = None       # 清空历史回调（/clear 命令，同 CLI/飞书）
        self._index_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "webui", "index.html",
        )

    def _read_index(self) -> str:
        """读取前端页面；缺失时降级为提示文本（不影响服务启动）。"""
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取前端页面失败，使用降级文本：%s", exc)
            return "<html><body><h1>NanoClaw</h1><p>webui/index.html 未找到。</p></body></html>"

    def _page_version(self) -> str:
        """用页面内容哈希做版本号；文件一变版本就变，前端据此自愈刷新。"""
        return hashlib.sha256(self._read_index().encode("utf-8")).hexdigest()[:12]

    def _render_index(self) -> str:
        """每次请求都重新读最新页面，并把版本号注入占位符（避免启动期缓存旧页）。"""
        html = self._read_index()
        return html.replace("__NC_PAGE_VERSION__", self._page_version())

    async def start(self) -> None:
        """捕获网关主事件循环，在后台线程启动 aiohttp 服务（非阻塞）。"""
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run_server, name="web", daemon=True)
        self._thread.start()
        print(f"（网页渠道已启动｜监听 http://{self.host}:{self.port}）")

    def _run_server(self) -> None:
        """在独立线程里跑 aiohttp（自带事件循环），与主事件循环解耦。"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._web_loop = loop

            # client_max_size：默认仅 1MB，上传照片会 413；放宽到 20MB
            app = web.Application(client_max_size=20 * 1024 * 1024)
            app.router.add_get("/", self._handle_index)
            app.router.add_get("/ws", self._handle_ws)
            app.router.add_post("/upload", self._handle_upload)
            app.router.add_post("/api/asr", self._handle_asr)
            app.router.add_post("/api/tts", self._handle_tts)
            app.router.add_get("/image", self._handle_get_image)
            app.router.add_get("/api/config", self._handle_get_config)
            app.router.add_post("/api/config", self._handle_post_config)
            # 会话侧边栏：列举 / 读取 / 删除（仅网页会话 web:*）
            app.router.add_get("/api/sessions", self._handle_list_sessions)
            app.router.add_get("/api/session", self._handle_get_session)
            app.router.add_delete("/api/session", self._handle_delete_session)

            runner = web.AppRunner(app)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, self.host, self.port)
            loop.run_until_complete(site.start())
            loop.run_forever()
        except Exception as exc:  # noqa: BLE001
            logger.exception("网页服务启动/运行异常：%s", exc)

    # —— HTTP 路由 ——
    async def _handle_index(self, request) -> web.Response:
        # 每次都读最新文件：改完前端无需重启即可生效，也避免启动期缓存旧页
        return web.Response(
            text=self._render_index(), content_type="text/html", headers=_NO_CACHE
        )

    async def _handle_get_config(self, request) -> web.Response:
        data = {k: getattr(self.config, k, None) for k in _CONFIG_FIELDS}
        return web.json_response(data, headers=_NO_CACHE)

    async def _handle_post_config(self, request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "请求体不是合法 JSON"}, status=400)

        changed = {}
        for k in _CONFIG_FIELDS:
            if k not in body:
                continue
            val = body[k]
            # 数值字段做类型归整，避免写脏数据
            if k in ("web_port", "max_iterations", "turn_timeout_sec"):
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    continue
            if k == "timezone":
                try:
                    from config import validate_iana_timezone
                    val = validate_iana_timezone(val)
                except ValueError as exc:
                    return web.json_response(
                        {"ok": False, "error": str(exc)}, status=400
                    )
            setattr(self.config, k, val)
            changed[k] = val

        # 持久化到本实例 config.json（与运行时对象同源）
        try:
            from config import save_config
            save_config(self.config, self.config_path)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"保存到 {self.config_path} 失败：{exc}"},
                status=500,
            )
        return web.json_response({
            "ok": True,
            "changed": changed,
            "note": "模型/人设等会在新会话使用；已在进行的会话保持原状。修改 web_host/web_port/timezone、MCP、Skill 或工具相关配置需重启本实例生效。",
        })

    @staticmethod
    def _valid_key(key: str) -> bool:
        """会话标识合法性校验：防止 key 携带路径分隔符/..，拼出穿越路径。"""
        return bool(key) and ":" in key and "/" not in key and "\\" not in key and ".." not in key

    # —— 图片上传（供网页发送图片，后端落盘并返回 image_id）——
    async def _handle_upload(self, request) -> web.Response:
        if self.image_store is None:
            return web.json_response(
                {"ok": False, "error": "图片存储未就绪"}, status=500
            )
        # key 为完整会话标识（含 web: 前缀），用于定位图片落盘目录
        key = request.query.get("key", "")
        if not self._valid_key(key):
            return web.json_response(
                {"ok": False, "error": "缺少合法的 key 参数（应为完整会话标识）"}, status=400
            )
        try:
            data = await request.post()
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"解析上传失败：{exc}"}, status=400)
        f = data.get("file")
        if f is None:
            return web.json_response({"ok": False, "error": "未收到文件字段 file"}, status=400)
        try:
            raw = f.file.read()
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"读取文件失败：{exc}"}, status=500)
        if not raw:
            return web.json_response({"ok": False, "error": "文件为空"}, status=400)
        filename = getattr(f, "filename", "") or "image.png"
        ext = os.path.splitext(filename)[1].lstrip(".") or "png"
        mime = getattr(f, "content_type", None) or "image/png"
        try:
            ref = self.image_store.save(key, raw, ext, mime)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"保存失败：{exc}"}, status=500)
        return web.json_response(
            {"ok": True, "image_id": ref.id, "mime": ref.mime}, headers=_NO_CACHE
        )

    @staticmethod
    def _asr_error(code: str, message: str, status: int) -> web.Response:
        """返回稳定的 ASR 错误结构，方便前端给出明确提示。"""
        return web.json_response(
            {"ok": False, "error": {"code": code, "message": message}},
            status=status,
            headers=_NO_CACHE,
        )

    async def _handle_asr(self, request) -> web.Response:
        """接收一段浏览器录音并在主事件循环调用本地 ASR 服务。

        本接口只返回转写文本；前端确认成功后仍通过既有纯文本 WebSocket
        入口发送消息，避免音频二进制进入 Bus、会话或图片存储。
        """
        if self.asr_service is None:
            return self._asr_error("asr_unavailable", "本实例尚未配置语音转写服务。", 503)
        if self._loop is None:
            return self._asr_error("asr_unavailable", "语音转写服务尚未启动。", 503)

        try:
            data = await request.post()
        except Exception as exc:  # noqa: BLE001
            return self._asr_error("invalid_upload", f"解析录音上传失败：{exc}", 400)
        upload = data.get("file")
        if upload is None:
            return self._asr_error("missing_file", "未收到录音文件字段 file。", 400)
        try:
            raw = upload.file.read()
        except Exception as exc:  # noqa: BLE001
            return self._asr_error("invalid_upload", f"读取录音文件失败：{exc}", 400)
        if not raw:
            return self._asr_error("empty_file", "录音文件为空，请重新录制。", 400)
        max_audio_bytes = getattr(self.asr_service, "max_audio_bytes", _DEFAULT_ASR_MAX_UPLOAD_BYTES)
        try:
            max_audio_bytes = int(max_audio_bytes)
        except (TypeError, ValueError):
            max_audio_bytes = _DEFAULT_ASR_MAX_UPLOAD_BYTES
        if max_audio_bytes <= 0:
            max_audio_bytes = _DEFAULT_ASR_MAX_UPLOAD_BYTES
        if len(raw) > max_audio_bytes:
            return self._asr_error(
                "file_too_large",
                f"录音文件超过 {max_audio_bytes // (1024 * 1024)} MB 限制。",
                413,
            )

        filename = getattr(upload, "filename", "") or "recording.webm"
        media_type = getattr(upload, "content_type", None) or "application/octet-stream"
        future = asyncio.run_coroutine_threadsafe(
            self.asr_service.transcribe(raw, filename=filename, media_type=media_type),
            self._loop,
        )
        try:
            result = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - service owns detailed failure semantics
            logger.warning("语音转写失败：%s", exc)
            if isinstance(exc, ASRError):
                message = str(exc) or "语音转写失败，请重试。"
            else:
                message = "语音转写服务暂时不可用，请稍后重试。"
            return self._asr_error("asr_failed", message, 422)

        if not hasattr(result, "text"):
            logger.warning("语音转写服务返回了无效结果：%r", result)
            return self._asr_error("asr_failed", "语音转写服务返回了无效结果。", 422)
        text = str(result.text or "").strip()
        if not text:
            return self._asr_error("empty_transcript", "没有识别到可发送的文字，请重新录制。", 422)
        return web.json_response({"ok": True, "text": text}, headers=_NO_CACHE)

    @staticmethod
    def _tts_error(message: str, status: int) -> web.Response:
        """返回不含上游细节的 TTS 错误，确保聊天不依赖语音服务。"""
        return web.json_response(
            {"ok": False, "error": {"code": "tts_failed", "message": message}},
            status=status,
            headers=_NO_CACHE,
        )

    async def _handle_tts(self, request) -> web.Response:
        """把一段回复片段交给可选 TTS 服务，返回临时音频字节。

        此端点不进入 MessageBus、不写会话；语音失败只影响本次播放请求。
        """
        if self.tts_service is None:
            return self._tts_error("本实例尚未配置文字转语音服务。", 503)
        if self._loop is None:
            return self._tts_error("文字转语音服务尚未启动。", 503)

        try:
            body = await request.json()
        except Exception:
            return self._tts_error("请求体不是合法 JSON。", 400)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            return self._tts_error("请求体必须包含文本字段 text。", 400)
        text = body["text"].strip()
        if not text:
            return self._tts_error("朗读文本不能为空。", 400)

        max_text_chars = getattr(self.tts_service, "max_text_chars", None)
        try:
            max_text_chars = int(max_text_chars)
        except (TypeError, ValueError):
            max_text_chars = None
        if max_text_chars is not None and max_text_chars > 0 and len(text) > max_text_chars:
            return self._tts_error("朗读文本超过允许长度。", 413)

        future = asyncio.run_coroutine_threadsafe(
            self.tts_service.synthesize(text), self._loop,
        )
        try:
            result = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except TTSError as exc:
            logger.warning("文字转语音失败：%s", exc)
            return self._tts_error(str(exc) or "文字转语音失败，请稍后重试。", 422)
        except Exception:  # noqa: BLE001 - 不暴露上游服务/网络异常细节
            logger.warning("文字转语音服务暂时不可用", exc_info=True)
            return self._tts_error("文字转语音服务暂时不可用，请稍后重试。", 422)

        audio = getattr(result, "audio", None)
        if not isinstance(audio, bytes) or not audio:
            logger.warning("文字转语音服务返回了无效音频结果")
            return self._tts_error("文字转语音服务返回了无效音频结果。", 422)
        # 第一版 edge-tts 的输出契约固定为 MP3；不把 Provider 返回的任意字符串
        # 直接写入响应头，避免未来第三方 Provider 注入无效 Content-Type。
        return web.Response(body=audio, content_type="audio/mpeg", headers=_NO_CACHE)

    # —— 图片回显（前端缩略图 / 历史回放用）——
    async def _handle_get_image(self, request) -> web.Response:
        if self.image_store is None:
            return web.Response(status=404)
        key = request.query.get("key", "")
        iid = request.query.get("id", "")
        # 仅允许访问本渠道会话的图片；key/id 均校验，防路径穿越
        if not key.startswith(f"{self.name}:") or not self._valid_key(key):
            return web.Response(status=400)
        if not iid or not iid.isalnum():
            return web.Response(status=400)
        ref = self.image_store.resolve(key, iid)
        if ref is None or not os.path.exists(ref.path):
            return web.Response(status=404)
        try:
            with open(ref.path, "rb") as f:
                data = f.read()
        except Exception:  # noqa: BLE001
            return web.Response(status=500)
        return web.Response(
            body=data,
            content_type=ref.mime or "application/octet-stream",
            headers=_NO_CACHE,
        )

    # —— 会话侧边栏 API（仅本渠道会话，前缀 web:）——
    async def _handle_list_sessions(self, request) -> web.Response:
        if self.session_manager is None:
            return web.json_response({"sessions": []})
        items = self.session_manager.list_sessions_detailed(prefix=f"{self.name}:")
        return web.json_response({"sessions": items}, headers=_NO_CACHE)

    async def _handle_get_session(self, request) -> web.Response:
        key = request.query.get("key", "")
        if not key or not key.startswith(f"{self.name}:"):
            return web.json_response({"ok": False, "error": "会话标识非法"}, status=400)
        if self.session_manager is None:
            return web.json_response({"ok": False, "error": "会话管理器未就绪"}, status=500)
        messages = self.session_manager.get_session_messages(key)
        return web.json_response({"key": key, "messages": messages}, headers=_NO_CACHE)

    async def _handle_delete_session(self, request) -> web.Response:
        key = request.query.get("key", "")
        if not key or not key.startswith(f"{self.name}:"):
            return web.json_response({"ok": False, "error": "会话标识非法"}, status=400)
        # 若有活跃连接正使用该会话，重置其当前会话为新会话（不能持锁调用
        # _new_key，因为它内部会再次取锁，会死锁）。注意存储的是本地标识，
        # 故与完整 key 比较时要补回渠道前缀。
        affected: list = []
        with self._lock:
            for cid, st in list(self._sessions.items()):
                if f"{self.name}:{st.get('current_key')}" == key:
                    affected.append(cid)
        for cid in affected:
            new_key = self._new_key(cid)
            with self._lock:
                self._sessions[cid]["current_key"] = new_key
        if self.session_manager is not None:
            self.session_manager.clear(key)
        if self.image_store is not None:
            self.image_store.clear(key)
        return web.json_response({"ok": True})

    # —— WebSocket 聊天 ——
    async def _handle_ws(self, request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        conn_id = f"ws-{uuid.uuid4().hex[:12]}"

        # 版本握手：把当前页面版本号随 hello 事件发给前端，前端据此判断自己
        # 是否为过期缓存页，若是则自动强制刷新（自愈，避免「旧页面把 JSON 当文本」）
        try:
            await self._ws_send_json(ws, {"type": "hello", "version": self._page_version()})
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._conns[conn_id] = ws
        # 把本连接的初始会话标识推给前端（上传图片时需要携带完整 key）。
        # 之后 new/open 都会再发 session_changed，前端始终持有最新 key。
        try:
            st0 = self._session_state(conn_id)
            await self._ws_send_json(
                ws, {"type": "session_changed", "key": f"{self.name}:{st0['current_key']}"}
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                        break
                    continue
                text = msg.data.strip()
                if not text:
                    continue

                try:
                    # 每条消息独立解析：obj 必须在循环体内重置，
                    # 否则纯文本消息会误用上一条 JSON 消息的残留值
                    obj = None
                    # 侧边栏控制消息：{"ctl":true,"type":"new"|"open","key":...}
                    # 仅当显式带 ctl 标记才走控制分支，避免误伤用户正常聊天文本。
                    if text.startswith("{"):
                        try:
                            obj = json.loads(text)
                        except Exception:
                            obj = None
                        if obj and obj.get("ctl") is True:
                            ctype = obj.get("type")
                            st = self._session_state(conn_id)
                            if ctype == "new":
                                st["current_key"] = self._new_key(conn_id)
                                # 前端高亮用的是完整标识（含 web: 前缀），故这里补回
                                await self._ws_send_json(
                                    ws, {"type": "session_changed", "key": f"{self.name}:" + st["current_key"]}
                                )
                            elif ctype == "open":
                                key = obj.get("key")
                                if key and key.startswith(f"{self.name}:"):
                                    # 去掉渠道前缀存本地标识；Gateway 会再补 "web:"，
                                    # 与 CLI/飞书一致，避免文件名出现 web_web 重复前缀
                                    st["current_key"] = key[len(self.name) + 1:]
                                    await self._ws_send_json(
                                        ws, {"type": "session_changed", "key": key}
                                    )
                            elif ctype == "cancel":
                                # 网页「停止」：把取消请求投递到网关。复用入站消息
                                # 总线（与普通聊天消息同一通道、跨线程投递到主循环），
                                # 用 raw 里的 ctl/cancel 标记，网关在 _process_inbound
                                # 拦截后取消该会话在途回合，绝不进入聊天流程。
                                inbound = InboundMessage(
                                    channel=self.name,
                                    sender_id=st["current_key"],
                                    chat_id=conn_id,
                                    content="",
                                    raw={"ctl": "cancel"},
                                )
                                asyncio.run_coroutine_threadsafe(
                                    self.bus.publish_inbound(inbound), self._loop
                                )
                            # 控制消息不进入聊天流程
                            continue
                    # 聊天用 JSON（不带 ctl 标记）：{"text":..., "images":[id,...]}
                    # 用于网页发送带图片的消息；图片 id 来自 /upload 的返回。
                    if obj and ("text" in obj or "images" in obj):
                        st = self._session_state(conn_id)
                        text2 = obj.get("text", "") or ""
                        images = []
                        skey = f"{self.name}:{st['current_key']}"
                        for iid in (obj.get("images") or []):
                            if self.image_store is not None:
                                ref = self.image_store.resolve(skey, iid)
                                if ref is not None:
                                    images.append(ref)
                        inbound = InboundMessage(
                            channel=self.name,
                            sender_id=st["current_key"],
                            chat_id=conn_id,
                            content=text2,
                            images=images or None,
                            raw={"conn_id": conn_id},
                        )
                        asyncio.run_coroutine_threadsafe(
                            self.bus.publish_inbound(inbound), self._loop
                        )
                        continue

                    # 内置命令：命中则直接回复，不经过 Agent
                    if text.startswith("/"):
                        reply = self._handle_command(conn_id, text)
                        if reply is not None:
                            await self._ws_send(ws, reply)
                            continue

                    st = self._session_state(conn_id)
                    inbound = InboundMessage(
                        channel=self.name,
                        sender_id=st["current_key"],
                        chat_id=conn_id,
                        content=text,
                        raw={"conn_id": conn_id},
                    )
                    # 跨线程投递到网关主事件循环的 bus（不在此处 await）
                    asyncio.run_coroutine_threadsafe(
                        self.bus.publish_inbound(inbound), self._loop
                    )
                except Exception as exc:  # noqa: BLE001 - 不让单条消息击垮连接；打印真实异常并回传前端
                    import sys as _sys, traceback as _tb
                    _tb.print_exc()
                    print(f"[WS_HANDLER_ERROR] conn={conn_id}: {type(exc).__name__}: {exc}", file=_sys.stderr, flush=True)
                    try:
                        await self._ws_send(ws, f"⚠️ 处理你的消息时后端出错：{type(exc).__name__}: {exc}")
                    except Exception:
                        pass
        finally:
            with self._lock:
                self._conns.pop(conn_id, None)
                self._sessions.pop(conn_id, None)
        return ws

    # —— 多会话命令（与 CLI/飞书对称）——
    def _session_state(self, conn_id: str) -> dict:
        with self._lock:
            st = self._sessions.get(conn_id)
            if st is None:
                # current_key 仅存「本地标识」（形如 <conn_id>:<序号>，不含渠道前缀），
                # 与 CLI/飞书一致；Gateway 会再补 "web:" 前缀，避免文件名出现
                # web_web 这种重复前缀。
                st = {"seq": 0, "current_key": f"{conn_id}:0"}
                self._sessions[conn_id] = st
            return st

    def _new_key(self, conn_id: str) -> str:
        """为本连接生成一个新会话本地标识（形如 <conn_id>:<序号>，不含渠道前缀）。"""
        st = self._session_state(conn_id)
        st["seq"] += 1
        return f"{conn_id}:{st['seq']}"

    def _handle_command(self, conn_id: str, text: str):
        """解析网页内置命令，返回回复文本；非命令返回 None（交给 Agent）。"""
        parts = text.split()
        cmd = parts[0]
        if cmd not in ("/new", "/sessions", "/switch", "/clear", "/exit"):
            return None
        if cmd == "/exit":
            return "网页端常驻运行，无需退出。"

        st = self._session_state(conn_id)
        if cmd == "/new":
            st["current_key"] = self._new_key(conn_id)
            return f"已新建会话 #{st['seq']}"
        if cmd == "/sessions":
            return "请使用左侧「会话」侧边栏查看、切换与删除历史会话。"
        if cmd == "/switch":
            if len(parts) != 2 or not parts[1].isdigit():
                return "用法：/switch <会话序号>，例如 /switch 0"
            n = int(parts[1])
            if n < 0 or n > st["seq"]:
                return f"会话 #{n} 不存在（有效范围 0~{st['seq']}）"
            st["current_key"] = f"{conn_id}:{n}"
            return f"已切换到会话 #{n}"
        if cmd == "/clear":
            # clear_callback 按「完整 session_key」（含 web: 前缀）查找 Agent，需补回
            full_key = f"{self.name}:{st['current_key']}"
            if self._clear_callback is not None:
                self._clear_callback(full_key)
            return f"会话 {full_key} 历史已清空"
        return None

    async def _ws_send(self, ws, text: str) -> None:
        """把文本推到指定 WebSocket（在 web 事件循环内调用）。"""
        try:
            # 超长消息切片，避免单包过大
            if len(text) > 8000:
                for i in range(0, len(text), 8000):
                    await ws.send_str(text[i:i + 8000])
            else:
                await ws.send_str(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("网页 WS 发送失败：%s", exc)

    async def send(self, message: OutboundMessage) -> None:
        """由网关出站分发调用（网关主循环内）：把回包推回对应 WS 连接。

        若回包已被流式事件完整覆盖（``streamed=True``，网页渠道收到过
        thinking/token/done 事件），则直接跳过，避免与流式渲染重复显示。

        连接归属 web 事件循环，故用 run_coroutine_threadsafe 跨线程调度，
        再以 wrap_future 在主循环里 await 其结果。
        """
        if getattr(message, "streamed", False):
            return  # 流式事件已覆盖，避免重复

        conn_id = message.chat_id
        with self._lock:
            ws = self._conns.get(conn_id)
        if ws is None:
            logger.warning("网页回包找不到连接 %s，已丢弃", conn_id)
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._ws_send(ws, message.content or ""), self._web_loop
            )
            await asyncio.wrap_future(fut)
        except Exception as exc:  # noqa: BLE001
            logger.warning("网页回包投递失败：%s", exc)

    async def stream_event(self, conn_id: str, event: dict) -> None:
        """由网关 _dispatch_stream 调用：把一个流式事件（JSON）推给指定 WS 连接。

        事件可能是思考增量、最终回答增量、工具调用/结果、完成信号等，
        具体由前端按 ``event["type"]`` 渲染。连接归属 web 事件循环，故跨线程调度。
        """
        with self._lock:
            ws = self._conns.get(conn_id)
        if ws is None:
            # 连接已断开，丢弃该事件（不影响其他连接）
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._ws_send_json(ws, event), self._web_loop
            )
            await asyncio.wrap_future(fut)
        except Exception as exc:  # noqa: BLE001
            logger.warning("网页流式事件投递失败：%s", exc)

    async def _ws_send_json(self, ws, event: dict) -> None:
        """把一个事件 dict 以 JSON 推到指定 WebSocket（web 事件循环内调用）。"""
        try:
            await ws.send_str(json.dumps({"event": event}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("网页 WS JSON 发送失败：%s", exc)

    async def stop(self) -> None:
        """后台线程为守护线程，进程退出时自动结束；此处仅占位。"""
        pass
