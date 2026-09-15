# -*- coding: utf-8 -*-
"""
测试组 8：输出路径与渲染版面的两个「踩过坑」回归

这两条都是**狗粮吃出来的**（拿自己的工具跑 README 里的例子就崩了），
不是想出来的边界情况，所以钉成测试：

  1. `--out` 给一个目录（`--out out/`）时，早期版本会把目录当文件写，
     Windows 上抛 `PermissionError: Permission denied: 'out/'` ——
     报错完全看不出真正原因。现在必须自动拼上文件名。
  2. 渲染的图例早期是画在画布左下角的**浮层**，节点正好落在那里就被压住。
     现在画布底部会留一条专属带，图例必须落在带子里、不碰任何节点。
"""
from __future__ import annotations

import os
import shutil
import tempfile

from harness import Suite, check, eq

from cwf import cli
from cwf.lib import render as R
from cwf.lib.graph import Graph, Node, Slot

suite = Suite()
TMP = os.path.join(tempfile.gettempdir(), "cwf_io_test")


def _fresh(sub: str) -> str:
    d = os.path.join(TMP, sub)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


# ================================================================
# 1. 输出路径：文件 / 目录 两种写法都要支持
# ================================================================

@suite.group("输出路径：--out 可以给文件，也可以给目录")
def _dest():
    d = _fresh("dest")
    sub = os.path.join(d, "out")

    # 目录不存在，但以分隔符结尾 → 建目录 + 拼名字
    p = cli.resolve_dest(sub + os.sep, "我的流")
    eq(p, os.path.join(sub, "我的流.json"), "以分隔符结尾应当拼上名字")
    check(os.path.isdir(sub), "应当顺手把目录建出来")

    # 目录已存在（没有尾分隔符）→ 也拼名字
    p = cli.resolve_dest(sub, "另一个流")
    eq(p, os.path.join(sub, "另一个流.json"), "已存在的目录应当拼上名字")

    # 给了 .json 结尾的名字，不能又拼一个 .json
    p = cli.resolve_dest(sub + os.sep, "带后缀.json")
    eq(p, os.path.join(sub, "带后缀.json"), "已经有 .json 后缀时不该再拼一层")

    # 明确给文件路径 → 原样用，并且把父目录建出来
    deep = os.path.join(d, "a", "b", "c.json")
    p = cli.resolve_dest(deep, "无所谓")
    eq(p, deep, "给文件路径就该原样返回")
    check(os.path.isdir(os.path.dirname(deep)), "父目录应当被建出来")

    # 没给 dest → 落到默认目录
    p = cli.resolve_dest(None, "新流", os.path.join(d, "default"))
    eq(p, os.path.join(d, "default", "新流.json"), "没给 --out 时落到默认目录")


suite.case("resolve_dest 认得文件、目录、以及不存在的多级父目录")(_dest)


def _save_to_dir():
    """把目录喂给 _save，不能崩 —— 这是当初 PermissionError 的那条路径。"""
    d = _fresh("save")
    g = Graph()
    g.add_node("KSampler", pos=(0, 0), size=(300, 140))
    import contextlib
    import io as _io
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._save(g, d + os.sep, cli.Out())
    files = [f for f in os.listdir(d) if f.endswith(".json")]
    eq(len(files), 1, "目录里应当出现恰好一个 json 文件")
    check(os.path.getsize(os.path.join(d, files[0])) > 0, "写出来的文件不该是空的")


suite.case("_save 收到目录时兜底补文件名，而不是抛 PermissionError")(_save_to_dir)


# ================================================================
# 2. 渲染：图例不许压到节点
# ================================================================

@suite.group("渲染版面：图例有专属底带，不压节点")
def _legend_band():
    # 单节点、无分区的最小图，最容易暴露浮层压节点的问题
    g = Graph()
    n = g.add_node("KSampler", pos=(0, 0), size=(320, 200))
    n.inputs = [Slot(name="model", type="MODEL")]
    n.outputs = [Slot(name="LATENT", type="LATENT")]

    off = R.build_scene(g, None, R.Opts(legend=False))
    on = R.build_scene(g, None, R.Opts(legend=True))
    check(on.h > off.h, "开了图例的画布必须更高（那条带子是实打实留出来的）")

    band = R.legend_band(len(on.legend), R.Opts(legend=True))
    eq(round(on.h - off.h, 1), round(band, 1),
       "多出来的高度应当正好等于图例带")

    # 图例真的落在底带里：它的上边缘必须在所有节点的下边缘之下
    hgt = R.legend_height(len(on.legend))
    y_legend_top = on.h - hgt - R.Opts().pad * 0.4
    y_nodes_bottom = max(b.y + b.h for b in on.boxes.values()) + on.off_y
    check(y_legend_top >= y_nodes_bottom,
          "图例上边缘 %.0f 压到了节点下边缘 %.0f" % (y_legend_top, y_nodes_bottom))


suite.case("开了图例就把画布加高一条带，且图例不碰节点")(_legend_band)


def _legend_off():
    """--no-legend 时不该白留一条空白带。"""
    g = Graph()
    g.add_node("KSampler", pos=(0, 0), size=(320, 200))
    a = R.build_scene(g, None, R.Opts(legend=False))
    b = R.build_scene(g, None, R.Opts(legend=False, pad=48.0))
    eq(a.h, b.h, "关掉图例时高度不该随图例条数变化")
    eq(R.legend_band(5, R.Opts(legend=False)), 0.0, "关掉图例时带高应当是 0")


suite.case("关掉图例时不留空白带")(_legend_off)


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
