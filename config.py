"""NanoClaw 配置管理。

集中存放运行所需的可配置项（API 地址、模型、工作区、迭代上限、人设文件名等），
并提供统一的加载入口 ``load_config``：

优先级（从低到高）：
1. 代码内默认值；
2. ``config.json`` 文件中的字段（存在即覆盖默认）；
3. 对应的密钥环境变量（最高优先级，覆盖配置文件中的 api_key）。

把敏感信息（API Key）走环境变量、其余走配置文件，是避免密钥误提交进版本库的
常见做法。
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional

# 硅基流动（SiliconFlow）OpenAI 兼容端点
_SILICONFLOW_URL = "https://api.siliconflow.cn/v1"

# 参与序列化/反序列化的字段清单（用于从 JSON 安全填充，避免读到无关键）
_CONFIG_FIELDS = (
    "api_key",
    "base_url",
    "model",
    "subagent_model",
    "workspace",
    "max_iterations",
    "identity_file",
    "feishu_app_id",
    "feishu_app_secret",
    "web_host",
    "web_port",
    "turn_timeout_sec",
    "mcp_servers",
    # 多模态（视觉）模型：基础模型无视觉能力时，由 ask_image 工具把图片转交它理解
    "multimodal_model",
    # 基础模型是否本身即多模态（自带视觉）。true 时图片直传基础模型、不注册 ask_image
    "base_model_multimodal",
    # 生图模型：由 generate_image 工具调用，具体服务与模型由用户配置（见 image_gen_model）。
    # 三者皆空视为未配置；api_key 可走环境变量 IMAGE_GEN_API_KEY 覆盖。
    "image_gen_model",
    # 语音识别：首版用于 Web 端录音转文字；api_key 可由 ASR_API_KEY 覆盖。
    "asr_model",
    # 文字转语音：首版使用 edge-tts；服务端能力与网页自动朗读开关相互独立。
    "tts_model",
)


@dataclass
class NanoClawConfig:
    """NanoClaw 运行配置。

    属性：
        api_key: 模型服务 API Key（建议用环境变量注入，留空则依赖 NANOCLAW_API_KEY）。
        base_url: OpenAI 兼容接口的 base_url。
        model: 使用的模型名。
        subagent_model: 子 Agent 默认模型（可选）；留空则沿用 model。
        workspace: 工具可访问的工作区根目录（相对或绝对路径）。
        max_iterations: Agent 主循环单轮最大迭代次数。
        identity_file: 人设文件名（位于 workspace 下）。
    """

    api_key: str = ""
    base_url: str = _SILICONFLOW_URL
    model: str = "Pro/moonshotai/Kimi-K2.5"
    # 子 Agent 默认模型：留空(None)则子 Agent 沿用主模型 model；
    # 若配置其他模型，子 Agent 默认改用该模型（仍可被调用时显式 model 参数覆盖）。
    subagent_model: Optional[str] = None
    workspace: str = "."
    max_iterations: int = 32
    identity_file: str = "identity.md"
    feishu_app_id: str = ""          # 飞书自建应用 App ID（留空则不启用飞书渠道）
    feishu_app_secret: str = ""      # 飞书自建应用 App Secret
    web_host: str = "0.0.0.0"        # 网页渠道监听地址（0.0.0.0 同局域网可达）
    web_port: int = 0                # 网页渠道端口；0 表示不启用网页渠道
    turn_timeout_sec: int = 600      # 单轮对话墙钟超时（秒）；超时强制终止，防卡死
    mcp_servers: dict = field(default_factory=dict)  # MCP Server 配置：{server_name: {command, args, env?, cwd?}}
    # 多模态（视觉）模型配置：基础模型为纯文本时，图片由 ask_image 工具转交该模型理解。
    # 三者皆空视为未配置；api_key 也可走环境变量 MULTIMODAL_API_KEY 覆盖。
    multimodal_model: dict = field(
        default_factory=lambda: {"api_key": "", "base_url": "", "model": ""}
    )
    # 基础模型本身是否即多模态（自带视觉）。true→图片直传基础模型、不注册 ask_image；
    # false→纯文本基础模型，需要 ask_image 工具（无论 multimodal_model 是否配置）。
    base_model_multimodal: bool = False
    # 生图模型配置：由 generate_image 工具调用。三者（api_key/base_url/model）皆空
    # 视为未配置；具体服务地址与模型名完全由用户填写，代码不预填、不绑定任何服务商。
    # api_key 也可走环境变量 IMAGE_GEN_API_KEY 覆盖。
    image_gen_model: dict = field(
        default_factory=lambda: {
            "api_key": "",
            "base_url": "",
            "model": "",
            "timeout_sec": 120,
            # 图生图（img2img）专用配置；留空则回落到上面通用的 model / 默认装配。
            # 源图编码、键名、位置、强度、标签均由服务商约定，全部可配、不写死任何家。
            "img2img_model": "",
            "img2img": {
                "image_field": "image",    # 源图塞进请求体的键名
                "image_location": "body",  # "body"=顶层 / "extra_body"=嵌套
                "encoding": "auto",       # "auto"=按源图自动(本地图base64内联/公网链接url直发) / "base64" / "url"
                "as_array": True,          # image 始终以数组形式传（支持多图，Agnes 即如此）
                "strength_field": "",      # 强度键名（空=不传）
                "strength": 0.0,           # 强度默认值（需 strength_field 非空才生效）
                "tags": [],                # 服务商标签列表，如 ["img2img"]
            },
        }
    )
    # 语音识别与主聊天模型使用独立配置和密钥。未启用或配置不完整时，
    # 普通文字聊天不受影响，Web 录音接口返回明确的未配置错误。
    asr_model: dict = field(
        default_factory=lambda: {
            "enabled": False,
            "provider": "openai_compatible",
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini-transcribe",
            "timeout_sec": 90,
            "max_retries": 1,
            "max_audio_bytes": 10 * 1024 * 1024,
            "max_duration_sec": 120,
            "max_concurrency": 2,
            "language": "",
            "prompt": "",
            "ffmpeg_path": "ffmpeg",
            "ffprobe_path": "ffprobe",
        }
    )
    # TTS 后端默认可用，但网页喇叭开关始终默认关闭；修改这些启动期参数后需重启。
    tts_model: dict = field(
        default_factory=lambda: {
            "enabled": True,
            "provider": "edge_tts",
            "voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "timeout_sec": 60,
            "connect_timeout_sec": 10,
            "receive_timeout_sec": 60,
            "max_text_chars": 4000,
            "max_audio_bytes": 16 * 1024 * 1024,
            "max_concurrency": 2,
        }
    )


def load_config(config_path: str = "config.json") -> NanoClawConfig:
    """加载配置。

    顺序：默认值 → config.json 字段 → 环境变量 NANOCLAW_API_KEY（仅 api_key）。
    文件不存在或解析失败时不报错，回退到默认值。

    参数：
        config_path: 配置文件路径，默认 ``config.json``。

    返回：
        ``NanoClawConfig`` 实例。
    """
    cfg = NanoClawConfig()

    # 2) 用 JSON 文件字段覆盖默认值
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in _CONFIG_FIELDS:
            if key in data and data[key] is not None:
                # ASR/TTS 配置允许只覆盖少数字段；其余继续使用当前代码默认值，
                # 方便未来新增可选参数而不要求用户立刻重写旧 config.json。
                if key in ("asr_model", "tts_model") and isinstance(data[key], dict):
                    merged = dict(getattr(cfg, key))
                    merged.update(data[key])
                    setattr(cfg, key, merged)
                else:
                    setattr(cfg, key, data[key])
    except FileNotFoundError:
        # 无配置文件：保持默认值即可
        pass
    except Exception as exc:  # noqa: BLE001 - 配置损坏不应阻断启动
        print(f"警告：读取配置文件 {config_path} 失败，使用默认配置：{exc}")

    # 3) 环境变量最高优先级（API Key 与飞书凭证均支持环境变量覆盖）
    env_key = os.environ.get("NANOCLAW_API_KEY")
    if env_key:
        cfg.api_key = env_key
    env_fs_id = os.environ.get("FEISHU_APP_ID")
    if env_fs_id:
        cfg.feishu_app_id = env_fs_id
    env_fs_secret = os.environ.get("FEISHU_APP_SECRET")
    if env_fs_secret:
        cfg.feishu_app_secret = env_fs_secret

    # 多模态（视觉）模型 API Key 走独立环境变量覆盖，避免与主模型 key 混淆
    env_mm_key = os.environ.get("MULTIMODAL_API_KEY")
    if env_mm_key:
        cfg.multimodal_model = dict(cfg.multimodal_model)
        cfg.multimodal_model["api_key"] = env_mm_key

    # 生图模型 API Key 走环境变量覆盖（IMAGE_GEN_API_KEY 命中即覆盖）
    env_img_key = os.environ.get("IMAGE_GEN_API_KEY")
    if env_img_key:
        cfg.image_gen_model = dict(cfg.image_gen_model)
        cfg.image_gen_model["api_key"] = env_img_key

    # ASR 使用独立密钥，避免默认复用主聊天模型的权限与账单边界。
    env_asr_key = os.environ.get("ASR_API_KEY")
    if env_asr_key:
        cfg.asr_model = dict(cfg.asr_model)
        cfg.asr_model["api_key"] = env_asr_key

    return cfg


def save_config(cfg: NanoClawConfig, config_path: str = "config.json") -> None:
    """把配置按白名单写回 JSON 文件（供网页配置页持久化）。

    只写出 ``_CONFIG_FIELDS`` 内的字段，避免把运行时派生状态误存。
    """
    data = {key: getattr(cfg, key) for key in _CONFIG_FIELDS}
    # load_config 会把环境变量密钥覆盖到运行时配置对象。网页保存其它配置时，
    # 不应把这份仅供进程使用的 ASR 密钥意外持久化到 config.json。
    if os.environ.get("ASR_API_KEY") and isinstance(data.get("asr_model"), dict):
        data["asr_model"] = dict(data["asr_model"])
        data["asr_model"]["api_key"] = ""
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
