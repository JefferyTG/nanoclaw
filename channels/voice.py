"""Voice 渠道：本地语音渠道（TASK-024 无音频骨架 → TASK-025 唤醒录音 ASR 闭环）。

本渠道是 NanoClaw 的第五个渠道，定位「本地对讲机」：短平快、无外部服务器
依赖。TASK-024 只做无音频骨架，证明「本地渠道」概念成立——

- 会话 key 用 ``voice:local:<seq>`` 多会话分片（sender_id=``local:<seq>``，
  Gateway 按 ``f"{channel}:{sender_id}"`` 推导出 ``voice:local:<seq>``）；
- 消息经 ``inject_text()`` 入站进 Agent，回复经 ``send()`` / ``_emit()`` 出站。

TASK-025 给渠道装上「耳朵」：可选注入 ``kws_detector``（KwsWakeDetector）与
``asr_service``（AudioTranscriptionService），``start()`` 进入唤醒监听循环——

    唤醒词命中 → 播放确认回应（甘雨音色，方案 B）→ 自动录音 record_sec 秒
    （内存 WAV） → ASR 转写 → ``inject_text()`` 进 Agent → 回复经 ``_emit()``
    出站

唤醒确认回应（2026-08-09 乖宝拍板方案 B）：唤醒后**先合成并播放**一条甘雨
回应（``voice.wake_replies`` 列表 + ``random.choice``，默认「哎，我在呢，你说
吧」），**播完才进入录音**——用户听到回应就知道小奈在，且录音不截用户的话。
``tts_service`` 为 None 或 ``wake_replies`` 为空/非 list 时跳过回应直接录音
（向后兼容既有行为）；合成/播放任一步失败降级为跳过回应继续录音，不阻塞唤醒
流程。回应播放由 ``voice/kws/player.py`` 完成（纯内存流转、播完才返回）。

- ``inject_text()`` 仍是唯一入站口（唤醒回调最终也调用它）；出站经 ``send()``
  下发——TASK-026 起 ``tts_service`` 就绪且文本不超 ``max_voice_chars`` 时，
  先把 Agent 回复合成甘雨语音并播放到系统默认输出，失败或超长降级回文字；
  文字兜底统一走 ``_emit`` 单一出口（``_reply_sink`` 或打印兜底）。
- 音频全程内存流转不落盘；转写文本才进 Agent。
- ``kws_detector`` / ``asr_service`` 任一为 None 时唤醒链路自动禁用，回退
  到 TASK-024 的空转行为（``test_start_idles_until_stop_event`` 保持通过）。

TASK-026 空闲自动分片 + 会话保留上限：voice 是长会话渠道，聊久了上下文
膨胀、翻旧账也乱，故引入「空闲分片」——距上次入站交互超过 ``idle_ttl_sec``
（默认 30 分钟）时，下一条正常入站消息自动开新会话（seq+1，旧会话保留可
``/switch`` 切回），提示语经 ``_emit`` 下发；同时用 ``max_sessions`` 限制
voice 渠道会话保留数量（默认最近 50 段，超出清理最老），真正删除动作由
main.py 注入的 ``session_pruner(seq)`` 回调完成（只清 voice 渠道，不动其它
渠道）。分片检查是惰性的：只在正常输入分支触发，内置命令（/clear /context
/new /sessions /switch）不触发分片但计入活动时间；时间判断用可注入的
``now_fn``（默认 ``time.time``），便于测试用假时钟推进。

TASK-027 连续对讲（第二步：渠道状态机）：把「单轮唤醒」升级为「连续对讲机」——

- 唤醒命中 → 播甘雨回应 → **进入连续对讲模式**（``_continuous``），此后全程
  不用再喊唤醒词：回复经 ``send()`` 播完自动调度下一轮「VAD 录音 → ASR →
  inject_text」，循环往复，像真对讲机；
- 录音改用 ``record_audio_vad``（TASK-027 第一步）：说完话停顿
  ``vad.silence_end_sec``（默认 1.2s）提前结束录音立即转写，不再干等满
  ``record_sec``（此时只是最长上限）；全程无人声判 ``is_silent``；
- 静默退出：连续对讲模式下每轮 ``is_silent=True`` 累计实际录音时长，一旦累计
  ≥ ``silence_timeout_sec``（默认 5s）→ 退出回待唤醒（``_emit`` 一条轻提示）；
  ``is_silent=False`` 则清零累计；
- [END] 结束语退出：``send()`` 收到含 ``[END]`` 标记的回复时**剥离标记**后正常
  播告别语（标记绝不出现在播报/文字里），播完退出连续对讲回待唤醒；纯文字
  兜底路径同样剥离并退出；剥离后为空则不播任何东西直接退出；
- 空闲分片配合：连续对讲进行中 ``_maybe_split_session`` 直接 return（不触发
  分片），每次 inject_text 照常 ``_bump_activity``；退出后分片逻辑恢复原样；
- 防重入：连续对讲模式下唤醒词再次命中沿用 ``_wake_in_progress`` 合并（在
  ``_on_wake`` 中追加 ``_continuous`` 判断）；``_schedule_next_listen`` 用
  ``self._listen_task`` 任务引用防重复启动，录音轮次内 try/finally 保证结束
  清引用（并发安全）。

多会话：以整数序号区分不同会话，对应 ``sender_id="local:<seq>"``，Gateway
据此推导出 session_key ``"voice:local:<seq>"``，每个序号对应一个独立会话。
内置命令 ``/new`` 开新会话、``/sessions`` 列表、``/switch <n>`` 切换；
``/clear`` 只清当前会话——与 CLI 渠道语义一致。
"""

