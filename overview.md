# Overview：NanoClaw 图像识别功能方案设计

## 任务概述
为 NanoClaw（本地优先的多渠道 AI Agent 网关）设计图像识别（视觉能力）功能。本次只做调研 + 方案设计，不写代码，产出方案文档供用户审阅。

## 完成内容

### 1. 项目架构熟悉
- 摸清了 NanoClaw 的消息流转链路：渠道 → MessageBus → Gateway → AgentLoop(ReAct) → Provider(OpenAI 兼容)。
- 定位了当前对图片「全链路不支持」的 5 个阻断点：飞书 `message_type != "text"` 丢弃图片、网页 WS 只收 TEXT 帧、`InboundMessage.content` 是纯字符串、`AgentLoop.run()` 只收 str、ContextBuilder 拼纯文本 content。
- **关键发现**：Provider 层（OpenAICompatProvider 用 AsyncOpenAI）天然兼容多模态 messages 格式，**一行都不用改**。

### 2. 竞品调研
- 硅基流动（当前 Provider）已原生支持多模态：标准 `/chat/completions`，content 用数组格式，支持 Qwen-VL/GLM/DeepseekVL2 等。
- 飞书图片下载必须用 `messages/{message_id}/resources/{file_key}` 接口（单独 image_key 会 400，社区高频踩坑）。
- 同类产品：LobeChat / Open WebUI / Cherry Studio 通用做法是前端 Base64 → image_url 字段。
- LangChain 多模态 Agent 两种模式：视觉直通 / 视觉即工具（后者契合本项目 Tool 机制）。
- Ollama 本地方案：`images=[base64]`，需 GPU，能力弱于云端 72B。

### 3. 产出方案文档
**文件**：`视觉识别功能方案设计.md`

含 3 个可选方案 + 推荐：
- **方案 A（推荐）云端 VLM 直通**：用户直接发图提问，Provider 零改动，改造集中在消息构建+渠道层。
- **方案 B 视觉即工具**：新增 `analyze_image` 工具，侵入极小，但用户不能直接发图，作为 A 的补充。
- **方案 C 本地 Ollama**：全本地但硬件门槛高、能力弱，暂不推荐。

推荐「方案 A 为主 + 方案 B 补充」，模型选 Qwen2.5-VL-72B，按需切换（有图切 vision_model，无图用 model）。

## 关键决策
- 全程未编写任何功能代码，严格遵循「先调研分析、以方案文档形式提交审阅」的要求。
- 方案文档末尾列出了 5 个待用户确认的事项（模型切换策略、图片上限、持久化策略、渠道优先级、是否叠加方案 B）。

## 产出文件
- `视觉识别功能方案设计.md` —— 完整方案文档（待审阅）

## 下一步
等待用户审阅方案文档并就 5 个待确认事项给出决策后，再进入实现阶段。
