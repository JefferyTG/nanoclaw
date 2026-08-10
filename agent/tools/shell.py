"""
Shell 工具：执行 Shell 命令并返回输出。

ExecTool 在指定的工作区目录（workspace）下运行用户提交的命令，
内置一份危险命令黑名单（deny_patterns）做前置拦截，避免递归删除、
格式化磁盘、关机重启、提权、开网络后门等破坏性操作。
所有执行结果（stdout/stderr/退出码）都会被捕获并以字符串返回，
超时、危险命令、运行异常都不会向外抛出，而是转成可读的错误信息。
"""

import asyncio
import os
import re
import signal
import subprocess

from agent.tools.base import Tool


class ExecTool(Tool):
    """在 workspace 目录下执行 Shell 命令的只读/受控工具。

    适用场景：
        - 查看目录、检索文件、运行脚本、调用命令行工具等日常操作。
    不适用场景：
        - 任何会被 deny_patterns 命中的高危命令（见 _is_dangerous）。

    子类可直接复用，无需额外实现；如需调整工作区，请在构造时传入 workspace。
    """

    # ---- Tool 抽象属性 ----
    name = "exec"
    description = (
        "在工作区目录下执行一条 Shell 命令，返回其标准输出、标准错误与退出码。"
        "危险命令（如 rm -rf、格式化、关机、提权等）会被自动拦截。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要在工作区内执行的 Shell 命令",
            }
        },
        "required": ["command"],
    }

    # ---- 危险命令黑名单（正则，忽略大小写）----
    # 命中即拦截，绝不执行。覆盖：递归删除、格式化、关机重启、
    # 提权、危险权限、覆写设备、下载执行、网络后门、磁盘覆写、Fork 炸弹。
    DENY_PATTERNS: list[str] = [
        r"rm\s+.*-r",        # rm 递归删除
        r"rm\s+-rf",         # rm 强制递归删除
        r"rmdir\s+/s",       # Windows 递归删除
        r"format\s+",        # 格式化磁盘
        r"mkfs",             # Linux 格式化
        r"shutdown",         # 关机
        r"reboot",           # 重启
        r"sudo\s+",          # 提权执行
        r"\bsu\b",           # 切换到 root
        r"chmod\s+777",      # 危险权限开放
        r">\s*/dev/(?!null\b)",  # 覆写设备文件（排除黑洞 /dev/null 的误伤）
        r"wget\s+.*\|\s*sh",     # 下载后直接执行脚本
        r"curl\s+.*\|\s*bash",   # 下载后直接执行脚本
        r"nc\s+-l",          # 监听端口开后门
        r"ncat\s+-l",        # 监听端口开后门
        r"dd\s+if=",         # 磁盘镜像覆写
        r":\(\)\{.*\}",      # Fork 炸弹
    ]

    def __init__(self, workspace: str = ".", timeout: int = 300) -> None:
        # 统一转成绝对路径，命令将固定在该目录下执行
        self.workspace = os.path.abspath(workspace)
        # 单条命令超时（秒）：超时后整组杀进程并返回错误；由 config.shell_timeout_sec 注入
        self.timeout = max(1, int(timeout))

    def _is_dangerous(self, command: str) -> str | None:
        """检查命令是否命中危险模式。

        命中则返回拦截提示（含命中的正则模式），否则返回 None。
        """
        for pattern in self.DENY_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return f"安全拦截：检测到危险命令模式 '{pattern}'"
        return None

    async def execute(self, command: str) -> str:
        """执行命令并返回结果字符串（不会向外抛异常）。"""
        # 1. 危险命令前置拦截
        danger = self._is_dangerous(command)
        if danger:
            return danger

        try:
            # 2. 在工作区目录下启动子进程，捕获 stdout/stderr
            #    - stdin=DEVNULL：避免命令（如 npx 首次装包问 "Ok to proceed?"）
            #      去等服务器进程的终端输入而卡死（这是 npx 类命令「假死」的主因）。
            #    - start_new_session=True：让子进程自成进程组，超时可整组杀掉，
            #      否则 npx 拉起的 node 孙进程会脱离控制继续存活。
            #    - env 注入 npm_config_yes=true + CI=true：让 npm/npx 在需要确认时
            #      自动选 yes（如 npx 首次装包问 "Ok to proceed?"），配合 stdin=DEVNULL
            #      既不卡死、又能真正执行，而不是读到 EOF 直接 Aborted 中止。
            env = dict(os.environ)
            env["npm_config_yes"] = "true"
            env["CI"] = "true"
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=self.workspace,
                start_new_session=True,
                env=env,
            )

            try:
                # 3. 超时保护（config.shell_timeout_sec，默认 300 秒）
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                # 超时：杀掉整个进程组（含 npx 拉起的 node 孙进程），再等待回收
                try:
                    if proc.pid is not None:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, Exception):
                    proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
                return f"命令执行超时（{self.timeout}秒），已终止"

            # 4. 拼接输出：先 stdout，stderr 非空时加“标准错误:”前缀
            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")
            parts: list[str] = []
            if out:
                parts.append(out)
            if err:
                parts.append("标准错误:\n" + err)
            result = "".join(parts)

            # 5. 超长截断
            if len(result) > 10000:
                result = result[:10000] + "\n...(输出过长，已截断)"

            # 6. 追加退出码
            result += f"\n[退出码: {proc.returncode}]"
            return result

        except Exception as e:
            # 7. 兜底：任何异常都转成错误信息返回
            return f"命令执行出错：{e}"
