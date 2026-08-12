# TASK-039：webui 移动端适配（远程工作台·第一版·打字）

## 任务卡

- 状态：**已完成**（2026-08-12 归档）
- 负责人：乖宝（验收 ✅）/ code-master（实现 ✅）
- 执行会话/子 Agent：code-master
- 基线 commit / 分支：`b25fdb3`（main，工作区干净）
- 依赖任务：无（TASK-001~038 全部已归档）

### 目标

让 NanoClaw 的网页端（webui/index.html）在手机浏览器上「打字聊天」体验良好，成为远程工作台第一版：
- iPhone Safari / Android Chrome 通过 `http://<Tailscale IP>:8080` 访问
- 布局自适应手机屏幕，无横向滚动、无错位
- 聊天、新会话、图片上传、配置面板、思考面板/工具卡片展开折叠等现有功能在手机上全部可用
- 桌面浏览器（PC）现有体验**不回归**

### 非目标

- ❌ HTTPS 配置（录音/ASR 在手机 HTTP 下会被浏览器拒绝，属独立任务，见「后续任务」）
- ❌ 实时语音通话（豆包 S2S 接入，后续任务）
- ❌ PWA（manifest / Service Worker / 推送，后续任务）
- ❌ 修改后端 `channels/web.py` 的路由/协议（除非适配中发现必须的小改动，需记录并单独确认）
- ❌ 不改动聊天功能逻辑本身

### 允许修改

- `webui/index.html`（唯一目标文件；CSS + 少量 JS）
- 必要时：`channels/web.py`（仅当响应式需要后端配合时，如 HTTP 头/缓存，需先说明）——**本次未动用**

### 禁止修改

- `main.py`、`gateway.py`、`config.json`、其它渠道代码
- 业务逻辑、会话管理、Agent 循环
- 未授权的 git commit / push

### 上下文与约束

- **相关代码入口**：
  - 前端单文件：`webui/index.html`（68KB，原生 HTML/CSS/JS，零框架、零构建）
  - 服务端：`channels/web.py`（`GET /` 返回 index.html；`WS /ws` 聊天；`/api/tts`、`/api/asr`、`/api/config`、`/api/sessions`、`/upload`）
  - 端口：`web_port=8080`（config.json）
- **现状盘点（已核实 2026-08-11 23:5x）**：
  - `body` 已是 `100vh` 纵向 flex，`#chat` 消息 `max-width:88%`，图片有 max-width 限制，viewport meta 已有 → 底子「半移动友好」
  - **0 个 `@media`** → 完全没做响应式，这是主要缺口
  - 固定宽度只有：`#sidebar 264px`（手机宽度 375px 下占 70%，必须抽屉化）
  - hover 交互 6 处（按钮/卡片，手机上需补 focus/touch 反馈）
  - 固定高度无碍（多为图片/卡片内部限制）
  - 已有功能：浏览器录音→`/api/asr` 转写（**手机 HTTP 下不可用，不属本任务**）、图片上传、会话列表、配置面板、思考面板、工具卡片、子 Agent 卡片
- **iOS 键盘坑**：`height:100vh` 在 iOS Safari 软键盘弹出时会顶飞底部输入栏 → 需 `100dvh`（iOS 15.4+ 支持，需考虑降级：`100vh` 兜底 + dvh 覆盖）
- **相关决策**：无历史冲突；远程工作台方向 2026-08-07 起在 followups.jsonl 跟踪，技术栈结论见该条目（2026-08-11 更新）
- **已知风险**：
  - 手机端桌面回归（改 CSS 必须守住桌面样式）
  - iOS Safari 独有怪癖（100dvh、橡皮筋滚动、底部安全区 `env(safe-area-inset-bottom)`）
  - Tailscale 访问的是 `http://100.x.x.x:8080`，地址栏可见，第一版接受

### 验收标准（乖宝 2026-08-12 真机验收全部通过 ✅）

