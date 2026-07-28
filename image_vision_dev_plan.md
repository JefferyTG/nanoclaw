# NanoClaw 图片 / 多模态支持 开发计划

> 本文档是实现的唯一权威依据。所有代码改动必须与此文档一致；若实现中发现设计盲点，先回到本文档修订，再改代码，避免"设计漂移"。

## 0. 背景与目标

基础模型当前为 DeepSeek（纯文本，无视觉能力）。用户希望：

1. **全链路支持图片**：图片能从 Web 渠道进入消息总线、网关、Agent，最终被模型"看见"。
2. **不换基础模型**：通过新增一个**视觉工具** `ask_image`，让纯文本基础模型把图片转交给配置的**多模态模型**理解，多模态模型根据题目作答，基础模型结合上下文汇总回答用户。
3. **基础模型本身是多模态时，不引入该工具**：若 `base_model_multimodal=true`，图片直接以多模态 content 透传给基础模型，基础模型自己看；`ask_image` 工具**不注册**，模型不知道它的存在。

配置缺失兜底：若 `base_model_multimodal=false` 且**未配置** `multimodal_model`，仍注册 `ask_image` 工具；工具返回"看不见图片"给基础模型，由基础模型继续回答用户的文字部分（**绝不**在系统层短路，避免用户一大段文字里只有图片看不见、其余问题也答不上来）。

---

## 1. 配置（`config.json` 新增字段）

```json
{
  "multimodal_model": {
    "api_key": "",
    "base_url": "",
    "model": ""
  },
  "base_model_multimodal": false
}
```

- `multimodal_model` 三者皆空 → 视为"未配置"。
- `base_model_multimodal`：`true`=基础模型自带视觉；`false`=纯文本，需要视觉工具。
- 两个字段加入 `config.py` 的 `_CONFIG_FIELDS` 白名单与 `NanoClawConfig` dataclass 字段；`config.example.json` 同步补充示例。
- `api_key` 也支持环境变量覆盖（如 `MULTIMODAL_API_KEY`），优先级同主配置。

---

## 2. 数据结构

### 2.1 `bus/queue.py`

新增 dataclass：

```python
@dataclass
class ImageRef:
    id: str            # uuid4 hex，全局唯一
    path: str          # 落盘绝对路径：<sessions_dir>/<safe_key>_images/<id>.<ext>
    mime: str = "image/png"
```

`InboundMessage` 新增字段：

```python
images: Optional[List[ImageRef]] = None
```

base64 不落盘、不存储，工具调用时现生成。

---

## 3. 图片存储（`agent/imagestore.py`，新增 ImageStore）

落盘位置（**方案 A，现有 `.jsonl` 结构零改动**）：

```
workspace/sessions/
├── <safe_key>.jsonl          ← 现有结构，完全不动
└── <safe_key>_images/        ← 新增：该会话图片，uuid 命名
    ├── <uuid1>.png
    └── <uuid2>.jpg
```

`<safe_key> = session_key.replace(":", "_")`。目录与 `.jsonl` 文件同名不同型，不冲突；`SessionManager` 的 `*.jsonl` 扫描 / 读写 / 自愈逻辑**一行不改**。

接口：

```python
class ImageStore:
    def __init__(self, sessions_dir: str): ...
    def save(self, session_key: str, data: bytes, ext: str, mime: str = "image/png") -> ImageRef:
        # 计算 safe_key，确保 <safe_key>_images/ 存在，写 <id>.<ext>，返回 ImageRef
    def resolve(self, session_key: str, image_id: str) -> Optional[ImageRef]:
        # 在 <safe_key>_images/ 下按 id 找文件；找不到返回 None
    def clear(self, session_key: str) -> None:
        # rmtree <safe_key>_images/（不存在则静默忽略）
```

清理挂钩：在 `main.py` 的 `clear_callback` 里增加 `image_store.clear(session_key)`；若 Web 有删会话入口，同样挂钩。

---

## 4. 渠道接入（第一版仅 Web）

### 4.1 `channels/web.py`

- 新增 HTTP 上传接口 `POST /upload`（复用 web channel 的 aiohttp app）：接收 multipart 文件，调用 `ImageStore.save` 落盘，返回 `{"image_id": <id>, "mime": <mime>}`。
- WS 文本消息支持 JSON 格式：若收到 `{"text": "...", "images": ["<id>", ...]}`，构造 `InboundMessage` 时带 `images`（用 `ImageStore.resolve` 或缓存把 id 还原成 `ImageRef`）；纯文本仍按原逻辑。
- 前端需配合改造（不在本次后端范围，但后端先提供接口）；CLI / 飞书本次不动（飞书后续接，结构已预留）。

