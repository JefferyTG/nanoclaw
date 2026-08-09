# NanoClaw 本地部署音色复刻 TTS 可行性调研与选型报告

> 调研日期：2026-07 ｜ 目标：本地部署能克隆「甘雨（林簌中配）」音色的 TTS，替代云端 DashScope `qwen3-tts-vc-realtime-2026-01-15`（按字符计费）
> 目标硬件：① RTX 4060（8GB 显存）② 未来 MacBook（M 系列，16/32/64GB 统一内存）
> 要求：克隆甘雨（林簌）声音、流式或近流式、低延迟、中文效果好

---

## 一、结论摘要

1. **重大发现：云端 `qwen3-tts-vc` 对应的模型已经开源。** Qwen 团队于 2026-01-22 发布 Qwen3-TTS 全系列（0.6B / 1.7B，Apache 2.0），其中 **Base 模型就是官方 3 秒语音克隆（voice clone）模型**，与 DashScope 上 `qwen3-tts-vc-*`（VC=Voice Clone）同源同架构。也就是说，**本地部署 = 把现在付费的云服务直接搬回家，听感一致性最高**。
2. **4060（8GB）上可行性明确：** Qwen3-TTS-0.6B 仅需约 4GB 显存、1.7B 约 6GB，官方报告 RTF≈0.29~0.31（即约 3 倍实时），支持流式（首包低至 97~101ms），并有 GGUF 量化版（0.6B Q8_0 仅 ~1GB）。**4060 首选 Qwen3-TTS，备选 CosyVoice2-0.5B / GPT-SoVITS。**
3. **MacBook 上也有官方生态：** `mlx-audio` 已原生支持 Qwen3-TTS（含音色克隆），8bit 量化后 1.7B 仅 2.4GB、0.6B 仅 1.3GB，M4 Air 实测内存占用 2~3GB。需注意自回归解码受内存带宽限制，基础版 M 芯片可能接近而非超过实时，M Pro/Max 无压力。
4. **甘雨克隆只需 3~10 秒参考音频，且已有社区验证：** fish.audio 上已存在社区上传的「甘雨」音色（400+ 次使用），ACGN TTS 在线站有 2000+ 游戏角色（含甘雨）。项目现有云端 voice_id 表明参考音频已备好，本地用同一份音频即可复现。
5. **成本结论：** 本地化后单次推理成本≈电费，适合 NanoClaw 这种高频、长文本、角色化对话场景；代价是需自行维护 GPU/环境/模型更新，且 4060 上若并发需求高需做排队/批处理。

---

## 二、qwen3-tts-vc 开源情况（重点）

**结论：Qwen3-TTS 已完全开源，且就是云端 qwen3-tts-vc 的本地版。**

| 项目 | 说明 |
|---|---|
| 官方仓库 | `QwenLM/Qwen3-TTS`（GitHub，12.9k★，Apache-2.0） |
| 权重下载 | HuggingFace `Qwen/Qwen3-TTS-*` 与 ModelScope `Qwen/Qwen3-TTS-*` 双通道 |
| 发布时间 | 2026-01-22（技术报告 arXiv:2601.15621，5M+ 小时语料，10 种语言含中文） |
| 与云端关系 | 阿里云百炼文档确认 Qwen-TTS 系列含 VC（Voice Clone）模型 `qwen3-tts-vc-2026-01-22`；官方 README 的 DashScope 章节列有「Real-time API for Qwen3-TTS of voice clone model」，即项目在用的 `qwen3-tts-vc-realtime-2026-01-15` 对应开源 Base 模型的克隆能力 |

### 开源权重清单与资源需求

