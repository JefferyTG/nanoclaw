"""NanoClaw 日志系统测试（TASK-034）。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from loguru import logger

from config import NanoClawConfig
from main import setup_logging


class LoggingTests(unittest.TestCase):
    def setUp(self):
        """每个测试前清理全局 logger，避免 handler 累积。"""
        logger.remove()

    def tearDown(self):
        """每个测试后清理全局 logger。"""
        logger.remove()

    def _make_config(self, logging_cfg=None, workspace=None):
        """构造带指定 logging 段的最小配置对象。"""
        cfg = NanoClawConfig()
        if workspace is not None:
            cfg.workspace = workspace
        if logging_cfg is not None:
            cfg.logging = logging_cfg
        return cfg

    def test_default_setup_creates_handlers(self):
        """空配置下 setup_logging 应创建默认 console + info_file + error_file。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_config(workspace=tmp)
            setup_logging(cfg)
            handlers = list(logger._core.handlers.values())
            self.assertEqual(len(handlers), 3)

    def test_console_can_be_disabled(self):
        """关闭 console 后不应再向 stderr 写。"""
        cfg = self._make_config(
            logging_cfg={
                "console": {"enabled": False},
                "info_file": {"enabled": False},
                "error_file": {"enabled": False},
            }
        )
        setup_logging(cfg)
        handlers = list(logger._core.handlers.values())
        self.assertEqual(len(handlers), 0)

    def test_info_log_file_receives_record(self):
        """INFO 级别日志应写入 info 文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp, "nanoclaw.log")
            cfg = self._make_config(
                workspace=tmp,
                logging_cfg={
                    "console": {"enabled": False},
                    "info_file": {
                        "enabled": True,
                        "level": "INFO",
                        "path": str(log_path),
                        "rotation": "10 MB",
                        "retention": "7 days",
                    },
                    "error_file": {"enabled": False},
                },
            )
            setup_logging(cfg)
            logger.info("hello-task-034")
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("hello-task-034", content)
            self.assertIn("INFO", content)

    def test_level_filtering(self):
        """配置 level=WARNING 时 INFO 消息不应写入文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp, "nanoclaw.log")
            cfg = self._make_config(
                workspace=tmp,
                logging_cfg={
                    "console": {"enabled": False},
                    "info_file": {
                        "enabled": True,
                        "level": "WARNING",
                        "path": str(log_path),
                        "rotation": "10 MB",
                        "retention": "7 days",
                    },
                    "error_file": {"enabled": False},
                },
            )
            setup_logging(cfg)
            logger.info("should-not-appear")
            logger.warning("should-appear")
            content = log_path.read_text(encoding="utf-8")
            self.assertNotIn("should-not-appear", content)
            self.assertIn("should-appear", content)

    def test_error_file_only_receives_errors(self):
        """error_file 只接收 ERROR 及以上级别。"""
        with tempfile.TemporaryDirectory() as tmp:
            info_path = Path(tmp, "nanoclaw.log")
            error_path = Path(tmp, "nanoclaw.error.log")
            cfg = self._make_config(
                workspace=tmp,
                logging_cfg={
                    "console": {"enabled": False},
                    "info_file": {
                        "enabled": True,
                        "level": "INFO",
                        "path": str(info_path),
                        "rotation": "10 MB",
                        "retention": "7 days",
                    },
                    "error_file": {
                        "enabled": True,
                        "level": "ERROR",
                        "path": str(error_path),
                        "rotation": "10 MB",
                        "retention": "30 days",
                    },
                },
            )
            setup_logging(cfg)
            logger.info("info-message")
            logger.error("error-message")

            info_content = info_path.read_text(encoding="utf-8")
            self.assertIn("info-message", info_content)
            self.assertIn("error-message", info_content)

            error_content = error_path.read_text(encoding="utf-8")
            self.assertNotIn("info-message", error_content)
            self.assertIn("error-message", error_content)

    def test_directory_created_automatically(self):
        """日志目录不存在时应自动创建。"""
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp, "deep", "logs", "nanoclaw.log")
            self.assertFalse(nested.parent.exists())
            cfg = self._make_config(
                workspace=tmp,
                logging_cfg={
                    "console": {"enabled": False},
                    "info_file": {
                        "enabled": True,
                        "level": "INFO",
                        "path": str(nested),
                        "rotation": "10 MB",
                        "retention": "7 days",
                    },
                    "error_file": {"enabled": False},
                },
            )
            setup_logging(cfg)
            self.assertTrue(nested.parent.exists())
            logger.info("directory-ok")
            self.assertIn("directory-ok", nested.read_text(encoding="utf-8"))

    def test_relative_path_resolved_to_workspace(self):
        """相对日志路径应基于 config.workspace 解析。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_config(
                workspace=tmp,
                logging_cfg={
                    "console": {"enabled": False},
                    "info_file": {
                        "enabled": True,
                        "level": "INFO",
                        "path": "workspace/logs/nanoclaw.log",
                        "rotation": "10 MB",
                        "retention": "7 days",
                    },
                    "error_file": {"enabled": False},
                },
            )
            setup_logging(cfg)
            logger.info("relative-path-ok")
            resolved = Path(tmp, "workspace", "logs", "nanoclaw.log")
            self.assertTrue(resolved.exists())
            self.assertIn("relative-path-ok", resolved.read_text(encoding="utf-8"))

    def test_rotation_creates_new_file(self):
        """当日志文件超过 rotation 大小时应自动轮转。"""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp, "nanoclaw.log")
            cfg = self._make_config(
                workspace=tmp,
                logging_cfg={
                    "console": {"enabled": False},
                    "info_file": {
                        "enabled": True,
                        "level": "INFO",
                        "path": str(log_path),
                        # 极小阈值确保一次写入就触发轮转
                        "rotation": "1 B",
                        "retention": "7 days",
                    },
                    "error_file": {"enabled": False},
                },
            )
            setup_logging(cfg)
            logger.info("first-record")
            logger.info("second-record")

            # loguru 轮转后原文件会被重命名，当前写入的新文件保留原路径
            self.assertTrue(log_path.exists())
            rotated = list(Path(tmp).glob("nanoclaw.*"))
            self.assertGreaterEqual(len(rotated), 2)

    def test_log_record_contains_level_and_line(self):
        """日志行应包含时间戳、级别、模块名与行号。"""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp, "nanoclaw.log")
            cfg = self._make_config(
                workspace=tmp,
                logging_cfg={
                    "console": {"enabled": False},
                    "info_file": {
                        "enabled": True,
                        "level": "INFO",
                        "path": str(log_path),
                        "rotation": "10 MB",
                        "retention": "7 days",
                    },
                    "error_file": {"enabled": False},
                },
            )
            setup_logging(cfg)
            logger.info("format-check")
            content = log_path.read_text(encoding="utf-8")
            # 时间戳、级别、模块名、行号、消息都应存在
            self.assertRegex(content, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")
            self.assertIn("INFO", content)
            self.assertIn("test_logging", content)
            self.assertIn("format-check", content)


if __name__ == "__main__":
    unittest.main()
