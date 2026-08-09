# TASK-023：KWS 语音唤醒监听验证

## 任务卡

- 状态：✅ 已完成（2026-08-09）
- 负责人：小奈
- 执行会话/子 Agent：小奈
- 基线 commit / 分支：main@origin/main（df2002c）
- 依赖任务：无（TASK-024~026 的语音渠道系列前置验证）

### 目标

验证本地语音唤醒技术栈可行：电脑麦克风监听「小奈小奈」能触发唤醒事件。这是语音渠道（TASK-024~026）的**前置风险趟平**步骤——最不确定的技术（sherpa-onnx KWS + 自定义中文唤醒词）先用独立 demo 验证，不碰 NanoClaw 任何代码。

### 非目标

- 不创建 voice 渠道、不改 NanoClaw 主链路（TASK-024 做）
- 不做唤醒后动作（录音/ASR/对话），只验证「能唤醒」
- 不实现常驻后台服务（先跑前台 demo 验证可行）

### 允许修改

- `pyproject.toml`（新增依赖：`sherpa-onnx`、`sounddevice`）
- 新建独立验证脚本（`voice/kws/demo_kws.py`）
- 下载 KWS 模型到本地（`voice/kws/models/`，已加入 .gitignore）

### 禁止修改

- 核心代码（main.py / config.py / bus/ / agent/ / channels/）
- 渠道代码、配置

### 上下文与约束

- 相关代码入口：无（纯新组件）；参考设计 `docs/VOICE_WAKE_KWS.md`
- 相关架构/历史决策：
  - sherpa-onnx 提供中文 KWS 模型：`sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`（中文，3.3M）
  - 自定义唤醒词需 `sherpa-onnx-cli text2token --tokens-type ppinyin` 把「小奈小奈」转成拼音 token
  - 输入目标：mono 16-bit PCM，16kHz
  - 设计文档要求：回调线程只拷贝帧入有界队列，绝不阻塞/重采样/调模型；独立 worker 做 KWS 推理 + 连续命中确认 + 冷却防抖
- 实测发现（2026-08-09）：
  - ⚠️ **sherpa-onnx 1.13.4 有 KWS 回归 bug：decode 零命中（test_wavs 全 miss、score 调大无效）→ 降级 1.12.40 后 4/7 测试 wav 正常命中，与官方示例一致**。TASK-023 固定用 1.12.40（已入 DECISIONS）。
  - ⚠️ **macOS arm64 wheel 缺 onnxruntime dylib**：需手动软链 `onnxruntime/capi/libonnxruntime.X.Y.Z.dylib` → `sherpa_onnx/lib/`。1.12.40 对应 onnxruntime 1.24.4。重建 venv 需重做（已入 DECISIONS 遗留）。
  - ✅ text2token ppinyin 需要 `sentencepiece` + `pypinyin` 依赖。
  - ✅ KWS 喂入正确姿势：流式分块（0.1s）或离线一次性 + 0.66s tail padding + `input_finished()`；命中后必须 `reset_stream`。
  - ✅ macOS 内置麦克风：sounddevice 0.5.5 wheel 自带 PortAudio，直接可用；TCC 首次运行弹权限框。
  - ✅ 资源占用实测：RSS ≈ 53MB；空闲监听 %CPU ≈ 9~14%（可优化：int8 + num_threads=1，TASK-024 常驻时评估）
- 已知风险：蓝牙耳机麦克风延迟/采样率不达标（未测，尽力而为）；长时间误触发未系统验证

### 验收标准（最终状态）

- [x] 模型 + 依赖安装可复现（命令见下，含 macOS dylib 软链补丁）
- [x] 关键词「小奈小奈」离线验证命中（macOS `say -v Tingting` 合成 wav 喂 KWS → 🔥 命中）
- [x] 麦克风监听「小奈小奈」能稳定触发唤醒（乖宝 2026-08-09 内置麦克风实测触发 ✅）
- [~] 不喊唤醒词时不误触发：后台采样 + 乖宝使用期间无异常误触发；长时间系统性观察未做（遗留）
- [~] 唤醒事件有冷却/防抖：代码实现（2s 冷却 + 连续命中确认），未专门连喊实测（逻辑简单，遗留）
- [x] macOS 麦克风权限流程明确（TCC 首次弹权限框；脚本对 PortAudioError 给出系统设置指引）

