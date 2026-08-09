---
name: email-check
description: 邮箱新邮件检查与管理技能（IMAP 只读）。当用户提到「看邮件」「查邮箱」「有没有新邮件」「读一下某封邮件」「邮箱收到什么」「帮我看看收件箱」「标记邮件已读」「订阅到期提醒」等与邮件相关的请求时使用。支持网易（163/126/yeah）、QQ、Gmail 等标准 IMAP 邮箱，可检查新邮件、列出最近邮件、查看邮件正文、标记已读。特别适合配合定时任务做每日新邮件提醒。技能触发时先读 SKILL.md，按其中的命令清单执行。
---

# 邮箱检查与管理技能 (Email Check)

通过 IMAP 协议读取邮箱（只读），检查新邮件、列出邮件、查看正文、标记已读。**不发送邮件、不删除邮件、不移动邮件**。

## 核心原则

1. **只读优先**：默认所有检查操作均以只读模式打开收件箱（`BODY.PEEK` 不置已读标记）。只有用户明确要求「标记已读」时才使用可写模式。
2. **授权码安全**：邮箱账号与授权码存放在 `workspace/email_accounts.json`（已 gitignore），绝不在对话中复述授权码、绝不写入 git。
3. **增量追踪**：脚本用状态文件 `workspace/email_state.json` 记录每个账号已读到的最新 UID，只报告新邮件，不重复打扰。
4. **优雅降级**：任何一步失败（连接、登录、搜索）都打印 `ERROR: ...` 而不是抛异常退出，方便上层（定时 agent）兜底处理。

## 文件布局

```
skills/email-check/
├── SKILL.md                    # 本文件
└── scripts/
    └── email_check.py          # 主脚本（纯 Python 标准库，零依赖）
```

配置文件（由用户或首次运行时创建，**不在技能目录内**）：
- `workspace/email_accounts.json` — 邮箱账号配置（email + 授权码 + 服务器）
- `workspace/email_state.json` — 检查状态（last_uid 增量追踪）

## 命令清单

所有命令在 NanoClaw 项目根目录下执行：

```bash
PY=".venv/bin/python"
SCRIPT="skills/email-check/scripts/email_check.py"

# 1) 检查所有邮箱的新邮件（默认增量模式）
$PY $SCRIPT

# 2) 只看指定账号（支持 name 或 email 关键字模糊匹配）
$PY $SCRIPT --account 网易
$PY $SCRIPT --account qq.com

# 3) 列出最近邮件（含 UID，供后续操作定位）
$PY $SCRIPT --list --since-days 7

# 4) 查看某封邮件的完整正文
$PY $SCRIPT --account 网易 --show <UID>

# 5) 把某几封邮件标记为已读
$PY $SCRIPT --account 网易 --mark-read <UID1> <UID2>

# 6) 调试：忽略状态文件，强制看最近 N 天
$PY $SCRIPT --since-days 3
```

## 输出约定

- **检查新邮件**：有新邮件打印 `📬 <账号> 有 N 封新邮件：` + 逐条 `UID <uid> | 来自 <发件人>：《<主题>》`；无新邮件打印 `NO_NEW_MAIL`。
- **列邮件**：`📬 <账号>（最近 N 天 M 封）：` + 逐条 `UID <uid> | <发件人> |《<主题>》`；无邮件打印 `📭 <账号>: 最近 N 天无邮件`。
- **查看正文**：打印发件人、主题、时间、正文（最多 3000 字符）。
- **标记已读**：打印 `✅ <账号>: 已标记 X/Y 封邮件为已读`。
- **失败**：统一 `ERROR: <描述>`，不抛异常。

## 定时提醒集成（NanoClaw 场景）

本技能常与 reminders 定时任务配合：每天早上新闻快讯的 agent 任务在 prompt 中追加「运行邮箱检查脚本」步骤即可。agent 任务拥有完整工具（含 exec），可执行 `python skills/email-check/scripts/email_check.py` 检查邮箱。

- 输出 `NO_NEW_MAIL` → 简报末尾不提邮箱。
- 输出邮件列表 → 用甜美语气在简报末尾追加 📬 提醒段。
- 输出 `ERROR:` → 轻松带过（如「邮箱今天闹脾气，小奈没连上」），不刷屏报错。

## 首次配置（授权码获取）

用户需在邮箱网页端开启 IMAP 服务并生成**客户端授权码**（16 位），填入 `workspace/email_accounts.json`：

```json
{
  "accounts": [
    {
      "name": "网易邮箱",
      "email": "user@163.com",
      "auth_code": "16位授权码",
      "host": "imap.163.com",
      "port": 993,
      "enabled": true
    },
    {
      "name": "QQ邮箱",
      "email": "123456@qq.com",
      "auth_code": "16位授权码",
      "provider": "qq",
      "enabled": true
    }
  ]
}
```

- 网易：网页版邮箱 → 设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP → 短信验证 → 生成授权码（只显示一次）。
- QQ：mail.qq.com → 设置 → 账号与安全 → 安全设置 → 开启 IMAP/SMTP → 验证 → 生成授权码。
- 服务器若未显式配置，脚本会按 provider/域名自动解析（163/126/qq/gmail 有内置表）。

## 常见问题

- **登录失败（535/认证错误）**：授权码错误或已失效 → 让用户去网页端重新生成。
- **找不到账号**：`--account` 关键字必须能模糊匹配 name 或 email。
- **超时**：单次连接 30 秒超时，网络差时重试即可。
- **QQ 未读很多**：`--list` 只是列出，不会自动标已读；只有 `--mark-read` 才改状态。
