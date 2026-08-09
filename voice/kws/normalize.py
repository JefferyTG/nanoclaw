"""播放防炸麦 DSP：峰值归一化 + 软限幅（TASK-028）。

在 ``voice/kws/player.py`` 的通用播放出口（唤醒回应 + Agent 回复都走它）上
对 int16 PCM 做一次性音量处理：把满幅/近满幅（峰值接近 int16 满幅 32767）的
音频压回目标峰值内，避免 TTS 峰值满幅削波导致的「炸麦」爆音/刺耳。

只接受 int16 numpy 数组（``voice/kws/player.py`` 的 ffmpeg 解码即输出这种
格式），处理全程 float64 计算再转回 int16，clip 防溢出；空数组 / 全零静音 /
低于目标峰值（且不抬升）时直接返回原数组（副本），不做无谓转换。纯 numpy
实现，无新依赖。

参数语义（与 config ``voice.playback.*`` 键名一一对应）：

- ``target_peak``：目标峰值（相对 int16 满幅 32767 的比例，0~1）。默认 0.89
  即 ≈ -1dBFS，给设备的实际输出留一点余量，既够响又不至于削波。
- ``max_gain_db``：允许的最大**抬升**增益（dB）。默认 0 表示「只压不抬」——
  峰值超过 ``target_peak`` 才按比例压下来，低响度音频（峰值已 ≤ 目标）不做
  放大，避免把录音/合成底噪一起抬起来。传 None 或负数同样视为「只压不抬」
  （负数钳到 0）；传正数时低响度音频最多被抬升 ``max_gain_db`` dB 到目标峰值
  为止，不会超过目标。
- ``soft_clip``：软限幅兜底开关。默认 True：缩放后仍可能瞬时过冲的部分用
  tanh 曲线（``y = target_abs * tanh(x / target_abs)``）软压住，输出峰值
  ≤ ``target_abs`` 且不硬削波（谐波少、不刺耳）；False 则仅硬 clip 到
  [-32768, 32767]，同样保证峰值 ≤ 目标。

边界安全：空数组 / 全零静音直接原样返回；``gain_eff`` 钳制在 ±48dB 内，
避免极小峰值时除零或放大爆炸。
"""

from __future__ import annotations

import numpy as np

# int16 满幅参考（绝对值）
_INT16_MAX_ABS = 32767.0
# 有效增益钳制上限（dB）：超过即视为配置/输入病态，钳住防爆炸
_GAIN_DB_LIMIT = 48.0


def normalize_playback_pcm(
    data: np.ndarray,
    *,
    target_peak: float = 0.89,
    max_gain_db: float = 0.0,
    soft_clip: bool = True,
) -> np.ndarray:
    """把一段 int16 PCM 做播放前音量归一化 + 限幅，返回 int16 数组。

    参数：
        data: int16 numpy 数组（允许非连续内存，内部先 ``np.asarray`` 处理）。
        target_peak: 目标峰值相对满幅比例（0~1，默认 0.89 ≈ -1dBFS）。
        max_gain_db: 允许的最大抬升增益（dB）。None / <0 / 0 → 只压不抬；
            >0 → 低响度最多抬升该 dB 到目标峰值为止。
        soft_clip: 是否启用 tanh 软限幅兜底（默认 True）。

    返回：
        与输入等长等 dtype（int16）的 numpy 数组——可能是原数组本身（空数组/
        静音）、原数组副本（无需处理）或新数组（经过缩放/限幅）。

    异常：
        ``ValueError``：输入 dtype 不是 int16，或 ``target_peak`` 不是正数。
    """
    data = np.asarray(data)
    if data.dtype != np.int16:
        raise ValueError(
            f"normalize_playback_pcm 只接受 int16 数组，收到 {data.dtype}"
        )
    if data.size == 0:
        return data

    # 峰值检测：abs 先在 int32 里算，避免 int16 的 -32768 无正表示、
    # np.abs 回绕成 -32768 导致满幅信号被误判为静音（边界安全）。
    peak = float(np.max(np.abs(data.astype(np.int32))))
    if peak <= 0.0:
        # 全零 / 静音：不放大、不做任何处理，原样返回
        return data

    # target_peak 归一化为正有限数（NaN / 非数字 / ≤0 视为契约违规）
    try:
        target_peak = float(target_peak)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_peak 必须是数字") from exc
    if not (target_peak > 0.0) or target_peak != target_peak:  # NaN 或 ≤0
        raise ValueError("target_peak 必须为正数")
    target_abs = target_peak * _INT16_MAX_ABS

    # 达到目标峰值所需的增益（dB）
    gain_db = 20.0 * np.log10(target_abs / peak)
    # max_gain_db 语义：None / <0 视为 0（只压不抬）；>=0 时允许最多抬升该 dB
    if max_gain_db is None:
        max_gain_db = 0.0
    else:
        try:
            max_gain_db = float(max_gain_db)
        except (TypeError, ValueError):
            max_gain_db = 0.0
    if max_gain_db < 0.0:
        max_gain_db = 0.0
    gain_eff = min(gain_db, max_gain_db)
    # 有效增益钳制 ±48dB：极小峰值时防放大爆炸、极端衰减时防过度压低
    gain_eff = max(-_GAIN_DB_LIMIT, min(_GAIN_DB_LIMIT, gain_eff))

    # 峰值已 ≤ 目标且不抬升（gain_eff ≤ 0）→ 无需任何处理，直接返回副本
    if peak <= target_abs and gain_eff <= 0.0:
        return data.copy()
    if gain_eff == 0.0:
        return data.copy()

    # float64 缩放 → （可选）tanh 软限幅 → clip → 回 int16
    scaled = data.astype(np.float64) * (10.0 ** (gain_eff / 20.0))
    if soft_clip:
        scaled = target_abs * np.tanh(scaled / target_abs)
    scaled = np.clip(scaled, -_INT16_MAX_ABS - 1.0, _INT16_MAX_ABS)
    return np.rint(scaled).astype(np.int16)
