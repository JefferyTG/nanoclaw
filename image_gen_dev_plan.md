# NanoClaw 生图工具（图像生成 API）开发计划

> 本文档是实现的唯一权威依据。所有代码改动必须与此文档一致；若实现中发现设计盲点，先回到本文档修订，再改代码，避免"设计漂移"。
> 配套文档：image_vision_dev_plan.md（看图能力，已落地）。

## 0. 背景与目标

基础模型（DeepSeek 等纯文本模型）本身不能生图。用户希望 NanoClaw 具备**文生图**能力，且使用用户配置的**图像生成 API**（OpenAI 兼容的 images/generations 协议）。

目标（已与用户对齐的设计决策）：

1. **v1 仅做文生图（text-to-image）**，具体模型名由 `image_gen_model.model` 配置决定，参数 `prompt` + `size`。
2. **图生图（img2img）已实现**：复用同一个 `generate_image` 工具，新增可选参数 `image_id` / `image_url`。源图支持①本会话已有图（`image_id`，从 ImageStore 读字节转 base64 内联发送，绝不暴露 localhost URL）②公网 `image_url`；二者皆空则走文生图。`image_id` 复用 `ask_image` 已有的会话内图片标识机制（用户上传 / 之前生图的图都能引用，见 loop.py 注入占位符带 image_id）。图生图模型与请求体完全由 `image_gen_model.img2img_model` / `image_gen_model.img2img` 配置驱动，代码不写死任何服务商或模型名。
3. **工具始终注册**（不受 `base_model_multimodal` 影响）。未配置 `image_gen_model` 时仍注册，但返回"未配置生图模型"的友好提示给主模型，由主模型用文字继续回答（绝不系统层短路）。
4. **图片显示走「新增 `image` 流事件 + 落盘持久化」**：生图成功后，工具通过 `stream_sink` 推一个 `image` 事件，网页端在气泡内联显示；同时图片字节落 `ImageStore`（与 `ask_image` 同目录，随 `/clear` 自动清理），并把 `image_id` 写进 assistant 消息元数据，供历史回放。
5. **size 开放可选**：用户给了比例用用户的，没给默认 `1024x1024`。
6. **超时防护走工具内 HTTP 超时 + 429/500 有限重试**（用户明确不选 loop 级 `wait_for` 包装）：默认 120s（可配 `image_gen_model.timeout_sec`），超时/限流由工具返回文本给主模型优雅收尾，**不拖垮整轮**。

---

## 1. 配置（`config.json` 新增字段）

```json
{
  "image_gen_model": {
    "api_key": "",
    "base_url": "",
    "model": "",
    "timeout_sec": 120,
    "img2img_model": "",
    "img2img": {
      "image_field": "image",
      "image_location": "body",
      "encoding": "auto",
      "as_array": true,
      "strength_field": "",
      "strength": 0.0,
      "tags": []
    }
  }
}
```

- `image_gen_model` 三者皆空 → 视为"未配置"（工具返回友好提示）。
- `image_gen_model` 默认值为空（不预填任何服务商），`api_key` / `base_url` / `model` 三者需用户自行填写（或 `api_key` 走环境变量）。**具体用哪个服务、哪个模型完全由用户决定，代码不绑定、不写死任何具体服务商或模型名。**
- `api_key` 也支持环境变量覆盖：`IMAGE_GEN_API_KEY`（命中即覆盖），优先级同主配置。
- `image_gen_model.timeout_sec`：生图 HTTP 超时（秒），文生图 / 图生图共用，仅此一个入口，缺省回落 120。
- `image_gen_model.img2img_model`：图生图专用模型；留空则回落到 `model`（部分服务商文 / 图生图共用同一模型）。
- `image_gen_model.img2img`：图生图请求体装配配置（服务商相关，全部可配、不写死）：`image_field` 源图键名（默认 `image`）、`image_location` 源图放顶层 `body` 还是 `extra_body` 嵌套（默认 `body`）、`encoding` 源图编码，默认 `auto`（按源图类型自动：本地图 `base64` 内联 / 公网链接 `url` 直发），也可显式 `base64` / `url` 强制统一；`as_array` 源图是否以数组形式传（默认 `true`，支持多图，如 Agnes 即如此）、`strength_field` 强度键名（空=不传）、`strength` 强度默认值、`tags` 服务商标签列表（如 `["img2img"]`）。
- `image_gen_model` 整体作为一颗 dict 加入 `config.py` 的 `_CONFIG_FIELDS` 白名单与 `NanoClawConfig` dataclass（含内嵌的 img2img 子配置）；`config.example.json` 同步补充（留空占位）；`channels/web.py` 的 `_CONFIG_FIELDS` 同步加入（保证网页「保存配置」不会把生图配置冲掉）。