- [x] iPhone Safari 打开 `http://<Mac Tailscale IP>:8080`：页面适配屏宽，**无横向滚动**
- [x] Android Chrome 同样无横向滚动、正常可用（乖宝真机确认）
- [x] 侧边栏（会话列表）在手机上是抽屉/覆盖层，汉堡按钮可开合，打开时聊天区不可误触
- [x] 底部输入栏在 iOS 软键盘弹出后仍然可见、可输入、不遮挡
- [x] 手机端可完成：打字发送消息、收到回复（含思考面板/工具卡片展开折叠）、新建会话、切换会话、删除会话、上传图片、打开/保存配置
- [x] 桌面浏览器（≥1024px）打开：与改造前视觉一致，无回归
- [x] 触控目标：主要按钮可点区域 ≥44px（或视觉可见大小+合理留白）
- [x] 全量测试通过：`unittest discover -s tests -t .` → **Ran 938 tests — OK**
- [x] 文档同步：PROJECT.md 能力矩阵（网页渠道·移动适配）、任务卡归档时同步（见「执行交接」）

### 必须执行的验证（全部真实执行 ✅）

```bash
git diff --check                                          # PASS
unittest discover -s tests -t .                           # Ran 938 tests — OK
python -m compileall -q channels webui                    # PASS（后端未改动，例行跑）
python -c "import main"                                   # PASS 冒烟
node --check（提取 <script> 49610 字节）                   # PASS
# 手动验证（乖宝真机 ✅）：iPhone Safari + Android + Tailscale → http://100.x.x.x:8080
#   桌面 Chrome 回归对照 ✅
```

## 实现方案（已按此实施）

1. **响应式断点**：`@media (max-width: 768px)` 覆盖手机；`(min-width: 769px)` 保持桌面原样。整个改造**只在 media query 内叠加**，桌面零影响。
2. **侧边栏抽屉化**：
   - 手机下 `#sidebar` 改为 `position:fixed; left:0; top:0; bottom:0; width:min(78vw,300px); transform:translateX(-100%)`，默认隐藏
   - header 加汉堡按钮（仅手机显示），点击开合；点遮罩/选中会话自动关闭
   - 新增 `toggleSidebar()` / `closeSidebar()` + 遮罩元素
3. **输入栏与键盘**：
   - `body` 高度 `100vh` 兜底 + `100dvh` 覆盖
   - 底部 `#bar` 加 `padding-bottom: calc(10px + env(safe-area-inset-bottom))`（iPhone 底部小黑条）
   - 输入框字号 iOS 防自动缩放：`font-size:16px`（iOS 对 <16px 输入框会自动放大）
4. **触控优化**：
   - 按钮 min-height 44px；`#bar` 按钮图标加大（20px）
   - hover 样式补 `:active`/focus 等价反馈（手机无 hover）
5. **字号/间距微调**：media query 内放大基础字号（13px→15px 级别）、消息间距微调
6. **图片/附件条**：`max-width` 在手机上收窄（用户图 40vw / 机器人图 80vw），附件格子保持 56px 可点
7. **配置面板**：`max-height:55vh` 可用，row 双列在窄屏改为单列（`flex-direction:column`）

## 后续任务（本任务不处理，仅记录）

- **TASK-040：webui 多端历史同步**（2026-08-12 建卡，乖宝实测多端并发发现：后端不回广播 + 前端无同步机制 → 双端历史不同步；方案 C 轻量同步通知，见 active/TASK-040）
- TASK-0??：webui HTTPS（Tailscale cert / 云服务器 + 域名），解锁手机录音/ASR、PWA、推送的前提
- TASK-0??：webui 实时语音（浏览器直连豆包 S2S realtime 渠道，专属音色 S_ZUpBmlGb2）
- TASK-0??：PWA 化（manifest + 图标[小奈 avatar 生成] + Service Worker），桌面图标、全屏
- TASK-0??：Web Push 通知（pywebpush + VAPID + 订阅接口），接「陪伴心跳」主动提醒

## 执行交接

