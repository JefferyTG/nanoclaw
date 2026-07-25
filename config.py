"""NanoClaw 配置管理。

集中存放运行所需的可配置项（API 地址、模型、工作区、迭代上限、人设文件名等），
并提供统一的加载入口 ``load_config``：

优先级（从低到高）：
1. 代码内默认值；
2. ``config.json`` 文件中的字段（存在即覆盖默认）；
3. 环境变量 ``NANOCLAW_API_KEY``（最高优先级，覆盖一切来源的 api_key）。

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

    return cfg


def save_config(cfg: NanoClawConfig, config_path: str = "config.json") -> None:
    """把配置按白名单写回 JSON 文件（供网页配置页持久化）。

    只写出 ``_CONFIG_FIELDS`` 内的字段，避免把运行时派生状态误存。
    """
    data = {key: getattr(cfg, key) for key in _CONFIG_FIELDS}
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
