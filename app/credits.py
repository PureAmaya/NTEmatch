"""版权与许可证信息：页脚「开源组件」面板的**唯一数据源**。

分三类，因为义务并不相同：

* ``RUNTIME`` —— 随本站一起分发的 Python 依赖（镜像里就有）；
* ``PROGRAM`` —— **独立进程、不由本站分发**的外部程序，要自己部署（MediaMTX）；
* ``DEV``     —— 只在开发与 CI 出现，不进镜像。

**许可证兼容性**：本项目以 AGPL-3.0 开源。下面所有组件的许可都是宽松许可或
弱著佐权（MIT / BSD-2 / BSD-3 / Apache-2.0 / MPL-2.0 / PSF-2.0），
**没有任何 GPL-only 组件**——只有 GPL-2.0-only 那类才会与 AGPL-3.0 冲突
（GPL 与 AGPL 之间是「单向兼容」：AGPL 的代码可以被 GPLv3 项目吸收，
但 GPLv2-only 不能与 AGPLv3 组合）。这一点由 ``tests/test_credits.py`` 守着：
换依赖时如果引入了一个 GPL-only 的包，测试会直接红。
"""

from __future__ import annotations

from typing import Any

#: 源码仓库（AGPL 第 13 条：网络使用者有权拿到对应源码，页脚给出入口）
SOURCE_URL = "https://github.com/PureAmaya/NTEmatch"

AUTHOR: dict[str, str] = {
    "name": "早八时睡觉的你",
    "qq": "805575780",
    # QQ 官方头像接口：直连即可，不必自己存一份——换了头像会自动跟着变。
    # 页脚用 referrerpolicy="no-referrer"，不把站内地址带给第三方。
    "qqAvatar": "https://q1.qlogo.cn/g?b=qq&nk=805575780&s=640",
    "bilibili": "https://space.bilibili.com/11393965",
}

LICENSE: dict[str, str] = {
    "id": "AGPL-3.0-only",
    "name": "GNU Affero General Public License v3.0",
    "url": "https://www.gnu.org/licenses/agpl-3.0.html",
    "summary": "可自由使用、修改与分发；但改动后**对外提供服务也必须公开源码**。",
}

# (名称, 许可证, 主页, 用途)
RUNTIME: list[tuple[str, str, str, str]] = [
    ("FastAPI", "MIT", "https://github.com/fastapi/fastapi", "Web 框架 / 接口与依赖注入"),
    ("Starlette", "BSD-3-Clause", "https://github.com/encode/starlette", "ASGI 工具集（FastAPI 的底座）"),
    ("Pydantic", "MIT", "https://github.com/pydantic/pydantic", "配置与数据模型校验"),
    ("pydantic-core", "MIT", "https://github.com/pydantic/pydantic-core", "Pydantic 的 Rust 内核"),
    ("annotated-types", "MIT", "https://github.com/annotated-types/annotated-types", "带注解的类型约束"),
    ("annotated-doc", "MIT", "https://github.com/pydantic/annotated-doc", "FastAPI 的依赖：把文档字符串带进接口说明"),
    ("typing-inspection", "MIT", "https://github.com/pydantic/typing-inspection", "运行时类型检查工具"),
    ("typing-extensions", "PSF-2.0", "https://github.com/python/typing_extensions", "新版类型特性回填"),
    ("Uvicorn", "BSD-3-Clause", "https://github.com/encode/uvicorn", "ASGI 服务器"),
    ("Click", "BSD-3-Clause", "https://github.com/pallets/click", "Uvicorn 的命令行解析"),
    ("h11", "MIT", "https://github.com/python-hyper/h11", "HTTP/1.1 协议实现"),
    ("httptools", "MIT", "https://github.com/MagicStack/httptools", "Uvicorn 的高速 HTTP 解析"),
    ("uvloop", "MIT", "https://github.com/MagicStack/uvloop", "事件循环（非 Windows 平台）"),
    ("watchfiles", "MIT", "https://github.com/samuelcolvin/watchfiles", "开发期热重载"),
    ("websockets", "BSD-3-Clause", "https://github.com/python-websockets/websockets", "实时推送（WebSocket）"),
    ("python-dotenv", "BSD-3-Clause", "https://github.com/theskumar/python-dotenv", "读取 .env"),
    ("PyYAML", "MIT", "https://github.com/yaml/pyyaml", "Uvicorn 的 YAML 配置"),
    # 本站自己的终端彩色输出是手写的（app/console.py，零依赖）；colorama 是 Click
    # 在 Windows 上的依赖，只是跟着装进来
    ("colorama", "BSD-3-Clause", "https://github.com/tartley/colorama", "Windows 终端的 ANSI 支持（Click 的依赖）"),
    ("AnyIO", "MIT", "https://github.com/agronholm/anyio", "异步兼容层（asyncio / trio）"),
    ("HTTPX", "BSD-3-Clause", "https://github.com/encode/httpx", "出站 HTTP 客户端"),
    ("httpcore", "BSD-3-Clause", "https://github.com/encode/httpcore", "HTTPX 的底层传输"),
    ("Certifi", "MPL-2.0", "https://github.com/certifi/python-certifi", "CA 根证书（弱著佐权，可自由组合）"),
    ("IDNA", "BSD-3-Clause", "https://github.com/kjd/idna", "国际化域名支持"),
    ("opentelemetry-api", "Apache-2.0", "https://github.com/open-telemetry/opentelemetry-python", "遥测接口（Starlette 依赖）"),
    (
        "Pillow",
        "MIT-CMU",
        "https://github.com/python-pillow/Pillow",
        "可选：比赛卡片图与帮助图的渲染；没装就退回纯文本推送、不发图，功能不残",
    ),
]