- 状态：**已完成**（2026-08-12 归档）
- 实际改动文件：`webui/index.html`（1455 → 1551 行，+96 行，0 删除；唯一改动文件；`channels/web.py` 未动用）
- 实现摘要：
  - 全局仅 3 处小手术（桌面零影响）：新增 `#sbToggle{display:none}`、`#sbMask{display:none}` 两条规则 + 两个隐藏元素；其余全部收敛在 1 个 `@media (max-width:768px)` 块（+83 行）
  - 媒体查询内 7 分区：① 高度 100vh 兜底+100dvh 覆盖（iOS 软键盘）+ 去 tap 高亮；② sidebar 抽屉化（fixed+translateX(-100%) 默认隐藏，body.sb-open 滑入，遮罩 z-index 90 < 侧栏 100）；③ 输入栏 calc(10px+env(safe-area-inset-bottom))、输入框 16px 防缩放、按钮/输入框 min-height 44px；④ 各按钮/卡片 :active 触控反馈（桌面 hover 不动）+ 🎙/🖼/🔇 图标 20px；⑤ 字号 13→15px 级别（侧栏标题/消息正文 16px/工具文本上浮）+ 状态文字省略号防溢出；⑥ 图片 180px→40vw、360px→80vw，附件格 56px 可点；⑦ 配置面板 .row 窄屏单列、按钮 44px、输入 16px
  - 新增 JS ~12 行：`toggleSidebar()`（切 body.sb-open + 汉堡 aria/title 同步）、`closeSidebar()`；openSession/newSession 内各插一行 closeSidebar()（选中会话/新会话自动收抽屉）
- 关键决策与假设：
  - 断点 768px，桌面 ≥769px 视觉与改造前一致（媒体查询内叠加）→ 已记入 DECISIONS.md
  - 100dvh 需 iOS 15.4+，旧版本走 100vh 兜底（可接受降级）
  - 🎙 录音按钮手机 HTTP 下不可用属既有约束（后续 HTTPS 任务），按钮保留+提示文案
  - 服务端 `_render_index()` 每次请求实时读文件（注释明确），改完**无需重启**，页面版本号按内容哈希自愈刷新——乖宝验证 ✅
- 验证命令与结果（2026-08-12 11:0x code-master 执行 + 主 Agent 复核 + 乖宝真机）：
  - `git diff --check` ✅ PASS
  - `node --check`（提取 `<script>` 49610 字节）✅ PASS
  - `.venv/bin/python -m unittest discover -s tests -t .` ✅ **Ran 938 tests — OK**（61.2s）
  - `python -m compileall -q channels webui` ✅ PASS
  - `.venv/bin/python -c "import main"` ✅ PASS 冒烟
  - `curl http://127.0.0.1:8080/` ✅ HTTP 200，返回 75522 字节含 sbToggle ×7 + @media 断点（确认实时读盘无需重启）
  - **乖宝真机验收** ✅ iPhone Safari + Android Chrome + Tailscale + 桌面回归对照，验收标准逐项通过
- 未验证项：无（真机已测）
- 风险与遗留问题：
  - **多端历史不同步**（乖宝 08-12 实测发现）→ 已建 TASK-040 独立处理
  - 🎙 手机 HTTP 录音不可用 → 后续 HTTPS 任务
  - 抽屉打开时遮罩覆盖 header，再点汉堡命中遮罩→关闭抽屉（行为等价，体验可接受）
- commit：`（见 git log，2026-08-12 乖宝授权「直接 commit」）`
- 当前 `git status --short --branch`：（归档提交后见 git status）
- 建议下一步：TASK-040 webui 多端历史同步（乖宝说「开始 TASK-040」→ code-master 按卡实现）

## 负责人验收

- [x] 检查 diff 与授权范围（仅 webui/index.html + 文档，无越界）
- [x] 独立复跑关键验证（diff --check / 938 tests / compileall / import main / curl 实时读盘）
- [x] 检查秘密/个人数据/运行产物（无涉密改动）
- [x] 检查文档与配置一致性（PROJECT.md 能力矩阵/里程碑、DECISIONS.md、MEMORY 指针已同步）
- [x] 更新 `docs/DECISIONS.md` 中相关状态（新增 webui 响应式决策条目）
- 验收结论：**通过** ✅（乖宝 2026-08-12 真机验收 + 授权 commit）
- 证据与备注：真机验收项全部 ✅；多端历史不同步为 039 边界外发现，已转 TASK-040
