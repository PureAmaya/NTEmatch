"""NTE 比赛 · **单一入口**（开发、发行版、镜像都跑这一个文件）。

    uv run main.py                 起服务（默认就是热更新守护）
    uv run main.py --debug         开发者模式（详细日志 / 接口文档 / 前端调试日志 / 机器人沙盒）
    uv run main.py --port 8123     换端口（默认 10961）
    uv run main.py --check         只体检、不起服务
    uv run main.py --help          完整命令表

这里**只做三件事**：把仓库根放进 ``sys.path``、调用 :func:`app.cli.main`、原样传回退出码。
启动、调试开关、热更新、侧车自举编译全在 ``app/cli.py``——入口是个薄壳，逻辑不复制第二份，
所以「用 main.py 跑」和「用 python -m app 跑」行为完全一致。

为什么要这个文件：``python -m app`` 要求**当前目录正好是仓库根**才 import 得到 ``app`` 包，
而多数人的习惯是 ``uv run main.py``；镜像里的 CMD 也用它，开发者与发行版走的同一条路。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 仓库根进 sys.path：从别处 `python /path/to/main.py` 也能 import 到 app 包
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 先把 sys.path 修好，再导入（顺序不能反）
from app.cli import main


def run() -> int:
    """跑命令行入口，返回退出码（``Ctrl+C`` 按惯例给 130）。"""
    try:
        return main()
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
