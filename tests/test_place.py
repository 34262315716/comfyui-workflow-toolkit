# -*- coding: utf-8 -*-
"""
测试组 6：手动摆位置（cwf place）

每一条都对应一个**真实踩到的坑**：

  * `--out` 不生效、结果直接写回输入文件（差点毁掉源文件）
  * 压紧时按「本列当前最右边」分列会滚雪球 —— 33 个节点被压成 390×4630 的**一列**
  * 无条件把坐标归一化到原点，会悄悄改掉明确指定的绝对坐标
  * 挪位置是纯外观操作：**接线和控件值一个都不能变**
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile

from harness import Suite, check, eq, skip

from cwf import cli
from cwf.lib.graph import Graph, Link, Node, Slot

suite = Suite()
TMP = os.path.join(tempfile.gettempdir(), "cwf_place_test")


def _node(nid, type_, title=None, pos=(0, 0), size=(300, 140), ins=(), outs=(),
          widgets=None) -> Node:
    n = Node(id=nid, type=type_, title=title, pos=pos, size=size)
    n.inputs = [Slot(name=a, type=b) for a, b in ins]
    n.outputs = [Slot(name=a, type=b) for a, b in outs]
    if widgets is not None:
        n.widgets = widgets
        n._wform = "list"
    return n


def _make(path: str) -> str:
    """造一张 4 节点的小图：加载 → 两个采样（同名类型，靠标题区分）→ 保存。"""
    ns = [
        _node(1, "CheckpointLoaderSimple", "主模型", (0, 0), (320, 100),
              outs=(("MODEL", "MODEL"), ("CLIP", "CLIP"), ("VAE", "VAE"))),
        _node(2, "KSampler", "采样甲", (500, 0), (320, 260),
              ins=(("model", "MODEL"),), outs=(("LATENT", "LATENT"),),
              widgets=[1, "fixed", 20, 8.0, "euler", "normal", 1.0]),
        _node(3, "KSampler", "采样乙", (500, 400), (320, 260),
              ins=(("model", "MODEL"),), outs=(("LATENT", "LATENT"),),
              widgets=[2, "fixed", 8, 1.0, "euler", "simple", 1.0]),
        _node(4, "VAEDecode", "解码", (1000, 0), (220, 90),
              ins=(("samples", "LATENT"),), outs=(("IMAGE", "IMAGE"),)),
        _node(5, "SaveImage", "存图", (1400, 0), (320, 300),
              ins=(("images", "IMAGE"),), widgets=["CWF/t"]),
    ]
    ls = [Link(id=1, origin_id=1, origin_slot=0, target_id=2, target_slot=0,
               type="MODEL"),
          Link(id=2, origin_id=2, origin_slot=0, target_id=4, target_slot=0,
               type="LATENT"),
          Link(id=3, origin_id=4, origin_slot=0, target_id=5, target_slot=0,
               type="IMAGE")]
    Graph(ns, ls, []).save(path)
    return path


def _fresh(name: str = "w.json") -> str:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    return _make(os.path.join(TMP, name))


def _run(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    eq(rc, 0, f"命令失败：{argv}")
    return buf.getvalue()


def _snapshot(path: str):
    g = Graph.load(path)
    return ({(n.id, s.name) for n in g.nodes for s in n.inputs if s.link is not None},
            [tuple(l.to_json()[:5]) for l in g.links],
            {n.id: list(n.widget_pairs()) for n in g.nodes})


# ---------------------------------------------------------------- 列计划


suite.group("列计划")


@suite("一条 --col 就是一列：从左到右排开，列内从上到下堆")
def _cols():
    src = _fresh()
    out = os.path.join(TMP, "cols.json")
    _run("place", src, "--col", "主模型 解码", "--col", "采样甲 采样乙",
         "--col", "存图", "--out", out)
    g = Graph.load(out)
    ck, va = g.find("主模型"), g.find("解码")
    k1, k2 = g.find("采样甲"), g.find("采样乙")
    save = g.find("存图")
    check(ck.pos[0] < k1.pos[0] < save.pos[0], "列没有从左到右排开")
    check(va.pos[0] < k1.pos[0], "同列节点该在同一列")
    check(k2.pos[1] > k1.pos[1], "同列节点该从上到下堆")



@suite("列计划里写错名字会明确报错，而不是静默少放一个")
def _cols_bad():
    src = _fresh()
    try:
        _run("place", src, "--col", "主模型 不存在的节点", "--out",
             os.path.join(TMP, "x.json"))
        check(False, "写错名字却没报错")
    except SystemExit:
        pass


@suite("--plan 文件里 # 开头的行是注释，col 前缀可写可不写")
def _plan_file():
    src = _fresh()
    plan = os.path.join(TMP, "plan.txt")
    with open(plan, "w", encoding="utf-8") as f:
        f.write("# 这是注释\ncol 主模型 解码\n\n采样甲\n采样乙 存图\n")
    out = os.path.join(TMP, "plan.json")
    _run("place", src, "--plan", plan, "--out", out)
    g = Graph.load(out)
    check(g.find("主模型").pos[0] < g.find("采样甲").pos[0],
          "注释行没被跳过，列顺序乱了")


# ---------------------------------------------------------------- 绝对 / 相对


suite.group("绝对与相对坐标")


@suite("`#id=X,Y` 就是最终坐标，不会被悄悄归一化挪走")
def _absolute():
    src = _fresh()
    out = os.path.join(TMP, "abs.json")
    _run("place", src, "#4=2000,50", "--out", out)
    g = Graph.load(out)
    eq(tuple(g.find("解码").pos), (2000.0, 50.0),
       "明确指定的绝对坐标被归一化改掉了")


@suite("`+=` 是相对位移，基于当前坐标")
def _relative():
    src = _fresh()
    before = Graph.load(src).find("采样甲").pos
    out = os.path.join(TMP, "rel.json")
    _run("place", src, "#2+=100,-40", "--out", out)
    after = Graph.load(out).find("采样甲").pos
    eq(tuple(after), (before[0] + 100, before[1] - 40))


@suite("坐标不是数字时给出人话报错")
def _bad_number():
    src = _fresh()
    try:
        _run("place", src, "#4=一千,50", "--out", os.path.join(TMP, "y.json"))
        check(False, "写了非数字却没报错")
    except SystemExit:
        pass


@suite("小幅负坐标原样保留；整图飞到很远的负半轴才拉回来")
def _negative():
    """踩过：无条件归一化会让 `#2+=0,-40` 变成"它没动、别人往下走"。"""
    src = _fresh()
    out = os.path.join(TMP, "neg.json")
    # 挑一个不会跟别人撞上的位置：消重叠的硬保证会把压在一起的节点推开，
    # 那是正确行为，但会掩盖"负坐标有没有被归一化"这件事。
    _run("place", src, "#4=1000,-40", "--out", out)
    eq(tuple(Graph.load(out).find("解码").pos), (1000.0, -40.0),
       "小幅负坐标被强行归一化了")

    out2 = os.path.join(TMP, "neg2.json")
    _run("place", src, "#4=-3000,-2000", "--out", out2)
    g2 = Graph.load(out2)
    eq(min(n.pos[0] for n in g2.nodes), 0.0, "整图飞到负半轴却没拉回来")


# ---------------------------------------------------------------- 落盘安全


suite.group("落盘安全")


@suite("--out 真的写去 --out，输入文件一个字节都不动（实测踩过）")
def _out_not_input():
    src = _fresh()
    before = open(src, "rb").read()
    out = os.path.join(TMP, "written.json")
    _run("place", src, "--col", "主模型", "--col", "采样甲", "--out", out)
    check(os.path.exists(out), "--out 没写出文件")
    eq(open(src, "rb").read(), before, "输入文件被改了 —— --out 形同虚设")


@suite("没给 --out / --in-place 就绝不落盘")
def _no_write():
    src = _fresh()
    before = open(src, "rb").read()
    text = _run("place", src, "--col", "主模型", "--col", "采样甲")
    eq(open(src, "rb").read(), before, "没让写却写了")
    check("没有写文件" in text, f"没提示未写文件：{text[-120:]}")


@suite("--in-place 会先备份")
def _inplace_backup():
    src = _fresh()
    _run("place", src, "#4=100,100", "--in-place")
    baks = [f for f in os.listdir(TMP) if ".bak-" in f]
    eq(len(baks), 1, f"没有备份：{os.listdir(TMP)}")
    eq(tuple(Graph.load(src).find("解码").pos), (100.0, 100.0))


# ---------------------------------------------------------------- 压紧


suite.group("压紧与折列")


@suite("--pack 不会把整张图压成一列（滚雪球 bug 的回归）")
def _pack_columns():
    """原来比的是「本列当前最右边」，列一边装一边变宽，把右边全吞进来。"""
    src = _fresh()
    out = os.path.join(TMP, "pack.json")
    text = _run("place", src, "--pack", "--gap-h", "60", "--gap-v", "24",
                "--out", out)
    g = Graph.load(out)
    xs = sorted({round(n.pos[0]) for n in g.nodes})
    check(len(xs) >= 3, f"5 个节点被压成了 {len(xs)} 列：{xs}")
    w = max(n.right for n in g.nodes)
    h = max(n.bottom for n in g.nodes)
    check(w > h * 0.5, f"版式太畸形：{w:.0f}×{h:.0f}")


@suite("--pack 之后没有节点重叠")
def _pack_no_overlap():
    src = _fresh()
    out = os.path.join(TMP, "pack2.json")
    _run("place", src, "--pack", "--out", out)
    g = Graph.load(out)
    ns = g.nodes
    for i in range(len(ns)):
        for j in range(i + 1, len(ns)):
            a, b = ns[i], ns[j]
            ox = min(a.right, b.right) - max(a.pos[0], b.pos[0])
            oy = min(a.bottom, b.bottom) - max(a.pos[1], b.pos[1])
            check(ox <= 0 or oy <= 0, f"#{a.id} 和 #{b.id} 重叠了")


@suite("--fold N 会把列数折到大概 1/N")
def _fold():
    src = _fresh()
    base = os.path.join(TMP, "f1.json")
    f2 = os.path.join(TMP, "f2.json")
    _run("place", src, "--pack", "--out", base)
    _run("place", src, "--pack", "--fold", "2", "--out", f2)
    n1 = len({round(n.pos[0]) for n in Graph.load(base).nodes})
    n2 = len({round(n.pos[0]) for n in Graph.load(f2).nodes})
    check(n2 <= n1, f"折了反而列更多：{n1} → {n2}")


# ---------------------------------------------------------------- 只动位置


suite.group("只动位置，别的都不许变")


@suite("挪位置前后：接线、控件值、to_api() 全部一致")
def _only_position():
    src = _fresh()
    g0 = Graph.load(src)
    before = _snapshot(src)
    api_before = json.dumps(g0.to_api(), sort_keys=True, default=str)

    out = os.path.join(TMP, "only.json")
    _run("place", src, "--pack", "--fold", "2", "--out", out)

    after = _snapshot(out)
    eq(after[0], before[0], "输入端的接线变了")
    eq(after[1], before[1], "连线列表变了")
    eq(after[2], before[2], "控件值变了")
    api_after = json.dumps(Graph.load(out).to_api(), sort_keys=True, default=str)
    eq(api_after, api_before, "to_api() 变了 —— 挪位置绝不该影响执行语义")


@suite("挪完之后分区框跟着重算（不然旧框会套错地方）")
def _regroup():
    src = _fresh()
    out = os.path.join(TMP, "grp.json")
    _run("place", src, "--col", "主模型", "--col", "采样甲 采样乙",
         "--col", "解码 存图", "--out", out)
    g = Graph.load(out)
    if not g.groups:
        skip("这张小图没触发分区框")
    # 每个分区框里至少得有一个节点落在框内
    for gp in g.groups:
        gx, gy, gw, gh = gp.bounding
        inside = [n for n in g.nodes
                  if gx <= n.pos[0] + n.width / 2 <= gx + gw
                  and gy <= n.pos[1] + n.height / 2 <= gy + gh]
        check(inside, f"分区框「{gp.title}」里一个节点都没有")


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
