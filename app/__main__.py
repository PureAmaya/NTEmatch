"""``python -m app``（以及 ``uv run main.py``）的入口，具体实现见 :mod:`app.cli`。

两个入口是一回事：``main.py`` 只是把仓库根放进 ``sys.path`` 再调用同一个 ``main()``，
所以「用哪个跑」不会带来行为差异。
"""

from __future__ import annotations

import sys

from .cli import main

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
