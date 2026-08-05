"""TASK-008 摘要输入降噪单测。

验证 ``ContextCompactor._messages_to_text`` 喂给摘要模型的视图：
- 写文件类工具：只留「工具名 + 成功/失败 + 路径 + 字符数」结论，不含文件全文；
- shell 输出：只留退出码与首尾摘要，不含完整 stdout/stderr；
- 搜索结果：只留标题级信息，省略正文；
- web_fetch / read_file / 大段代码：通用首尾窗口截断；
- 用户 / assistant 普通文本不受影响；图片占位行为不变；
- 摘要关键事实（写文件结论、shell 退出码）不因降噪丢失；
- 敏感值不透传：token/secret/api_key 等敏感键（含 camelCase 变体）整键丢弃，
  嵌套 JSON 字符串与 sk-/AKIA/eyJ 等特征值级检测丢弃；
- 图类工具（generate_image/ask_image）白名单保留 prompt/question 关键事实，
  丢弃 base64 大字段。
"""

import unittest

from agent.memory import ContextCompactor


def _assistant_tool_msg(call_id: str, name: str, arguments: dict) -> dict:
    """构造 OpenAI 格式的 assistant tool_calls 消息（arguments 为 JSON 字符串）。"""
    import json

    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def _tool_msg(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class ToolDenoiseTests(unittest.TestCase):
    # —— 规则 1：write_file 等写文件类工具只留结论 ——

    def test_write_file_result_presents_conclusion_only(self):
        secret_content = "TOP_SECRET_FILE_BODY_" + ("x" * 5000)
        messages = [
            _assistant_tool_msg("c1", "write_file", {
                "file_path": "output/result.txt",
                "content": secret_content,
            }),
            _tool_msg("c1", "已写入 5000 字符到 output/result.txt"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertNotIn(secret_content, text)          # 文件全文不出现
        self.assertNotIn("TOP_SECRET_FILE_BODY", text)
        self.assertIn("write_file", text)               # 工具名出现
        self.assertIn("output/result.txt", text)        # 写入目标路径出现
        self.assertIn("已写入 5000 字符", text)          # 字符数结论出现

    def test_write_file_short_result_passthrough(self):
        messages = [
            _assistant_tool_msg("c1", "write_file", {
                "file_path": "a.md",
                "content": "hello",
            }),
            _tool_msg("c1", "已写入 5 字符到 a.md"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn("已写入 5 字符到 a.md", text)      # 短结论原样保留
        self.assertNotIn("hello", text)

    # —— 规则 2：shell 输出只留退出码与首尾摘要 ——

    def test_exec_output_keeps_exit_code_and_head_tail(self):
        lines = [f"line-{i:04d} " + "z" * 40 for i in range(200)]
        stdout = "\n".join(lines)
        big_output = f"{stdout}\n标准错误:\nboom\n[退出码: 0]"
        messages = [
            _assistant_tool_msg("s1", "exec", {"command": "cat big.log"}),
            _tool_msg("s1", big_output),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn("[退出码: 0]", text)               # 退出码保留（在末尾）
        self.assertIn("line-0000", text)                 # 首部摘要保留
        # 中段原文不出现（首部窗口约 133 字符、尾部窗口约 67 字符，
        # 中间行既不在首部也不在尾部，保证没有把完整输出灌进去）
        self.assertNotIn("line-0050", text)
        self.assertNotIn("line-0150", text)
        # 输出被压缩到 ~200 字符窗口内（含 role 前缀开销）
        tool_line = next(
            ln for ln in text.splitlines() if ln.startswith("[tool]")
        )
        self.assertLess(len(tool_line), 220)

    def test_exec_short_error_passthrough(self):
        messages = [
            _assistant_tool_msg("s1", "exec", {"command": "nope"}),
            _tool_msg("s1", "命令执行超时（60秒），已终止"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn("命令执行超时", text)

    # —— 规则 3：搜索结果只留标题级信息 ——

    def test_web_search_result_titles_only(self):
        body_secret = "SEARCH_RESULT_BODY_SENSITIVE_" + "q" * 300
        search_result = (
            "### 1. 最新 AI 新闻\n"
            "链接: https://example.com/1\n"
            f"{body_secret}\n\n"
            "### 2. 市场动态\n"
            "链接: https://example.com/2\n"
            f"{body_secret}\n"
        )
        messages = [
            _assistant_tool_msg("w1", "web_search", {
                "query": "AI 新闻", "max_results": 5,
            }),
            _tool_msg("w1", search_result),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn("### 1. 最新 AI 新闻", text)       # 标题保留
        self.assertIn("链接: https://example.com/1", text)
        self.assertIn("### 2. 市场动态", text)
        self.assertIn("https://example.com/2", text)
        self.assertNotIn(body_secret, text)              # 正文省略
        self.assertNotIn("SEARCH_RESULT_BODY_SENSITIVE", text)
        # query 参数保留在参数摘要里
        self.assertIn("AI 新闻", text)

    # —— 规则 4：web_fetch / read_file / 大段代码 通用首尾截断 ——

    def test_read_file_and_web_fetch_generic_truncation(self):
        file_body = "FILE_START_HEADER\n" + ("m" * 3000) + "\nFILE_END_FOOTER"
        messages = [
            _assistant_tool_msg("r1", "read_file", {"file_path": "big.py"}),
            _tool_msg("r1", file_body),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn("FILE_START_HEADER", text)         # 首部保留
        self.assertIn("FILE_END_FOOTER", text)           # 尾部保留
        self.assertNotIn("m" * 500, text)                # 中段省略（首部窗口只含 ~115 个 m）
        self.assertIn("big.py", text)

        fetch_messages = [
            _assistant_tool_msg("f1", "web_fetch", {"url": "https://a.com/page"}),
            _tool_msg("f1", "TITLE_LINE\n" + ("n" * 2000) + "\nTAIL_MARK"),
        ]
        fetch_text = ContextCompactor._messages_to_text(fetch_messages)
        self.assertIn("TITLE_LINE", fetch_text)
        self.assertIn("TAIL_MARK", fetch_text)
        self.assertNotIn("n" * 500, fetch_text)
        self.assertIn("https://a.com/page", fetch_text)  # URL 保留

    # —— 规则 5：arguments 解析失败降级为安全截断 ——

    def test_arguments_parse_failure_safe_truncation(self):
        # 故意弄坏 JSON（未闭合引号 + 超长 content 值）
        broken_json = '{"file_path": "a", "content": "' + ("q" * 2000)
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "b1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": broken_json},
                }],
            },
            _tool_msg("b1", "已写入 1 字符到 a"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        # 不抛异常，且不出现大段原文（被安全截断）
        self.assertLess(text.count("q"), 400)
        # 结论仍保留
        self.assertIn("已写入 1 字符到 a", text)

    # —— 规则 6：嵌套大字段（如 spawn_subagent 的 prompt）也被丢弃 ——

    def test_heavy_nested_args_dropped(self):
        secret_prompt = "SPAWN_SECRET_PROMPT_" + ("p" * 1500)
        messages = [
            _assistant_tool_msg("sp1", "spawn_subagent", {
                "name": "helper",
                "instructions": secret_prompt,
                "config": {"prompt": secret_prompt, "mode": "normal"},
            }),
            _tool_msg("sp1", "子 Agent 已完成任务"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn("spawn_subagent", text)
        self.assertIn("helper", text)
        self.assertNotIn("SPAWN_SECRET_PROMPT", text)    # 嵌套 prompt 不出现
        self.assertNotIn(secret_prompt, text)

    # —— 规则 7：图片占位行为不变 ——

    def test_image_placeholder_unchanged(self):
        secret_b64 = "SENSITIVE_BASE64_" + ("B" * 200)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看看这张图"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + secret_b64,
                }},
            ],
        }]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn("图片内容已省略", text)
        self.assertNotIn(secret_b64, text)

    # —— 规则 8：用户 / assistant 普通文本不受影响 ——

    def test_user_assistant_plain_text_unaffected(self):
        long_user = "user 正文 " + ("u" * 5000)
        long_assistant = "assistant 正文 " + ("a" * 5000)
        messages = [
            {"role": "user", "content": long_user},
            {"role": "assistant", "content": long_assistant},
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn(long_user, text)                   # 全文保留
        self.assertIn(long_assistant, text)

    # —— 规则 9：关键事实不因降噪丢失（多工具混合场景） ——

    def test_key_facts_preserved_mixed_scenario(self):
        messages = [
            _assistant_tool_msg("c1", "write_file", {
                "file_path": "report.md",
                "content": "SECRET_REPORT_BODY" + "r" * 3000,
            }),
            _tool_msg("c1", "已写入 3000 字符到 report.md"),
            _assistant_tool_msg("s1", "exec", {"command": "python run.py"}),
            _tool_msg("s1", ("out\n" * 500) + "[退出码: 1]"),
            {"role": "assistant", "content": "任务失败，已修改脚本"},
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertNotIn("SECRET_REPORT_BODY", text)     # 文件全文不出现
        self.assertNotIn("out\n" * 100, text)             # shell 全文不出现（首部窗口只含 ~33 个 out）
        self.assertIn("report.md", text)                 # 写入路径保留
        self.assertIn("已写入 3000 字符", text)           # 写文件结论保留
        self.assertIn("[退出码: 1]", text)               # 退出码保留
        self.assertIn("python run.py", text)             # 命令参数保留
        self.assertIn("任务失败，已修改脚本", text)        # 普通文本不受影响


    # —— 规则 10：P1-A 敏感值不透传 ——

    def test_nested_json_string_secret_dropped(self):
        # config 是字符串字段，内部嵌套 JSON 且含 api_key 密钥 —— 必须整串清洗
        secret_key = "sk-SECRET-" + "K" * 30
        nested = (
            '{"api_key": "%s", "model": "gpt-4o", '
            '"settings": {"mode": "fast"}}' % secret_key
        )
        messages = [
            _assistant_tool_msg("c1", "configure", {"config": nested}),
            _tool_msg("c1", "配置已保存"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertNotIn(secret_key, text)   # 内嵌 api_key 值不出现
        self.assertNotIn("api_key", text)    # 敏感键名也不出现
        self.assertIn("gpt-4o", text)        # 非敏感字段保留
        self.assertIn("fast", text)

    def test_sensitive_token_key_dropped(self):
        # token 挂在非黑名单容器键（creds）下 —— 敏感键递归命中即丢弃
        secret_token = "sk-TOPSECRET-" + "T" * 20
        messages = [
            _assistant_tool_msg("c1", "some_tool", {
                "options": {"creds": {"token": secret_token, "scope": "read"}},
                "target": "prod",
            }),
            _tool_msg("c1", "done"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertNotIn(secret_token, text)
        self.assertNotIn("sk-TOPSECRET", text)
        self.assertIn("prod", text)          # 非敏感字段保留

    def test_camel_case_sensitive_key_dropped(self):
        # camelCase 变体归一为 snake_case 后命中黑名单（accessKey→access_key）
        messages = [
            _assistant_tool_msg("c1", "upload", {
                "accessKey": "AKIA1234567890ABCDEF",
                "fileName": "a.txt",
                "fileContent": "C" * 500,
            }),
            _tool_msg("c1", "上传完成"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertNotIn("AKIA1234567890ABCDEF", text)  # 敏感键值不出现
        self.assertNotIn("C" * 100, text)               # file_content 大字段不出现
        self.assertIn("a.txt", text)                    # 普通字段保留

    # —— 规则 11：图类工具 prompt/question 保留，base64 大字段丢弃 ——

    def test_generate_image_prompt_preserved_base64_dropped(self):
        prompt = "一只戴红围巾的柴犬在雪地奔跑"
        b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        messages = [
            _assistant_tool_msg("g1", "generate_image", {
                "prompt": prompt,
                "size": "1024x1024",
                "image_url": "data:image/png;base64," + b64,
            }),
            _tool_msg("g1", "已生成图片 image_123（1024x1024）"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn(prompt, text)          # prompt 关键事实保留
        self.assertIn("1024x1024", text)
        self.assertNotIn(b64, text)          # base64 大字段不出现
        self.assertNotIn("data:image/png", text)

    def test_ask_image_question_preserved(self):
        question = "这两张图里的建筑风格有什么不同？"
        messages = [
            _assistant_tool_msg("g1", "ask_image", {
                "image_id": ["img_1", "img_2"],
                "question": question,
            }),
            _tool_msg("g1", "左图是哥特式，右图是巴洛克式"),
        ]
        text = ContextCompactor._messages_to_text(messages)
        self.assertIn(question, text)        # question 关键事实保留
        self.assertIn("img_1", text)         # 图片引用保留
        self.assertIn("左图是哥特式", text)


if __name__ == "__main__":
    unittest.main()
