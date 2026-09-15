# -*- coding: utf-8 -*-
"""
测试组 7：执行护栏 + 消重叠硬保证

两条硬约束，这里把它们钉死在代码里：

  * **跑不动的活不许悄悄提交**：ControlNet 类节点吃显存和内存很凶，
    视频生成是长任务、崩一次损失大。这两条不能写成"注意事项"靠记得住，
    必须让命令**默认拒绝**，要跑得显式放行。判定按工作流本身
    （节点类型 / 端口类型）来，不假设谁的显卡多大；阈值与开关都可用
    CWF_VRAM_GB / CWF_MAX_MP / CWF_GUARD 调。
  * **节点重叠是工具的责任**：交出去的图必须一个重叠都没有，
    否则用户还得回 ComfyUI 手动拖一遍，那这个工具就白做了。
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile

from harness import Suite, check, eq, skip

from cwf.lib import paths
from cwf import cli
from cwf.lib.graph import Graph, Link, Node, Slot

suite = Suite()
TMP = os.path.join(tempfile.gettempdir(), "cwf_guard_test")
WF = paths.default_workflow_root()


def _node(nid, type_, **kw) -> Node:
    n = Node(id=nid, type=type_, pos=kw.get("pos", (0, 0)),
             size=kw.get("size", (300, 140)), title=kw.get("title"))
    n.inputs = [Slot(name=a, type=b) for a, b in kw.get("ins", ())]
    n.outputs = [Slot(name=a, type=b) for a, b in kw.get("outs", ())]
    if kw.get("widgets") is not None:
        n.widgets = kw["widgets"]
        n._wform = "list"
    return n


def _save(nodes, path) -> str:
    Graph(nodes, [], []).save(path)
    return path


def _run(*argv):
    """跑一条命令，返回 (输出, 是否被 SystemExit 拦下)。"""
    buf = io.StringIO()
    blocked = False
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(list(argv))
    except SystemExit:
        blocked = True
    return buf.getvalue(), blocked


def _fresh():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    return TMP


# ---------------------------------------------------------------- 红线识别


suite.group("执行护栏：识别")


@suite("含 ControlNet 的图会被标出来")
def _cn_detect():
    p = _save([_node(1, "CheckpointLoaderSimple"),
               _node(2, "ControlNetLoader"),
               _node(3, "ControlNetApplyAdvanced",
                     ins=(("conditioning", "CONDITIONING"),
                          ("control_net", "CONTROL_NET")))],
              os.path.join(_fresh(), "cn.json"))
    text, blocked = _run("precheck", p, "--offline")
    check(not blocked, "precheck 不该拦，只报告")
    check("ControlNet" in text, f"没识别出 ControlNet：\n{text}")


@suite("只靠 CONTROL_NET 端口类型也能认出来（节点名里没有 controlnet）")
def _cn_by_port():
    p = _save([_node(1, "SomeFancyPreprocessor",
                     outs=(("out", "CONTROL_NET"),))],
              os.path.join(_fresh(), "cn2.json"))
    text, _ = _run("precheck", p, "--offline")
    check("ControlNet" in text, f"端口类型没被当线索：\n{text}")


@suite("含视频节点的图会被标出来")
def _vid_detect():
    p = _save([_node(1, "WanVideoModelLoader", outs=(("model", "MODEL"),)),
               _node(2, "SaveVideo", ins=(("video", "VIDEO"),))],
              os.path.join(_fresh(), "vd.json"))
    text, _ = _run("precheck", p, "--offline")
    check("视频" in text, f"没识别出视频：\n{text}")


@suite("纯图片工作流放行：没触红线")
def _clean_pass():
    p = _save([_node(1, "CheckpointLoaderSimple", outs=(("MODEL", "MODEL"),)),
               _node(2, "KSampler", ins=(("model", "MODEL"),),
                     outs=(("LATENT", "LATENT"),))],
              os.path.join(_fresh(), "ok.json"))
    text, _ = _run("precheck", p, "--offline")
    check("没触到红线" in text, f"纯图片图被误判了：\n{text}")


@suite("大画布会单独提醒（超阈值就提示，阈值可用 CWF_MAX_MP 调）")
def _big_canvas():
    n = _node(1, "EmptyLatentImage", widgets=[1920, 1080, 1])
    n._wnames = ["width", "height", "batch_size"]
    p = _save([n], os.path.join(_fresh(), "big.json"))
    text, _ = _run("precheck", p, "--offline")
    check("MP" in text, f"大画布没提醒：\n{text}")


# ---------------------------------------------------------------- 红线拦截


suite.group("执行护栏：拦截")


@suite("run 默认**拒绝**提交含 ControlNet 的图，并告诉你放行开关")
def _cn_blocked():
    p = _save([_node(1, "ControlNetLoader"),
               _node(2, "KSampler",
                     ins=(("model", "MODEL"),))],
              os.path.join(_fresh(), "cn.json"))
    text, blocked = _run("run", p, "--offline")
    check(blocked, "含 ControlNet 却照样提交了 —— 这正是要防的")
    check("ControlNet" in text, f"没说明原因：\n{text}")
    check("allow-controlnet" in text, f"没给出放行办法：\n{text}")


@suite("run 默认**拒绝**提交视频工作流")
def _vid_blocked():
    p = _save([_node(1, "WanVideoModelLoader", outs=(("model", "MODEL"),)),
               _node(2, "KSampler", ins=(("model", "MODEL"),))],
              os.path.join(_fresh(), "vd.json"))
    text, blocked = _run("run", p, "--offline")
    check(blocked, "视频图却照样提交了")
    check("allow-video" in text, f"没给出放行办法：\n{text}")


@suite("放行之后就不再卡在红线上（用假服务器证明它走到了提交那一步）")
def _allow_passes_guard():
    """把服务器指到一个没人监听的端口：连接必然失败。
    能走到"连接失败"，就说明红线放行了、校验也过了。"""
    p = _save([_node(1, "ControlNetLoader"),
               _node(2, "KSampler", ins=(("model", "MODEL"),))],
              os.path.join(_fresh(), "cn.json"))
    text, blocked = _run("run", p, "--offline", "--allow-controlnet",
                         "--force", "--server", "http://127.0.0.1:1",
                         "--timeout", "1")
    check("拒绝提交" not in text, f"放行了还是被拦：\n{text}")
    check(blocked, "假服务器应该连不上并退出")


# ---------------------------------------------------------------- 消重叠


suite.group("消重叠硬保证")


@suite("把四个节点叠在同一坐标，交出来必须是 0 重叠")
def _overlap_fixed():
    d = _fresh()
    ns = [_node(i, "KSampler", pos=(0, 0), size=(320, 260)) for i in range(1, 5)]
    src = _save(ns, os.path.join(d, "ov.json"))
    out = os.path.join(d, "ov_fixed.json")
    text, _ = _run("place", src, "#1=0,0", "#2=0,0", "#3=0,0", "#4=0,0",
                   "--out", out)
    check("节点重叠 0 处" in text, f"没有给出 0 重叠的结论：\n{text}")
    g = Graph.load(out)
    for i, a in enumerate(g.nodes):
        for b in g.nodes[i + 1:]:
            ox = min(a.right, b.right) - max(a.pos[0], b.pos[0])
            oy = min(a.bottom, b.bottom) - max(a.pos[1], b.pos[1])
            check(ox <= 0 or oy <= 0,
                  f"#{a.id} 与 #{b.id} 还叠在一起：{a.pos} / {b.pos}")


@suite("本来就没重叠时不会乱动（不能为了消重叠把好版式推散）")
def _no_gratuitous_move():
    d = _fresh()
    ns = [_node(1, "A", pos=(0, 0), size=(200, 100)),
          _node(2, "B", pos=(500, 0), size=(200, 100)),
          _node(3, "C", pos=(1000, 0), size=(200, 100))]
    src = _save(ns, os.path.join(d, "fine.json"))
    out = os.path.join(d, "fine2.json")
    _run("place", src, "--out", out)
    g = Graph.load(out)
    eq(sorted(round(n.pos[0]) for n in g.nodes), [0, 500, 1000],
       "没重叠却把节点挪了")


@suite("--no-fix-overlap 时才允许留着重叠")
def _opt_out():
    d = _fresh()
    ns = [_node(i, "KSampler", pos=(0, 0), size=(320, 260)) for i in range(1, 4)]
    src = _save(ns, os.path.join(d, "ov2.json"))
    out = os.path.join(d, "ov2b.json")
    _run("place", src, "#1=0,0", "#2=0,0", "--no-fix-overlap", "--out", out)
    g = Graph.load(out)
    overlap = False
    pos = sorted(tuple(n.pos) for n in g.nodes)
    check(len(set(pos)) == 1, f"显式关掉了却还是被推开：{pos}")


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
