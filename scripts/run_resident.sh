#!/bin/bash
# NanoClaw 常驻启动脚本（macOS launchd 托管）
# 用 caffeinate 阻止系统「空闲睡眠」，让助手在本机登录期间尽量不掉线。
#
# 睡眠行为说明（重要）：
#   - caffeinate -i  只防「空闲自动睡眠」（息屏/无操作一段时间后睡）。
#   - 合盖睡眠：默认仍会发生。要让「合盖也不睡」，需满足其一：
#       1) Mac 接电源 + 外接显示器，进入 Clamshell（合盖外接）模式；
#       2) 改用 `caffeinate -s`（系统睡眠断言，仅在接电源时有效）。
#   - 纯电池 + 合盖：macOS 出于散热/安全仍会睡眠，无法用软件完全阻止。
#   - 睡眠期间飞书/微信 WS 断开，唤醒后自动重连；但睡眠时段收到的消息会漏
#     （WS 不持久队列）。
#
# 由 bin/nanoclawctl-mac 生成的 LaunchAgent（com.nanoclaw.agent）在登录时自启、
# 崩溃自动拉起。所有路径均从本脚本位置自动推导，仓库 clone 到任何位置都能用。

set -e

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$PROJECT_DIR"

# -i : 阻止空闲睡眠
exec caffeinate -i .venv/bin/python main.py