---

## 5. 网关（`gateway.py`）

`_handle_one` 改为：

```python
reply = await agent.run(msg.content, images=msg.images, stream_sink=stream_sink)
```

`agent.run` 内部用 `self.session_key` 推导 `safe_key`，无需网关额外传 session_key。

---

## 6. Agent（`agent/loop.py` + `agent/context.py`）

### 6.1 `agent.run` 签名

```python
async def run(self, message: str, images=None, stream_sink=None) -> str
```

- `images`：`Optional[List[ImageRef]]`，来自 `msg.images`。
- 用 `self.session_key` 推导 `safe_key`，供 ImageStore 使用。

### 6.2 消息装配（核心分支）

令 `base_mm = config.base_model_multimodal`。

**分支 A：`base_mm == true`（基础模型多模态）**
- 把 images 拼成多模态 content，加入当前 user 消息：
  ```python
  current_content = [{"type": "text", "text": text}]
  for ref in images:
      b64 = _b64(ref.path)
      current_content.append({
          "type": "image_url",
          "image_url": {"url": f"data:{ref.mime};base64,{b64}"},
      })
  ```
- `context.build_messages(history, current_content)` 需支持 `current_message` 为 `str` 或 `list`。
- **不注册** `ask_image` 工具。
- 历史回放时，若某 user 消息带 `images` 元数据，同样还原成多模态 content（见 §7）。

**分支 B：`base_mm == false`（纯文本基础模型）**
- 抽走 images，正文后追加占位符：
  ```
  [用户附 N 张图片，image_id：id1, id2… 如需理解或回答关于图片内容的问题，请调用 ask_image 工具，传入对应 image_id 与你的（可含上下文的）问题。]
  ```
- **始终注册** `AskImageTool`（注册见 §8）。
- 历史回放时，带 `images` 元数据的 user 消息同样用占位符表示，**绝不把图发回基础模型**。

### 6.3 工具执行时注入 session_key

`AskImageTool` 是共享单例（注册在 `build_shared` 的 tools 里，跨会话共用），需要当前 session 才能 resolve 图片路径。在 `loop.py` 的 `_execute_tools` 中，对 `ask_image` 的工具调用参数注入 `session_key=self.session_key`（其它工具忽略该参数即可）。

---

## 7. 历史持久化（跨轮引用）

- 保存 user 消息时，在 record 里附加 `"images": [{"id": ..., "mime": ...}, ...]` 元数据。`SessionManager.save_message` 原样 dump dict，无需改。
- `get_history` 回放时该字段会随消息返回（目前只 pop `timestamp` / `reasoning_content`）。使用处处理：
  - 发给 API 前**务必剥掉 `images` 字段**（OpenAI 不认自定义字段，否则 400）。
  - `base_mm=true`：把历史 user 消息里的 `images` 还原成多模态 content。
  - `base_mm=false`：用占位符替换。
- 图片字节落盘存活，重启后仍可按 `image_id` 取到 → 跨轮引用（"和刚才那张比一下"）成立。

---

## 8. 视觉工具（`agent/tools/vision.py`，新增 `AskImageTool`）

```python
class AskImageTool(Tool):
    name = "ask_image"
    description = (
        "当用户发送了图片、且需要理解或回答关于图片内容的问题时调用。"
        "把图片交给视觉模型解读并返回其回答。image_id 来自用户消息中的占位符；"
        "question 应是用户关于图片的问题（可包含相关上下文）。"
        "若未配置视觉模型，会说明无法看图，由你仅基于文字作答。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_id": {
                "type": ["string", "array"],
                "description": "图片标识（来自用户消息占位符），支持单张字符串或多张数组",
            },
            "question": {"type": "string", "description": "关于图片的问题，含必要上下文"},
        },
        "required": ["image_id", "question"],
    }

    def __init__(self, image_store, multimodal_cfg, session_key_getter):
        # image_store: ImageStore
        # multimodal_cfg: dict {api_key, base_url, model}
        # session_key_getter: callable() -> 当前 session_key（由 Agent 注入闭包）
        # 若 multimodal_cfg 已配置，懒构造 OpenAICompatProvider(api_key, base_url, model)

    async def execute(self, image_id, question, session_key=None):
        # 1. 归一化 image_id 为列表
        # 2. 逐个 ImageStore.resolve(session_key, id) -> ImageRef；读字节 -> base64
        #    找不到则返回 "未找到图片（image_id=...）"
        # 3. 若 multimodal_cfg 未配置：
        #        return "⚠️ 当前未配置视觉模型，我暂时看不见这张图片（image_id=...）。我将仅根据你文字里的问题作答。"
        #        （DeepSeek 会基于 question 中已有的上下文继续回答用户文字部分）
        # 4. 若已配置：
        #        content = [{"type":"text","text":question}]
        #        + 每张图 {"type":"image_url","image_url":{"url": f"data:{mime};base64,{b64}"}}
        #        resp = await vision_provider.chat([{"role":"user","content": content}])
        #        return resp.content or "视觉模型未返回内容"
```

