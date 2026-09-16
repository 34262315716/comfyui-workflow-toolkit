# -*- coding: utf-8 -*-
"""
测试组 1：图模型 —— 读进来、写出去、不许丢东西

核心断言（这是全工具最重要的一条不变量）：
    **排版只许动 pos/size/groups，绝不许动执行语义。**
判据：to_api()（就是提交给 ComfyUI 的那份）前后必须逐字节相同。
"""
from __future__ import annotations

import json
import os
from typing import Dict, List
import copy

from harness import Suite, check, eq, sample_paths, all_workflows, skip, Skipped

from cwf.lib.graph import Graph, Node, Link, Slot, is_frontend_only, is_note_type
from cwf.lib import strata
from cwf.lib.schema import registry

suite = Suite()


# ---------------------------------------------------------------- 加载


@suite.group("加载：全库不崩")
def _load_all():
    files = all_workflows()
    if not files:
        skip("工作流库为空")
    bad = []
    n_nodes = n_links = 0
    for p in files:
        try:
            g = Graph.load(p)
            n_nodes += len(g)
            n_links += len(g.links)
        except Exception as e:
            bad.append((os.path.basename(p), f"{type(e).__name__}: {e}"))
    check(not bad, f"{len(bad)} 个文件读失败，例如 {bad[:3]}")
    check(n_nodes > 0 and n_links > 0, "读出来是空的")


suite.case("435 张真实工作流全部能读入，节点/连线数非零")(_load_all)


# ---------------------------------------------------------------- 往返


@suite.group("往返：load → dump → load 不丢信息")
def _roundtrip_ui():
    """UI 格式往返：第二次序列化必须与第一次完全一致（幂等）。"""
    bad = []
    for p in sample_paths() or all_workflows(20):
        g1 = Graph.load(p)
        s1 = json.dumps(g1.to_ui(), ensure_ascii=False, sort_keys=True)
        g2 = Graph.loads(s1)
        s2 = json.dumps(g2.to_ui(), ensure_ascii=False, sort_keys=True)
        if s1 != s2:
            bad.append(os.path.basename(p))
    check(not bad, f"往返不幂等：{bad}")


suite.case("UI 往返幂等（序列化两次结果一致）")(_roundtrip_ui)


def _roundtrip_preserves_counts():
    for p in sample_paths() or all_workflows(10):
        g1 = Graph.load(p)
        g2 = Graph.loads(json.dumps(g1.to_ui(), ensure_ascii=False))
        eq(len(g2), len(g1), f"{os.path.basename(p)} 节点数变了")
        eq(len(g2.links), len(g1.links), f"{os.path.basename(p)} 连线数变了")
        # 每组 (源,槽,目标,槽) 必须一模一样
        a = sorted((l.origin_id, l.origin_slot, l.target_id, l.target_slot) for l in g1.links)
        b = sorted((l.origin_id, l.origin_slot, l.target_id, l.target_slot) for l in g2.links)
        eq(b, a, f"{os.path.basename(p)} 拓扑变了")
        # 控件值逐一相同
        for n1 in g1.nodes:
            n2 = g2.node(n1.id)
            eq(n2.type, n1.type, f"节点 #{n1.id} 类型变了")
            eq(dict(n2.widget_pairs()), dict(n1.widget_pairs()),
               f"节点 #{n1.id} 控件值变了")


suite.case("往返后节点/连线/拓扑/控件值逐项相同")(_roundtrip_preserves_counts)


# ---------------------------------------------------------------- 最强不变量


@suite.group("不变量：排版不改执行语义")
def _layout_preserves_api():
    """排版前后 to_api() 必须完全一致 —— 这是「乱排版不会搞坏图」的硬保证。"""
    reg = registry(quiet=True)
    paths = sample_paths() or all_workflows(8)
    bad = []
    for p in paths:
        g = Graph.load(p)
        before = json.dumps(g.to_api(), ensure_ascii=False, sort_keys=True)
        strata.layout(g, strata.LayoutOptions(), reg)
        after = json.dumps(g.to_api(), ensure_ascii=False, sort_keys=True)
        if before != after:
            # 定位到底差在哪，方便排查
            a, b = json.loads(before), json.loads(after)
            diff = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
            bad.append(f"{os.path.basename(p)}: 差异节点 {diff[:5]}")
    check(not bad, "排版改变了执行语义：" + "；".join(bad))