| 模型 | 定位 | 克隆 | 流式 | 指令控制 | 显存(bf16) | 量化后 |
|---|---|---|---|---|---|---|
| Qwen3-TTS-12Hz-**0.6B-Base** | 3 秒语音克隆 | ✅ | ✅ | — | ~4GB | Q8_0≈1.0GB / Q4_K_M≈0.63GB |
| Qwen3-TTS-12Hz-**1.7B-Base** | 3 秒语音克隆（效果最佳） | ✅ | ✅ | — | ~6GB | Q8_0≈2.1GB / Q4_K_M≈1.2GB |
| 0.6B/1.7B-CustomVoice | 9 个内置音色 + 指令风格控制 | — | ✅ | ✅ | 同上 | 同上 |
| 1.7B-VoiceDesign | 用文字描述生成新音色 | — | ✅ | ✅ | ~6GB | 同上 |

**性能（官方技术报告，NVIDIA GPU + vLLM，单并发）：**
- 12Hz-0.6B：首包延迟 **97ms**，RTF **0.288**（约 3.5 倍实时）
- 12Hz-1.7B：首包延迟 **101ms**，RTF **0.313**
- 流式架构为「Dual-Track hybrid streaming」，单字符输入即可出首个音频包，满足实时对话

**社区实测补充：** 整合包作者实测 0.6B 用 4G 显存可跑（推理速度约 1:0.5）、1.7B 用 6G 显存可跑；YouTube 评测称单模型约 3~4GB VRAM（1.7B bf16）；`qwentts.cpp`（GGUF）提供 0.6B/1.7B 的 Q4/Q8 量化可直接在低显存跑。

**对 NanoClaw 的意义：** 目前云端音色 `qwen-tts-vc-myclone-voice-...` 是用参考音频创建的克隆音色，本地用同一段参考音频走开源 Base 模型 `generate_voice_clone()`，即可获得与云端同源的甘雨音色——**不需要微调，零训练成本**。

---

## 三、开源本地音色复刻 TTS 横向对比

> 评价口径：克隆像不像（原声相似度）、中文效果、资源需求、速度、流式、部署难度、双平台可行性。

| 方案 | 克隆效果（像不像） | 中文效果 | 显存/内存 | 速度（RTF/实时倍数） | 流式 | 部署难度 | 4060(8G) | MacBook |
|---|---|---|---|---|---|---|---|---|
| **Qwen3-TTS Base 0.6B/1.7B**（阿里，Apache-2.0） | ★★★★★ 官方宣称 3s 克隆 SOTA，**与云端 qwen3-tts-vc 同源** | ★★★★☆ 中文原生 10 语言之一，韵律自然 | 4GB / 6GB（bf16）；GGUF Q8 1~2GB | RTF 0.29~0.31（≈3x 实时，官方 vLLM 数据） | ✅ 原生流式，首包 ~100ms | 中（pip 装 qwen-tts，vLLM 可上生产） | ✅✅ 首选 | ✅✅ MLX 官方支持 |
| **CosyVoice2-0.5B**（阿里 FunAudioLLM，Apache-2.0） | ★★★★★ 3s 极速复刻，中文音色还原口碑最佳 | ★★★★★ 中文效果公认顶级（阿里中文家底） | 5~6GB（0.5B，社区整合包 6G 占用） | RTF 0.25（v2），首包 ~150ms | ✅ 原生流式（含 OGG 流式接口） | 中（ModelScope 下载 + 依赖稍多） | ✅✅ 完全可跑 | ❌ 官方无 MPS，仅 CPU（M3 Pro 单句 ~800ms） |
| **GPT-SoVITS**（RVC-Boss，60k★，MIT） | ★★★★☆ zero-shot 5s 极限复刻；1 分钟微调后相似度极高、能学说话习惯 | ★★★★★ 中文社区最活跃、最稳 | 推理 2GB；微调训练 6GB+（4060 可训 batch1~2） | 接近实时（优化后百字 <1.5s） | ⚠️ 有限（分块/WebSocket 社区方案） | 中偏高（训练/切片/ASR 流程繁琐） | ✅✅ 推理无压力，微调亦可 | ⚠️ CPU 可跑较慢，MPS 支持有限 |
| **F5-TTS**（SWivid，代码 MIT / 权重 **CC-BY-NC 非商用**） | ★★★★☆ few-shot 5~15s 克隆，相似度不错 | ★★★☆☆ 中英基础，多音字/稳定性一般（长文易爆音） | ~3GB VRAM（模型小） | RTF：4090 5x / 4070 3x / 3060 2x / M4 Max 1.5x / M2 0.8x / CPU 0.3x | ⚠️ 有流式实现但质量略降 | 低 | ✅ 可跑 | ✅ MPS 可跑（M2 略低于实时） |
| **Fish-Speech 1.4**（fishaudio，代码 BSD-3 / 权重 **CC-BY-NC-SA 非商用**） | ★★★★☆ 10~30s 克隆，稳定但音色相似度中上 | ★★★★☆ 中文好、多语言混读强 | 推理 4GB；微调 16GB | 4060 笔记本 RTF≈1:5（fish-tech 加速），不编译则慢 | ⚠️ 有限 | 中 | ✅ 可跑 | ⚠️ 官方支持 macOS 但非 MLX 原生 |
| **ChatTTS**（CC-BY-NC 非商用） | ❌ 官方不支持自定义克隆（只能选预设音色） | ★★★★☆ 对话场景中文自然 | 轻量（4G 可跑） | 接近实时 | ✅ | 低 | ✅ | ⚠️ |
| **OpenVoice V2**（MyShell，MIT） | ★★★☆☆ 音色转换/跨语种强，但强项英文，**中文合成效果一般** | ★★☆☆☆ 中文一般 | 轻量 | 快 | ❌ | 中 | ✅ | ⚠️ |

