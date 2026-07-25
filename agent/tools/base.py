"""Tool 抽象基类。

本模块定义了 Agent 可调用工具的统一抽象接口 `Tool`。所有具体工具
（如搜索、文件读写、代码执行等）都应继承本类，从而获得一致的调用契约与
自动生成 OpenAI function-calling 定义的能力。

设计要点：
- 用 ABC + abstractmethod 强制子类实现必要的元信息与执行逻辑，缺任何一项
  都会在实例化时直接报错，把问题暴露在早期而非运行期。
- name / description / parameters 三项元信息用抽象属性（property）声明，
  子类既可以用 class 级别的类属性覆盖，也可以用 @property 动态计算，
  两种写法都能满足抽象契约，灵活性更好。
- execute 定义为 async，统一按异步工具对待。即使某个工具本身是同步逻辑，
  也应包装成 async，避免调用方在同步/异步之间反复分叉。
- to_function_definition 把三项元信息自动组装成 OpenAI tools 所需的 JSON
  结构，子类无需重复手写，减少格式出错的概率。
"""

from abc import ABC, abstractmethod


class Tool(ABC):
    """Agent 工具的抽象基类。

    子类必须提供三项元信息和一个异步执行方法：

    - ``name``：工具的唯一标识，供模型在 function-calling 时按名调用，
      建议使用小写加下划线（如 ``web_search``）。
    - ``description``：工具用途的自然语言描述，是模型判断"何时该调用本工具"
      的主要依据，应写清楚能力边界与适用场景。
    - ``parameters``：符合 JSON Schema 的参数定义（object 类型），描述本工具
      接受哪些入参、各自类型与是否必填。
    - ``execute``：真正干活的异步方法，接收关键字参数并返回字符串结果。

    子类实现示例::

        class EchoTool(Tool):
            name = "echo"
            description = "原样返回传入的文本，用于测试。"
            parameters = {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要返回的文本"},
                },
                "required": ["text"],
            }

            async def execute(self, **kwargs) -> str:
                return kwargs.get("text", "")

    组装 OpenAI tools 定义::

        tool = EchoTool()
        tools = [tool.to_function_definition()]
        # 直接传给 OpenAI 接口的 tools 参数
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具的唯一名称，供模型按名调用。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具用途描述，是模型决定是否调用本工具的主要依据。"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """工具参数的 JSON Schema 定义（object 类型）。"""
        ...

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具逻辑并返回字符串结果。

        参数以关键字形式传入，具体键名由 ``parameters`` 约定。
        统一返回字符串，方便直接回填给模型作为工具调用结果。
        """
        ...

    def to_function_definition(self) -> dict:
        """组装为 OpenAI function-calling 所需的 tools JSON 结构。

        返回形如::

            {
                "type": "function",
                "function": {
                    "name": <self.name>,
                    "description": <self.description>,
                    "parameters": <self.parameters>,
                },
            }

        该结果可直接放入 OpenAI 接口的 ``tools`` 列表。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
