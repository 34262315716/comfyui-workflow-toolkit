#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
node_report.py —— 导出本机节点库的机器可读总览。

为什么需要它：`cwf nodes stats` 是给人读的文本，agent 想知道
「本机到底有多少节点 / 哪些插件贡献最多 / 某个包装了些什么」时，
直接吃这个脚本的 JSON 更省事，也不用把整份节点库读进上下文。

用法：
    python node_report.py                 # 全量总览（默认 JSON）
    python node_report.py --top 30        # 只看贡献最多的 30 个包
    python node_report.py --package kjnodes   # 看某个包装了哪些节点
    python node_report.py --text          # 输出人读文本

依赖：cwf 工具内核（仓库根可用 CWF_HOME 环境变量指定；
      不给就从这个脚本的位置往上找，找得到 cwf_run.py 的那层就是仓库根）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _find_home() -> str:
    """找到 cwf 仓库根。CWF_HOME 优先，其次从脚本位置往上找。"""
    env = os.environ.get("CWF_HOME")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(6):
        if os.path.isfile(os.path.join(cur, "cwf_run.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


HOME = _find_home()
if HOME and os.path.isdir(HOME):
    sys.path.insert(0, HOME)


def main() -> int:
    ap = argparse.ArgumentParser(description="导出本机 ComfyUI 节点库总览")
    ap.add_argument("--top", type=int, default=20, help="列出贡献最多的前 N 个包")
    ap.add_argument("--package", default=None, help="只看某个包（包名子串匹配）")
    ap.add_argument("--text", action="store_true", help="输出人读文本而不是 JSON")
    ap.add_argument("--refresh", action="store_true", help="强制重建节点库缓存")
    ap.add_argument("--root", default=None, help="工作流库根目录（用于统计使用频次）")
    args = ap.parse_args()

    try:
        from cwf.lib import catalog as cat_mod
        from cwf.lib import paths
        from cwf.lib.schema import DEFAULT_SERVER
    except ImportError as e:
        print(f"✖ 加载 cwf 内核失败：{e}\n  现在认的仓库根是 {HOME!r}。"
              f"用 CWF_HOME=<克隆下来的仓库路径> 指定。", file=sys.stderr)
        return 2

    root = args.root or os.environ.get(
        "CWF_WORKFLOWS",
        paths.default_workflow_root())
    cat = cat_mod.build_catalog(DEFAULT_SERVER, root, refresh=args.refresh)
    totals = cat["totals"]
    pkgs = cat_mod.package_report(cat, limit=args.top)

    report = {
        "built": cat["built_str"],
        "source": cat["source"],
        "workflows_root": root,
        "totals": totals,
        "top_packages": [
            {"package": p, "types": t, "uses": u} for p, t, u in pkgs
        ],
    }

    if args.package:
        q = args.package.lower()
        rows = [(t, v) for t, v in cat["types"].items()
                if q in (v.get("package") or "").lower()]
        rows.sort(key=lambda kv: -kv[1].get("usage", 0))
        report["package_filter"] = args.package
        report["matched_types"] = len(rows)
        report["nodes"] = [
            {"type": t, "category": v.get("category_zh") or v.get("category"),
             "package": v.get("package"), "usage": v.get("usage", 0),
             "outputs": v.get("outputs", []), "widgets": v.get("widgets", [])}
            for t, v in rows[:200]
        ]

    if args.text:
        print(f"本机节点库（{report['built']}，来源 {report['source']}）")
        print(f"  节点类型总数  {totals['types']}")
        print(f"  用过的类型    {totals['with_usage']}")
        print(f"  插件包数量    {totals['packages']}")
        print(f"  扫描工作流    {totals['workflows_scanned']} 个"
              f"（{totals['node_instances']} 个节点实例）")
        print(f"\n贡献最多的 {len(pkgs)} 个包：")
        for p, t, u in pkgs:
            print(f"  {p:<44} {t:>4} 种 / 用过 {u} 次")
        if args.package:
            print(f"\n包 {args.package!r} 匹配 {report['matched_types']} 种节点：")
            for n in report.get("nodes", [])[:40]:
                print(f"  {n['type']:<44} {n['category'] or '-':<18} ×{n['usage']}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