**一句话定位：**
- **ChatTTS**：对话场景预设音色 TTS，不做克隆 → 直接排除。
- **OpenVoice**：本质是音色转换（VC）+ 跨语言，不是端到端中文 TTS → 定位不符，排除。
- **F5-TTS / Fish-Speech**：克隆和速度都不错，但**权重非商用授权**，且中文/稳定性不如前两者 → 备选。
- **核心候选：Qwen3-TTS Base（同源云端）、CosyVoice2-0.5B（中文最强）、GPT-SoVITS（可微调、中文最稳、显存最低）。**

---

## 四、甘雨（林簌中配）克隆可行性评估

**1. 最少参考音频时长（各方案官方口径）：**

| 方案 | 最少参考音频 | 备注 |
|---|---|---|
| Qwen3-TTS Base | **3 秒** | 官方宣称 3 秒快速克隆 |
| CosyVoice2-0.5B | **3 秒**（极速复刻） | 官方 3s 极速复刻；30s 内效果更稳 |
| GPT-SoVITS | **5 秒**（zero-shot 极限复刻） | 1 分钟微调后相似度/稳定性大幅提升 |
| F5-TTS | 5~15 秒 | 官方推荐 5-15s 清晰无噪参考 |
| Fish-Speech 1.4 | 10~30 秒 | 官方克隆最佳实践 |

**2. 对 2~10 秒样本的克隆效果排序（综合社区评测）：** CosyVoice ≥ Qwen3-TTS ＞ GPT-SoVITS（zero-shot）≈ F5-TTS。若愿花 1 分钟音频微调，GPT-SoVITS 可追平甚至超越（能学说话习惯、稳定性最佳）。

**3. 现成参考音频来源（仅评估可行性，不实际获取）：**
- **项目已有资产**：NanoClaw 现云端 voice_id（`qwen-tts-vc-myclone-voice-...`）说明甘雨参考音频早已准备好且克隆成功——**本地部署可直接复用同一份参考音频**，这是最稳的路径。
- **社区已验证来源**：fish.audio 上存在社区上传的「甘雨（原神/冰系/璃月）」音色（400+ 次使用量）；ACGN TTS 在线站（acgn.ttson.cn）提供 2000+ 游戏角色（含甘雨）配音；B站/YouTube 有大量角色语音合集视频（游戏内语音可切出数秒~数十秒干净人声）。

**4. 版权提示：** 甘雨中配由配音演员林簌演绎，声音/角色版权归米哈游及相关权利人。本地个人/项目内使用（克隆评估、自用合成）在多数司法辖区属灰色地带，风险自担；**对外商用发布必须取得授权**。本报告仅做可行性评估，不提供实际音频。