---

## 2. 数据结构与复用

- **复用 `ImageStore`**（agent/imagestore.py）：`save(session_key, data, ext, mime) -> ImageRef`，落盘到 `<safe_key>_images/`，与 `.jsonl` 并存、零改动；`/clear` 自动清理（main.py 的 `clear_callback` 已挂钩）。
- **复用 `/image` 端点**（channels/web.py）：`GET /image?key=<web:...>&id=<hex>` 回显图片字节；前端缩略图与历史回放已用它。生图服务返回的是公网 CDN URL，但本项目不依赖外部 CDN（图片下载后落本地 `ImageStore`），即使 CDN 失效也不影响已落盘图片的回显。
- **新增流事件 `image`**（复用于 `StreamEvent` / `stream_sink` 通道，仅网页消费，与 thinking/token/tool_call 同级）。

---

## 3. 生图工具（`agent/tools/imagegen.py`，新增 `GenerateImageTool`）

继承 `agent/tools/base.py` 的 `Tool` ABC（name / description / parameters / async execute）。

```python
class GenerateImageTool(Tool):
    name = "generate_image"
    description = (
        "根据文字描述生成一张图片。当用户明确要求'画图/生图/画一张…'时调用。"
        "prompt 为画面描述（中文即可）；size 可选，形如 '1024x1024'/'1024x768'/"
        "'768x1024'，不填默认 1024x1024。生成成功后图片会直接在对话里显示，"
        "也可在本会话内引用。若想基于某张已有图片改造（图生图），可额外传入 "
        "image_id（本会话已有图片的标识，见 ask_image 的 image_id）或 image_url"
        "（公网图片链接）；不传则按文字全新生成。若未配置生图模型，会告知无法生图。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "画面描述（中文即可）"},
            "size": {"type": "string",
                     "description": "可选尺寸，形如 '1024x1024'/'1024x768'/'768x1024'，不填默认 1024x1024"},
            "image_id": {"type": "string",
                     "description": "可选，图生图源图标识：本会话已有图片的 image_id（用户上传或之前生成的图）"},
            "image_url": {"type": "string",
                     "description": "可选，图生图源图公网 URL；与 image_id 二选一，image_id 优先"},
        },
        "required": ["prompt"],
    }

    def __init__(self, image_store, config):
        # image_store: ImageStore（落盘生成图 + 按 image_id 读取源图）
        # config: NanoClawConfig（实时读 image_gen_model，含 timeout_sec / img2img_model / img2img）
        # 具体服务地址与模型名由配置决定，本工具不绑定任何服务商。

    async def execute(self, prompt, size=None, image_id=None, image_url=None,
                      session_key=None, stream_sink=None, _generated_ids=None) -> str:
        # 1. 未配置 → 友好提示（让主模型继续文字回答）
        # 2. 解析 size（默认 1024x1024，格式非法回落默认）
        # 3. 解析源图：image_id（从 ImageStore 读字节）/ image_url（公网）二选一
        #    → 有源图则走 img2img 分支；都没有则走 txt2img
        # 4. txt2img:
        #    payload = {"model": model, "prompt": prompt, "size": "WxH"}
        #    img2img:
        #    model = img2img_model or model
        #    source = base64(data:...) 或 url（按 img2img.encoding）
        #    payload = {model, prompt, size?(仅用户显式传 size 才带), <image_field>: source,
        #               [strength_field: strength], [tags]}  （location=extra_body 时整体塞进 extra_body）
        # 5. httpx POST <image_gen_model.base_url>/images/generations
        #    headers: Authorization: Bearer <key>, Content-Type: application/json
        #    timeout = image_gen_model.timeout_sec（默认 120s，缺省回落）
        #    重试：429（等 8s）/ 5xx（等 5s）/ 连接或超时异常，最多 3 次；
        #          全部失败或最终超时 → 返回文本提示给主模型，不抛异常、不拖垮整轮
        # 6. 解析返回：优先 data[0].url（下载字节），其次 data[0].b64_json（解码）
        # 7. ImageStore.save(session_key, 字节, ext, mime) → ImageRef
        # 8. stream_sink 非 None 且 session_key 有值 →
        #    await stream_sink({"type":"image","key":session_key,
        #                        "id":ref.id,"url":f"/image?key={session_key}&id={ref.id}",
        #                        "mime":ref.mime})
        # 9. _generated_ids 列表存在 → 追加 ref.id（供主循环写历史元数据）
        # 10. 返回文本：✅ 已[文生图/图生图]生成图片（image_id=...，WxH）。图片已保存到本会话…
```

