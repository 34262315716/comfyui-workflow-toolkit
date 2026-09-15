# -*- coding: utf-8 -*-
"""
测试组 9：文档里的命令必须真实存在

写文档最容易犯的错，是凭印象写出一个不存在的参数，然后读者照着敲报错。
这个用例把 README 和 docs/ 里出现的每条 `cwf <子命令> --参数` 抓出来，
对着真实的 `--help` 逐个核对。

没有这条测试的后果是实打实的：本仓库的 README 里就曾经同时写着
`cwf render --png`（该命令只认 `--out`）、以及一个子命令总数（写多了），
都是靠这条检查抓出来的。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from harness import Suite, check

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

suite = Suite()

# 抓 `cwf xxx --flag` / `python cwf_run.py xxx --flag`。
# 必须用 [ \t]+ 而不是 \s+ —— 否则 "cd cwf\npython ..." 里的 `cwf`
# 会把下一行的 `python` 当成子命令（这个假阳性踩过）。
CMD_RE = re.compile(r"(?<![A-Za-z])(?:cwf_run\.py|cwf)[ \t]+([a-z][a-z0-9-]*)"
                    r"((?:[ \t]+--?[A-Za-z][\w-]*)*)")


def _run(args):
    return subprocess.run([sys.executable, "-X", "utf8",
                           os.path.join(ROOT, "cwf_run.py")] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=ROOT, timeout=120)


def _docs():
    out = [os.path.join(ROOT, "README.md")]
    dd = os.path.join(ROOT, "docs")
    if os.path.isdir(dd):
        for fn in sorted(os.listdir(dd)):
            if fn.endswith(".md"):
                out.append(os.path.join(dd, fn))
    return [p for p in out if os.path.exists(p)]


@suite.group("文档与命令行对齐")
def _docs_match_cli():
    help_all = _run(["--help"])
    src = (help_all.stdout or "") + (help_all.stderr or "")
    m = re.search(r"\{([a-z0-9,\-]+)\}", src)
    check(m is not None, "拿不到子命令列表，--help 输出变了？")
    cmds = set(m.group(1).split(","))

    # 子命令 help 只查一次，缓存住 —— 否则每条命令都要起一个进程
    opt_cache = {}

    def opts_of(cmd: str) -> set:
        if cmd not in opt_cache:
            r = _run([cmd, "--help"])
            txt = (r.stdout or "") + (r.stderr or "")
            opt_cache[cmd] = set(re.findall(r"(--[a-zA-Z][\w-]*)", txt))
        return opt_cache[cmd]

    bad = []
    n_checked = 0
    for path in _docs():
        rel = os.path.relpath(path, ROOT)
        txt = open(path, encoding="utf-8").read()
        seen = set()
        for mm in CMD_RE.finditer(txt):
            sub, flags = mm.group(1), mm.group(2).strip()
            if (sub, flags) in seen:
                continue
            seen.add((sub, flags))
            n_checked += 1
            if sub not in cmds:
                bad.append("%s: 子命令 `%s` 不存在" % (rel, sub))
                continue
            for f in re.findall(r"--[A-Za-z][\w-]*", flags):
                if f not in opts_of(sub):
                    bad.append("%s: `%s %s` 里没有 %s" % (rel, sub, flags, f))

    check(n_checked > 10,
          "只从文档里抓到 %d 条命令，检查器多半失效了" % n_checked)
    check(not bad, "；".join(bad[:8]))
    print(f"        核对了 {n_checked} 条文档命令，全部真实存在")


suite.case("README / docs 里的每条命令都能对上下真实的 CLI")(_docs_match_cli)


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
