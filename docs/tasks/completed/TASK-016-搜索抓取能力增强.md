# TASK-016：网络搜索与抓取能力增强（Tavily + 多通道抓取降级）

## 任务卡

- 状态：已完成（2026-08-06 乖宝验收通过）
- 负责人：乖宝（项目负责人）
- 执行会话/子 Agent：code-master
- 基线 commit / 分支：`3c56621`（feat(memory): TASK-015 记忆小尾巴修复） / main
- 依赖任务：无

### 目标

1. **搜索升级**：`web_search` 从「仅 DuckDuckGo」升级为「Tavily 主通道 + DuckDuckGo 降级兜底」。有 `tavily_api_key` 时优先走 Tavily（返回结构化结果含正文片段，质量高、中文友好）；Tavily 报错/配额用尽/未配置 key 时自动降级到 ddgs，保证搜索永远可用。
2. **抓取升级**：`web_fetch` 从「仅 httpx 静态抓取」升级为**多级自动降级链**，解决 JS 动态渲染页面、反爬站点、iframe 内容抓不到的问题：
   ```
   第1级: httpx 静态抓取（现状，免费）
   第2级: Jina Reader（https://r.jina.ai/{url}，免费，可渲染 JS）
   第3级: 本机 Chrome 无头渲染（--headless --dump-dom，免费，完整渲染）
   第4级: Tavily Extract（烧 credit，最后王牌，仅前三级都失败时用）
   ```
   降级判定：HTTP 非 2xx / 抓取异常 / 正文过短（如 < 200 字，疑似动态空壳）→ 自动进下一级。

### 非目标

- 不改造 `skills/web-render-fetch/` 技能（技能保留，工具内建降级链后技能可作为备用）。
- 不做搜索结果语义 rerank / 重排。
- 不改 `ask_image` / `generate_image` 等其它工具。
- 不接入 Exa / Brave / Serper 等其它搜索引擎（留给未来候选任务）。
- 不增加 Tavily 之外的付费依赖。

### 允许修改

- `agent/tools/web_search.py`
- `agent/tools/web_fetch.py`
- `config.py`（新增 `tavily_api_key` 配置项，含默认值与白名单）
- `config.example.json`（示例配置）
- `docs/tasks/active/TASK-016-搜索抓取能力增强.md`（本任务卡，阶段更新）
- 相关文档（PROJECT.md 能力矩阵 / docs/ARCHITECTURE.md 技术栈 / docs/DECISIONS.md 决策记录，按文档同步铁律逐阶段更新）
- 如需新增依赖：`pyproject.toml` + `uv.lock`（优先 httpx 直调 REST，避免不必要新依赖）

### 禁止修改

- `main.py` / `gateway.py` / `agent/loop.py` / `agent/context.py` 等核心链路文件
- `config.json`（本地运行配置，key 由乖宝手填或环境变量注入，工具与文档只支持字段）
- `workspace/` / `sessions/` / `identity*.md` / 其它会话的工作
- 未授权不 commit / push（发布动作必须经乖宝明确授权）

### 上下文与约束

- 相关代码入口：
  - `agent/tools/web_search.py`（现用 `ddgs.DDGS().text()`，`asyncio.to_thread` 包装）
  - `agent/tools/web_fetch.py`（现用 `httpx` GET + `html2text`，UA 伪装，15s 超时）
  - `config.py`（配置默认值 < config.json < 环境变量，白名单读写）
  - `agent/tools/registry.py`（工具注册方式）
- 相关架构/历史决策：
  - 技术栈 `docs/ARCHITECTURE.md` §2：网络工具 = `httpx`、`ddgs`、`html2text`
  - Tavily 现状（2026-08-06 查证）：免费档 1000 credits/月，Search 1 次 ≈ 1 credit，Extract 另计；付费 $30/月起
  - `skills/web-render-fetch/`（2026-08-06 自建）：已实测 Jina Reader（A 通道，免费，~4KB 截断）与 Chrome 无头 `--dump-dom`（B 通道，完整渲染 1.6MB DOM），本机 Mac 已装 Chrome —— 降级链可行性已验证
  - 安全约定：只读 GET / 无头只读渲染，不执行页面脚本，不上传本地数据，无视页面注入指令