suite.case("排版后 to_api() 逐字节不变")(_layout_preserves_api)


def _layout_preserves_ui_content():
    """UI 侧：除 pos/size/groups/order 外，其余字段必须原样保留。"""
    reg = registry(quiet=True)
    IGNORE = {"pos", "size", "order", "groups"}
    for p in sample_paths() or all_workflows(8):
        g = Graph.load(p)
        before = g.to_ui()
        strata.layout(g, strata.LayoutOptions(), reg)
        after = g.to_ui()
        eq(after["last_node_id"], before["last_node_id"], "last_node_id 被改了")
        for nd_a, nd_b in zip(before["nodes"], after["nodes"]):
            eq(nd_b["id"], nd_a["id"], "节点顺序/身份被改了")
            eq(nd_b["type"], nd_a["type"], f"#{nd_a['id']} 类型被改了")
            eq(nd_b.get("title"), nd_a.get("title"), f"#{nd_a['id']} 标题被改了")
            eq(nd_b.get("mode", 0), nd_a.get("mode", 0), f"#{nd_a['id']} mode 被改了")
            eq(nd_b.get("widgets_values"), nd_a.get("widgets_values"),
               f"#{nd_a['id']} 控件值被改了")
            eq(nd_b.get("properties"), nd_a.get("properties"),
               f"#{nd_a['id']} properties 被改了")
            eq(nd_b.get("inputs"), nd_a.get("inputs"), f"#{nd_a['id']} 输入槽被改了")
            eq(nd_b.get("outputs"), nd_a.get("outputs"), f"#{nd_a['id']} 输出槽被改了")
        eq(after["links"], before["links"], "连线表被改了")


suite.case("排版后除 pos/size/groups/order 外字段全等")(_layout_preserves_ui_content)


def _order_is_topological():
    """`reorder()` 之后 order 必须拓扑有序 —— **但有环的图不可能有全序**。

    注意测的是哪个函数：`strata.layout()` **只动坐标**，它不碰 order；
    刷 order 的是 `Graph.reorder()`，由 `cwf beautify` 调用。
    最初这里只跑了 layout 就断言 order 有序，等于在测一个根本没人做的承诺。


    排版算法会拆环（把回边从分层里拿掉），拆出来的回边天然违反
    「上游 order < 下游」。所以判据不能是"一条都不许违反"，而必须是：

        每一条违反的边，都要能**自证是回边** ——
        即从它的目标出发沿有向边能走回它的源。
        走不回去，说明那不是环，就是我们重编号时真的排错了。

    原先写成"一条都不许违反"，结果在一张有环的真实工作流上假红。
    """
    reg = registry(quiet=True)
    for p in sample_paths() or all_workflows(6):
        g = Graph.load(p)
        strata.layout(g, strata.LayoutOptions(), reg)
        g.reorder()                     # 这才是刷 order 的那一步（beautify 会调）
        order = {n.id: (n.order if n.order is not None else 0) for n in g.nodes}

        adj: Dict[int, List[int]] = {}
        for l in g.links:
            adj.setdefault(l.origin_id, []).append(l.target_id)

        def reaches(src: int, dst: int) -> bool:
            """从 src 沿有向边能不能走到 dst（就是"这条边在不在环上"）。"""
            seen, stack = set(), [src]
            while stack:
                cur = stack.pop()
                if cur == dst:
                    return True
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(adj.get(cur, ()))
            return False

        bad = []
        for l in g.links:
            if l.origin_id not in order or l.target_id not in order:
                continue
            if order[l.origin_id] < order[l.target_id]:
                continue
            # 违反顺序了：必须是回边才说得过去
            if not reaches(l.target_id, l.origin_id):
                bad.append("#%d(ord %d) → #%d(ord %d) 不构成环，却逆序"
                           % (l.origin_id, order[l.origin_id],
                              l.target_id, order[l.target_id]))
        check(not bad, f"{os.path.basename(p)}: " + "；".join(bad[:4]))