**5. 结论：** 甘雨克隆完全可行，参考音频 3~10 秒即可；最推荐用 Qwen3-TTS Base（与云端同源，直接用现有参考音频复刻，效果与当前一致）。

---

## 五、MacBook（M 系列）现实性评估

| 方案 | Mac 支持方式 | 现实性 |
|---|---|---|
| **Qwen3-TTS（MLX）** | `mlx-audio` 官方支持，`mlx-community/Qwen3-TTS-12Hz-0.6B/1.7B-Base-8bit` 现成权重；另有 `qwen3-tts-apple-silicon` 一键项目 | ✅✅ **首选**。8bit 1.7B=2.4GB / 0.6B=1.3GB；M4 Air 实测 MLX 内存 2~3GB、低温低功耗；支持 5s 克隆 |
| CosyVoice2 | 官方无 MPS 加速（GitHub issue #134 长期开放），社区方案走 CPU-only | ⚠️ 能用但慢：M3 Pro 单句 ~800ms、批处理 10 句 3.2s；仅作次选 |
| GPT-SoVITS | CPU 推理可行，MPS 支持有限 | ⚠️ 慢，适合离线批量；不建议 Mac 实时 |
| F5-TTS | MPS 原生可跑 | ✅ M4 Max 1.5x / M2 0.8x；中文一般 + 非商用，备选 |

**关键 caveat：** Qwen3-TTS 是自回归解码，受**内存带宽**限制。社区实测提示 Apple Silicon 上可能接近而非稳定超过实时（M 基础版/ Air 带宽低，M Pro/Max 带宽高更从容）。建议：Mac 上优先 0.6B 8bit + 流式模式，实测后再决定是否上 1.7B。CPU-only（不加载 MLX）跑 0.6B 可出结果但不建议实时。

---

## 六、综合推荐

### ① RTX 4060（8GB）首选：Qwen3-TTS-12Hz-0.6B-Base（可升 1.7B）
- **理由**：与云端 `qwen3-tts-vc-realtime` **同源同架构**，用现有甘雨参考音频克隆，听感与当前云端一致（迁移零感知）；原生流式（首包 ~100ms）满足低延迟；0.6B 仅 4GB 显存、4060 余量充足，后续可平滑升 1.7B（6GB）；Apache-2.0 可商用无授权顾虑；vLLM 可上生产、GGUF 可降显存。
- **落地建议**：vLLM 部署 `Qwen3-TTS-12Hz-0.6B-Base`，参考音频复用云端创建 voice_id 时的那份；用 `generate_voice_clone()` 预热音色嵌入缓存，首包延迟可压到百毫秒级。
- **备选（若追求中文极致听感）**：CosyVoice2-0.5B（中文公认最佳、3s 复刻、流式、5~6GB 显存 4060 够用）；或 GPT-SoVITS（愿意花 1 分钟微调、要最高音色相似度+最低显存）。

### ② MacBook（M 系列）首选：Qwen3-TTS MLX（0.6B/1.7B 8bit，mlx-audio）
- **理由**：MLX 官方适配 + 音色克隆能力完整保留（这是多数 Mac TTS 方案没有的）；量化后 1.3~2.4GB 内存占用，16GB 内存的 MacBook 即可跑；与 4060 方案共用同一套模型权重和参考音频，跨机器无缝切换（**同一模型家族 = 同一套甘雨音色**）。
- **建议**：M 基础版/Air 用 0.6B 8bit + 流式；M Pro/Max 可上 1.7B 8bit。若未来以 Mac 为主力，建议选 32GB+ 内存型号。

### ③ 与云端 qwen3-tts-vc 的对比结论