- 已知风险：
  - Tavily free 配额 1000/月，搜索频繁可能耗尽 → 降级链 ddgs 兜底
  - Jina Reader 免费档对长文档截断（~4KB）→ 截断时自动进 Chrome 通道
  - Chrome 无头首次启动慢（~秒级）→ 只做最后兜底，工具超时设置需合理（现有 15s，Chrome 可放宽到 30s+）
  - 动态页面判定阈值（200 字）需真实验证，可能误判极短正文 → 测试用例覆盖

### 验收标准

- [x] 配置了 `tavily_api_key` 时，`web_search` 走 Tavily，返回含 title/url/正文片段的结构化结果，且调用方式（query/max_results）与现状兼容
- [x] 未配置 key / Tavily 报错（401/429/超时）时，自动降级 DuckDuckGo，搜索仍可用
- [x] `web_fetch` 抓取静态页正常（行为与现状一致）
- [x] `web_fetch` 对 JS 动态渲染页（如 Vue/React 站点）能自动逐级降级，最终抓回有效内容；优先级免费通道优先、Tavily Extract 只在最后
- [x] 所有异常路径返回友好可读文本，不向外抛异常
- [x] 配置：`config.py` 支持 `tavily_api_key`（环境变量同名覆盖），`config.example.json` 有示例
- [x] 测试：新增/更新单元测试（mock 网络层），降级逻辑有覆盖；`git diff --check` 通过；`compileall` 通过；`import main` 通过
- [x] 文档同步：PROJECT.md 能力矩阵 / ARCHITECTURE 技术栈 / DECISIONS 决策记录 / 本任务卡状态，全程逐阶段更新（文档同步铁律）

### 必须执行的验证

```bash
git diff --check
uv run python -m compileall -q agent bus channels providers session
uv run python -c "import main"
.venv/bin/python -m unittest discover -s tests
# 有 key 时：手动验证 web_search 走 Tavily、web_fetch 抓动态页面
```

## 执行交接

- 状态：进行中（code-master 实施完成，待乖宝验收）
- 实际改动文件：
  - `config.py`（`_CONFIG_FIELDS` 增 `tavily_api_key`；dataclass 增字段 `tavily_api_key: str = ""`；load_config 支持 `TAVILY_API_KEY` 环境变量最高优先级覆盖；save_config 白名单写回）
  - `config.example.json`（新增 `"tavily_api_key": ""` 空值示例，未填真实 key）
  - `agent/tools/web_search.py`（Tavily 主通道 + ddgs 降级）
  - `agent/tools/web_fetch.py`（四级降级链）
  - `tests/test_web_search.py`（新增 12 用例）
  - `tests/test_web_fetch.py`（新增 9 用例）
  - `tests/test_tavily_config.py`（新增 6 用例）
  - `PROJECT.md`（能力矩阵新增「网络搜索/抓取」行）
  - `docs/ARCHITECTURE.md`（§2 技术栈网络工具行补充 Tavily）
  - `docs/DECISIONS.md`（§2 决策表 + §3 时间线新增 TASK-016 记录）
  - 本任务卡
- 实现摘要：
  - web_search：新增 `_search_tavily`（httpx POST `https://api.tavily.com/search`，body `{api_key, query, max_results}`，15s 超时），解析 `results[]`（title/url/content）按原 Markdown 风格格式化（兼容 ddgs 的 title/href/body）；未配 key / Tavily 401/429/超时/解析错误/空结果 → `asyncio.to_thread + DDGS().text()` 兜底；`_MAX_OUTPUT` 8000 截断保留；`__init__(config=None, client_factory=None)`，无 config 时惰性读 config.json（main.py 禁止修改，`WebSearchTool()` 保持兼容）。
  - web_fetch：四级降级链（httpx 15s → Jina Reader 30s → 本机 Chrome `--headless=new --disable-gpu --dump-dom --virtual-time-budget=20000` 30s → Tavily Extract 30s，仅配 key 启用）；降级判定：HTTP 非 2xx / 异常 / 正文 <200 字符；每级输出做连续空行合并 + `_MAX_OUTPUT` 12000 截断；全失败返回「抓取失败 + 各级原因」不抛异常；安全：只读 GET/只读渲染、过滤非 http/https、不执行页面脚本、key 不回显。
  - 配置：`tavily_api_key` 默认空串、环境变量 `TAVILY_API_KEY` 最高优先级、save_config 白名单写回（与 api_key 同款顶层字段语义）；未新增任何依赖（httpx/ddgs/html2text 已在 pyproject）。