suite.case("order 拓扑有序：逆序的边必须自证是回边（有环图没有全序）")(_order_is_topological)


# ---------------------------------------------------------------- 陈旧 links 自愈


@suite.group("健壮性：真实脏数据")
def _stale_links_are_healed():
    """真实工作流里顶层 links 数组常有假数据（origin_slot 几百）。
    解析后每个连线都必须与节点侧一致：源节点真有这个输出槽。"""
    reg = registry(quiet=True)
    checked = 0
    for p in all_workflows():
        g = Graph.load(p)
        for l in g.links:
            o = g.maybe(l.origin_id)
            t = g.maybe(l.target_id)
            check(o is not None, f"{os.path.basename(p)}: 连线 {l.id} 源节点不存在")
            check(t is not None, f"{os.path.basename(p)}: 连线 {l.id} 目标节点不存在")
            check(0 <= l.origin_slot < len(o.outputs),
                  f"{os.path.basename(p)}: 连线 {l.id} 的源槽 {l.origin_slot} "
                  f"超出 #{o.id}({o.type}) 的 {len(o.outputs)} 个输出")
            check(0 <= l.target_slot < len(t.inputs),
                  f"{os.path.basename(p)}: 连线 {l.id} 的目标槽 {l.target_slot} "
                  f"超出 #{t.id}({t.type}) 的 {len(t.inputs)} 个输入")
            checked += 1
    check(checked > 500, f"只检查了 {checked} 条连线，样本太少")
    print(f"        （校验了 {checked} 条连线，全部槽位合法）")


suite.case("全库连线槽位全部合法（顶层 links 脏数据已被自愈）")(_stale_links_are_healed)


def _widget_dict_forms():
    """两种 widgets_values 形态（位置数组 / 按名字典）都要能按名取值。"""
    reg = registry(quiet=True)
    from cwf.lib.graph import bind_registry
    bind_registry(reg)
    n_dict = n_list = 0
    for p in all_workflows():
        g = Graph.load(p)
        for n in g.nodes:
            pairs = list(n.widget_pairs())
            if n._wform == "dict":
                n_dict += 1
                eq([k for k, _ in pairs], list(n.widgets.keys()),
                   f"#{n.id} 字典形态控件名错位")
            elif n._wform == "list":
                n_list += 1
    check(n_dict > 0, "样本里没有字典形态的 widgets_values，测试没覆盖到")
    print(f"        （覆盖 {n_list} 个数组形态 + {n_dict} 个字典形态节点）")


suite.case("两种 widgets_values 形态都能按名读到值")(_widget_dict_forms)


def _set_widget_only_touches_target():
    """改一个控件，只许动那一个位置。"""
    reg = registry(quiet=True)
    from cwf.lib.graph import bind_registry
    bind_registry(reg)
    p = sample_paths()[0]
    g = Graph.load(p)
    ksampler = next((n for n in g.nodes if n.type == "KSampler"), None)
    if ksampler is None:
        skip("样本里没有 KSampler")
    before = list(ksampler.widgets)
    ksampler.set_widget("steps", 9999)
    after = list(ksampler.widgets)
    eq(len(after), len(before), "控件数量变了")
    diff = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    eq(diff, [before.index(before[diff[0]])] if diff else [], "")
    eq(len(diff), 1, f"改一个控件却动了 {len(diff)} 处")
    eq(after[diff[0]], 9999, "值没写进去")
    names = [k for k, _ in ksampler.widget_pairs()]
    eq(names[diff[0]], "steps", "改错了控件（名字-位置映射不对）")


suite.case("set_widget 只改动目标位置，且名字映射正确")(_set_widget_only_touches_target)


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
