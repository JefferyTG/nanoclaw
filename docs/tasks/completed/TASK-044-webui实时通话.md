# TASK-044-webui 实时通话（浏览器「打电话」）

> 状态：已完成（2026-08-13 真机验收通过）
> 创建：2026-08-12 ｜ 负责人：小奈 + code-master ｜ 基线 commit：main（TASK-043 之后）

## 目标
在 webui 里加一个「📞 打电话」入口：手机/浏览器点开后与豆包 S2S 全双工实时语音对话（浏览器采集麦克风/播放回复），复用人设 `realtime_identity.md` 与专属音色 `S_ZUpBmlGb2`。

## 背景
- 已有 `realtime` 渠道（TASK-037）：豆包 S2S 全双工已打通，但音频绑死 MacBook 本地声卡（sounddevice），只能在 Mac 上用，手机/浏览器不可用。
- 工作区有豆包官方 `web_demo`（`workspace/demo/web_demo/web_duplex_demo/`）：浏览器 getUserMedia 采集 → WS 转发 → 豆包 S2S；前端 `app.js`（3198 行）含完整录音/opus 解码/播放实现。
- 结论（乖宝 2026-08-12 确认）：**连豆包复用 `voice/realtime_s2s/client.py`（纯网络层，与声卡解耦）；新写一个中继桥把浏览器 WS 与 client 事件流对接；前端复用/精简 demo 的 app.js**。

## 范围
- 后端：web 服务（`channels/web.py`）新增一条 WebSocket 路由（`/api/realtime`），中继桥实现：浏览器 WS ⇄ `RealtimeS2SClient` 事件/音频双向转发。
- 复用 `voice/realtime_s2s/client.py`（connect / create_session / send_event / iter_events），**不改 client.py**（实际未改）。
- 人设：从 `realtime_identity.md` 读取（与 realtime 渠道一致）；音色：config `realtime.voice`（当前 `S_ZUpBmlGb2`）。
- 前端：webui 增加「📞 打电话」按钮 + 通话界面（接通/挂断/状态显示），录音/播放核心参考 demo `app.js` 精简移植。
- 多会话/并发：一次只允许一路实时通话——**方案 A（乖宝 2026-08-13 确认）：第二路直接拒绝并提示，不踢旧通话**。

## 非目标
- 不做本地声卡采集（那是 realtime 渠道的活，保持现状）。
- 不做 KWS 唤醒（浏览器端不需要）。
- 不改 `voice/realtime_s2s/uplink.py` / `downlink.py`（本地声卡路径不动）。
- 不重写 demo 前端，只移植必要的录音/播放核心。

## 验收标准
- [x] webui 出现「📞」按钮，点击后建立通话；浏览器授权麦克风后可与小奈实时语音对话（手机+桌面浏览器各验一次）
- [x] 语音走豆包 S2S：人设来自 `realtime_identity.md`、音色为 `S_ZUpBmlGb2`（`config.realtime.voice`）
- [x] 通话中可正常打断（服务端动态判停）、可挂断；挂断后 session 优雅关闭（复用 client.close_session）
- [x] 多端 WebSocket 并发下状态正确：通话中再次点击不重复建会话
- [x] 无安全上下文（HTTP 非 localhost）时给出友好提示（对齐 TASK-043 权限提示风格）
- [x] 测试：`unittest discover -s tests -t .` 全量通过（962 tests OK）；新增中继桥单测（fake WS / fake client，7 用例 OK）

## 相关模块
- `channels/web.py`（WebSocket 路由）｜ `webui/index.html`（前端入口与通话 UI）｜ `voice/realtime_s2s/client.py`（复用）｜ `realtime_identity.md`（人设源，仓库根目录，.gitignore）
- 参考：`workspace/demo/web_demo/web_duplex_demo/server.py`（中继思路）＋ `static/app.js`（前端音频核心）

## 实现方案
```
手机浏览器(app.js 精简移植) ⇄ WS(/api/realtime) ⇄ RealtimeBridge ⇄ RealtimeS2SClient ⇄ 豆包
```
- RealtimeBridge：持有 client + 浏览器 WS 连接；浏览器上行音频帧（base64）→ `client.send_event(input_audio_buffer.append)`；client `iter_events` 下行 → WS 推送 `response.output_audio.delta` 等事件给浏览器解码播放。
- 会话生命周期：WS 连接建立 → `client.connect()` → `create_session(instructions=realtime_identity, voice_type=config.realtime.voice)` → 双向转发 → WS 断开/客户端挂断 → `close_session()` 优雅关闭。
- 人设/音色/API key 均从 config 与现有文件读取，不硬编码；key 不落文档。

