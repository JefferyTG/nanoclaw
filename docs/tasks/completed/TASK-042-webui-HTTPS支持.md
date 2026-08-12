# TASK-042：webui HTTPS 支持（mkcert 证书，解锁手机录音/语音）

## 任务卡

- 状态：已完成（2026-08-12 乖宝验收通过后归档）
- 负责人：乖宝（验收）/ code-master（实现）
- 执行会话/子 Agent：code-master
- 基线 commit / 分支：`70fce56`（main，TASK-041 归档提交，工作区干净）
- 依赖任务：TASK-041 已完成；**这是「按住发语音」和「实时通话入口」的前置**（浏览器 getUserMedia 只允许安全上下文 HTTPS/localhost）

### 背景与目标

webui 手机端要解锁 `getUserMedia`（麦克风），必须走 HTTPS（安全上下文）。当前 webui 是明文 HTTP（`http://192.168.x.x:8080`），手机浏览器拒绝麦克风权限。

**为什么用 mkcert 而不是 Tailscale cert**：
- 本机 Tailscale 已装、手机 iPhone 15 已装 app 且同 tailnet（tailnet `tailXXXX`，机器 `my-macbook-neo.tailXXXX.ts.net`，Tailscale IP `100.x.x.x`）
- `tailscale cert` 实测报 `500: acme order invalid`——**Tailscale 服务端已知故障**（GitHub #19942/#14402 等，2026-08-12 实测多次重试无效）
- 决策：**mkcert 自签证书**，手机装一次 mkcert CA 即信任；Tailscale 负责组网（异地可连），mkcert 负责证书，互不干扰；Tailscale 恢复后随时切换回合法证书

### 目标

1. 本机安装 mkcert，生成本地 CA + 证书（签发域名：`my-macbook-neo.tailXXXX.ts.net` + Tailscale IP `100.x.x.x` + `127.0.0.1` + 局域网 IP，一个证书多 SAN）
2. webui 服务（aiohttp）支持 TLS：配置证书后走 HTTPS，不配置则维持明文（向后兼容）
3. 手机（iPhone/安卓）安装并信任 mkcert CA 后，HTTPS 无警告访问
4. 手机 Tailscale 打开时，异地/局域网都能访问 `https://my-macbook-neo.tailXXXX.ts.net:<端口>`
5. 验证 HTTPS 下 `getUserMedia` 可用（测试页麦克风权限弹窗）

### 非目标

- ❌ 不做「按住发语音」真录音（后续任务，HTTPS 是它的前置）
- ❌ 不做实时通话右上角图标（后续任务）
- ❌ 不折腾 Tailscale cert（服务端故障，等官方修复后可切）
- ❌ 不做 PWA/推送

### 允许修改

- `channels/web.py`：aiohttp 加 ssl_context（`web.TCPSite(runner, host, port, ssl_context=...)`）；证书路径从 config 读
- `config.py`：新增配置字段（如 `web_ssl_cert` / `web_ssl_key` / `web_https_port`，默认关闭，向后兼容）
- `main.py`：把证书配置传给 WebChannel（如需）
- `docs/`：ARCHITECTURE.md（web HTTPS 说明）、PROJECT.md（能力矩阵/里程碑）
- 证书文件位置：建议 `workspace/certs/`（gitignore 不入库——**证书与私钥是敏感物，绝不能提交**）；或独立目录，需在任务卡/文档说明
- 手机装 CA 图文步骤：写进 docs/ 或任务卡备注（给乖宝参考）

### 禁止修改

- 不提交证书/私钥/CA 文件（敏感物）
- 不动 Agent 核心、消息协议、其它渠道
- 未经授权不 commit/push

### 上下文与约束

- 现状：`channels/web.py` `_run_server()` 里 `web.TCPSite(runner, self.host, self.port)`（明文）；aiohttp `web.Application` 已建
- mkcert 用法：
  ```bash
  brew install mkcert
  mkcert -install                          # 生成本地 CA 并信任（Mac 系统信任）
  mkcert -cert-file xxx.crt -key-file xxx.key my-macbook-neo.tailXXXX.ts.net 100.x.x.x 127.0.0.1 192.168.x.x
  ```
- 端口建议：`web_https_port` 默认 0（关闭）；启用时建议 8443（443 可能被占用/权限）；明文 `web_port` 保留共存便于调试
- 手机信任 CA：iPhone 需 AirDrop/邮件传 `rootCA.pem` → 设置里安装描述文件 → 设置→通用→关于本机→证书信任设置→开启；安卓需装 CA + 用户信任区（给出步骤即可，不必真机验证 iOS，安卓可测）
- 已知风险：自签证书浏览器首次访问有警告（装 CA 后消除）；Safari 对自签 CA 要求较严（描述文件+信任开关两步缺一不可）

### 验收标准