import asyncio
import os
import io
import random
import re
import time
import wave

from channels.base import Channel
from bus.queue import InboundMessage, OutboundMessage
from voice.asr.base import ASRError
from voice.kws.errors import KwsError
from voice.kws.player import play_audio
from voice.kws.vad import record_audio_vad
from voice.media import MediaError


# —— TASK-027 补充：TTS 前文本清洗正则（剥 markdown / 删 emoji 与装饰符号 /
# 压缩连续标点；中文标点【】「」（）等 TTS 可正常处理，保留不动）——
# emoji 与符号块：主要 emoji、区域指示符（国旗）、箭头、几何形状、杂项符号/
# 装饰符号、补充箭头、emoji 变体选择符（U+FE0F）、ZWJ（U+200D 连接符）。
# 注意：正则用 raw string + 单反斜杠，由 re 引擎解释元字符/反向引用。
_TTS_EMOJI_BLOCK_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U000025A0-\U000025FF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\uFE0F"
    "\u200D"
    "]+"
)
# markdown：链接 [text](url) → text；**加粗** / __加粗__ → 加粗；`code` → code
_TTS_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TTS_MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_TTS_MD_CODE_RE = re.compile(r"`([^`]*)`")
# 行首标记：标题 #、列表 -/*/+、引用 >、数字列表 1. / 1、/ 1)（re.M 按行处理）
_TTS_MD_LINE_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|>\s?|\d+[.、)][ \t]+)",
    re.M,
)
# 额外装饰符号（个别不在上述 Unicode 块内的）与连续标点压缩
_TTS_EXTRA_SYMBOL_RE = re.compile(r"[★☆◆◇●○◎△▲※〓™®©]")
_TTS_REPEAT_PUNCT_RE = re.compile(r"([!！?？。…~～])\1+")
_TTS_SPACES_RE = re.compile(r"[ \t\u3000]+")


def _normalize_float(value, default: float) -> float:
    """参数归一化为数字；None / 非数字 / NaN 一律回退默认，保证不崩。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return v


def _normalize_bool(value, default: bool) -> bool:
    """参数归一化为布尔；None / 无法识别的值一律回退默认，保证不崩。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return default