### 必须执行的验证（可复现命令）

```bash
# 依赖安装（实测可用组合）
uv add "sherpa-onnx==1.12.40" sounddevice "onnxruntime==1.24.4" sentencepiece pypinyin
# macOS arm64 补 onnxruntime dylib 软链（wheel 打包缺陷，重建 venv 需重做）：
ln -sf ../../onnxruntime/capi/libonnxruntime.1.24.4.dylib \
  .venv/lib/python3.13/site-packages/sherpa_onnx/lib/libonnxruntime.1.24.4.dylib
# 模型下载 + 解压（模型已 gitignore，需重新下载）：
#   https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
#   → voice/kws/models/ 解压
# text2token 生成「小奈小奈」关键词（输入文件 input_keywords.txt：`小奈小奈 @小奈小奈`）：
sherpa-onnx-cli text2token --tokens <模型>/tokens.txt --tokens-type ppinyin input_keywords.txt keywords_xiaonai.txt
# 运行 demo：对着麦克风喊「小奈小奈」
.venv/bin/python voice/kws/demo_kws.py --model-dir voice/kws/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
# 预期输出：检测到唤醒词 → 打印 🔥 唤醒事件
```

## 执行交接

- 状态：已完成
- 实际改动文件：
  - `pyproject.toml` / `uv.lock`：新增 sherpa-onnx==1.12.40、sounddevice、onnxruntime==1.24.4、sentencepiece、pypinyin
  - `voice/kws/demo_kws.py`（新建，麦克风 KWS demo：回调只入有界队列 + worker 推理 + 连续命中确认 + 冷却防抖，支持 --int8/--cooldown/--confirm-hits/--list-devices）
  - `voice/kws/models/`（模型 + 关键词文件，已 gitignore）
  - `.gitignore`：新增 `voice/kws/models/`
- 实现摘要：
  - 依赖装齐（sherpa-onnx 1.12.40 + onnxruntime 1.24.4 + sounddevice 0.5.5 + sentencepiece + pypinyin）
  - 模型下载解压到 `voice/kws/models/`；「小奈小奈」→ ppinyin tokens 生成关键词文件
  - demo_kws.py 跑通 sounddevice（默认输入设备 MacBook Neo 麦克风）
  - 离线验证：test_wavs 3/4/5/6.wav 命中对应关键词；macOS TTS 合成「小奈小奈」喂 KWS → 🔥 命中
  - **根因排查记录：sherpa-onnx 1.13.4 KWS 零命中 → 降级 1.12.40 解决**
  - 乖宝内置麦克风实测「小奈小奈」触发 🔥 成功
  - 资源实测：RSS ≈ 53MB、空闲 %CPU 9~14%
- 关键决策与假设：唤醒词「小奈小奈」（乖宝定）；中文 3.3M 模型；sherpa-onnx 锁定 1.12.40（1.13.4 KWS bug）
- 验证命令与结果：`git diff --check` OK；全量 `unittest discover -s tests` **560 全过**；`compileall` demo_kws OK；`import main` OK
- 未验证项（遗留）：长时间误触发观察；冷却/防抖连喊实测；蓝牙耳机输入
- 风险与遗留问题：sherpa-onnx 版本需锁定 1.12.40；onnxruntime dylib 软链依赖 venv 路径，重建 venv 需重做；常驻监听 CPU 可优化（int8+单线程）
- commit：df2002c 之后待提交（依赖 + demo + 文档）
- 建议下一步：TASK-024 voice 渠道骨架（可复用 demo 的线程模型）

## 负责人验收

- [x] 检查 diff 与授权范围（未改核心代码/渠道/配置，符合任务卡）
- [x] 独立复跑关键验证（test_wavs 命中、TTS 合成「小奈小奈」命中、560 测试全过）
- [x] 检查秘密/个人数据/运行产物（无敏感信息；模型文件已 gitignore）
- [x] 检查文档与配置一致性（任务卡/DECISIONS/PROJECT/MEMORY 已同步）
- [x] 更新 `docs/DECISIONS.md` 中相关状态（sherpa-onnx 版本锁定决策 + 遗留问题）
- 验收结论：✅ 通过（2026-08-09 小奈验收，乖宝拍板完成）
- 证据与备注：乖宝内置麦克风实测触发；离线 TTS 合成「小奈小奈」命中；资源占用 ~53MB / 9~14% CPU