FRONTEND: list[tuple[str, str, str, str]] = [
    (
        "Feather Icons",
        "MIT",
        "https://github.com/feathericons/feather",
        "编辑器工具栏的 SVG 图标（内联，随主题色；本站不使用图标字体）",
    ),
]

PROGRAM: list[tuple[str, str, str, str]] = [
    (
        "MediaMTX",
        "MIT",
        "https://github.com/bluenviron/mediamtx",
        "直播推流与分发（WHIP 推流 / WebRTC 与 HLS 观看）。独立部署，不在本仓库内",
    ),
]

DEV: list[tuple[str, str, str, str]] = [
    ("Ruff", "MIT", "https://github.com/astral-sh/ruff", "Python 静态检查"),
    ("pytest", "MIT", "https://github.com/pytest-dev/pytest", "测试框架"),
    ("pytest-asyncio", "Apache-2.0", "https://github.com/pytest-dev/pytest-asyncio", "异步测试支持"),
    ("pluggy", "MIT", "https://github.com/pytest-dev/pluggy", "pytest 插件机制"),
    ("iniconfig", "MIT", "https://github.com/pytest-dev/iniconfig", "INI 配置解析"),
    ("packaging", "Apache-2.0 或 BSD-2-Clause", "https://github.com/pypa/packaging", "版本与依赖规范"),
    ("Pygments", "BSD-2-Clause", "https://github.com/pygments/pygments", "终端高亮"),
]

#: 前端本体没有第三方 JS 库：原生 ESM + 手写 CSS；只有图标取自 Feather（内联 SVG）。
FRONTEND_NOTE = (
    "前端为零依赖的原生 ESM（无打包器、无第三方 JS 库），样式为手写 CSS；"
    "工具栏图标取自 Feather Icons（MIT），以 SVG 内联，跟随主题色。"
)


def _rows(items: list[tuple[str, str, str, str]]) -> list[dict[str, str]]:
    return [
        {"name": name, "license": lic, "url": url, "note": note}
        for name, lic, url, note in items
    ]


def groups() -> list[dict[str, Any]]:
    return [
        {"key": "runtime", "title": "运行依赖", "note": "随本站一起安装 / 分发", "items": _rows(RUNTIME)},
        {
            "key": "frontend",
            "title": "前端资源",
            "note": "内联进页面，不额外请求",
            "items": _rows(FRONTEND),
        },
        {
            "key": "program",
            "title": "外部程序",
            "note": "独立运行，需自行部署（本站只与它对接）",
            "items": _rows(PROGRAM),
        },
        {"key": "dev", "title": "开发与 CI", "note": "不进镜像、不随本站分发", "items": _rows(DEV)},
    ]


def component_names() -> set[str]:
    """清单里出现过的全部组件名（小写）。

    ``tests/test_credits.py`` 用它对齐 ``pyproject.toml`` 的依赖声明——
    加了依赖却忘了写进清单，测试会提醒（这正是「别忘了那个 M 开头的」的机器版本）。
    """
    names = {name.lower() for name, *_ in RUNTIME + FRONTEND + PROGRAM + DEV}
    # pydantic-core / typing-extensions 这类名字在 pyproject 里不会出现，
    # 对齐时按「声明名」统一一下大小写与连字符即可
    return names


def payload() -> dict[str, Any]:
    """下发给前端的整包数据（公开信息，无敏感内容）。

    ``AUTHOR`` 里的 ``qq`` **不下发**：页面不展示它（署名只给名字 / B 站 / 仓库），
    那就不该躺在每个访客都能拿到的 JSON 里。它仍然留在 ``AUTHOR`` 中——头像是
    按它拼出来的 ``qqAvatar``。
    """
    author = {key: value for key, value in AUTHOR.items() if key != "qq"}
    return {
        "author": author,
        "license": LICENSE,
        "source": SOURCE_URL,
        "frontendNote": FRONTEND_NOTE,
        "groups": groups(),
    }
