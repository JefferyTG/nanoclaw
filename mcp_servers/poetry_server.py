"""古诗词查询 MCP Server。

使用 mcp 库的 FastMCP 简化模式，通过 stdio 传输暴露三个工具：
  - search_poetry(keyword): 按关键词搜索诗词（标题/正文）
  - random_poetry():        随机返回一首诗
  - list_poets():           返回去重的诗人列表

运行方式（由 MCP 客户端以子进程方式拉起，默认 stdio）：
  uv run python mcp_servers/poetry_server.py
"""

from mcp.server.fastmcp import FastMCP
import random

# ---------------------------------------------------------------------------
# 数据源：硬编码诗词库（唐诗宋词，>=10 首）
# ---------------------------------------------------------------------------
POEMS = [
    {"title": "静夜思", "author": "李白", "dynasty": "唐",
     "content": "床前明月光，疑是地上霜。举头望明月，低头思故乡。"},
    {"title": "春晓", "author": "孟浩然", "dynasty": "唐",
     "content": "春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。"},
    {"title": "登鹳雀楼", "author": "王之涣", "dynasty": "唐",
     "content": "白日依山尽，黄河入海流。欲穷千里目，更上一层楼。"},
    {"title": "相思", "author": "王维", "dynasty": "唐",
     "content": "红豆生南国，春来发几枝。愿君多采撷，此物最相思。"},
    {"title": "鹿柴", "author": "王维", "dynasty": "唐",
     "content": "空山不见人，但闻人语响。返景入深林，复照青苔上。"},
    {"title": "江雪", "author": "柳宗元", "dynasty": "唐",
     "content": "千山鸟飞绝，万径人踪灭。孤舟蓑笠翁，独钓寒江雪。"},
    {"title": "悯农", "author": "李绅", "dynasty": "唐",
     "content": "锄禾日当午，汗滴禾下土。谁知盘中餐，粒粒皆辛苦。"},
    {"title": "望庐山瀑布", "author": "李白", "dynasty": "唐",
     "content": "日照香炉生紫烟，遥看瀑布挂前川。飞流直下三千尺，疑是银河落九天。"},
    {"title": "早发白帝城", "author": "李白", "dynasty": "唐",
     "content": "朝辞白帝彩云间，千里江陵一日还。两岸猿声啼不住，轻舟已过万重山。"},
    {"title": "黄鹤楼送孟浩然之广陵", "author": "李白", "dynasty": "唐",
     "content": "故人西辞黄鹤楼，烟花三月下扬州。孤帆远影碧空尽，唯见长江天际流。"},
    {"title": "水调歌头", "author": "苏轼", "dynasty": "宋",
     "content": "明月几时有？把酒问青天。不知天上宫阙，今夕是何年。人有悲欢离合，月有阴晴圆缺，此事古难全。但愿人长久，千里共婵娟。"},
    {"title": "念奴娇·赤壁怀古", "author": "苏轼", "dynasty": "宋",
     "content": "大江东去，浪淘尽，千古风流人物。故垒西边，人道是，三国周郎赤壁。乱石穿空，惊涛拍岸，卷起千堆雪。江山如画，一时多少豪杰。"},
    {"title": "声声慢", "author": "李清照", "dynasty": "宋",
     "content": "寻寻觅觅，冷冷清清，凄凄惨惨戚戚。乍暖还寒时候，最难将息。三杯两盏淡酒，怎敌他、晚来风急？雁过也，正伤心，却是旧时相识。"},
    {"title": "如梦令", "author": "李清照", "dynasty": "宋",
     "content": "昨夜雨疏风骤，浓睡不消残酒。试问卷帘人，却道海棠依旧。知否，知否？应是绿肥红瘦。"},
    {"title": "醉花阴", "author": "李清照", "dynasty": "宋",
     "content": "薄雾浓云愁永昼，瑞脑销金兽。佳节又重阳，玉枕纱厨，半夜凉初透。东篱把酒黄昏后，有暗香盈袖。莫道不销魂，帘卷西风，人比黄花瘦。"},
]


def _format_poem(p: dict) -> str:
    """统一格式化单首诗词。"""
    return f"《{p['title']}》—— {p['author']}（{p['dynasty']}）\n{p['content']}"


# ---------------------------------------------------------------------------
# FastMCP 实例
# ---------------------------------------------------------------------------
mcp = FastMCP("poetry-server")


@mcp.tool()
def search_poetry(keyword: str) -> str:
    """搜索包含指定关键词的古诗词（在标题与正文中匹配）。

    Args:
        keyword: 要搜索的关键词，例如「月」「李白」「思乡」。
    """
    kw = (keyword or "").strip()
    if not kw:
        return "请提供搜索关键词。"

    matched = [
        p for p in POEMS
        if kw in p["title"] or kw in p["content"]
    ]

    if not matched:
        return "未找到包含该关键词的诗词"

    return "\n\n".join(_format_poem(p) for p in matched)


@mcp.tool()
def random_poetry() -> str:
    """随机返回一首古诗词。"""
    return _format_poem(random.choice(POEMS))


@mcp.tool()
def list_poets() -> str:
    """返回诗词库中所有诗人列表（去重）。"""
    poets = []
    for p in POEMS:
        name = f"{p['author']}（{p['dynasty']}）"
        if name not in poets:
            poets.append(name)
    return "诗人列表（共 {} 位）：\n{}".format(
        len(poets), "\n".join(f"{i+1}. {n}" for i, n in enumerate(poets))
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
