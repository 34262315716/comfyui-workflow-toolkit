# -*- coding: utf-8 -*-
"""跑全部自测。

    python tests/run_tests.py          全部跑
    python tests/run_tests.py -v       失败时打完整堆栈
    python tests/run_tests.py layout   只跑文件名含 layout 的

零依赖 —— 用的是标准库，不装 pytest。
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main() -> int:
    argv = sys.argv[1:]
    verbose = "-v" in argv
    filters = [a for a in argv if not a.startswith("-")]

    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if filters:
        files = [f for f in files
                 if any(k.lower() in os.path.basename(f).lower() for k in filters)]
    if not files:
        print("没有匹配的测试文件")
        return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"

    failed_files = []
    print("cwf 自测 —— %d 个文件" % len(files))
    print("=" * 56)
    for f in files:
        name = os.path.basename(f)
        print("\n### %s" % name)
        cmd = [sys.executable, "-X", "utf8", f]
        if verbose:
            cmd.append("-v")
        r = subprocess.run(cmd, cwd=HERE, env=env)
        if r.returncode != 0:
            failed_files.append(name)

    print("\n" + "=" * 56)
    if failed_files:
        print("失败文件：%s" % ", ".join(failed_files))
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
