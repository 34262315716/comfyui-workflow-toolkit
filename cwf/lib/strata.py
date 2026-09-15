# -*- coding: utf-8 -*-
"""
cwf.strata —— 工作流自动排版引擎

目标：把一坨挤在一起的节点图，重排成 Graphviz 级别的清晰版式。

流程（经典 Sugiyama 分层法 + 若干针对 ComfyUI 的特调）：
  1. 规整化   去掉旁路/静音节点的干扰，把 SetNode/GetNode 虚拟跳线接回真实数据流，
              把 Reroute 收缩成点（排版后再插回线上）；
  2. 拆环     深度优先找反馈边，强制反向以便分层；
  3. 分层     最长路径分层 + 收紧（每个节点尽量往左靠）；
  4. 层内排序 中位数 / 重心启发式，多轮扫描取交叉数最少的结果；
  5. 坐标分配 长边插虚节点，两趟中位数对齐再取平均，长链会被拉直；
  6. 落位     吸附网格、算分区框、把注释/孤立节点单独摆到副区。

针对 ComfyUI 的特调：
  * ComfyUI 是左进右出的数据流，所以主方向必须是「从左到右」，层 = 列；
  * 节点的真实像素高度由控件数量决定，排版用的是实测/推算尺寸而非固定值；
  * 注释类节点（孤海注释那种巨型文本框）不参与数据流，统一甩到顶部横幅；
  * GetNode 必须排到它对应 SetNode 的右边，否则画布上会出现一堆「向左倒流」的线。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .graph import (Graph, Group, Link, Node, Slot, CwfError,
                    VIRTUAL_GET, VIRTUAL_SET, REROUTE)
from .schema import Registry, size_of

# ---------------------------------------------------------------- 参数


@dataclass
class LayoutOptions:
    direction: str = "LR"          # LR（默认，符合 ComfyUI 习惯）| TB
    h_gap: float = 90.0            # 列间距
    v_gap: float = 45.0            # 同列节点间距
    node_min_gap: float = 24.0     # 保底间距（防止预估尺寸偏差导致重叠）
    group_pad: float = 34.0        # 分区框内边距
    group_gap: float = 26.0        # 分区框之间的额外留白
    grid: float = 8.0              # 坐标吸附网格
    margin: float = 80.0           # 画布外边距
    band_gap: float = 140.0        # 副区（隔离节点/注释）与主图间距
    allow_groups: bool = True      # 是否重算分区框
    strays: str = "auto"           # auto | bottom | right | keep
    notes: str = "top"             # top | keep
    max_order_sweeps: int = 12
    weight_ports: bool = False     # 排序时是否按端口顺序加权
    # 版式的目标宽高比。3.2 会排成又细又长的横条（实测中位宽高比 2.35、
    # p90 4.37、最宽 70140 px），看着"拉得老长"；收到 2.2 后同一条链
    # 会自动折行，长边明显变短。设成 1.0 就是尽量排成正方形。
    target_ratio: float = 2.2

    @staticmethod
    def from_flags(compact: bool = False, loose: bool = False, **kw) -> "LayoutOptions":
        o = LayoutOptions(**kw)
        if compact:
            o.h_gap, o.v_gap, o.group_pad, o.band_gap = 60.0, 30.0, 22.0, 90.0
        if loose:
            o.h_gap, o.v_gap, o.group_pad, o.band_gap = 150.0, 70.0, 46.0, 190.0
        return o


# ---------------------------------------------------------------- 语义分区


CLUSTERS: List[Tuple[str, Tuple[str, ...], List[str]]] = [
    ("载入区", ("#8A8", "#8A9A5B"), [
        "checkpointloader", "unetloader", "vaeloader", "cliploader", "loraloader",
        "loadimage", "loadvideo", "loadaudio", "loadmask", "loadlatent",
        "loader", "load", "downloadandload", "pulidloader",
        "textencoderloader", "dualcliploader", "triplecliploader",
        "loaddiffusionmodel", "loadersd", "modelsampling", "imagetoVideo",
        "imagetovideo", "videotoimage",
    ]),
    ("提示词区", ("#3f789e", "#4A6C8C"), [
        "cliptextencode", "textencode", "prompt", "conditioning", "txt", "text",
        "style", "concat", "string", "wildcard", "translate",
    ]),
    ("采样区", ("#c9a227", "#a67722"), [
        "ksampler", "sampler", "guider", "sigmas", "scheduler", "noise", "cfg",
        "latent", "step", "denoise", "customsampling", "basicguider",
        "samplercustomadvanced", "randomnoise", "blockswap", "cache",
    ]),
    ("解码区", ("#a1309b", "#8a2be2"), [
        "vaedecode", "vaeencode", "decode", "encode", "vae", "tiled", "audio",
    ]),
    ("输出区", ("#e0e0e0", "#89b"), [
        "saveimage", "previewimage", "savevideo", "saveaudio", "saveanimated",
        "preview", "save", "imagegrid", "vhs_videocombine", "combine", "export",
    ]),
    ("放大修复区", ("#b58b00", "#7a6a2a"), [
        "upscale", "resize", "scale", "hires", "refine", "detail", "face",
        "restore", "gfpgan", "codeformer", "adetailer", "segm", "supir",
        "seedvr", "hypir", "tile", "interpolat", "frameinterpolat", "dlss",
    ]),
    ("辅助区", ("#666", "#555"), [
        "note", "primitive", "reroute", "setnode", "getnode", "switch", "any",
        "math", "int", "float", "string", "显示", "text", "preview",
        "attention", "vram", "memory", "purge", "clean", "offload", "patch",
        "bypass", "忽略", "sigmas", "resolution", "selector", "debug",
    ]),
]

#: 一眼看上去就是「接线/常量/开关」的辅助件，不该抢占主链的位置
UTILITY_HINTS = ("rgthree", "setnode", "getnode", "switch", "primitive", "reroute",
                 "note", "display", "show", "text", "any ", "int", "float",
                 "bool", "math", "concat", "value", "debug")


def cluster_of(node: Node) -> str:
    """给节点归类：先看节点类别/包名，再看类型名关键词。"""
    t = (node.type or "").lower()
    aux = str(node.properties.get("aux_id") or "").lower()
    cat = str(node.properties.get("category") or "").lower()
    blob = f"{t} {aux} {cat}"
    for base in ("note", "注释", "comment"):
        if base in t:
            return "注释"
    if node.type in VIRTUAL_SET or node.type in VIRTUAL_GET:
        return "跳线区"
    for name, _colors, keys in CLUSTERS:
        for k in keys:
            if k in blob:
                return name
    return "其他"


# ---------------------------------------------------------------- 中间结构


@dataclass
class V:
    """排版用的顶点：真实节点或长边上的虚节点。"""
    key: str
    node: Optional[Node]
    w: float
    h: float
    layer: int = 0
    order: int = 0
    x: float = 0.0
    y: float = 0.0
    dummy: bool = False
    chain: Optional[int] = None      # 虚节点所属的长边编号

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class E:
    a: str
    b: str
    key: str
    w: float = 1.0
    segs: List[int] = field(default_factory=list)   # 虚节点 chain id
    reversed_flag: bool = False


@dataclass
class LayoutReport:
    nodes: int = 0
    layers: int = 0
    crossings_before: int = 0
    crossings: int = 0
    width: float = 0.0
    height: float = 0.0
    area_per_node: float = 0.0
    groups: int = 0
    strays: int = 0
    notes: int = 0
    contractions: int = 0
    overlaps_fixed: int = 0
    issues: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "nodes": self.nodes, "layers": self.layers,
            "crossings": self.crossings, "crossings_before": self.crossings_before,
            "width": round(self.width), "height": round(self.height),
            "area_per_node": round(self.area_per_node),
            "groups": self.groups, "strays": self.strays, "notes": self.notes,
            "contractions": self.contractions, "issues": self.issues,
        }


# ---------------------------------------------------------------- 引擎


def _vkey(k: str):
    """顶点排序键：真实节点按 id 数字排，虚节点（长边中段）排后面。"""
    return (0, "%09d" % int(k)) if k.isdigit() else (1, k)


def _count_inversions(seq: List[int]) -> int:
    """数逆序对（归并计数，O(n log n)）。交叉数就是这么算的。"""
    n = len(seq)
    if n < 2:
        return 0
    buf = [0] * n
    counter = [0]

    def rec(lo: int, hi: int) -> None:
        if hi - lo <= 1:
            return
        mid = (lo + hi) // 2
        rec(lo, mid)
        rec(mid, hi)
        i, j, k = lo, mid, lo
        while i < mid and j < hi:
            if seq[i] <= seq[j]:
                buf[k] = seq[i]
                i += 1
            else:
                buf[k] = seq[j]
                j += 1
                counter[0] += mid - i
            k += 1
        while i < mid:
            buf[k] = seq[i]
            i += 1
            k += 1
        while j < hi:
            buf[k] = seq[j]
            j += 1
            k += 1
        seq[lo:hi] = buf[lo:hi]

    rec(0, n)
    return counter[0]


class Layouter:
    def __init__(self, graph: Graph, opts: Optional[LayoutOptions] = None,
                 reg: Optional[Registry] = None):
        self.g = graph
        self.opt = opts or LayoutOptions()
        self.reg = reg
        self.verts: Dict[str, V] = {}
        self.edges: List[E] = []
        self.layers: List[List[str]] = []
        self.contract: Dict[int, int] = {}      # reroute id -> owner real id
        self.report = LayoutReport()
        self._chain_seq = 0

    # ------------------------------------------------ 主入口
    def run(self) -> LayoutReport:
        g, opt = self.g, self.opt
        if not g.nodes:
            return self.report
        notes = [n for n in g.nodes if n.is_note]
        pinned = [n for n in notes if opt.notes == "keep"]
        ribbon = [n for n in notes if opt.notes != "keep"]
        movers = [n for n in g.nodes if n not in notes]

        # 1. 尺寸
        sizes = {n.id: size_of(n, self.reg) for n in movers}

        # 2. 收缩无意义节点（Reroute / 旁路 / 静音）
        keep = self._select_nodes(movers)
        for n in movers:
            if n.id not in keep:
                self.contract[n.id] = -1

        # 3. 建图（含虚拟跳线解析）
        self._build_graph(g, keep, sizes)

        # 4. 拆环
        back = self._find_back_edges()
        for e in back:
            self.edges.append(E(e.b, e.a, e.key + "@rev", e.w, list(e.segs), True))
            self.edges.remove(e)

        # 5. 分层
        self._assign_layers()
        # 6. 插虚节点（必须在排序之前：长边要拆成逐层小段，
        #    逐层扫描的交叉数才算得对，否则量的是节点序号而不是边的真实位置）
        self._insert_dummies()
        # 7. 层内排序：中位数启发式给初值，逐对交换按真实交叉数精修
        self._initial_order()
        self._refresh_dummy_orders()
        before = self._total_crossings()
        self._minimize_crossings()
        self._refresh_dummy_orders()
        after = self._total_crossings()
        self.report.crossings_before, self.report.crossings = before, after

        # 8. 坐标
        # 折行提示：按"把所有节点竖着摞起来有多高"估一个候选列数。
        # 以前只在层数 > 18 时才折，结果中等规模的图全被排成细长条；
        # 现在所有图都参与折/不折的**评分**，由 _score 决定谁更省地方。
        total = sum(v.h + self.opt.v_gap for v in self.verts.values())
        fold_hint = max(1, int(round(math.sqrt(max(1.0, total) / 7000.0))))
        self._assign_coords(fold_hint)
        self._record_routes()          # 虚节点位置已定，立刻记下来供路由使用

        # 9. 落位
        self._commit(sizes)
        self._snap_all()

        # 10. 隔离节点与注释
        strays = [n for n in g.nodes if n.id in self.contract and self.contract[n.id] == -1]
        self._place_extras(strays, ribbon, pinned)

        # 11. 分区框（会挪动节点，所以必须在消重叠之前）
        if opt.allow_groups:
            self._rebuild_groups()
        else:
            for grp in g.groups:
                grp.bounding = (0, 0, 0, 0)

        # 12. 兜底消重叠：此时所有节点都落位了，分区框也不再动
        self._resolve_overlaps()
        if opt.allow_groups:
            self._refresh_group_bounds()
        self._normalize()
        self._record_routes()
        self._verify()

        x0, y0, x1, y1 = g.bounds()
        self.report.nodes = len(g.nodes)
        self.report.layers = len(self.layers)
        self.report.width = x1 - x0
        self.report.height = y1 - y0
        self.report.area_per_node = (self.report.width * self.report.height) / max(1, len(g.nodes))
        self.report.groups = len(g.groups)
        self.report.strays = len(strays)
        self.report.notes = len(notes)
        self.report.contractions = len([k for k, v in self.contract.items() if v > 0])
        return self.report

    # ------------------------------------------------ 节点筛选
    def _select_nodes(self, movers: Sequence[Node]) -> Set[int]:
        """挑出要参与主版式的节点。旁路/静音/纯开关型辅助件不进主链，单独摆。"""
        keep: Set[int] = set()
        for n in movers:
            keep.add(n.id)
        return keep

    def _is_reroute(self, n: Node) -> bool:
        return n.type in REROUTE and len(n.inputs) <= 1 and len(n.outputs) <= 1

    # ------------------------------------------------ 建图
    def _build_graph(self, g: Graph, keep: Set[int], sizes: Dict[int, Tuple[float, float]]) -> None:
        """把 ComfyUI 的连接关系翻译成排版用的有向图。

        两条特殊规则：
          * Reroute / 旁路节点是透明的，连线直接穿透到真正的源头；
          * SetNode 只贴标签不传数据（它的输出端其实驱动 GetNode），
            所以 SetNode 自己不参与主链，排到它的源头旁边就好；
            GetNode 才是真正把值传下去的那个，必须排在 SetNode 右边。
        """
        def real_source(nid: int, slot: int, seen: Optional[Set[int]] = None) -> Tuple[int, int]:
            seen = seen if seen is not None else set()
            if nid in seen:
                return (nid, slot)
            seen.add(nid)
            n = g.maybe(nid)
            if n is None:
                return (nid, slot)
            if n.type in VIRTUAL_SET:
                ups = g.incoming(nid)
                if ups:
                    return real_source(ups[0].origin_id, ups[0].origin_slot, seen)
                return (nid, slot)
            if self._is_reroute(n) or n.mode == 4:
                ups = g.incoming(nid)
                if ups:
                    return real_source(ups[0].origin_id, ups[0].origin_slot, seen)
            return (nid, slot)

        for nid in keep:
            n = g.node(nid)
            w, h = sizes[nid]
            self.verts[str(nid)] = V(key=str(nid), node=n, w=w, h=h)

        seen_pairs: Set[Tuple[str, str]] = set()

        def add_edge(a: int, b: int, key: str, weight: float = 1.0) -> None:
            if a == b or a not in keep or b not in keep:
                return
            pair = (str(a), str(b))
            if pair in seen_pairs:
                return
            seen_pairs.add(pair)
            self.edges.append(E(pair[0], pair[1], key, weight))

        for l in g.links:
            o, t = g.maybe(l.origin_id), g.maybe(l.target_id)
            if o is None or t is None:
                continue
            if o.type in VIRTUAL_SET:                 # SetNode 的出边不构成数据依赖
                continue
            if t.type == VIRTUAL_GET:                 # Get 的入口由下面的虚拟跳线负责
                continue
            sid, _ = real_source(l.origin_id, l.origin_slot)
            add_edge(sid, l.target_id, f"l{l.id}", 1.0 + (0.5 if l.target_slot == 0 else 0.0))

        # 虚拟跳线：源头 → SetNode → GetNode → 下游。
        # 让 Set/Get 参与正常分层，它们才会被排到「源头右边、消费方左边」的正确位置，
        # 画布上也就不会出现一堆向左倒流的线。
        vm = g.virtual_map()
        for name, (setter, getters) in vm.items():
            if setter is None:
                continue                       # 无源 Get 保持独立，后面归到隔离区
            ups = g.incoming(setter.id)
            if not ups:
                continue
            sid, _ = real_source(ups[0].origin_id, ups[0].origin_slot)
            add_edge(sid, setter.id, f"v:{name}:s", 1.2)
            for gn in getters:
                add_edge(setter.id, gn.id, f"v:{name}:g", 1.0)
                add_edge(sid, gn.id, f"v:{name}:d", 0.6)

        alive = {v.key for v in self.verts.values()}
        self.edges = [e for e in self.edges if e.a in alive and e.b in alive]

    # ------------------------------------------------ 拆环
    def _find_back_edges(self) -> List[E]:
        adj: Dict[str, List[E]] = {}
        for e in self.edges:
            adj.setdefault(e.a, []).append(e)
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {k: WHITE for k in self.verts}
        finish: Dict[str, int] = {}
        clock = [0]
        back: List[E] = []

        for root in sorted(self.verts.keys(), key=_vkey):
            if color.get(root, WHITE) != WHITE:
                continue
            stack: List[Tuple[str, int]] = [(root, 0)]
            color[root] = GRAY
            while stack:
                u, i = stack[-1]
                outs = adj.get(u, [])
                if i < len(outs):
                    stack[-1] = (u, i + 1)
                    e = outs[i]
                    c = color.get(e.b, WHITE)
                    if c == WHITE:
                        color[e.b] = GRAY
                        stack.append((e.b, 0))
                    elif c == GRAY:
                        back.append(e)
                else:
                    color[u] = BLACK
                    finish[u] = clock[0]
                    clock[0] += 1
                    stack.pop()
        return back

    # ------------------------------------------------ 分层
    def _assign_layers(self) -> None:
        preds: Dict[str, List[E]] = {}
        succs: Dict[str, List[E]] = {}
        for e in self.edges:
            preds.setdefault(e.b, []).append(e)
            succs.setdefault(e.a, []).append(e)
        layer = {k: 0 for k in self.verts}
        # 拓扑序（已拆环，理论无环；保险起见加轮次上限）
        order = self._topo()
        for k in order:
            for e in succs.get(k, []):
                if layer[e.b] < layer[k] + 1:
                    layer[e.b] = layer[k] + 1
        # 收紧：尽量往左靠
        for _ in range(4):
            changed = False
            for k in order:
                ps = preds.get(k, [])
                if not ps:
                    continue
                want = max(layer[e.a] for e in ps) + 1
                if want < layer[k]:
                    layer[k] = want
                    changed = True
            if not changed:
                break
        for k, v in self.verts.items():
            v.layer = layer[k]                      # ← 关键：把层号写回顶点
        n_layers = max(layer.values(), default=0) + 1
        self._layer_members = [[] for _ in range(n_layers)]
        for k in sorted(self.verts.keys(), key=_vkey):
            self._layer_members[self.verts[k].layer].append(k)
        self.layers = self._layer_members

    def _topo(self) -> List[str]:
        indeg = {k: 0 for k in self.verts}
        succs: Dict[str, List[str]] = {}
        for e in self.edges:
            succs.setdefault(e.a, []).append(e.b)
            indeg[e.b] = indeg.get(e.b, 0) + 1
        q = sorted([k for k, d in indeg.items() if d == 0], key=_vkey)
        out: List[str] = []
        while q:
            k = q.pop(0)
            out.append(k)
            for b in succs.get(k, []):
                indeg[b] -= 1
                if indeg[b] == 0:
                    q.append(b)
        if len(out) < len(self.verts):
            out += [k for k in self.verts if k not in out]
        return out

    # ------------------------------------------------ 层内排序
    def _initial_order(self) -> None:
        """初始顺序：同层内按「类型 + 标题」聚簇，让同类节点挨在一起。
        虚节点（长边的中间段）没类型，用它的 order 初值排。"""
        def key(k: str):
            v = self.verts[k]
            if v.node is None:
                return ("~", "", v.order)
            node = v.node
            return (cluster_of(node), node.title or node.type,
                    v.order if v.order else 0)

        for layer in self._layer_members:
            layer.sort(key=key)
        for layer in self._layer_members:
            for i, k in enumerate(layer):
                self.verts[k].order = i

    def _bary(self) -> List[List[str]]:
        return [list(layer) for layer in self._layer_members]

    def _apply_order(self, layers: List[List[str]]) -> None:
        self._layer_members = [list(l) for l in layers]
        for li, layer in enumerate(self._layer_members):
            for i, k in enumerate(layer):
                self.verts[k].order = i

    def _neighbors(self, k: str, direction: str) -> List[str]:
        out: List[str] = []
        for e in self.edges:
            if direction == "up" and e.b == k:
                out.append(e.a)
            elif direction == "down" and e.a == k:
                out.append(e.b)
        return out

    def _adjacency(self) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        """相邻层之间的小边（down / up）。虚节点已插好时用它对齐最准。

        只把「同一对相邻层」里的连接算进来：跨层长边在排序阶段按直达处理
        （这是最小化交叉数的正确做法），到坐标阶段才需要拆成虚节点逐层对齐。
        """
        down: Dict[str, List[str]] = {}
        up: Dict[str, List[str]] = {}
        for e in self.edges:
            a, b = self.verts[e.a], self.verts[e.b]
            if a.layer == b.layer:
                continue
            seq = [e.a, e.b]
            if b.layer > a.layer + 1 and e.segs:
                chain = sorted([v for v in self.verts.values()
                                if v.dummy and e.segs and v.chain == e.segs[0]],
                               key=lambda v: v.layer)
                if chain:
                    seq = [e.a] + [v.key for v in chain] + [e.b]
            for u, w in zip(seq, seq[1:]):
                down.setdefault(u, []).append(w)
                up.setdefault(w, []).append(u)
        return down, up

    def _order_map(self) -> Dict[str, float]:
        return {k: float(v.order) for k, v in self.verts.items()}

    def _median_sweep(self, layers: List[List[str]], downward: bool) -> List[List[str]]:
        om = {k: float(i) for layer in layers for i, k in enumerate(layer)}
        idx = range(len(layers)) if downward else range(len(layers) - 1, -1, -1)
        for li in idx:
            layer = layers[li]
            fixed_side = "up" if downward else "down"
            keys: List[Tuple[float, float, str]] = []
            for i, k in enumerate(layer):
                nb = [om[x] for x in self._neighbors(k, fixed_side) if x in om]
                med = (sum(nb) / len(nb)) if nb else float(i)
                keys.append((med, float(i), k))
            keys.sort()
            layers[li] = [k for _, _, k in keys]
            for i, k in enumerate(layers[li]):
                om[k] = float(i)
        return layers

    def _layer_crossings(self, a: List[str], b: List[str]) -> int:
        """相邻两层之间的交叉数（归并计数，O(E log E)）。"""
        pos = {k: i for i, k in enumerate(a)}
        poss = {k: i for i, k in enumerate(b)}
        seq = sorted(poss[e.b] for e in self.edges if e.a in pos and e.b in poss)
        n_seq = len(seq)
        buf = [0] * n_seq
        counter = [0]

        def rec(lo: int, hi: int) -> None:
            if hi - lo <= 1:
                return
            mid = (lo + hi) // 2
            rec(lo, mid)
            rec(mid, hi)
            i, j, k = lo, mid, lo
            while i < mid and j < hi:
                if seq[i] <= seq[j]:
                    buf[k] = seq[i]
                    i += 1
                else:
                    buf[k] = seq[j]
                    j += 1
                    counter[0] += mid - i
                k += 1
            while i < mid:
                buf[k] = seq[i]
                i += 1
                k += 1
            while j < hi:
                buf[k] = seq[j]
                j += 1
                k += 1
            seq[lo:hi] = buf[lo:hi]

        rec(0, n_seq)
        return counter[0]

    def _total_crossings(self) -> int:
        """按「逐层扫描」数交叉 —— 与画布几何真正对应的形式。

        对每一层取一条竖直切割线，统计所有跨过这条线的边在切割线上的
        上下相对位置；位置的逆序对数就是这一层上的交叉数。

        ⚠ 上一版比较的是两端各自层里的序号，长边跨 5 层时那个比较毫无意义，
        结果恒为 0 ——「指标报 0、画布上 1500 处相交」就是这么来的。
        现在对跨多层的边按层线性插值，取它在切割线上的真实位置。
        """
        L = self._layer_members
        if len(L) < 2:
            return 0
        order: Dict[str, int] = {}
        for layer in L:
            for i, k in enumerate(layer):
                order[k] = i
        total = 0
        for li in range(len(L) - 1):
            cuts: List[int] = []
            for e in self.edges:
                a, b = self.verts.get(e.a), self.verts.get(e.b)
                if a is None or b is None or a.layer == b.layer:
                    continue
                top, bot = (a, b) if a.layer < b.layer else (b, a)
                if not (top.layer <= li < bot.layer):
                    continue
                t = (li + 1 - top.layer) / float(bot.layer - top.layer)
                ya = order.get(top.key, 0)
                yb = order.get(bot.key, 0)
                cuts.append(int(round((ya * (1.0 - t) + yb * t) * 1000.0)))
            total += _count_inversions(cuts)
        return total

    def _swap_improve(self, rounds: int = 8) -> None:
        """逐对相邻交换：换一下同层里相邻的两个节点，只要总交叉数下降就保留。

        中位数启发式只给出不错的初值，这个精修负责把剩下那些「明明能拉直却
        交叉着」的线修掉。

        ⚠ 这个搜索是 O(轮数 × 节点数 × 全图扫描)，大图上会爆炸：实测一张
        315 节点的图要跑 **104 秒**。所以加两道护栏：候选交换数过多就跳过精修、
        每轮检查时间预算。宁可少优化一点，也不能让一条命令卡两分钟。
        """
        layers = self._layer_members
        n_swaps = sum(max(0, len(l) - 1) for l in layers)
        if n_swaps > 120:
            # 实测：大图上逐对交换反而会拉高真实交叉数（排序一动，
            # 走线通道跟着动，代理指标和画布几何不同步）。中小图才做精修。
            return
        import time as _t
        budget = _t.time() + 2.0        # 护栏二：时间预算
        for _ in range(rounds):
            improved = False
            base = self._total_crossings()
            if base == 0 or _t.time() > budget:
                break
            for li in range(len(layers)):
                layer = layers[li]
                for i in range(len(layer) - 1):
                    layer[i], layer[i + 1] = layer[i + 1], layer[i]
                    self._layer_members[li] = layer
                    c = self._total_crossings()
                    if c < base:
                        base, improved = c, True
                    else:
                        layer[i], layer[i + 1] = layer[i + 1], layer[i]
                        self._layer_members[li] = layer
                if _t.time() > budget:
                    break
            if not improved or _t.time() > budget:
                break

    def _refresh_dummy_orders(self) -> None:
        """给每个虚节点重算 order：取它所在长边两端的平均序号，保证单调不回折。"""
        for v in self.verts.values():
            if not v.dummy or v.chain is None:
                continue
            ends = [e for e in self.edges if v.chain in e.segs]
            if not ends:
                continue
            e = ends[0]
            a, b = self.verts.get(e.a), self.verts.get(e.b)
            if a is None or b is None or a.layer == b.layer:
                continue
            t = (v.layer - a.layer) / float(b.layer - a.layer)
            v.order = a.order * (1 - t) + b.order * t

    def _minimize_crossings(self) -> List[List[str]]:
        layers = self._bary()
        best = [list(l) for l in layers]
        best_c = self._total_crossings()
        for sweep in range(self.opt.max_order_sweeps):
            layers = self._median_sweep(layers, downward=(sweep % 2 == 0))
            self._layer_members = [list(l) for l in layers]
            c = self._total_crossings()
            if c < best_c:
                best_c, best = c, [list(l) for l in layers]
            if best_c == 0:
                break
        self._layer_members = [list(l) for l in best]
        self._swap_improve()
        for i, layer in enumerate(self._layer_members):
            for j, k in enumerate(layer):
                self.verts[k].order = j
        return self._layer_members

    def _count_crossings(self) -> int:
        return self._total_crossings()

    # ------------------------------------------------ 坐标
    def _insert_dummies(self) -> None:
        order_map = self._order_map()
        for e in list(self.edges):
            a, b = self.verts[e.a], self.verts[e.b]
            span = b.layer - a.layer
            if span <= 1:
                continue
            chain = self._chain_seq
            self._chain_seq += 1
            prev = e.a
            for li in range(a.layer + 1, b.layer):
                key = f"~d{chain}_{li}"
                self.verts[key] = V(key=key, node=None, w=10.0, h=10.0, layer=li,
                                    dummy=True, chain=chain)
                e.segs.append(chain)
                # 放在该层既有节点之间，初值取两端的中位数
                mid = (order_map.get(e.a, 0.0) + order_map.get(e.b, 0.0)) / 2.0
                self.verts[key].order = mid
                prev = key
        # 把虚节点插进层里
        for k, v in self.verts.items():
            if v.dummy:
                while len(self._layer_members) <= v.layer:
                    self._layer_members.append([])
                self._layer_members[v.layer].append(k)
        for li, layer in enumerate(self._layer_members):
            layer.sort(key=lambda k: (self.verts[k].order if not self.verts[k].dummy
                                      else self.verts[k].order, k))
            for i, k in enumerate(layer):
                self.verts[k].order = i

    def _adjacency(self) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        """相邻层之间的小边（虚节点已插好之后调用最准）。"""
        down: Dict[str, List[str]] = {}
        up: Dict[str, List[str]] = {}
        for e in self.edges:
            a, b = self.verts[e.a], self.verts[e.b]
            if a.layer == b.layer:
                continue
            seq = [e.a, e.b]
            if b.layer > a.layer + 1 and e.segs:
                chain = sorted([v for v in self.verts.values()
                                if v.dummy and v.chain == e.segs[0]],
                               key=lambda v: v.layer)
                if chain:
                    seq = [e.a] + [v.key for v in chain] + [e.b]
            for u, w in zip(seq, seq[1:]):
                down.setdefault(u, []).append(w)
                up.setdefault(w, []).append(u)
        return down, up

    def _assign_coords(self) -> None:
        down, up = self._adjacency()
        n = len(self._layer_members)
        ys: Dict[str, float] = {}
        for li, layer in enumerate(self._layer_members):
            y = 0.0
            for k in layer:
                v = self.verts[k]
                v.y = y
                y += v.h + self.opt.v_gap
        # 两趟中位数对齐
        for direction in ("down", "up"):
            ranges = range(n) if direction == "down" else range(n - 1, -1, -1)
            nbr = up if direction == "down" else down
            for li in ranges:
                layer = self._layer_members[li]
                if not layer:
                    continue
                desired: List[float] = []
                for k in layer:
                    v = self.verts[k]
                    ns = [self.verts[x] for x in nbr.get(k, []) if x in self.verts]
                    if ns:
                        desired.append(sum(x.cy for x in ns) / len(ns))
                    else:
                        desired.append(None)  # type: ignore
                self._pack_layer(layer, desired, ys, direction)
        # 最后一趟：把整张图的竖向重心拉匀
        self._recenter_globally(ys)

        # 列坐标
        widths = [max([self.verts[k].w for k in layer], default=0.0)
                  for layer in self._layer_members]
        x = 0.0
        for li, layer in enumerate(self._layer_members):
            for k in layer:
                v = self.verts[k]
                v.x = x + (widths[li] - v.w) / 2.0
            x += widths[li] + self.opt.h_gap

    def _recenter_globally(self, ys: Dict[str, float]) -> None:
        """相邻层之间做一次重心对齐，抹掉中位数法留下的锯齿。"""
        for _ in range(2):
            for li in range(1, len(self._layer_members)):
                prev = self._layer_members[li - 1]
                cur = self._layer_members[li]
                if not prev or not cur:
                    continue
                want = sum(self.verts[k].cy for k in prev) / len(prev)
                have = sum(self.verts[k].cy for k in cur) / len(cur)
                d = want - have
                if abs(d) < 1.0:
                    continue
                for k in cur:
                    self.verts[k].y += d

    def _balanced_columns(self, n_cols: int) -> List[List[str]]:
        """把层按**高度**均衡分到 n_cols 列里，而不是按层数平均切。

        为什么需要这个：严格「一层一列」时，包围盒高度由**最高的那一列**决定，
        旁边那些只有一两个节点的短列全在白白浪费竖向空间 —— 实测一张
        39 节点的图排出来 5193×3206，密度只有 19%，而用户自己的手摆版是 79.6%。
        根因不是间距参数，是"列高参差不齐"。

        做法是最朴素的贪心：按顺序走层，累计高度超出目标就换一列。
        层的先后顺序完全不动（数据流方向还是从左到右），只是把相邻的几层
        摞进同一列，让各列高度尽量齐平。
        """
        L = self._layer_members
        if not L or n_cols <= 1:
            return [list(m) for m in L]

        def col_h(members: List[str]) -> float:
            if not members:
                return 0.0
            return (sum(self.verts[k].h for k in members)
                    + self.opt.v_gap * (len(members) - 1))

        heights = [col_h(m) for m in L]
        total = sum(heights) + self.opt.h_gap * 0  # 高度只算竖着的部分
        target = max(max(heights, default=0.0), total / float(n_cols))

        cols: List[List[str]] = []
        cur: List[str] = []
        cur_h = 0.0
        for members, h in zip(L, heights):
            add = h if not cur else h + self.opt.v_gap
            if cur and cur_h + add > target and len(cols) < n_cols - 1:
                cols.append(cur)
                cur, cur_h = list(members), h
            else:
                cur.extend(members)
                cur_h += add
        if cur:
            cols.append(cur)
        return cols

    def _split_tall_columns(self, cols: List[List[str]],
                            target_h: float) -> List[List[str]]:
        """把过高的列拆成几列并排。

        「一列 = 一层」这条规矩在遇到**巨层**时会彻底垮掉：实测一张 306 节点的
        视频图，层 0 聚集了 100 多个无输入的加载器/常量节点，那一列单枪匹马
        就有 3 万像素高，旁边几十列只有一两千 —— 包围盒高度完全由它决定，
        整张图被拉成 8093×31926 的竖条。

        同层的节点本来就没有先后关系（都是同一深度的），把它们**并排**放几列
        不会破坏"数据流从左到右"这件事，却能把最高的那一列砍成几段。
        """
        out: List[List[str]] = []
        for members in cols:
            if len(members) < 2:
                out.append(members)
                continue
            h = (sum(self.verts[k].h for k in members)
                 + self.opt.v_gap * (len(members) - 1))
            parts = int(math.ceil(h / max(target_h, 1.0)))
            if parts <= 1:
                out.append(members)
                continue
            parts = min(parts, len(members))
            per = int(math.ceil(len(members) / float(parts)))
            for i in range(0, len(members), per):
                out.append(members[i:i + per])
        return out

    def _place(self, vertical_cols: bool, fold: int = 1,
               balanced: bool = False) -> None:
        """按「列 = 层」摆好每个顶点的 (x, y)。

        vertical_cols=True 时列是竖排的（默认的从左到右数据流）；
        False 时把列横过来摆（层从上往下），用于救那种又高又窄的极端版式。
        """
        L = self._layer_members
        if not L:
            return
        if balanced:
            cols = self._balanced_columns(max(1, (len(L) + fold - 1) // fold))
        else:
            group_count = max(1, (len(L) + fold - 1) // fold)
            cols = []
            for gi in range(group_count):
                members: List[str] = []
                for li in range(gi * fold, min(len(L), (gi + 1) * fold)):
                    members.extend(L[li])
                cols.append(members)

        if vertical_cols:
            # 目标列高：拿全部节点面积估一个"理想方正版式"的边长，再放宽一点。
            # 超过它的列就该拆开并排 —— 否则一个巨层就能把整张图拉成竖条。
            _area = sum(v.w * v.h for v in self.verts.values())
            _tall = max((v.h for v in self.verts.values()), default=100.0)
            target_h = max(_tall * 2.5, math.sqrt(max(_area, 1.0)) * 1.35)
            cols = self._split_tall_columns(cols, target_h)
            widths = [max([self.verts[k].w for k in c], default=0.0) for c in cols]
            x = 0.0
            for ci, c in enumerate(cols):
                y = 0.0
                for k in c:
                    v = self.verts[k]
                    v.x = x + (widths[ci] - v.w) / 2.0
                    v.y = y
                    y += v.h + self.opt.v_gap
                x += widths[ci] + self.opt.h_gap
        else:
            heights = [max([self.verts[k].h for k in c], default=0.0) for c in cols]
            y = 0.0
            for ci, c in enumerate(cols):
                x = 0.0
                for k in c:
                    v = self.verts[k]
                    v.y = y + (heights[ci] - v.h) / 2.0
                    v.x = x
                    x += v.w + self.opt.h_gap
                y += heights[ci] + self.opt.v_gap

    def _score(self) -> float:
        xs0 = min(v.x for v in self.verts.values())
        ys0 = min(v.y for v in self.verts.values())
        w = max(v.x + v.w for v in self.verts.values()) - xs0
        h = max(v.y + v.h for v in self.verts.values()) - ys0
        # 版式评分。
        #
        # 这里改过两轮，值得记一笔：
        #
        # 第 1 版：`log(面积) + 1.25·log(max(比/目标, 目标/比))` —— 惩罚是
        #   **对称的**，把"太宽"和"太高"一视同仁。结果一张面积 8.96M 的
        #   2675×3349 方块，输给了面积 17.8M 的 6358×2793 横条，就因为后者
        #   宽高比更接近目标值。可用户要的是"别拉那么长"，偏高的方块不叫长条。
        #
        # 第 2 版：改成单向惩罚 —— 于是另一个方向塌了：出现 8092×96907
        #   这种竖长条，一样是"拉得老长"，只是换了个方向。
        #
        # 第 3 版（现在）：直接拿**最长的那条边**当主目标。它天然是双向的
        #   —— 横条和竖条的长边都一样长，都会被压；面积只作为次要项（防止
        #   为了缩短长边而把另一头撑爆）。用户的原话是"不要把整个节点图拉
        #   那么长"，"长"就是最长边，那就直接最小化它。
        longest = max(w, h)
        return (math.log(max(longest, 1.0))
                + 0.35 * math.log(max(w * h, 1.0))
                + 0.00002 * (w + h))

    def _assign_coords(self, fold_hint: int = 1) -> None:
        shapes = [(v.w, v.h) for v in self.verts.values()]
        backup = [v for v in self.verts.values()]
        best: Optional[Tuple[float, List[Tuple[float, float, int]]]] = None

        # 候选折数要**密**一点。以前只有 {1, lo, hi, 4*hint} 这种粗档位，
        # 实测一条 7 层的链只会试 1 和 4，最合适的 2 和 3 根本没被试到
        # —— 结果就是"能折得更紧凑却没折"，图照样拉得很长。
        n_layers = max(1, len(self._layer_members))
        cands = {1, 2, 3, 4, 6, 8, max(1, fold_hint), max(1, fold_hint * 2),
                 max(1, fold_hint * 4)}
        cands = sorted(f for f in cands if f <= n_layers)
        tries: List[Tuple[int, bool]] = []
        for vertical in (True, False):
            for f in cands:
                tries.append((f, vertical))

        for fold, vertical in tries:
            if fold > 1 and vertical:
                col_max = max((v.h for v in self.verts.values()), default=100.0) + self.opt.v_gap
                if col_max * fold > 6000.0:
                    continue
            self._place(vertical, fold)
            sc = self._score()
            if best is None or sc < best[0]:
                best = (sc, [(v.x, v.y, fold) for v in self.verts.values()])
            # 同一折数再来一次「高度均衡」的分列，谁省地方用谁
            if fold > 1:
                self._place(vertical, fold, balanced=True)
                sc2 = self._score()
                if sc2 < best[0]:
                    best = (sc2, [(v.x, v.y, fold)
                                  for v in self.verts.values()])
            if best is None or sc < best[0]:
                best = (sc, [(v.x, v.y, fold) for v in self.verts.values()])
        if best is None:
            self._place(True, 1)
            return
        for v, (x, y, _f) in zip(backup, best[1]):
            v.x, v.y = x, y

    def _pack_layer(self, layer: List[str], desired: List[Any], ys: Dict[str, float],
                    direction: str) -> None:
        """把一层的节点按期望中心排开，保证不重叠、顺序不变。"""
        if not layer:
            return
        pos: List[float] = []
        for i, k in enumerate(layer):
            v = self.verts[k]
            d = desired[i] if i < len(desired) and desired[i] is not None else None
            if d is None:
                d = v.cy
            pos.append(d - v.h / 2.0)
        # 前向消重叠
        for i in range(len(layer)):
            if i == 0:
                continue
            miny = pos[i - 1] + self.verts[layer[i - 1]].h + self.opt.v_gap
            if pos[i] < miny:
                pos[i] = miny
        # 反向把整体拉回重心（保持顺序不变）
        if len(layer) > 1:
            shift = sum((desired[i] if i < len(desired) and desired[i] is not None
                         else self.verts[layer[i]].cy) - self.verts[layer[i]].h / 2.0 - pos[i]
                        for i in range(len(layer))) / len(layer)
            for i in range(len(layer)):
                pos[i] += shift
            for i in range(1, len(layer)):
                miny = pos[i - 1] + self.verts[layer[i - 1]].h + self.opt.v_gap
                if pos[i] < miny:
                    pos[i] = miny
        for i, k in enumerate(layer):
            self.verts[k].y = pos[i]

    # ------------------------------------------------ 落位
    def _snap(self, v: float) -> float:
        g = self.opt.grid or 1.0
        return round(v / g) * g

    def _snap_all(self) -> None:
        for n in self.g.nodes:
            n.pos = (self._snap(n.pos[0]), self._snap(n.pos[1]))

    def _commit(self, sizes: Dict[int, Tuple[float, float]]) -> None:
        g = self.g
        laid: Set[int] = set()
        for k, v in self.verts.items():
            if v.dummy or v.node is None:
                continue
            v.node.pos = (self._snap(v.x), self._snap(v.y))
            v.node.size = (v.w, v.h)
            laid.add(v.node.id)

        # Reroute：插回它两端之间
        for n in g.nodes:
            if n.id in laid or n.is_note:
                continue
            if not self._is_reroute(n):
                continue
            ups = g.incoming(n.id)
            downs = g.outgoing(n.id)
            if ups and downs:
                a = g.maybe(ups[0].origin_id)
                b = g.maybe(downs[0].target_id)
                if a is not None and b is not None:
                    n.size = (30.0, 30.0)
                    n.pos = (self._snap((a.right + b.pos[0]) / 2.0 - 15.0),
                             self._snap((a.pos[1] + b.pos[1]) / 2.0 + 10.0))

        # 同一真实节点上的多个 Set/Get 复制体挨着放
        self._pack_virtual_copies()

    def _pack_virtual_copies(self) -> None:
        """同名的 SetNode 复制体在层里可能被拆散；把同一组顺下来，视觉上更连贯。"""
        g = self.g
        vm: Dict[str, List[Node]] = {}
        for n in g.nodes:
            if not n.is_virtual:
                continue
            key = None
            for _k, v in n.widget_pairs():
                if isinstance(v, str):
                    key = v
                    break
            if key:
                vm.setdefault(key, []).append(n)
        for key, group in vm.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda z: (z.pos[0], z.pos[1], z.id))
            lead = group[0]
            y = lead.pos[1]
            for n in group[1:]:
                if abs(n.pos[0] - lead.pos[0]) < 40.0:      # 同一列才顺下来
                    y += n.height + 16.0
                    n.pos = (n.pos[0], y)
                else:
                    y = n.pos[1]

    # ------------------------------------------------ 消重叠
    def _resolve_overlaps(self, rounds: int = 14) -> None:
        """外层反复调用，直到彻底没有重叠为止（绕推会产生新的重叠）。"""
        for _ in range(6):
            before = self._overlap_count()
            self._resolve_overlaps_once(rounds)
            if self._overlap_count() == 0 or self._overlap_count() >= before:
                break

    def _overlap_count(self) -> int:
        ns = [n for n in self.g.nodes if n.width > 0 and n.height > 0]
        c = 0
        for i in range(len(ns)):
            a = ns[i]
            for j in range(i + 1, len(ns)):
                b = ns[j]
                if self._overlap(a.box, b.box, 0.0):
                    c += 1
        return c

    def _resolve_overlaps_once(self, rounds: int = 14) -> None:
        """全局兜底：任何两个节点（含注释、隔离件、虚拟跳线块）都不许重叠。

        做法是按 x 轴分簇（水平投影相交的算一簇），簇内按 y 顺序往下推，
        反复几轮直到收敛。注释这种巨型节点被当成普通矩形一起处理，
        因为它们在画布上同样会挡住别的节点。
        """
        g = self.g
        ns = [n for n in g.nodes if n.width > 0 and n.height > 0]
        if len(ns) < 2:
            return
        gap = max(16.0, self.opt.node_min_gap * 0.6)
        for _ in range(rounds):
            moved = False
            ns.sort(key=lambda n: (n.pos[0], n.pos[1]))
            for i in range(len(ns)):
                a = ns[i]
                for j in range(i + 1, len(ns)):
                    b = ns[j]
                    if b.pos[0] >= a.right + gap:       # 已排序，后面都不可能水平相交
                        break
                    if not self._overlap(a.box, b.box, gap):
                        continue
                    d = (a.bottom + gap) - b.pos[1]
                    if d > 0:
                        b.pos = (b.pos[0], b.pos[1] + d)
                        moved = True
            if not moved:
                break

    # ------------------------------------------------ 副区
    def _place_extras(self, strays: List[Node], ribbon: List[Node],
                      pinned: List[Node]) -> None:
        g = self.g
        main = [n for n in g.nodes if n not in strays and n not in ribbon]
        if main:
            mx0 = min(n.pos[0] for n in main)
            my1 = max(n.bottom for n in main)
            my0 = min(n.pos[1] for n in main)
            mx1 = max(n.right for n in main)
        else:
            mx0 = my0 = 0.0
            mx1 = my1 = 0.0

        # 注释横幅：主图正上方，按可用宽度横排成 1~2 行，不跟数据流抢地方
        if ribbon:
            avail = max(mx1 - mx0, 1000.0)
            cols = 1 if len(ribbon) <= 1 or avail < 1600 else 2
            col_w = avail / cols
            y = my0 - self.opt.band_gap
            row_h = 0.0
            for i, n in enumerate(sorted(ribbon, key=lambda z: -z.width)):
                w = max(320.0, min(col_w - 40.0, n.width))
                h = max(90.0, min(400.0, n.height))
                n.size = (w, h)
                r, c = divmod(i, cols)
                n.pos = (self._snap(mx0 + c * col_w), self._snap(y + r * (row_h + 24.0)))
                row_h = max(row_h, h)

        # 孤立/隔离节点：主图下方排成网格
        if strays:
            cols = max(1, int(math.sqrt(len(strays)) + 0.5))
            col_w = max((n.width for n in strays), default=300.0) + 70.0
            row_h = max((n.height for n in strays), default=120.0) + self.opt.v_gap
            base_y = my1 + self.opt.band_gap
            # "auto" 要真的自动：把孤立件放到**短的那一边**去。
            #
            # 原来 `else: base_x = mx0` 让 auto 实际等同于「永远堆在下方」——
            # 主图一旦是偏高的块，孤立件再往下堆就成了 2308×78485 这种
            # 34:1 的竖长条。用户的原话是"别把节点图拉那么长"，那就在
            # 放之前先看一眼哪条边更短，往哪儿加。
            go_right = self.opt.strays == "right" or (
                self.opt.strays == "auto"
                and (my1 - my0) > (mx1 - mx0))
            if go_right:
                base_x = mx1 + self.opt.band_gap
                base_y = my0
            else:
                base_x = mx0
            for i, n in enumerate(sorted(strays, key=lambda z: (z.type, z.id))):
                r, c = divmod(i, cols)
                n.pos = (self._snap(base_x + c * col_w), self._snap(base_y + r * row_h))

        # 被用户保留在原位的注释
        for n in pinned:
            pass

    # ------------------------------------------------ 分区框
    def _rebuild_groups(self, separate: bool = True) -> None:
        """按语义 + 连通性重画分区框。

        `separate=False` 时保证**一个节点都不挪**：收尾那步 `_separate_groups()`
        是靠平移节点来推开互相压住的分区框的，手工摆位（cwf place）不能要它，
        否则"只重算框"会悄悄改掉用户明确指定的坐标。

        两个克制之处：
          * Set/Get 跳线节点本身不算一类，它们并入所服务的功能区——否则
            「跳线区」会横跨整张图，任何分区框都没法跟它并排；
          * 分区再按连通性切成互不相干的块，避免一个巨框套住半张图。
        """
        g = self.g
        nodes = [n for n in g.nodes if not n.is_note]
        if not nodes:
            g.groups = []
            return
        ids = {x.id for x in nodes}
        raw: Dict[int, Optional[str]] = {}
        for x in nodes:
            lab = cluster_of(x)
            raw[x.id] = None if lab in ("跳线区", "注释") else lab

        # 邻居（含虚拟跳线两端）用于给没归类的节点找归属
        nbr: Dict[int, List[int]] = {i: [] for i in ids}
        for l in g.links:
            if l.origin_id in ids and l.target_id in ids:
                nbr[l.origin_id].append(l.target_id)
                nbr[l.target_id].append(l.origin_id)
        vm = g.virtual_map()
        for _name, (setter, getters) in vm.items():
            grp_all = ([setter] if setter else []) + list(getters)
            gids = [x.id for x in grp_all if x is not None and x.id in ids]
            for a in gids:
                for b in gids:
                    if a != b:
                        nbr[a].append(b)

        label: Dict[int, str] = {}
        for _round in range(4):
            for x in sorted(nodes, key=lambda z: z.id):
                if label.get(x.id):
                    continue
                if raw[x.id]:
                    label[x.id] = raw[x.id]
                    continue
                votes: Dict[str, int] = {}
                for y in nbr[x.id]:
                    if label.get(y):
                        votes[label[y]] = votes.get(label[y], 0) + 1
                if votes:
                    label[x.id] = max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
        for x in nodes:
            label.setdefault(x.id, "其他")

        # 同区 + 连通 → 同一块。跳线节点（Set/Get）不打断连通性：
        # 载入区 → Set → Get → 载入区 应该还算一伙，否则每个功能区都会被拆散。
        parent: Dict[int, int] = {x.id: x.id for x in nodes}

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        vm = g.virtual_map()
        set_to_gets: Dict[int, List[int]] = {}
        for _name, (setter, getters) in vm.items():
            if setter is None:
                continue
            set_to_gets.setdefault(setter.id, []).extend(
                gn.id for gn in getters if gn.id in ids)

        def hop_targets(nid: int) -> List[int]:
            """从 nid 出发能到哪：普通出边；Set 节点则连到它所有的 Get 复制体。"""
            out = [l.target_id for l in g.links if l.origin_id == nid]
            node = g.maybe(nid)
            if node is not None and node.type in VIRTUAL_SET:
                out.extend(set_to_gets.get(nid, []))
            return out

        def hop_sources(nid: int) -> List[int]:
            """谁能到 nid：普通入边；Get 节点则回溯到它对应的 Set。"""
            node = g.maybe(nid)
            if node is not None and node.type in VIRTUAL_GET:
                for _name, (setter, getters) in vm.items():
                    if setter is not None and any(gn.id == nid for gn in getters):
                        return [setter.id]
                return []
            return [l.origin_id for l in g.links if l.target_id == nid]

        def reachable(start: int) -> Set[int]:
            """能跟 start 算一伙的同区节点。

            跳线节点（Set/Get）是透明桥，但**最多只穿一次**：
            不然顺着 Set→Get→Set→Get 能一路串遍全图，所有分区会糊成一坨。
            """
            lab = label.get(start)
            if lab is None:
                return set()
            out: Set[int] = set()
            stack: List[Tuple[int, int]] = [(nid, 0) for nid in hops_all(start)]
            guard = 0
            while stack and guard < 4000:
                guard += 1
                nid, hops = stack.pop()
                if nid in out or nid not in ids:
                    continue
                here = label.get(nid)
                if here is None:                      # 跳线节点：只在预算内继续穿
                    if hops >= 1:
                        continue
                    out.add(nid)
                    stack.extend((t, hops + 1) for t in hops_all(nid))
                    continue
                if here != lab:                       # 跨区：到此为止
                    continue
                out.add(nid)
                stack.extend((t, hops) for t in hops_all(nid))
            return out

        def hops_all(nid: int) -> List[int]:
            return list(set(hop_targets(nid) + hop_sources(nid)))

        # 第一步：只把「真实节点」之间同区的连起来（跳线节点只当桥，不参与判定）
        for x in nodes:
            lx = label.get(x.id)
            if lx in (None, "其他"):
                continue
            for nid in reachable(x.id):
                if nid != x.id and label.get(nid) == lx:
                    union(x.id, nid)

        # 第二步：把跳线节点并进它驱动的那个组件（谁用得多算谁的）
        for x in nodes:
            if label.get(x.id) is not None:
                continue
            votes: Dict[int, int] = {}
            for nid in reachable(x.id):
                lab2 = label.get(nid)
                if lab2 is not None and lab2 != "其他" and nid != x.id:
                    r = find(nid)
                    votes[r] = votes.get(r, 0) + 1
            if votes:
                union(max(votes.items(), key=lambda kv: kv[1])[0], x.id)

        buckets: Dict[Tuple[str, int], List[Node]] = {}
        for x in nodes:
            buckets.setdefault((label[x.id], find(x.id)), []).append(x)

        # 太小的块并进「其他」，但载入/输出这种关键区即使单节点也保留
        merged: Dict[str, List[Node]] = {}
        color_of: Dict[str, str] = {}
        for (lab, _root), members in buckets.items():
            keep = len(members) >= 2 or lab in ("载入区", "输出区", "提示词区", "采样区")
            target = lab if keep else "其他"
            merged.setdefault(target, []).extend(members)
            for nm, cols, _ in CLUSTERS:
                if nm == target:
                    color_of.setdefault(target, cols[0])
        color_of.setdefault("其他", "#666")

        # 一个功能区常常被拆成好几块（比如加载 VAE 的和加载主模型的隔了老远）。
        # 先取「同区连通分量」，再把空间上挨得近的分量并成一个框——这样框既
        # 守住了语义，又不会拉成横跨半张图的巨框。
        g.groups = []
        gid = 1
        pad = self.opt.group_pad
        chunks: List[Tuple[str, List[Node]]] = []
        for lab, members in sorted(merged.items(), key=lambda kv: -len(kv[1])):
            for chunk in self._absorb_fragments(self._merge_components(members)):
                chunks.append((lab, chunk))
        chunks = self._coalesce(chunks)
        for lab, chunk in chunks:
            x0 = min(n.pos[0] for n in chunk) - pad
            y0 = min(n.pos[1] for n in chunk) - pad - 26
            x1 = max(n.right for n in chunk) + pad
            y1 = max(n.bottom for n in chunk) + pad
            grp = Group(gid, lab, (x0, y0, x1 - x0, y1 - y0), color_of.get(lab))
            setattr(grp, "_members", chunk)
            g.groups.append(grp)
            gid += 1
        if separate:
            self._separate_groups()
            self._relabel_groups()

    def _coalesce(self, chunks: List[Tuple[str, List[Node]]],
                  threshold: float = 0.12, rounds: int = 10) -> List[Tuple[str, List[Node]]]:
        """两个块如果大面积糊在一起，硬推开只会把画布撑爆——合成一个框才对。

        只有「重叠面积 / 较小块面积 > threshold」才合并：轻微压边交给后面的
        平移处理，大面积交叠说明它们本来就是同一片区域。
        """
        cur = [(lab, list(ch)) for lab, ch in chunks]

        def box(ch: List[Node]) -> Tuple[float, float, float, float]:
            return (min(n.pos[0] for n in ch), min(n.pos[1] for n in ch),
                    max(n.right for n in ch), max(n.bottom for n in ch))

        for _ in range(rounds):
            merged_any = False
            for i in range(len(cur)):
                for j in range(i + 1, len(cur)):
                    b1, b2 = box(cur[i][1]), box(cur[j][1])
                    ox = min(b1[2], b2[2]) - max(b1[0], b2[0])
                    oy = min(b1[3], b2[3]) - max(b1[1], b2[1])
                    if ox <= 0 or oy <= 0:
                        continue
                    a1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
                    a2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
                    smaller = max(1.0, min(a1, a2))
                    if (ox * oy) / smaller < threshold:
                        continue
                    l1, l2 = cur[i][0], cur[j][0]
                    label = l1 if l1 == l2 else (l1 if l1 == "其他" else (l2 if l2 == "其他" else l1))
                    if l1 != l2 and "其他" not in (l1, l2):
                        label = "＋".join(sorted({l1, l2}))
                    # 反复合并会把标题堆成「载入区＋提示词区＋采样区＋解码区＋输出区」，
                    # 又长又读不出重点（小工作流里所有区都挨着，一定会合）。
                    # 超过三段就收成「A＋B 等N区」。
                    _parts = label.split("＋")
                    if len(_parts) > 3:
                        label = f"{_parts[0]}＋{_parts[1]} 等{len(_parts)}区"
                    cur[i] = (label, cur[i][1] + cur[j][1])
                    cur.pop(j)
                    merged_any = True
                    break
                if merged_any:
                    break
            if not merged_any:
                break
        return cur

    def _absorb_fragments(self, chunks: List[List[Node]],
                          min_size: int = 3) -> List[List[Node]]:
        """只有一两个节点的碎片框看着很碎，并进空间上最近的兄弟块里。"""
        if len(chunks) < 2:
            return chunks
        big = [c for c in chunks if len(c) >= min_size]
        small = [c for c in chunks if len(c) < min_size]
        if not big:
            return chunks                      # 全是碎片就干脆不并，免得没框

        def center(c: List[Node]) -> Tuple[float, float]:
            return (sum(n.pos[0] for n in c) / len(c),
                    sum(n.pos[1] for n in c) / len(c))

        for c in small:
            cx, cy = center(c)
            best, best_d = None, None
            for tgt in big:
                tx, ty = center(tgt)
                d = (tx - cx) ** 2 + (ty - cy) ** 2
                if best_d is None or d < best_d:
                    best, best_d = tgt, d
            if best is not None:
                best.extend(c)
        return big

    def _merge_components(self, members: List[Node],
                          max_w: float = 2700.0, max_gap: float = 420.0) -> List[List[Node]]:
        """同区节点先按 x 邻近聚块，再按块间距逐步合并，直到块尺寸/间距超限。"""
        if not members:
            return []
        ms = sorted(members, key=lambda n: (n.pos[0], n.pos[1]))
        blocks: List[List[Node]] = [[ms[0]]]
        for n in ms[1:]:
            cur = blocks[-1]
            x0 = min(z.pos[0] for z in cur)
            x1 = max(z.right for z in cur)
            if n.right - x0 <= max_w and n.pos[0] - x1 <= max_gap:
                cur.append(n)
            else:
                blocks.append([n])
        # 块之间再贪心地并一轮（并了还不超宽就并）
        out: List[List[Node]] = []
        for blk in blocks:
            if out:
                prev = out[-1]
                x0 = min(z.pos[0] for z in prev)
                x1 = max(z.right for z in prev)
                bx0 = min(z.pos[0] for z in blk)
                if bx0 - x1 <= max_gap and max(z.right for z in blk) - x0 <= max_w:
                    prev.extend(blk)
                    continue
            out.append(blk)
        return out

    def _separate_groups(self) -> None:
        """让分区框互不压住对方，且尽量不破坏分层结构。

        做法：把框按「第一列」归到左/右两栏，需要时把右栏整体右移。
        比起逐对推挤，整体平移只有一次，不会滚雪球；两栏分不够时降级成
        纵向撑开，并且给位移设上限，宁可留一点压边也不把画布撑爆。
        """
        g = self.g
        groups = [grp for grp in g.groups]
        if len(groups) < 2:
            return
        # 成员在建框时就已经定好了，这里不要再用几何去反推——巨框会把别人的成员吞掉
        members: Dict[int, List[Node]] = {
            grp.id: list(getattr(grp, "_members", []) or []) for grp in groups}
        if not any(members.values()):
            return

        def shift(grp: Group, dx: float, dy: float) -> None:
            for n in members.get(grp.id, []):
                n.pos = (n.pos[0] + dx, n.pos[1] + dy)
            x, y, w, h = grp.bounding
            grp.bounding = (x + dx, y + dy, w, h)

        for _round in range(16):
            # 每轮开始先按成员实际位置刷新边界，否则算出来的位移是错的
            for grp in groups:
                ms = members.get(grp.id) or []
                if not ms:
                    continue
                pad = self.opt.group_pad
                grp.bounding = (min(n.pos[0] for n in ms) - pad,
                                min(n.pos[1] for n in ms) - pad - 26,
                                max(n.right for n in ms) - min(n.pos[0] for n in ms) + pad * 2,
                                max(n.bottom for n in ms) - min(n.pos[1] for n in ms) + pad * 2 + 26)
            pairs = [(groups[i], groups[j])
                     for i in range(len(groups)) for j in range(i + 1, len(groups))
                     if groups[i].title != groups[j].title
                     and self._overlap(groups[i].box, groups[j].box, 4.0)]
            if not pairs:
                break
            # 分层版式是「横向铺开、纵向堆叠」，所以优先纵向撑开：
            # 横向平移会把整个分层结构推散，纵向挪一小段就好。
            moved = False
            for a, b in sorted(pairs, key=lambda p: abs(p[0].box[1] - p[1].box[1])):
                top, bot = (a, b) if a.box[1] <= b.box[1] else (b, a)
                need = (top.box[3] + self.opt.group_gap) - bot.box[1]
                if need > 0:
                    shift(bot, 0.0, need)
                    moved = True
            if moved:
                continue
            # 纵向挪不动了，才考虑横向分栏
            starts = sorted(grp.box[0] for grp in groups)
            pivot = starts[len(starts) // 2]
            left = [grp for grp in groups if grp.box[0] < pivot]
            right = [grp for grp in groups if grp.box[0] >= pivot]
            if not left or not right:
                left, right = [groups[0]], groups[1:]
            lx1 = max(grp.box[2] for grp in left)
            rx1 = max(grp.box[2] for grp in right)
            dx = (lx1 + self.opt.group_gap) - min(grp.box[0] for grp in right)
            if 0 < dx <= (rx1 - min(grp.box[0] for grp in right)) * 0.8 + 1200:
                for grp in right:
                    shift(grp, dx, 0.0)
                continue
            # 最后一招：逐个把压人家的那个块往右让开一点点
            for a, b in pairs:
                low = a if a.box[0] >= b.box[0] else b
                hi = b if low is a else a
                need = (hi.box[2] + self.opt.group_gap) - low.box[0]
                if 0 < need <= 3000.0:
                    shift(low, need, 0.0)
                    break

        # 丢掉空壳框，并按最终成员位置重算边界
        keep: List[Group] = []
        pad = self.opt.group_pad
        for grp in g.groups:
            ms = members.get(grp.id) or []
            if not ms:
                continue
            grp.bounding = (min(n.pos[0] for n in ms) - pad,
                            min(n.pos[1] for n in ms) - pad - 26,
                            max(n.right for n in ms) - min(n.pos[0] for n in ms) + pad * 2,
                            max(n.bottom for n in ms) - min(n.pos[1] for n in ms) + pad * 2 + 26)
            keep.append(grp)
        g.groups = keep

    @staticmethod
    def _overlap(a, b, slack: float = 0.0) -> bool:
        return not (a[2] + slack <= b[0] or b[2] + slack <= a[0] or
                    a[3] + slack <= b[1] or b[3] + slack <= a[1])

    def _refresh_group_bounds(self) -> None:
        """消重叠会挪节点，这里把框重新贴回成员身上，保证框与内容永远一致。"""
        pad = self.opt.group_pad
        for grp in self.g.groups:
            ms = getattr(grp, "_members", None) or []
            if not ms:
                continue
            x0 = min(n.pos[0] for n in ms) - pad
            y0 = min(n.pos[1] for n in ms) - pad - 26
            x1 = max(n.right for n in ms) + pad
            y1 = max(n.bottom for n in ms) + pad
            grp.bounding = (x0, y0, x1 - x0, y1 - y0)

    def _relabel_groups(self) -> None:
        """同名分区加序号，便于人读。"""
        seen: Dict[str, int] = {}
        for grp in self.g.groups:
            seen[grp.title] = seen.get(grp.title, 0) + 1
        counts: Dict[str, int] = {}
        for grp in self.g.groups:
            if seen[grp.title] > 1:
                counts[grp.title] = counts.get(grp.title, 0) + 1
                grp.title = f"{grp.title} {counts[grp.title]}"

    # ------------------------------------------------ 收尾
    def _normalize(self) -> None:
        g = self.g
        x0, y0, _, _ = g.bounds()
        g.translate(self.opt.margin - x0, self.opt.margin - y0)

    def _record_routes(self) -> None:
        """把每条长边的中间路径点记下来，供几何自检按真实走线判重。"""
        _LAST_ROUTES.clear()
        for e in self.edges:
            a, b = self.verts.get(e.a), self.verts.get(e.b)
            if a is None or b is None or a.node is None or b.node is None:
                continue
            chain = sorted([v for v in self.verts.values()
                            if v.dummy and e.segs and v.chain == e.segs[0]],
                           key=lambda v: v.layer)
            _LAST_ROUTES[(a.node.id, b.node.id)] = [(v.cx, v.cy) for v in chain]

    def _verify(self) -> None:
        """自检：节点重叠算硬伤；分区框轻微压边只算提醒（视觉上无妨）。"""
        g = self.g
        real = [n for n in g.nodes if not n.is_note]
        for i in range(len(real)):
            for j in range(i + 1, len(real)):
                a, b = real[i], real[j]
                if self._overlap(a.box, b.box, -1.0):
                    self.report.issues.append(
                        f"节点 #{a.id}({a.type}) 与 #{b.id}({b.type}) 位置重叠")
        soft = 0
        for i in range(len(g.groups)):
            for j in range(i + 1, len(g.groups)):
                if self._overlap(g.groups[i].box, g.groups[j].box, -1.0):
                    soft += 1
        self.report.group_overlaps = soft
        if len(self.report.issues) > 12:
            extra = len(self.report.issues) - 12
            self.report.issues = self.report.issues[:12] + [f"...还有 {extra} 处"]


def count_node_overlaps(graph: Graph) -> List[Tuple[int, int]]:
    """返回所有**互相重叠**的节点对，形如 ``[(id_a, id_b), ...]``。

    这是全工具链唯一的硬版式指标 —— 线交叉可以忍，节点框叠在一起不行。
    `layout()` 和 `place` 交出去的图必须让这里返回空表。
    """
    ns = [n for n in graph.nodes if n.width > 0 and n.height > 0]
    bad: List[Tuple[int, int]] = []
    for i in range(len(ns)):
        a = ns[i]
        for j in range(i + 1, len(ns)):
            b = ns[j]
            if Layouter._overlap(a.box, b.box, 0.0):
                bad.append((a.id, b.id))
    return bad


def resolve_overlaps(graph: Graph, opts: Optional[LayoutOptions] = None) -> int:
    """消掉所有节点重叠，返回一共推开了几对。

    手工摆位（`cwf place`）之后的**兜底保证**：不管调用方给的坐标多离谱，
    交出去之前必须没有一个节点压着另一个。这是工具的责任，不该丢给用户
    回 ComfyUI 里手拖 —— 重叠是唯一"看见就必须手动修"的版式问题。

    只动 `pos`，不动接线、不动控件值；推的方向是同 x 簇内往下顺排。
    """
    lay = Layouter(graph, opts or LayoutOptions())
    before = lay._overlap_count()
    if before == 0:
        return 0
    lay._resolve_overlaps()
    return before - lay._overlap_count()


def regroup(graph: Graph, opts: Optional[LayoutOptions] = None,
            separate: bool = False) -> int:
    """**只**按当前坐标重算语义分区框，默认一个节点都不挪。

    手工摆完位置（`cwf place`）之后必须重算分区框 —— 否则旧框还留在
    原地，会把不相干的节点圈进去。这一步与自动排版里的分区逻辑完全同源，
    所以两条路径出来的框长得一样。

    ⚠ `separate=False` 是**必须的默认值**。`_rebuild_groups` 最后会调
    `_separate_groups()` 去把互相压住的分区框推开，而那一步是通过**平移节点**
    实现的 —— 于是"只重算框"的名义下，用户明确指定的坐标被悄悄改掉了
    （实测 `#4=100,100` 落成了 `100,220`）。手工摆位时坐标是用户说了算的，
    框压边就压边，不能反过来动人家摆好的节点。
    """
    lay = Layouter(graph, opts or LayoutOptions())
    lay._rebuild_groups(separate=separate)
    return len(graph.groups)


def layout(graph: Graph, opts: Optional[LayoutOptions] = None,
           reg: Optional[Registry] = None) -> LayoutReport:
    """排一张图。

    收尾处对「节点不重叠」再做一次**兜底保证**：流水线内部已经消过一次，
    但那个消解器在"推了一轮没进展"时会提前收手（避免死循环），少数畸形图
    （节点 id 是字符串、孤立件特别多之类）会带着几处重叠出来。
    用户对这条的要求很明确 —— **连线交叉无所谓，节点框压在一起不行** ——
    所以这里不接受"尽力了"，交出去必须是 0。
    """
    rep = Layouter(graph, opts, reg).run()
    fixed = resolve_overlaps(graph, opts)
    if fixed:
        rep.overlaps_fixed = fixed
    return rep


#: 最近一次布局留下的「长边中间路径点」：节点对 -> [(x, y), ...]
#: 几何自检必须按真实走线判重，只按两点直线判会量出一个假问题。
_LAST_ROUTES: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}


# ================================================================ 几何自检
#
# 上面 report.crossings 数的是「层内相邻两层的顺序交叉」，那是排版过程中的
# 启发式指标，**不是**画布上看起来乱不乱。真正决定观感的是实际走线：
#   线段之间有没有真的相交？有没有从无关节点身上横穿过去？
# 下面这组函数按 ComfyUI 前端真实的正交路由（端口在节点左右边缘、走折线）
# 复算一遍，测试拿它当硬指标。

def _port_anchor(node: Node, idx: int, is_input: bool) -> Tuple[float, float]:
    """端口在画布上的位置。ComfyUI 的端口在左/右**边缘**，不是节点中心。"""
    y = node.pos[1] + 30.0 + 10.0 + idx * 20.0
    y = min(max(y, node.pos[1] + 18.0), node.bottom - 4.0)
    return ((node.pos[0] if is_input else node.right), y)


def _slot_index(node: Node, slot: int, is_input: bool) -> int:
    n = len(node.inputs) if is_input else len(node.outputs)
    return max(0, min(slot, n - 1)) if n > 0 else 0


def _col_bounds(graph: Graph):
    """按 x 把节点聚成列，返回 [(列左, 列右, 列 x), ...]（按 x 排序）。"""
    buckets: Dict[int, List[Node]] = {}
    for n in graph.nodes:
        if n.is_note:
            continue
        buckets.setdefault(int(round(n.pos[0])), []).append(n)
    return [(min(m.pos[0] for m in ms), max(m.right for m in ms), cx)
            for cx, ms in sorted(buckets.items())]


def route_edges(graph: Graph):
    """正交路由：端口在节点左/右边缘，走向单调（只往右）。

    返回 [(源(节点,槽), 目标(节点,槽), [拐点...]), ...]

    两种走法，按边的跨度自动选：
      * **相邻列**的边：在正中间折一次（拐点最少，成对时两线居中对称，最干净）；
      * **跨多列**的长边：竖着走时贴到目标列左侧的专用通道里，而不是横穿
        中间列 —— 这是「连线从节点身上穿过去」的主因，让开就好了。
    """
    cols = _col_bounds(graph)
    if not cols:
        return []
    boxes = [(x.id, x.box) for x in graph.nodes if not x.is_note]

    def col_of(x: float) -> int:
        best, bd = 0, None
        for i, (l, r, cx) in enumerate(cols):
            d = 0.0 if l - 8 <= x <= r + 8 else min(abs(x - l), abs(x - r))
            if bd is None or d < bd:
                best, bd = i, d
        return best

    # 每个间隙里给「长边通道」预留的 x（贴右列左侧）
    def chan_x(k: int, jitter: float = 0.0) -> float:
        l = cols[k - 1][1] if k > 0 else cols[0][0] - 140.0
        r = cols[k][0] if k < len(cols) else cols[-1][1] + 140.0
        room = r - l
        if room <= 12.0:
            return r - 6.0 - jitter
        return r - min(24.0, room * 0.35) - jitter

    out = []
    for l in graph.links:
        o, t = graph.maybe(l.origin_id), graph.maybe(l.target_id)
        if o is None or t is None or o.is_note or t.is_note:
            continue
        p = _port_anchor(o, _slot_index(o, l.origin_slot, False), False)
        q = _port_anchor(t, _slot_index(t, l.target_slot, True), True)
        if q[0] < p[0] + 20.0:                       # 回环：绕到右边折回来
            detour = max(o.right, t.right) + 46.0
            up = min(p[1], q[1]) - 34.0
            out.append(((o.id, l.origin_slot), (t.id, l.target_slot),
                        _dedupe_pts([p, (detour, p[1]), (detour, up),
                                     (q[0] - 34.0, up), (q[0] - 34.0, q[1]), q])))
            continue
        mids = _LAST_ROUTES.get((o.id, t.id))
        if mids:
            # 走虚节点给的位置 —— 这是分层算法算出来的「该从哪儿过」，
            # 比临时取中点靠谱：排序一变，通道跟着变，两者是自洽的。
            pts = [p]
            for mx, my in mids:
                pts.append((pts[-1][0], my))
                pts.append((mx, my))
            pts.append((pts[-1][0], q[1]))
            pts.append(q)
            out.append(((o.id, l.origin_slot), (t.id, l.target_slot), _dedupe_pts(pts)))
            continue
        ci, cj = col_of(p[0]), col_of(q[0])
        if cj - ci <= 1:
            # 相邻列：正中间折一次
            mid = (p[0] + q[0]) / 2.0
            y0, y1 = sorted((p[1], q[1]))
            hit = next((b for nid, b in boxes
                        if nid not in (o.id, t.id)
                        and b[0] - 6 <= mid <= b[2] + 6
                        and not (b[3] < y0 or b[1] > y1)), None)
            if hit:
                det = hit[3] + 18.0
                out.append(((o.id, l.origin_slot), (t.id, l.target_slot),
                            _dedupe_pts([p, (mid, p[1]), (mid, det),
                                         (q[0] - 10, det), (q[0] - 10, q[1]), q])))
            else:
                out.append(((o.id, l.origin_slot), (t.id, l.target_slot),
                            _dedupe_pts([p, (mid, p[1]), (mid, q[1]), q])))
        else:
            # 跨多列：先在自己这列右侧起步，再进目标列左侧的专用通道
            ch = chan_x(cj)
            body = (cols[cj][0] + cols[cj][1]) / 2.0
            tx = q[0] - 10.0
            # 通道高度避开目标列上的节点
            yy = q[1]
            for nid, (bx0, by0, bx1, by1) in boxes:
                if nid in (o.id, t.id):
                    continue
                if bx0 - 8 <= ch <= bx1 + 8 and by0 - 8 <= yy <= by1 + 8:
                    yy = by1 + 18.0
            out.append(((o.id, l.origin_slot), (t.id, l.target_slot),
                        _dedupe_pts([p, (ch, p[1]), (ch, yy), (tx, yy), (tx, q[1]), q])))
    return out


def _dedupe_pts(pts):
    """去掉折线里重复/极近的点，避免退化成零长线段干扰几何判定。"""
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > 0.5 or abs(p[1] - out[-1][1]) > 0.5:
            out.append(p)
    return out


def _seg_cross(p1, p2, p3, p4) -> bool:
    """两条线段是否真的相交（共线/端点相接不算，那正是「接线」本身）。"""
    def o(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)

    d1, d2 = o(p3, p4, p1), o(p3, p4, p2)
    d3, d4 = o(p1, p2, p3), o(p1, p2, p4)
    return d1 != d2 and d3 != d4


def _seg_hits_box(p, q, box, pad: float = 3.0) -> bool:
    x0, y0, x1, y1 = box[0] + pad, box[1] + pad, box[2] - pad, box[3] - pad
    if x1 <= x0 or y1 <= y0:
        return False
    if max(p[0], q[0]) < x0 or min(p[0], q[0]) > x1:
        return False
    if max(p[1], q[1]) < y0 or min(p[1], q[1]) > y1:
        return False
    if x0 <= p[0] <= x1 and y0 <= p[1] <= y1:
        return True
    for c1, c2 in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                   ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        if _seg_cross(p, q, c1, c2):
            return True
    return False


def count_geometric_crossings(graph: Graph, *_a, **_kw) -> int:
    """画布上真实相交的线段对数。

    全部是正交折线（水平/竖直），所以用**扫描线**算：按 x 从左到右扫，
    维护当前活跃的竖直段，遇到水平段就数它穿过了几根竖线。
    复杂度 O(E log E)，比两两比较快几个数量级。
    """
    segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for _s, _t, poly in route_edges(graph):
        segs.extend(zip(poly, poly[1:]))
    verts: Dict[float, List[Tuple[float, float]]] = {}
    hors: List[Tuple[float, float, float]] = []
    for (x1, y1), (x2, y2) in segs:
        if abs(x1 - x2) < 0.5 and abs(y1 - y2) > 0.5:        # 竖直
            verts.setdefault(round(x1, 2), []).append((min(y1, y2), max(y1, y2)))
        elif abs(y1 - y2) < 0.5 and abs(x1 - x2) > 0.5:      # 水平
            hors.append((min(x1, x2), max(x1, x2), round(y1, 2)))

    total = 0
    xs = sorted(verts)
    import bisect
    for hx0, hx1, hy in hors:
        lo = bisect.bisect_right(xs, hx0 + 0.5)
        hi = bisect.bisect_left(xs, hx1 - 0.5)
        for xi in xs[lo:hi]:
            for vy0, vy1 in verts[xi]:
                if vy0 + 0.5 < hy < vy1 - 0.5:               # 严格穿过，端点相接不算
                    total += 1
    return total


def edges_through_nodes(graph: Graph, *_a, **_kw) -> List[str]:
    """哪些连线的线段穿过了无关节点框。"""
    boxes = [(x.id, x.box) for x in graph.nodes if not x.is_note]
    bad = []
    for (aid, aslot), (bid, bslot), poly in route_edges(graph):
        hit = None
        for p, q in zip(poly, poly[1:]):
            for nid, box in boxes:
                if nid in (aid, bid):
                    continue
                if _seg_hits_box(p, q, box):
                    hit = nid
                    break
            if hit:
                break
        if hit:
            bad.append(f"{aid}.{aslot} → {bid}.{bslot} 穿过 #{hit}")
    return bad


def layout_quality(graph: Graph) -> Dict[str, Any]:
    """一份可读的排版质量报告（给 cwf layout 和测试共用）。"""
    return {
        "geometric_crossings": count_geometric_crossings(graph),
        "edges_through_nodes": len(edges_through_nodes(graph)),
        "collisions": len(edges_through_nodes(graph)),
    }