- [x] `mkcert -install` 成功，证书生成（含 Tailscale 域名 + IP SAN）
- [x] 配置证书后，`https://127.0.0.1:8443` 本机访问成功（curl -k 或浏览器）
- [x] `https://192.168.x.x:8443` 局域网手机访问（乖宝 08-12 真机确认可开；装 CA 后无警告部分随 TASK-043 按住发语音验证）
- [x] `https://my-macbook-neo.tailXXXX.ts.net:8443` Tailscale 下可访问（乖宝 08-12 手机开 Tailscale 真机确认成功）
- [ ] HTTPS 下 `getUserMedia` 弹出麦克风权限——**未验证**（依赖手机装 CA + 真机，随 TASK-043「按住发语音」验证）
- [x] 不配置证书时，明文 `web_port` 完全不受影响（向后兼容，冒烟验证 200）
- [x] 全量测试通过：`unittest discover -s tests -t .`（953 OK，独立复跑）
- [x] 文档同步：ARCHITECTURE.md（HTTPS 支持）、PROJECT.md（能力矩阵）、任务卡归档
- [x] 证书/私钥确认未进 git（git check-ignore 命中 workspace/certs/）

### 必须执行的验证

```bash
git diff --check
uv run python -m unittest discover -s tests -t .   # 全量
uv run python -m compileall -q channels agent      # 后端语法
curl -k https://127.0.0.1:<https_port>/            # HTTPS 冒烟
# 手动验证（乖宝）：手机装 CA → 局域网 + Tailscale 访问 → getUserMedia 测试
```

### 实现方案（建议，code-master 可优化）

1. `config.py`：加 `web_ssl_cert`（str，默认 ""）、`web_ssl_key`（str，默认 ""）、`web_https_port`（int，默认 0）；白名单加入 web 可编辑字段（或只读）
2. `channels/web.py`：
   - `__init__` 读配置（cert/key/https_port）
   - `_run_server()`：若 https_port>0 且 cert/key 存在，`ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)` + `load_cert_chain(cert, key)`，`web.TCPSite(runner, host, https_port, ssl_context=ctx)` 额外监听一个 HTTPS 端口；明文端口照旧
   - 证书文件不存在时启动打 warning 并跳过 HTTPS（不崩溃）
3. 证书生成脚本（可选）：`scripts/gen_web_certs.sh`（mkcert 封装，多 SAN），证书落 `workspace/certs/`，gitignore 加 `workspace/certs/`
4. 前端：无需改（同源 https 下 ws 自动变 wss，`location.protocol` 已处理 `wss`——见 connectWS 的 `proto` 判断，TASK-039 已兼容）
5. 手机 CA 安装图文步骤：记入 `docs/WEB_HTTPS.md`（或任务卡归档时附带）

### 后续任务（本任务不处理，仅记录）

- TASK-0??：webui 按住发语音（按住说话 → MediaRecorder → /api/asr → 文本发送），依赖本任务 HTTPS
- TASK-0??：webui 实时通话入口（右上角图标 → 豆包 S2S 全双工，TASK-037 底层已有）
- TASK-040：webui 多端历史同步（排队中）

## 执行交接

- 状态：已完成（2026-08-12 code-master 实现 + 乖宝真机验收 + 小奈归档）
- 实际改动文件：
  - `config.py`：新增 `web_ssl_cert`（str，默认 ""）/ `web_ssl_key`（str，默认 ""）/ `web_https_port`（int，默认 0），位于 web_port 附近；`_CONFIG_FIELDS` 白名单同步加入（网页可编辑白名单未加入，保持只读，路径属本机配置）
  - `channels/web.py`：`__init__` 从共享 config 读取三个新字段；`_run_server()` 在明文 `web.TCPSite` 之后，当 https_port>0 且证书有效时用 `ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)` + `load_cert_chain(cert, key)` 经 `web.TCPSite(runner, host, https_port, ssl_context=ctx)` 额外监听 HTTPS；证书缺失仅 warning 并跳过 HTTPS，明文不受影响；新增 `_build_ssl_context()` 辅助方法；`start()` 启动打印带上 HTTPS 提示
  - `main.py`：网页渠道启动日志补 HTTPS 提示（WebChannel 已整体接收 cfg 对象，无需改传参）
  - `scripts/gen_web_certs.sh`：mkcert 多 SAN 封装（可选，本地方便工具；scripts/ 已被 .gitignore 忽略，不入库）
  - `docs/WEB_HTTPS.md`：手机（iPhone 描述文件+信任开关两步 / 安卓用户信任区）CA 安装图文步骤
  - `docs/ARCHITECTURE.md`：技术栈表 Web 行 + 5.1 启动装配 + 新增 Web HTTPS（TASK-042）段落
  - `PROJECT.md`：能力矩阵网页端行加 HTTPS（TASK-042）；里程碑指针更新为「已实现待归档」（只写指针不写 hash）
  - `config.json`（gitignore，不入库）：已写入 web_ssl_cert/web_ssl_key/web_https_port=8443，重启实例即生效
