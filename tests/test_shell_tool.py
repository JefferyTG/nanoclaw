"""ExecTool 危险命令黑名单正则的纯字符串匹配测试。

只调用 ExecTool._is_dangerous(command) 做纯正则判断，绝不对测试中的
危险命令调用 execute() 真执行。字符串里出现危险命令字样仅是作为参数
传给纯函数，不会被系统执行。
"""

import unittest

from agent.tools.shell import ExecTool


class TestExecToolDangerPatterns(unittest.TestCase):
    """验证 DENY_PATTERNS 正则的拦截与放行行为。"""

    def setUp(self) -> None:
        self.tool = ExecTool()

    # ---- 应放行（返回 None）----

    def test_discard_stderr_is_allowed(self):
        """2>/dev/null 丢弃标准错误应放行。"""
        self.assertIsNone(self.tool._is_dangerous("grep foo bar.txt 2>/dev/null"))

    def test_discard_stdout_no_space_is_allowed(self):
        """>/dev/null 丢弃标准输出应放行（无空格）。"""
        self.assertIsNone(self.tool._is_dangerous("echo hi >/dev/null"))

    def test_discard_stdout_with_space_is_allowed(self):
        """> /dev/null 丢弃标准输出应放行（带空格）。"""
        self.assertIsNone(self.tool._is_dangerous("echo hi > /dev/null"))

    def test_discard_all_output_is_allowed(self):
        """&>/dev/null 丢弃全部输出应放行。"""
        self.assertIsNone(self.tool._is_dangerous("echo hi &>/dev/null"))

    def test_exec_discard_output_is_allowed(self):
        """exec >/dev/null 重定向当前 shell 输出应放行。"""
        self.assertIsNone(self.tool._is_dangerous("exec >/dev/null"))

    def test_safe_plain_command_is_allowed(self):
        """普通安全命令应放行。"""
        self.assertIsNone(self.tool._is_dangerous("ls -la"))
        self.assertIsNone(self.tool._is_dangerous("cat file.txt"))

    # ---- 应拦截（返回非 None 字符串）----

    def test_block_overwrite_block_device_sda(self):
        self.assertIsNotNone(self.tool._is_dangerous("echo x > /dev/sda"))

    def test_block_overwrite_block_device_disk0(self):
        self.assertIsNotNone(self.tool._is_dangerous("echo x > /dev/disk0"))

    def test_block_overwrite_mem_device(self):
        self.assertIsNotNone(self.tool._is_dangerous("echo x > /dev/mem"))

    def test_block_overwrite_partition_sda1(self):
        self.assertIsNotNone(self.tool._is_dangerous("echo x > /dev/sda1"))

    def test_block_overwrite_wildcard_device(self):
        """/dev/sd* 通配符设备也应拦截。"""
        self.assertIsNotNone(self.tool._is_dangerous("echo x > /dev/sd*"))

    def test_block_null_like_device(self):
        """>/dev/nulllike 之类以 /dev/null 为前缀的冒名设备也应拦截。"""
        self.assertIsNotNone(self.tool._is_dangerous("echo x >/dev/nulllike"))

    def test_block_rm_recursive(self):
        self.assertIsNotNone(self.tool._is_dangerous("rm -rf /tmp/x"))

    def test_block_sudo(self):
        self.assertIsNotNone(self.tool._is_dangerous("sudo ls"))

    def test_block_shutdown(self):
        self.assertIsNotNone(self.tool._is_dangerous("shutdown now"))

    def test_block_dd_disk_overwrite(self):
        self.assertIsNotNone(
            self.tool._is_dangerous("dd if=/dev/zero of=/dev/sda")
        )

    def test_block_nc_listener(self):
        self.assertIsNotNone(self.tool._is_dangerous("nc -l 4444"))

    def test_block_chmod_777(self):
        self.assertIsNotNone(self.tool._is_dangerous("chmod 777 file"))

    def test_block_mkfs(self):
        self.assertIsNotNone(self.tool._is_dangerous("mkfs.ext4 /dev/sda"))


if __name__ == "__main__":
    unittest.main()