- **HTTP 客户端用 httpx**（pyproject 已依赖 `httpx>=0.28.1`）：直接打图像端点，精确控制 timeout 与 429/5xx 重试，避开 OpenAI SDK 的 `extra_body` 怪异行为（response_format/image 必须塞 `extra_body`，易踩坑）。
- **不把图片字节回传给主模型**（主模型是纯文本，塞图无意义）；只回文字结果 + image_id。
- **图生图（img2img）实现**：单工具内部分支。`execute` 收到 `image_id` / `image_url` 即走图生图：
  - 源图解析 `_resolve_source`：`image_id` 从本会话 `ImageStore` 读字节（base64 内联用），`image_url` 直发；都无则文生图。
  - 请求体拼装 `_build_img2img_payload`：**纯配置驱动、不写死服务商**——按 `image_gen_model.img2img`（image_field / image_location / encoding / as_array / strength_field / strength / tags）组装：源图编码默认 `auto`（按每张源图自动——本地图 base64 内联、公网链接 url 直发），也可显式 base64/url 强制统一；**多张源图各自拼好后放进同一个 `image` 数组**（as_array 默认 true，符合 Agnes「extra_body 里传 image 数组、支持多图」的契约）；放顶层 `body` 或 `extra_body` 嵌套；可选强度与标签。模型取 `img2img_model` 缺省回落 `model`。`size` 仅当用户显式传入时才带（避免与源图尺寸冲突）。
  - 其余（httpx 超时 + 重试、下载/解码、落 ImageStore、推 image 事件、回写 image_id、友好收尾）与文生图**完全复用**同一段逻辑，仅 `payload` 不同。

---

## 4. 工具注册（`main.py`）

在 `build_shared()` 中**无条件**注册（与 `base_model_multimodal` 无关）：

```python
tools.register(GenerateImageTool(image_store, config))
```

放在 `ask_image` 注册之后。`clear_callback` 已挂钩 `image_store.clear`，生图落盘随 `/clear` 自动清理，无需额外改动。

---

## 5. Agent（`agent/loop.py`）

### 5.1 工具执行注入参数

`_execute_tools` 中，对 `generate_image` 注入三个额外参数（与 `ask_image` 注入 `session_key` 同机制）：

```python
exec_args = dict(tc.arguments)
if tc.name in ("ask_image", "generate_image"):
    exec_args["session_key"] = self.session_key
if tc.name == "generate_image":
    exec_args["stream_sink"] = stream_sink          # 实时推 image 事件（非网页为 None）
    exec_args["_generated_ids"] = gen_ids           # 收集生成的 image_id
```

`gen_ids` 在 `_execute_tools` 入口初始化为空列表，跨本轮多个 tool_call 累积。

### 5.2 assistant 消息持久化附 generated_images 元数据

`assistant_msg`（带 `tool_calls`）原在 `run()` 里于执行工具**前**持久化；改为执行工具**后**持久化，以便把生成的 `image_id` 写回：

- `run()` 不再在构造 `assistant_msg` 后立即 `self._persist(assistant_msg)`，改为把 `assistant_msg` 传给 `_execute_tools`，由其负责持久化。
- `_execute_tools` 正常结束（未触发熔断/提前 return）时：
  - 若 `gen_ids` 非空：`record = dict(assistant_msg); record["generated_images"] = gen_ids; self._persist(record)`，然后 `assistant_msg.pop("generated_images", None)`（保证内存中 `messages` 副本干净，避免下一轮把 `generated_images` 回传给 API 触发 400）。
  - 否则：`self._persist(assistant_msg)`。
- 熔断分支提前 return 前也 `self._persist(assistant_msg)`（此时无生成图）。

### 5.3 历史回放剥离渲染元数据

`_history_item_to_api` 构造发给 API 的消息时，剥离渲染专用字段：

```python
return {k: v for k, v in msg.items() if k not in ("images", "generated_images")}
```

（原只剥 `images`，现一并剥 `generated_images`，避免回传 API 400。）

---

## 6. 网页前端（`webui/index.html`）

复用现有「一个 bot 回合 = 思考 + 工具活动 + 正式回答」结构，新增图片条：