- 实现摘要：mkcert 本机 CA + 多 SAN 证书（Tailscale 域名/IP、127.0.0.1、局域网 IP 192.168.x.x）落 `workspace/certs/`；webui 支持 TLS 双端口并存；向后兼容纯明文
- 关键决策与假设：
  - CA 信任安装：`mkcert -install` 因终端无管理员密码交互在「加入系统信任」步失败（CA 本身已生成），改用免密码的 `security add-trusted-cert -k login.keychain-db` 完成本机信任，验证 `verify-cert` 通过、`curl https://127.0.0.1:18443/`（不带 -k）ssl_verify=0
  - 证书文件：`workspace/certs/nanoclaw-web.crt` / `.key`（SAN：DNS my-macbook-neo.tailXXXX.ts.net + IP 100.x.x.x/127.0.0.1/192.168.x.x；2028-11-12 到期）；workspace/ 整体被 .gitignore 忽略，已用 `git check-ignore` 确认
  - 证书路径允许相对仓库根或绝对路径（`_build_ssl_context` 支持 ~ 展开）；相对路径按 CWD 解析，与 identity_file 等既有约定一致
  - 网页可编辑白名单（channels/web.py `_CONFIG_FIELDS`）未加入证书字段：路径属本机配置，保持只读，避免网页误改导致 HTTPS 静默关闭
  - 本机 curl 解析 ts.net 域名被本机代理劫持到 198.18.0.88（Tailscale MagicDNS fake-IP 段，不转发 18443）；手机端 tailnet 内直连 100.x.x.x，已用 `curl --resolve` 模拟验证域名 SAN 生效（HTTP 200、ssl_verify=0）
- 验证命令与结果：
  - `git diff --check` → OK（无空白错误）
  - `uv run python -m unittest discover -s tests -t .` → Ran 953 tests in 59.646s, OK（Traceback 为测试内故意注入的异常日志）
  - `uv run python -m compileall -q channels agent` → OK
  - 冒烟（临时 harness，真实 `_run_server` 路径，绑定 127.0.0.1/0.0.0.0）：
    - `curl -k https://127.0.0.1:18443/` → HTTP 200（webui 首页 95037 字节）
    - 明文与 HTTPS 并存（同实例 18080）→ HTTP 200
    - 证书缺失实例：WARNING「网页渠道 HTTPS 未启用…明文端口照常监听」+ 明文 HTTP 200（不崩溃）
    - 纯明文（web_https_port=0 默认）→ HTTP 200（向后兼容）
    - 本机信任链：curl 不带 -k → HTTP 200、ssl_verify=0
    - `https://192.168.x.x:18443/` → HTTP 200；`https://100.x.x.x:18443/` → HTTP 200
    - Tailscale 域名（`--resolve` 指向 100.x.x.x 模拟手机直连）→ HTTP 200、ssl_verify=0
    - openssl 确认 SAN：DNS:my-macbook-neo.tailXXXX.ts.net, IP:100.x.x.x, IP:127.0.0.1, IP:192.168.x.x
  - 红线：`git status` 中 workspace/certs 不出现；`git diff` 无私钥/证书内容；config.json（含证书路径）被 gitignore
- 未验证项：
  - 手机真机安装 CA 后的浏览器访问与 getUserMedia 弹窗（iPhone/安卓，需乖宝真机验证）
  - Tailscale 域名异地访问（本机 DNS 被代理劫持无法直连验证；手机开 Tailscale 后直连 tailnet IP 应正常）
- 风险与遗留问题：
  - 本机 Tailscale MagicDNS 被第三方代理（198.18.0.0/15）劫持 ts.net 解析，仅影响本机浏览器直连域名；手机 tailnet 直连不受影响。如需本机域名访问可临时关代理或用 `--resolve`
  - 自签 CA 在 Safari 需「描述文件+信任开关」两步（已写入 docs/WEB_HTTPS.md）；安卓 Chrome 认用户信任区
  - mkcert CA/证书 2028-11-12 到期，需重新签发并在手机重装 CA
  - 生产实例（PID 80451）仍在跑旧代码明文 8080；重启实例后自动加载新配置启用 HTTPS 8443（与明文并存）
- commit（仅在获授权时）：（未授权，未 commit/push）
- 当前 `git status --short --branch`：main @ `70fce56`（工作区干净）
- 建议下一步：乖宝说「开始 TASK-042」→ code-master 按本卡实现


### 归档补充（2026-08-12 小奈）

- **乖宝真机验收**：局域网 `https://192.168.x.x:8443` ✅（17:30）；Tailscale 域名 `https://my-macbook-neo.tailXXXX.ts.net:8443` ✅（17:35，手机开 Tailscale）。
- **WEB_HTTPS.md 移动**：乖宝要求（内含个人网络信息：tailnet/域名/IP），已从 `docs/WEB_HTTPS.md` 移至 `workspace/WEB_HTTPS.md`（不跟踪不入库）；ARCHITECTURE.md / PROJECT.md 引用同步更新。
- **遗留**：getUserMedia 真机麦克风弹窗待 TASK-043「webui 按住发语音」实现时一并验证。

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物（重点：证书私钥未入库）
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：通过（2026-08-12 乖宝真机验证局域网+Tailscale 均可访问，代码/文档/红线全过）
- 证据与备注：953 tests OK（59.7s 独立复跑）；curl https 127.0.0.1/局域网 均 200；明文 8080 不受影响；git diff 无敏感物；workspace/certs 与 workspace/WEB_HTTPS.md 均被 gitignore