| 维度 | 云端 qwen3-tts-vc-realtime | 本地 Qwen3-TTS（4060/Mac） |
|---|---|---|
| 听感（甘雨） | 基线（当前在用） | **≈一致**（同源模型 + 同参考音频）；1.7B 甚至可能略优 |
| 延迟 | 首包 ~100ms + 网络 RTT | 首包 ~100ms（无网络抖动，交互更稳） |
| 成本 | 按字符计费，长文本高频场景贵 | 一次性硬件/电费；用量越大越划算 |
| 并发/稳定性 | 云上托管，无需运维 | 需自行维护（4060 并发 1~3 路，超出需排队/批处理） |
| 隐私 | 音频/文本上云 | 完全本地 |

**一句话：** 同源模型让「听感不降级、迁移零成本」成为可能——**本地化唯一要付的代价是运维与并发，而不是效果**。建议以 Qwen3-TTS Base 为统一底座，4060 作主力推理机、Mac 作移动/备用推理机，长期看成本与体验均优于继续按字符付费。

---

## 七、信息来源链接

**Qwen3-TTS 官方**
- 官方 GitHub：https://github.com/QwenLM/Qwen3-TTS
- 官方 Blog（开源公告）：https://qwen.ai/blog?id=qwen3tts-0115
- 技术报告（arXiv:2601.15621）：https://arxiv.org/html/2601.15621v1
- HuggingFace 模型集合：https://huggingface.co/collections/Qwen/qwen3-tts
- ModelScope 集合：https://modelscope.cn/collections/Qwen/Qwen3-TTS
- 0.6B-Base 模型卡：https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base
- 0.6B-CustomVoice 模型卡：https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
- GGUF 量化版（qwentts.cpp）：https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF

**阿里云 / DashScope 官方**
- 声音复刻用户指南（确认 qwen3-tts-vc 为 VC 模型、10~20s 音频复刻）：https://www.alibabacloud.com/help/zh/model-studio/voice-cloning-user-guide
- 实时语音合成指南：https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide
- 非实时语音合成指南（含 qwen3-tts-vc-2026-01-22 模型清单）：https://help.aliyun.com/zh/model-studio/non-realtime-tts-user-guide

**CosyVoice**
- 官方 GitHub（FunAudioLLM/CosyVoice，22.6k★）：https://github.com/FunAudioLLM/CosyVoice
- Mac GPU 加速诉求 issue #134（确认官方无 MPS）：https://github.com/FunAudioLLM/CosyVoice/issues/134
- PAI + CosyVoice2 性能优化（首包/RTF 数据）：https://segmentfault.com/a/1190000047487236

**GPT-SoVITS / F5-TTS / Fish-Speech / ChatTTS / OpenVoice**
- GPT-SoVITS GitHub（60k★，MIT）：https://github.com/RVC-Boss/GPT-SoVITS
- GPT-SoVITS 低显存优化（4~6GB 推理、百字 <1.5s）：https://opc.csdn.net/698463f27bbde9200b9857da.html
- F5-TTS 性能表（各硬件 RTF）：https://localaimaster.com/blog/f5-tts-setup-guide
- F5-TTS 非商用授权说明：https://huggingface.co/SWivid/F5-TTS/discussions/18
- Fish-Speech 1.4（4060 RTF≈1:5、克隆时长）：https://zhuanlan.zhihu.com/p/8603402649
- ChatTTS 官网：https://chattts.com/zh

**Mac / MLX**
- mlx-audio（Apple Silicon TTS 库，支持 Qwen3-TTS）：https://github.com/Blaizzy/mlx-audio ｜ https://blaizzy.github.io/mlx-audio
- Qwen3-TTS for Mac（M4 Air MLX 实测：RAM 2~3GB）：https://github.com/kapi2800/qwen3-tts-apple-silicon
- MLX Qwen3-TTS 权重（8bit/bf16 体积）：https://soniqo.audio/guides/speak
- Mac 本地 TTS 方案盘点（2026）：https://www.murmurtts.com/blog/local-tts-models-mac-creator-guide-2026

**甘雨（林簌）参考音频来源（仅可行性评估）**
- fish.audio 社区甘雨音色：https://fish.audio/zh-CN/m/60f1c401686349b5b881c4a36ed01ceb
- ACGN 角色 TTS 在线站（2000+ 角色含甘雨）：https://acgn.ttson.cn