- 视觉模型调用直接复用 `OpenAICompatProvider`（OpenAI 兼容视觉接口，base64 data URL 内嵌）。
- 多图：content 里放多张 `image_url` + 一个问题文本（一次性对比）。
- 省 token 关键点：**只把"精炼问题 + 图片"发给视觉模型，不灌整段历史**；图片绝不进基础模型。

---

## 9. 工具注册（`main.py`）

在 `build_shared()` 中：

```python
image_store = ImageStore(sessions_dir)
# ...
if not config.base_model_multimodal:
    tools.register(AskImageTool(image_store, multimodal_cfg, session_key_getter))
```

- `base_model_multimodal == true` → **不注册** `ask_image`。
- `multimodal_cfg` 从 `config.multimodal_model` 取出（空则视为未配置，但工具仍注册，由工具内部判断）。
- `session_key_getter`：由于工具是共享单例，`AgentLoop` 在构造时把自己的 `session_key` 通过闭包暴露；或在 `loop.py` 的工具执行处注入 `session_key` 参数（见 §6.3）。二者取其一，实现时以"执行时注入 session_key 参数"为准，更简洁。

`clear_callback` 增加 `image_store.clear(session_key)`。

---

## 10. 涉及文件清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `config.py` | 加 `multimodal_model` / `base_model_multimodal` 字段 + 白名单；环境变量 `MULTIMODAL_API_KEY` |
| 2 | `config.example.json` | 补充示例 |
| 3 | `bus/queue.py` | 新增 `ImageRef`；`InboundMessage.images` |
| 4 | `agent/imagestore.py` | 新增 `ImageStore`（save/resolve/clear） |
| 5 | `agent/tools/vision.py` | 新增 `AskImageTool` |
| 6 | `agent/context.py` | `build_messages` 支持 `current_message` 为 `str` 或 `list` |
| 7 | `agent/loop.py` | `run` 签名 + 分支装配 + 占位符 + 执行工具时注入 `session_key` |
| 8 | `gateway.py` | 透传 `images` |
| 9 | `channels/web.py` | `POST /upload` 接口 + WS JSON 携带 `images` |
| 10 | `main.py` | 装配 `ImageStore` + 条件注册 `AskImageTool` + `clear` 挂钩 |

---

## 11. 实施步骤（按序，每步可独立验证）

1. `config.py` + `config.example.json`：新增字段与白名单、环境变量。
2. `bus/queue.py`：新增 `ImageRef` 与 `InboundMessage.images`。
3. `agent/imagestore.py`：实现 `ImageStore`。
4. `agent/tools/vision.py`：实现 `AskImageTool`。
5. `agent/context.py`：`build_messages` 支持 `list` 形式 `current_message`。
6. `agent/loop.py`：`run` 分支装配 + 占位符 + 工具执行注入 `session_key`。
7. `gateway.py`：透传 `images`。
8. `channels/web.py`：上传接口 + WS JSON。
9. `main.py`：装配 `ImageStore` + 条件注册 + `clear` 挂钩。
10. 联调自测：
    - 场景一：`base_model_multimodal=false` + 配 `multimodal_model` → Web 上传图 → DeepSeek 调 `ask_image` → 视觉模型作答 → DeepSeek 汇总。
    - 场景二：`base_model_multimodal=false` + **未配** `multimodal_model` → 上传图 → 工具返回"看不见图片" → DeepSeek 仍答文字部分。
    - 场景三：`base_model_multimodal=true` → 图片直传基础模型，无 `ask_image` 工具。
    - 场景四：跨轮引用（"和刚才那张比一下"）在场景一/三下生效；`/clear` 后图片目录被清理。

---

## 12. 验收标准

- 图片从 Web 上传 → 落盘 `<safe_key>_images/` → 进入总线 → 被模型消费，全链路贯通。
- `base_model_multimodal=false` 时基础模型全程不收到图片字节，仅收到占位符 + `ask_image` 工具返回。
- `base_model_multimodal=true` 时图片作为多模态 content 直传，且 `ask_image` 不在工具列表。
- 多模态模型未配置时，用户文字问题仍被正常回答，仅图片部分提示不可见。
- 现有 `*.jsonl` 会话结构完全不变；`/clear` 清理图片目录。
