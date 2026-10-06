"""热更新守护（薄封装）：真正实现在 :mod:`app.hotrun`。

为什么做成薄封装而不是把逻辑写在这里：装成包（``uv sync`` 后从任何目录跑）时
``tools/`` 根本不存在，而热更新是**运行期功能**（管理端那个「立即更新」按钮要用它），
所以它必须住在 ``app/`` 里。这个文件只是给习惯 ``tools/xxx.py`` 的人一个入口。

    uv run python tools/hotrun.py --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hotrun import main

if __name__ == "__main__":
    raise SystemExit(main())