class VoiceChannel(Channel):
    """本地语音渠道（name 固定为 "voice"，bus 由构造参数传入）。

    ``inject_text()`` 是唯一入站口（TASK-025 的唤醒录音/ASR 回调也调用它）；
    出站经 ``send()`` 下发（TASK-026：TTS 合成播放，失败降级文字），文字兜底
    统一走 ``_emit``。``start()`` 在注入 ``kws_detector`` 时进入唤醒监听循环，
    否则空转等待 ``_stop_event``（TASK-024 行为不变）。

    TASK-027 连续对讲：唤醒后进入连续对讲模式，``send()`` 播完回复自动调度
    下一轮 VAD 录音监听；``[END]`` 结束语 / 静默超时退出回待唤醒。构造参数
    新增 ``record_delay_sec``（回复播完到开录的间隔）、``silence_timeout_sec``
    （静默退出阈值）与可选 ``vad_params``（传给 record_audio_vad 的覆盖参数）。

    TASK-028 播放防炸麦：构造参数新增可选 ``playback_params``（传给
    ``voice/kws/player.play_audio`` 的 DSP 覆盖参数，键名与
    ``normalize_playback_pcm`` 一致：target_peak / max_gain_db / soft_clip）。
    ``send()`` 与唤醒回应播放都把它透传给 ``play_audio``；渠道内按白名单
    清洗（类型归一、非法回退默认），None / 非 dict 用 DSP 内置默认。
    """

    # 连续对讲结束语标记：模型回复带此标记 → 剥离后播告别语并退出连续对讲
    _END_MARKER = "[END]"

    def __init__(
        self,
        bus,
        *,
        kws_detector=None,
        asr_service=None,
        record_sec: float = 8.0,
        kws_device=None,
        tts_service=None,
        wake_replies=None,
        max_voice_chars: int = 300,
        idle_ttl_sec: float = 1800,
        now_fn=None,
        max_sessions: int = 50,
        session_pruner=None,
        record_delay_sec: float = 0.5,
        silence_timeout_sec: float = 5.0,
        vad_params: dict | None = None,
        playback_params: dict | None = None,
        wake_replies_dir: str | None = None,
    ) -> None:
        super().__init__(name="voice", bus=bus)
        # 停止事件：start() 的监听/空转循环等待它，stop() 置位唤醒
        self._stop_event = asyncio.Event()
        # TASK-025 唤醒闭环注入（任一无则链路自动禁用，回退空转）
        self._kws_detector = kws_detector
        self._asr_service = asr_service
        self._record_sec = float(record_sec) if record_sec else 8.0
        self._kws_device = kws_device  # 录音设备（None=系统默认输入，与 KWS 同源）
        # TASK-025 方案 B：唤醒确认回应（tts_service None / wake_replies 非
        # list 或空 → 跳过回应直接录音，向后兼容既有行为）
        self._tts_service = tts_service
        self._wake_replies = wake_replies
        # TASK-029 唤醒回应本地缓存随机播放：wake_replies_dir 指向放预录 WAV 的
        # 目录（默认 workspace/voice/wake_replies/）；_wake_audio_cache 为 None 时
        # 未加载，[] 为已加载但目录空/不存在。缓存非空时 _play_wake_reply 优先随机
        # 播一条缓存 WAV（不调云端 TTS）；缓存为空回退现有云端合成路径。
        self._wake_replies_dir = (
            wake_replies_dir
            if wake_replies_dir is not None
            else "workspace/voice/wake_replies/"
        )
        self._wake_audio_cache: list[bytes] | None = None
        # TASK-026 回复播放文本上限：>0 时超出直接回文字不合成播放；
        # ≤0 表示不截断（0 与负数都视为「不限」）。
        self._max_voice_chars = (
            int(max_voice_chars) if max_voice_chars is not None else 300
        )
        # 唤醒防抖状态：动作进行中时新唤醒事件合并
        self._wake_in_progress: bool = False
        self._coalesced_wakes: int = 0
        self._wake_count: int = 0

        # —— TASK-027 连续对讲状态机 ——
        # _continuous：连续对讲进行中标志（True 期间不触发空闲分片、唤醒词再
        # 命中被合并、send() 播完自动调度下一轮录音）。
        self._continuous: bool = False
        # 静默累计时长：连续对讲模式下每轮 is_silent=True 的录音实际时长累加，
        # 一旦 ≥ silence_timeout_sec → 退出回待唤醒；有人声则清零。
        self._silence_accum_sec: float = 0.0
        # 当前进行中的一轮录音监听任务引用（防重入：非 None 且未结束则不重复
        # create_task；录音轮次结束经 try/finally 清引用）。
        self._listen_task: asyncio.Task | None = None
        # record_delay_sec：回复播完到下一轮开录的间隔（避免截到小奈话音尾巴）
        self._record_delay_sec = max(
            _normalize_float(record_delay_sec, 0.5), 0.0
        )
        # silence_timeout_sec：静默退出阈值；>0 启用，≤0 表示不因静默退出
        # （仅 [END] 能退出连续对讲）。
        self._silence_timeout_sec = _normalize_float(silence_timeout_sec, 5.0)
        # vad_params：传给 record_audio_vad 的覆盖参数白名单（渠道级参数
        # max_duration_sec / device 由渠道统一传入，避免冲突）。
        self._vad_params = self._sanitize_vad_params(vad_params)
        # playback_params：传给 play_audio 的播放 DSP 覆盖参数白名单
        # （TASK-028 播放防炸麦，键名与 normalize_playback_pcm 一致；
        # 渠道级参数由渠道统一传入，避免冲突）。None/非 dict → {}（DSP 默认）。
        self._playback_params = self._sanitize_playback_params(playback_params)

        # 以下属性由 main.py 或测试注入，VoiceChannel 自身不依赖 Agent
        self._clear_callback = None   # 清空历史回调（/clear 命令调用）
        self._context_callback = None  # 上下文占用查询回调（/context 命令调用）
        self._reply_sink = None        # 出站回复回调（默认 None 时打印兜底）

        # —— 多会话状态 ——
        # 会话以整数序号标识，sender_id="local:<seq>" -> Gateway 的 session_key
        # 为 "voice:local:<seq>"。_session_seq 是已创建会话数（也作最新序号），
        # _current_session 是当前活动会话序号。
        self._session_seq: int = 0
        self._current_session: int = 0

        # —— TASK-026 空闲自动分片 + 会话保留上限 ——
        # idle_ttl_sec：距上次活动超过该秒数（>0 时启用）→ 下一条正常入站消息
        # 自动开新会话（seq+1，旧会话保留）；≤0 表示禁用自动分片。
        self._idle_ttl_sec = (
            float(idle_ttl_sec) if idle_ttl_sec is not None else 1800.0
        )
        # now_fn：可注入时钟函数（默认 time.time），返回 unix 秒；测试用假时钟
        # 推进时间验证空闲分片，生产走真实时钟。
        self._now = now_fn if now_fn is not None else time.time
        # max_sessions：voice 渠道会话保留上限（现存 0.._session_seq 共
        # _session_seq + 1 个）；>0 时每次创建新会话后超限清理最老 voice 会话；
        # ≤0 表示不限制。
        self._max_sessions = (
            int(max_sessions) if max_sessions is not None else 50
        )
        # session_pruner：真正删除落盘会话的回调 session_pruner(seq)，由 main.py
        # 注入（只清 voice 渠道）；voice 渠道只决定删哪些 seq，不碰文件系统。
        self._session_pruner = session_pruner
        # 上次活动时间（unix 秒）：构造时初始化，之后每次 inject_text 交互更新。
        self._last_activity_ts: float = self._now()

    # —— TASK-027 构造辅助 ——

    @staticmethod
    def _sanitize_vad_params(vad_params) -> dict:
        """清洗 vad_params：只透传可覆盖参数，渠道级参数（max_duration_sec /
        device）由渠道统一传入，避免关键字冲突。"""
        if not isinstance(vad_params, dict):
            return {}
        return {
            key: value
            for key, value in vad_params.items()
            if key not in ("max_duration_sec", "device")
        }

    @staticmethod
    def _sanitize_playback_params(playback_params) -> dict:
        """清洗 playback_params：只透传 normalize_playback_pcm 认识的三个键
        （target_peak / max_gain_db / soft_clip），值做基本类型归一
        （float / float / bool，非法回退默认），未知键丢弃；非 dict 输入返回
        {}（DSP 用内置默认）。"""
        if not isinstance(playback_params, dict):
            return {}
        out: dict = {}
        tp = playback_params.get("target_peak")
        if tp is not None:
            tp = _normalize_float(tp, 0.89)
            if tp > 0:  # 目标峰值必须为正（≤0 视为非法，回退默认不传）
                out["target_peak"] = tp
        mg = playback_params.get("max_gain_db")
        if mg is not None:
            out["max_gain_db"] = _normalize_float(mg, 0.0)
        sc = playback_params.get("soft_clip")
        if sc is not None:
            out["soft_clip"] = _normalize_bool(sc, True)
        return out

    @staticmethod
    def _strip_end_marker(text: str) -> tuple[str, bool]:
        """剥离 [END] 标记：返回 (剥离后文本, 是否含标记)。

        [END] 是渠道内部协议标记，**绝不能**出现在播报/文字内容里——剥离要
        在 TTS 合成之前完成。标记可以出现在回复末尾或任意位置，全部移除。
        """
        if not text or VoiceChannel._END_MARKER not in text:
            return (text or "", False)
        return (text.replace(VoiceChannel._END_MARKER, "").strip(), True)

    @staticmethod
    def _sanitize_for_tts(text: str) -> str:
        """清洗待 TTS 合成的文本：剥 markdown、删 emoji/装饰符号、压缩连续标点。

        - markdown：``[文字](url)`` → 文字、``**加粗**`` → 加粗、`` `code` `` →
          code、行首 ``# 标题`` / ``- 列表`` / ``> 引用`` / ``1. 条目`` 标记剥离；
        - emoji / 装饰 / 箭头 / 几何符号块整段删除（含变体选择符与 ZWJ）；
        - 连续标点（！！！/？？/。。。）压缩为单个；
        - 多余空白压缩、去首尾空白。中文标点【】「」（）等 TTS 可正常读，保留。
        """
        if not text:
            return text or ""
        text = _TTS_MD_LINK_RE.sub(r"\1", text)
        text = _TTS_MD_BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)
        text = _TTS_MD_CODE_RE.sub(r"\1", text)
        text = _TTS_MD_LINE_RE.sub("", text)
        text = _TTS_EMOJI_BLOCK_RE.sub("", text)
        text = _TTS_EXTRA_SYMBOL_RE.sub("", text)
        text = _TTS_REPEAT_PUNCT_RE.sub(r"\1", text)
        text = _TTS_SPACES_RE.sub(" ", text)
        return text.strip()

    @staticmethod
    def _wav_duration_sec(wav: bytes) -> float:
        """解析 WAV bytes 得到实际录音时长（秒）。解析失败回退 0。"""
        try:
            with wave.open(io.BytesIO(wav), "rb") as f:
                rate = f.getframerate()
                if rate <= 0:
                    return 0.0
                return f.getnframes() / rate
        except Exception:  # noqa: BLE001 - 解析失败只回退 0，不崩
            return 0.0

    # —— TASK-027 连续对讲状态机 ——

    def _enter_continuous(self) -> None:
        """进入连续对讲模式：置标志、清零静默累计（幂等）。"""
        self._continuous = True
        self._silence_accum_sec = 0.0

    def _exit_continuous(self) -> None:
        """退出连续对讲模式回待唤醒（幂等）。

        清除标志与静默累计；若仍有一轮监听任务在跑（防御性，正常时序下
        send() 收到回复时上一轮已结束）则取消它——不取消当前任务自身
        （静默超时等内部路径会自然 return）。
        """
        if not self._continuous:
            return
        self._continuous = False
        self._silence_accum_sec = 0.0
        print("[voice] 🔌 退出连续对讲，回待唤醒")
        task = self._listen_task
        self._listen_task = None
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()

    def _schedule_next_listen(self) -> None:
        """连续对讲模式下启动一轮录音监听（防重入）。

        - ``_continuous`` 为 False 时不动作（已退出/从未进入）；
        - ``self._listen_task`` 非 None 且未结束 → 已有轮次在跑，不重复创建。
        """
        if not self._continuous:
            return
        task = self._listen_task
        if task is not None and not task.done():
            return
        self._listen_task = asyncio.create_task(self._listen_round())

    async def _listen_round(self) -> None:
        """连续对讲的一轮监听：等 record_delay → VAD 录音 → 静默判定 → ASR →
        inject_text。

        - 录音用 ``record_audio_vad``（record_sec 为最长上限，说话停顿提前
          结束）；``is_silent=True``（或未采到帧）→ 累计实际录音时长，超
          ``silence_timeout_sec`` 退出，否则调度下一轮继续听；
        - 有人声 → 清零静默累计；ASR 转写成功 → ``inject_text`` 进 Agent，
          回复经 ``send()`` 播完后由 send() 调度下一轮；
        - 转写失败/没听清 → 调度下一轮继续听；
        - 录音失败（KwsError）→ ``_emit`` 友好提示并退出连续对讲回待唤醒；
        - 结束经 try/finally 清 ``_listen_task`` 引用（仅当引用仍指向本任务，
          避免覆盖自调度创建的新任务引用，保证并发安全）。
        """
        try:
            await asyncio.sleep(self._record_delay_sec)
            if not self._continuous:
                return

            # —— VAD 录音（record_sec 为最长上限，静默可提前结束）——
            try:
                wav, is_silent = await record_audio_vad(
                    self._record_sec,
                    device=self._kws_device,
                    **self._vad_params,
                )
            except KwsError as exc:
                self._emit(f"📛 录音失败：{exc.message}")
                self._exit_continuous()
                return
            except Exception as exc:  # noqa: BLE001 - 录音异常只提示不扩散
                self._emit(f"📛 录音异常：{exc}")
                self._exit_continuous()
                return

            # —— 静默判定：累计静默时长，超时退出；未超时继续听 ——
            if is_silent or not wav:
                # 全程无人声（或短促噪音不足 min_voice_sec）：把该轮实际录音
                # 时长计入累计；未采到帧（wav 为 None）时按整轮 record_sec 计
                # （避免 0 时长死循环）。
                dur = self._wav_duration_sec(wav) if wav else self._record_sec
                self._silence_accum_sec += dur
                if (
                    self._silence_timeout_sec > 0
                    and self._silence_accum_sec >= self._silence_timeout_sec
                ):
                    self._emit(
                        "不说话的话我先待机啦，喊「小奈小奈」叫我"
                    )
                    self._exit_continuous()
                    return
                self._listen_task = None  # 先清引用再自调度，避免防重入误拦
                self._schedule_next_listen()
                return

            # 检测到有人声 → 清零静默累计
            self._silence_accum_sec = 0.0

            # —— ASR 转写 → inject_text（回复经 send() 播完后由 send() 调度
            # 下一轮）——
            text = await self._transcribe_round(wav)
            if text is None:
                self._listen_task = None  # 先清引用再自调度，避免防重入误拦
                self._schedule_next_listen()
                return
            print(f"[voice] 🗣️ 乖宝说：{text}")
            await self.inject_text(text)
        finally:
            if self._listen_task is asyncio.current_task():
                self._listen_task = None

    async def _transcribe_round(self, wav: bytes) -> str | None:
        """转写一轮录音；失败/空文本走 ``_emit`` 友好提示并返回 None。"""
        try:
            result = await self._asr_service.transcribe(
                wav, filename="voice_round.wav", media_type="audio/wav"
            )
        except (ASRError, MediaError) as exc:
            self._emit(
                f"📛 没听清，再说一次？（{getattr(exc, 'message', exc)}）"
            )
            return None
        except Exception as exc:  # noqa: BLE001 - 转写异常只提示不扩散
            self._emit(f"📛 转写异常：{exc}")
            return None

        text = ""
        if result is not None:
            text = str(getattr(result, "text", "") or "").strip()
        if not text:
            self._emit("📛 没听清，再说一次？")
            return None
        return text

    def _current_sender_id(self) -> str:
        """当前活动会话对应的 sender_id（Gateway 据此推导 session_key）。"""
        return f"local:{self._current_session}"

    def _emit(self, text: str) -> None:
        """统一文字兜底出口：优先交给注入的 ``_reply_sink``，否则打印兜底。

        任何出口路径都不抛异常：注入回调异常只降级为打印，打印本身也不应让
        出站分发循环崩溃。``send()`` 的 TTS 合成/播放失败也会降级回文字经
        本出口，保证回复不静默、渠道不崩溃。
        """
        if self._reply_sink is not None:
            try:
                self._reply_sink(text)
                return
            except Exception as exc:  # noqa: BLE001 - 注入回调异常只降级打印
                print(f"[voice] 出站回调异常，降级打印：{exc}")
        print(f"[voice] {text}")

    def _bump_activity(self) -> None:
        """记录一次交互活动：更新最近活动时间戳（供空闲分片判定）。"""
        self._last_activity_ts = self._now()

    def _create_session(self) -> None:
        """开新会话：seq+1 并切换过去（旧会话保留），随后清理超限老会话。

        与 ``/new`` 共用同一逻辑，空闲自动分片也复用本方法。新会话总是 seq+1
        后立即切换，因此当前会话必然是最新序号——``_prune_old_sessions`` 从最老
        序号开始清理，不会误删当前会话。
        """
        self._session_seq += 1
        self._current_session = self._session_seq
        self._prune_old_sessions()

    def _prune_old_sessions(self) -> None:
        """会话保留上限：现存会话超过 max_sessions 时从最老序号清到 ≤ 上限。

        - max_sessions ≤ 0（不限制）或 session_pruner 为 None（未注入真正删除
          回调）时直接返回，不崩；
        - 现存会话为 0.._session_seq 共 _session_seq + 1 个；超限时从序号 0
          开始逐个调用 session_pruner(seq)，直到剩余数量 ≤ max_sessions；
        - pruner 抛异常只降级打印警告，不阻断后续清理与分片/新建流程。
        """
        if self._max_sessions <= 0 or self._session_pruner is None:
            return
        excess = (self._session_seq + 1) - self._max_sessions
        if excess <= 0:
            return
        for seq in range(excess):
            try:
                self._session_pruner(seq)
            except Exception as exc:  # noqa: BLE001 - 清理回调异常只降级打印
                print(f"[voice] 清理老会话 #{seq} 失败：{exc}")

    def _maybe_split_session(self) -> None:
        """空闲自动分片（惰性检查）：距上次活动超过 idle_ttl_sec 时自动开新会话。

        - TASK-027 起：连续对讲进行中（``_continuous``）直接 return，对话活跃
          不分片；退出后逻辑恢复 TASK-026 原样；
        - idle_ttl_sec ≤ 0（禁用）或未超阈值时不动作；
        - 分片成功后与 ``/new`` 相同：seq+1 切换并清理超限老会话，同时 ``_emit``
          一条轻提示告知用户开了新话题；旧会话保留可 ``/switch`` 切回。
        """
        if self._continuous:
            return
        if self._idle_ttl_sec <= 0:
            return
        if self._now() - self._last_activity_ts <= self._idle_ttl_sec:
            return
        self._create_session()
        self._emit(f"⏱️ 聊了挺久，开个新话题啦（会话 #{self._current_session}）")

    async def inject_text(self, text: str) -> None:
        """公共注入入站口：处理内置命令或封装为 InboundMessage 投递进总线。

        TASK-025 的唤醒录音/ASR 回调将调用本方法；TASK-024 用于测试与手动验证。
        命令分支同步改状态、回执走 ``_emit``；正常消息才进 bus。
        """
        text = str(text).strip()
        if not text:
            return

        # 内置命令也算活动（任何交互都算），但不触发分片检查——避免用户查
        # /sessions、/switch 时被空闲分片切到新会话。分片检查只在正常输入分支做。
        if text.startswith("/"):
            self._bump_activity()

        # —— 内置命令（语义与 CLI 渠道一致，回执统一走 _emit）——
        if text == "/clear":
            if self._clear_callback is not None:
                self._clear_callback(f"voice:local:{self._current_session}")
            self._emit(f"🧹 当前会话 #{self._current_session} 历史已清空")
            return
        if text == "/context":
            # 直接从回调查询当前会话占用并回显，不经过模型。
            if self._context_callback is not None:
                reply = self._context_callback(
                    f"voice:local:{self._current_session}"
                )
                self._emit(f"📊 {reply}")
            else:
                self._emit("📊 当前实例未注入上下文占用回调。")
            return
        if text == "/new":
            # 新建一个空白会话并立即切换过去；旧会话保留（磁盘+缓存），
            # 可经 /switch 切回，不会被丢弃。创建后按 max_sessions 清理超限
            # 最老会话（当前会话是最新序号，不会被误删）。
            self._create_session()
            self._emit(f"🆕 已新建会话 #{self._current_session}"
                       f"（旧会话已保留，可 /switch 切回）")
            return
        if text == "/sessions":
            items = []
            for i in range(self._session_seq + 1):
                mark = " ← 当前" if i == self._current_session else ""
                items.append(f"  会话 #{i}{mark}")
            self._emit("📋 已有会话：\n" + "\n".join(items))
            return
        if text.startswith("/switch"):
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit():
                self._emit("⚠️ 用法：/switch <会话序号>，例如 /switch 0")
                return
            target = int(parts[1])
            if target < 0 or target > self._session_seq:
                self._emit(f"⚠️ 会话 #{target} 不存在"
                           f"（有效范围 0~{self._session_seq}）")
                return
            self._current_session = target
            self._emit(f"🔀 已切换到会话 #{target}")
            return

        # —— 正常输入：封装成入站消息投递进 bus ——
        # 空闲自动分片检查（惰性）：距上次活动超 idle_ttl_sec 自动开新会话、
        # 旧会话保留；随后无论是否分片都更新活动时间。连续对讲进行中不分片
        # （_maybe_split_session 内已短路）。
        self._maybe_split_session()
        self._bump_activity()
        # sender_id 携带当前会话序号，Gateway 据此派生独立 session_key，
        # 从而不同会话的历史互不干扰。
        msg = InboundMessage(
            channel="voice",
            sender_id=self._current_sender_id(),
            chat_id="direct",
            content=text,
        )
        await self.bus.publish_inbound(msg)

    async def start(self) -> None:
        """启动渠道。

        - ``kws_detector`` 为 None：空转等待停止事件（TASK-024 行为）。
        - 注入 ``kws_detector``：启动唤醒监听；唤醒 → 播回应 → 连续对讲
          （VAD 录音 → ASR → inject_text → send() 播完自动续听）由
          ``_on_wake`` / ``_handle_wake`` 完成，本方法负责等待停止事件并在期间
          响应 stop()。
        - 唤醒启动失败（麦克风权限/设备问题）时降级为空转，渠道仍存活。
        """
        detector = self._kws_detector
        if detector is None:
            await self._stop_event.wait()
            return
        try:
            await detector.start(on_wake=self._on_wake)
        except KwsError as exc:
            self._emit(f"📛 唤醒未就绪：{exc.message}")
            self._kws_detector = None  # 降级：仅 inject_text 可用
        except Exception as exc:  # noqa: BLE001 - 启动失败只降级，不崩溃
            self._emit(f"📛 唤醒未就绪：{exc}")
            self._kws_detector = None
        else:
            self._emit("🔊 唤醒监听已启动（喊「小奈小奈」试试）")
        await self._stop_event.wait()

    async def stop(self) -> None:
        """置位停止事件，结束 ``start()`` 的监听/空转；同步停止 KWS 检测器。"""
        self._stop_event.set()
        detector = self._kws_detector
        if detector is not None:
            try:
                await detector.stop()
            except Exception:  # noqa: BLE001 - 停止失败不阻断渠道关闭
                pass

    async def _on_wake(self) -> None:
        """唤醒回调（在事件循环内执行）：动作进行中或连续对讲中，新唤醒合并。

        TASK-027 起连续对讲期间唤醒词再次命中也沿用 ``_wake_in_progress``
        防抖：``_continuous`` 为 True 时新唤醒事件直接合并计数，不打断当前
        连续对话（全程不用再喊唤醒词）。
        """
        if self._wake_in_progress or self._continuous:
            print("[voice] 🎤 唤醒词命中（对话进行中，合并计数）")
            self._coalesced_wakes += 1
            return
        print("[voice] 🎤 唤醒词命中，进入对话")
        self._wake_in_progress = True
        try:
            await self._handle_wake()
        finally:
            self._wake_in_progress = False

    async def _play_wake_reply(self) -> bool:
        """唤醒确认回应：优先本地缓存随机播一条 WAV，缓存缺失回退云端 TTS。

        TASK-029 改造逻辑：

        1. **懒加载** ``self._wake_audio_cache``：为 None 时扫描
           ``wake_replies_dir`` 下所有 ``wake_*.wav`` 文件（排序后读 bytes），
           存入 ``self._wake_audio_cache``；目录不存在 / 无匹配文件 → ``[]``。
           仅首次调用扫描，之后复用缓存。
        2. **缓存非空**（``len > 0``）→ ``random.choice`` 取一条 bytes 直接
           ``play_audio`` 播放（media_type=``audio/wav``），**不调 tts_service**；
           播放失败 → 降级尝试云端合成（走现有 tts 路径）；tts 也失败/None →
           跳过（返回 False）。
        3. **缓存为空** → 走现有 ``tts.synthesize`` + ``play_audio`` 路径
           （完全向后兼容，不改原有逻辑）。

        - ``tts_service`` 为 None 或 ``wake_replies`` 为空/非 list → 跳过回应
          直接录音（返回 False，向后兼容 TASK-025 既有行为）——仅在缓存为空
          且需回退云端时检查。
        - 合成/播放任一步失败 → 降级跳过回应（``_emit`` 一条轻提示），
          不阻塞唤醒流程。
        - 返回 True 表示回应已**完整播完**，调用方此时才应进入连续对讲。
        """
        # —— 懓加载本地缓存音频 ——
        if self._wake_audio_cache is None:
            self._wake_audio_cache = self._load_wake_audio_cache()
        # —— 缓存非空：直接播放，不调 TTS ——
        if self._wake_audio_cache:
            audio_bytes = random.choice(self._wake_audio_cache)
            try:
                await play_audio(
                    audio_bytes,
                    "audio/wav",
                    playback_params=self._playback_params,
                )
                return True
            except Exception as exc:  # noqa: BLE001 - 缓存播放失败降级云端
                # 缓存播放失败 → 尝试云端合成（如果可用），否则跳过
                tts = self._tts_service
                replies = self._wake_replies
                if tts is None or not isinstance(replies, list) or not replies:
                    self._emit(
                        f"🔇 回应播放失败，继续听你说（{getattr(exc, 'message', exc)}）"
                    )
                    return False
                try:
                    text = random.choice(replies)
                    result = await tts.synthesize(text)
                    await play_audio(
                        result.audio,
                        result.media_type,
                        playback_params=self._playback_params,
                    )
                except Exception as exc2:  # noqa: BLE001 - 云端也失败则跳过
                    self._emit(
                        f"🔇 回应播放失败，继续听你说（{getattr(exc2, 'message', exc2)}）"
                    )
                    return False
                return True
        # —— 缓存为空：走现有云端合成路径（向后兼容）——
        tts = self._tts_service
        replies = self._wake_replies
        if tts is None or not isinstance(replies, list) or not replies:
            return False
        try:
            text = random.choice(replies)
            result = await tts.synthesize(text)
            await play_audio(
                result.audio,
                result.media_type,
                playback_params=self._playback_params,
            )
        except Exception as exc:  # noqa: BLE001 - 回应失败只降级，不阻塞唤醒
            self._emit(
                f"🔇 回应播放失败，继续听你说（{getattr(exc, 'message', exc)}）"
            )
            return False
        return True

    def _load_wake_audio_cache(self) -> list[bytes]:
        """扫描 ``wake_replies_dir`` 下所有 ``wake_*.wav`` 文件，读 bytes 存列表。

        目录不存在 / 无匹配文件 → 返回空列表 ``[]``（表示已加载但无缓存音频，
        不再重复扫描）。文件按文件名排序后依次读取；单个文件读取失败跳过
        （不阻塞整体加载）。
        """
        d = self._wake_replies_dir
        if not d or not os.path.isdir(d):
            return []
        files = sorted(
            f for f in os.listdir(d) if f.startswith("wake_") and f.endswith(".wav")
        )
        cache: list[bytes] = []
        for fname in files:
            fpath = os.path.join(d, fname)
            try:
                with open(fpath, "rb") as fp:
                    cache.append(fp.read())
            except OSError:
                continue  # 单文件读取失败跳过，不影响其余
        return cache

    async def _handle_wake(self) -> None:
        """唤醒处理：播甘雨回应（方案 B）→ 进入连续对讲 → 启动首轮录音监听。

        TASK-027 起从「单轮唤醒」升级为「连续对讲机」：回应**播完才返回**后
        进入连续对讲模式（``_continuous``），由 ``_schedule_next_listen`` 启动
        首轮「VAD 录音 → ASR → inject_text」监听；Agent 回复经 ``send()`` 播
        完后自动调度下一轮，直到 ``[END]`` 结束语或静默超时退出回待唤醒。
        音频全程内存流转不落盘；转写文本才进 Agent。任一环节失败走 ``_emit``
        友好提示，不回退崩溃。
        """
        self._wake_count += 1
        if self._kws_detector is None or self._asr_service is None:
            return  # 唤醒链路未装配（缺检测器或 ASR），自动禁用

        # 方案 B：先播甘雨确认回应（播完才返回），再进入连续对讲。
        await self._play_wake_reply()
        self._enter_continuous()
        self._schedule_next_listen()

    async def send(self, message: OutboundMessage) -> None:
        """把出站回复下发到本渠道（Agent 回复 → 甘雨 TTS → 播放默认输出）。

        - 内容非空且 ``tts_service`` 就绪、文本长度不超过 ``max_voice_chars``
          （>0 时）→ 先 ``synthesize`` 合成甘雨语音，再 ``play_audio`` 播放到
          系统默认输出；否则直接 ``_emit(text)`` 回文字（tts 未配置 / 文本
          超长 / 内容为空）；
        - 合成或播放任一步失败（TTSError / KwsError / 其它异常）→ 先 ``_emit``
          一条轻提示，再 ``_emit(text)`` 把原文发出，不静默、不崩溃；
        - TASK-027 ``[END]`` 结束语：收到含 ``[END]`` 标记的文本 → **在 TTS
          合成之前**剥离标记（播报/文字内容绝不含标记），正常播告别语；播完
          （或文字兜底路径）后退出连续对讲回待唤醒，不再触发下一轮录音；剥离
          后为空 → 不播任何东西直接退出；
        - TASK-027 连续对讲续听：未收到 ``[END]`` 且处于连续对讲模式
          （``_continuous``）时，播放完成（成功或降级）后调度下一轮录音任务。
        ``_emit`` 仍是文字兜底单一出口，语义与签名不变。
        """
        text = message.content
        if not text:
            # 空内容不触发 TTS（合成会因空文本报错），保持原 _emit 语义。
            self._emit(text)
            return
        # [END] 标记剥离必须发生在 TTS 合成之前（播出的告别语不含标记）；
        # 随后做 TTS 前清洗（剥 markdown/emoji/装饰符号），保证合成听感干净。
        text, end_marker = self._strip_end_marker(text)
        text = self._sanitize_for_tts(text)
        print(
            f"[voice] 🎀 小奈说：{text}"
            + ("（含 [END] 结束语）" if end_marker else "")
        )
        if not text:
            # 剥离后为空：不播放任何东西，直接退出连续对讲。
            if end_marker:
                self._exit_continuous()
            return

        tts = self._tts_service
        limit = self._max_voice_chars
        if tts is None or (limit > 0 and len(text) > limit):
            # 纯文字兜底路径（tts 未配置 / 文本超长）：同样剥离标记并退出。
            self._emit(text)
            if end_marker:
                self._exit_continuous()
            return
        try:
            result = await tts.synthesize(text)
            await play_audio(
                result.audio,
                result.media_type,
                playback_params=self._playback_params,
            )
        except Exception as exc:  # noqa: BLE001 - 合成/播放失败降级文字，不静默不崩溃
            self._emit(
                f"🔇 语音播放失败，改文字回复（{getattr(exc, 'message', exc)}）"
            )
            self._emit(text)
        # 播完（或降级文字）后统一处理：含 [END] → 退出连续对讲；否则连续对讲
        # 模式下调度下一轮录音（防重入由 _schedule_next_listen 内部保证）。
        if end_marker:
            self._exit_continuous()
        elif self._continuous:
            self._schedule_next_listen()
