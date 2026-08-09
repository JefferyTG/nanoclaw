"""本地语音唤醒（KWS）与唤醒后录音（TASK-025）。

- ``detector``：KwsWakeDetector，把 TASK-023 demo 的「PortAudio 回调 → 有界
  队列 → KWS worker → 冷却/连续命中确认 → asyncio 唤醒事件」抽为可复用模块；
- ``recorder``：唤醒后录制 N 秒 int16 mono PCM 并封装为 WAV bytes（内存流转，
  不落盘）；
- ``vad``：流式静音检测录音（TASK-027 第一步）：InputStream 逐块采集 +
  RMS 能量阈值检测人声，说完话停顿 ``silence_end_sec`` 提前结束，全程无人声
  判 ``is_silent``；唤醒单轮与连续对讲共用；

- ``player``：唤醒确认回应播放（TASK-025 方案 B）：把 TTS 合成音频用 ffmpeg
  解码为 24kHz 单声道 int16 PCM（TemporaryDirectory 即用即删、纯内存不落盘），
  ``sd.play`` + ``sd.wait`` 走 ``asyncio.to_thread``，**播完才返回**；输出/解码
  失败统一转 :class:`KwsError`。
- ``errors``：KWS 专用可读错误类型。
"""