## 测试方式
- 单测：`tests/` 下新增 `test_realtime_bridge.py`（fake browser WS + fake client 验证转发/生命周期/并发互斥）。
- 集成冒烟：起服务 → curl/浏览器连 `/api/realtime` → 验证 session.create 与事件流转（需真实 API key，仅手动/本地）。
- 全量：`unittest discover -s tests -t .`。

## 风险
- ~~浏览器 opus 解码（demo 用 WebCodecs/MediaSource）在低端安卓兼容性待真机验证~~ → **已消除**：实现走 **PCM 直通**（下行 `pcm_s16le` 24k 直接 AudioBuffer 播放），不搬 demo 的 opus 解码。
- 浏览器 `AudioContext({sampleRate:24000})` 个别老内核可能忽略参数导致播放采样率错配——真机验收时留意音调/语速。
- 上行用 `ScriptProcessor`（已废弃但兼容性好），极端低端安卓可能有轻微丢帧/延迟，必要时可升级 AudioWorklet（后续再做）。
- 中继桥与 realtime 渠道同时启用时的 API key / 音色一致性问题（同一份 config，无冲突预期）。
- 手机浏览器麦克风权限弹窗循环问题（OPPO 自带浏览器不记忆权限）——非本任务范围，建议引导用 Chrome。

## 实现记录（2026-08-13）

### 改动文件
- `channels/web.py`（+192）：新增 `RealtimeBridge` 类（run：connect→create_session→relay→finally close_session；_relay：上行/下行两 task FIRST_COMPLETED 互取消）、`/api/realtime` 路由 + `_handle_realtime` + `_serve_realtime`（互斥校验→api_key 校验→读人设→建桥→转发→释放互斥位）、`_realtime_config`/`_load_realtime_identity`/`_realtime_close`、模块级 `REALTIME_IDENTITY_FILE`（与 realtime 渠道同文件）、`__init__` 加 `realtime_client_factory` 注入点与 `_active_realtime` 互斥位。复用 `RealtimeS2SClient`，`client.py` 未改。
- `webui/index.html`（+305）：header 加「📞」按钮 + 通话悬浮面板（状态/转写 + 挂断）；自包含 ES5 通话代码——getUserMedia→AudioContext+ScriptProcessor→16k PCM→20ms/640B 切帧→base64 上行；下行 base64→int16 PCM→AudioBuffer 播放（`CallPcmPlayer`）；安全上下文检查；通话中禁用 📞。
- `tests/test_realtime_bridge.py`（新增，236 行）：7 用例（上行转发+生命周期顺序 / 下行转发 / 心跳 None 跳过 / create 失败仍 close / 并发互斥 busy 不踢旧 / no_api_key 拒绝 / config+人设透传）。

### 关键决策
- **下行 PCM 直通，不走 opus**：`client.py::create_session` 输出写死 `pcm_s16le` 24k，与 demo 的 `ogg_opus` 不一致；走 opus 需在 Python 端加编码或搬 demo 的 WebCodecs/MediaSource 解码，改动大且低端安卓兼容风险高。PCM 带宽 ≈48KB/s（base64 后 ≈64KB/s），局域网/手机无压力，浏览器 AudioBuffer 播 int16 PCM 仅约 20 行。**符合 ponytail：砍掉 demo 的 OggOpusDemuxer/WebCodecsOggOpusPlayer/MediaSourceEncodedAudioPlayer。**
- **打断对齐 realtime 渠道**：`input_audio_transcription.started` 时停掉当前下行播放器丢排队旧音频，打断交豆包服务端动态判停，客户端不发 `response.cancel`。
- **并发方案 A**：`_active_realtime` 互斥位（web 事件循环内 check 与 set 之间无 await，单线程原子），第二路返回 `realtime.error(code=busy)` 并关闭，不踢旧通话。

### 验证结果（真实输出）
- `git diff --check` → 干净
- `python -m compileall -q channels` → OK；`import main` → OK
- 新单测 `.venv/bin/python -m unittest tests.test_realtime_bridge -v` → **Ran 7 tests OK**
- 全量 `.venv/bin/python -m unittest discover -s tests -t .` → **Ran 962 tests OK**
- `node --check`（提取 index.html 主脚本）→ JS syntax OK

### 真机验收结论（2026-08-13 乖宝确认）
- 手机 + 桌面浏览器均可正常打电话、实时对话、挂断，音色/打断正常。
- 敏感信息提交前已扫描：无真实 api_key / tailnet / IP / 机器名 / 证书路径泄露；`config.json` / `realtime_identity.md` / `workspace/` 均在 .gitignore 覆盖内。

## 下一步
- 无（已完成归档）。
