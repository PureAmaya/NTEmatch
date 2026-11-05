"""生成默认分享图（og:image）。

站点的平面语言（墨黑底 + 青/品红 + 网点 + 警示胶带 + 渐变标题）**用代码画出来**，
而不是塞一份设计稿：这样改主色、改站名都能重新生成，仓库里也不会多一个二进制设计源文件。

画法与机器人出图是**同一份实现**：``go/internal/render`` 的 ``share`` 分支
（配色与静态资源都在那边，这里只负责传参）。所以要先有二进制：

    cd go && go build -o bin/nterender ./cmd/nterender

然后：

    uv run python tools/make_share_card.py
    uv run python tools/make_share_card.py --title "异环赛事" --tagline "S2 · 秋季赛"

产物：``static/og.png``（1200×630，绝大多数分享卡片都吃这个尺寸）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能 import 到仓库里的 app 包（``python tools/xxx.py`` 时 sys.path[0] 是 tools/）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import render as renderer


def main() -> None:
    parser = argparse.ArgumentParser(description="生成分享图 static/og.png")
    parser.add_argument("--title", default="NTE 比赛", help="大标题（默认 NTE 比赛）")
    parser.add_argument("--tagline", default="NEVERNESS TO EVERNESS", help="副标题（建议西文）")
    parser.add_argument("--note", default="自动分组 · 积分结算 · 实时排行 · 直播推流", help="左下角说明")
    parser.add_argument("--out", default="static/og.png", help="输出路径")
    args = parser.parse_args()

    if not renderer.available():
        print(
            "出图侧车不可用：先构建 cd go && go build -o bin/nterender ./cmd/nterender",
            file=sys.stderr,
        )
        raise SystemExit(1)

    result = renderer.render(
        "share",
        {"title": args.title, "tagline": args.tagline, "note": args.note},
    )
    if result is None:
        print("分享图生成失败（详见日志）", file=sys.stderr)
        raise SystemExit(1)
    data, width, height = result
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"已生成 {out} ({out.stat().st_size // 1024} KB, {width}×{height})")


if __name__ == "__main__":
    main()
