# -*- coding: utf-8 -*-
"""cwf 命令行入口。让 `cwf.cmd` 和 `python -m cwf` 都指向同一处。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cwf.cli import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
