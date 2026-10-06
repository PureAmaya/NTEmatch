"""手工重画 QQ 机器人**帮助图**（站点的 ``/help.jpg``）。

**平时不需要跑它**：服务启动时会自动重画一份（见 ``app/helpcard.py``），
产物也不入库。这个脚本只是留给两种场合：

* 想在本地立刻看一眼新版式（不想重启服务）；
* 想把图写到别处（``--out``，比如贴群公告用的一张副本）。

用法：

    uv run python tools/make_help_card.py
    uv run python tools/make_help_card.py --out /tmp/help.jpg

图上的文字在 ``app/helpcard_content.py``（零依赖，测试直接读它比对插件的 ``HELP_TEXT``），
排版在 ``app/helpcard.py``——改了文案重启一次站点就是新的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能 import 到仓库里的 app 包（``python tools/xxx.py`` 时 sys.path[0] 是 tools/）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import helpcard


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成 QQ 机器人帮助图（站点 /help.jpg）",
        epilog=(
            "站点启动时会自动重画一份，所以平时用不到这个脚本；产物也不入库。\n"
            "图上的命令与插件 HELP_TEXT 由测试比对，两边漂了会红。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", default="", help="输出路径（默认 static/help.jpg）")
    parser.add_argument("--quality", type=int, default=92, help="JPEG 质量（默认 92）")
    args = parser.parse_args()

    out = Path(args.out) if args.out else None
    info = helpcard.write(out=out, quality=args.quality)
    if not info["ok"]:
        print(f"! 生成失败：{info.get('reason')}")
        raise SystemExit(1)
    path = Path(info["path"])
    total = sum(len(rows) for _t, rows in helpcard.SECTIONS)
    print(f"已生成 {path}（{total} 条命令，{info['bytes'] // 1024} KB）")
    print(f"指纹已写入 {path.name}{helpcard.STAMP_SUFFIX}")


if __name__ == "__main__":
    main()