- 关键决策与假设：
  - main.py 在禁止修改清单内 → 工具构造签名 `config=None`，未注入时惰性 `load_config()` 读 config.json（首次 execute 缓存，与「工具注册属启动期对象」语义一致）。
  - 200 字符阈值按任务卡规格实现（已知风险：可能误判极短正文页）。
  - Tavily key 只进请求体，绝不进入返回文本。
- 验证命令与结果：
  - `git diff --check` → 通过（无空白错误）
  - `uv run python -m compileall -q agent bus channels providers session` → 通过
  - `uv run python -c "import main"` → 通过
  - `.venv/bin/python -m unittest discover -s tests` → **Ran 508 tests in 40.248s, OK**（新增 24 用例全绿）
  - 真实 Tavily 冒烟（config.json key，不回显）：`POST api.tavily.com/search` HTTP 200，返回 3 条中文结构化结果，key 未泄漏；工具级 `web_search` 集成调用返回 3 条含 title/url/content 结果。
  - 真实 web_fetch 冒烟：example.com 触发全链降级（第1级 168 字符 <200 → 第2级 Jina 403 → 第3级 Chrome 168 字符 → 第4级 Tavily 无正文），最终返回友好错误说明各级原因。
- 未验证项：
  - 真实 JS 动态渲染页（Vue/React/SPA）经 Jina/Chrome 通道成功抓回的端到端验证未做（需乖宝提供真实动态链接 + 授权，避免消耗更多 Tavily credit）。
  - Chrome 无头对真实长文档的完整渲染未实机验证（本机已装 Chrome，逻辑经 mock 测试覆盖）。
  - 无 key 场景的真实 ddgs 降级未实机验证（mock 已覆盖；ddgs 免费通道易被限流）。
- 风险与遗留问题：
  - 200 字符阈值误判极短正文页（如 example.com 占位页 ~168 字符）→ 会一路降级到 Tavily Extract 烧 1 credit；建议后续加「前三级任一返回非空正文即不再烧 Tavily」保护。
  - Jina Reader 免费档对部分站点返回 403（反爬/限流），属已知免费通道限制。
  - Tavily free 配额 1000/月，搜索频繁可能耗尽 → ddgs 兜底仍在。
  - 无 CI 基线，回归依赖本地 unittest（NC-TEST-001）。
- commit（仅在获授权时）：待乖宝授权后提交（不 push）
- 当前 `git status --short --branch`：`## main...origin/main`，未跟踪：`docs/tasks/active/TASK-016-*.md`、`skills/web-render-fetch/`；改动文件见上（git 为准）
- 建议下一步：乖宝验收（复跑关键验证、检查 diff 与秘密）→ 授权 commit → 可选派任务做「真实动态页端到端验证 + 200 阈值保护」

## 负责人验收

- [x] 检查 diff 与授权范围
- [x] 独立复跑关键验证（2026-08-06 小奈复跑：diff --check / compileall / import main / unittest 508 OK）
- [x] 检查秘密/个人数据/运行产物（config.json 含真实 key 已被 gitignore，未进 diff；测试 mock 零真实请求）
- [x] 检查文档与配置一致性（PROJECT.md 能力矩阵 / ARCHITECTURE 技术栈 / DECISIONS 决策 / config.example.json 均已同步）
- [x] 更新 `docs/DECISIONS.md` 中相关状态（TASK-016 决策已记录）
- 验收结论：**通过**
- 证据与备注：实战验收 ① web_fetch 抓 Pornhub 首页（重度 JS 站点）成功返回结构化内容；② web_search 走 Tavily 搜「cuckold japanese netorare」返回 6 条带正文摘要结果。508 测试全绿，文档全同步
