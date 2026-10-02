"""``python -m app`` / ``nte-match`` 的入口，具体实现见 :mod:`app.cli`。"""

from __future__ import annotations

import sys

from .cli import main

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