1. **CSS**：`.bot-images { justify-content:flex-start; margin-top:8px; }`（复用 `.img-strip` 的图片样式，仅左对齐）。
2. **`newTurn()`**：在 `activity` 与 `ansWrap` 之间插入 `botImages = div.img-strip.bot-images`，并把它加入返回的 turn 对象。
3. **`onEvent` 处理 `image` 事件**：
   ```js
   } else if (t === 'image') {
     var url = ev.url || ('/image?key=' + encodeURIComponent(currentKey) + '&id=' + encodeURIComponent(ev.id || ''));
     var im = document.createElement('img');
     im.src = url; im.loading = 'lazy'; im.style.maxWidth = '320px';
     cur.botImages.appendChild(im);
   }
   ```
   生图事件在工具执行阶段到达，图片出现在正式回答之前，符合直觉。
4. **`renderHistory` 回放 assistant 消息**：
   ```js
   if (m.generated_images && m.generated_images.length && currentKey) {
     m.generated_images.forEach(function (iid) {
       var im = document.createElement('img');
       im.src = '/image?key=' + encodeURIComponent(currentKey) + '&id=' + encodeURIComponent(iid);
       im.loading = 'lazy'; im.style.maxWidth = '320px';
       c.botImages.appendChild(im);
     });
   }
   ```
   使重新打开会话时，生过的图能再次显示。

非网页渠道（CLI/飞书）：`stream_sink` 为 `None`，`image` 事件不触发；工具仍落盘并返回文字结果（含 image_id），CLI 用户可在磁盘取到图片。不阻塞这些渠道。

---

## 7. 涉及文件清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `image_gen_dev_plan.md` | 新文档（本文） |
| 2 | `config.py` | 加 `image_gen_model` 字段（含 timeout_sec）+ 白名单；环境变量 `IMAGE_GEN_API_KEY` |
| 3 | `config.example.json` | 补充 `image_gen_model` 示例（含 timeout_sec，留空占位） |
| 4 | `channels/web.py` | `_CONFIG_FIELDS` 加入 `image_gen_model`（保存配置不被冲掉） |
| 5 | `agent/tools/imagegen.py` | 新增 `GenerateImageTool`（httpx + 重试 + 落盘 + image 事件 + 文生图/图生图单工具分支） |
| 6 | `agent/loop.py` | `_execute_tools` 注入 session_key/stream_sink/_generated_ids；assistant_msg 后置持久化附 generated_images；`_history_item_to_api` 剥离 generated_images |
| 7 | `webui/index.html` | newTurn 加 bot-images；onEvent 处理 image；renderHistory 回放 generated_images |
| 8 | `main.py` | 无条件注册 `GenerateImageTool` |

---

## 8. 实施步骤（按序，每步可独立验证）

1. `config.py` + `config.example.json` + `channels/web.py`：新增字段与白名单、环境变量。
2. `agent/tools/imagegen.py`：实现 `GenerateImageTool`（文生图 + 图生图单工具分支，配置驱动拼装）。
3. `agent/loop.py`：注入与持久化改造。
4. `webui/index.html`：内联图片与历史回放。
5. `main.py`：注册工具。
6. 联调自测：
   - 场景一：未配置 `image_gen_model` → 调 `generate_image` → 工具返回"未配置"提示 → 主模型用文字继续。
   - 场景二：配了生图模型 key → 文生图 → 网页端内联显示图片，消息历史存 image_id。
   - 场景三：size 不填 → 默认 1024x1024；填 `1024x768` → 用用户值。
   - 场景四：模拟超时/429 → 工具返回文本，不拖垮整轮、网页不卡死。
   - 场景五：`/clear` 后图片目录被清理；重新打开历史会话能回放生过的图。
   - 场景六（图生图）：传 `image_id`（本会话已有图）→ 工具读字节转 base64 内联、按 `img2img` 配置拼装请求 → 生成改造图，同样内联显示、可继续引用；传 `image_url` 公网图同理。
   - 场景七（图生图模型回落）：未配 `img2img_model` → 自动用 `model`；配了 → 用 `img2img_model`。

---

## 9. 验收标准

- 配置 `image_gen_model.api_key` 后，文字描述能生成图片并在网页对话气泡内联显示。
- 未配置时工具返回友好提示，主模型照常文字回答，不报错、不短路。
- 生成的图片落 `ImageStore`，随 `/clear` 清理；历史回放可再次显示。
- 生图慢/限流时，工具内部超时与重试兜住，主模型收到文本收尾，整轮不卡死。
- 现有 `.jsonl` 会话结构不被破坏；`generated_images` 等渲染元数据不回传给 API（无 400）。
