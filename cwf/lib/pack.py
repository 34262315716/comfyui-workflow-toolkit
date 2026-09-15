# -*- coding: utf-8 -*-
"""
cwf.pack —— 工作流模块化拆装（排列组合）

一个「模块」= 从某张工作流里切下来的一小块子图 + 它的对外接口。
存成小 JSON 之后，可以反复塞进别的工作流，接口按类型自动对接。

模块 JSON 长这样：
{
  "cwf_pack": 1,
  "name": "采样区",
  "description": "...",
  "source": "H3/xxx.json",
  "inputs":  [{"name": "model", "type": "MODEL", "node": 12, "slot": "model"}],
  "outputs": [{"name": "LATENT", "type": "LATENT", "node": 15, "slot": 0}],
  "nodes":   [ ...完整节点 JSON，坐标已归一化... ],
  "links":   [ ...只含模块内部连线... ]
}
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .graph import CwfError, Graph, Group, Link, Node, Slot

PACK_VERSION = 1


# ---------------------------------------------------------------- 选择器


def parse_node_selector(sel: str, g: Graph) -> List[Node]:
    """支持 `#1,#2` / `10-30` / `标题子串` / `Type:KSampler` / `标题~正则`。"""
    picked: List[Node] = []
    for part in re.split(r"[,\s]+", sel.strip()):
        if not part:
            continue
        part = part.strip()
        if part.startswith("#"):
            picked.append(g.node(int(part[1:])))
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            picked.extend(n for n in g.nodes if lo <= n.id <= hi)
            continue
        if ":" in part:
            kind, _, val = part.partition(":")
            if kind in ("type", "类型"):
                picked.extend(g.by_type(val))
                continue
        if part.startswith("~"):
            pat = re.compile(part[1:])
            picked.extend(n for n in g.nodes if pat.search(n.title or ""))
            continue
        hits = g.by_title(part)
        if not hits:
            hits = [n for n in g.nodes if part.lower() in n.type.lower()]
        if not hits:
            raise CwfError(f"选择器 {part!r} 没匹配到任何节点")
        picked.extend(hits)
    seen = set()
    out = []
    for n in picked:
        if n.id not in seen:
            seen.add(n.id)
            out.append(n)
    return out


def expand_closure(g: Graph, seeds: Sequence[Node], mode: str = "strict") -> List[Node]:
    """把选择范围扩成一个「好使的」子图。

    strict: 只保留 seed 之间互相连通的节点（默认）
    up    : 连上所有上游依赖
    down  : 连上所有下游消费者
    """
    ids = {n.id for n in seeds}
    if mode == "strict":
        return list(seeds)
    seen = set(ids)
    frontier = list(ids)
    while frontier:
        nid = frontier.pop()
        nbrs = (g.incoming(nid) if mode == "up" else g.outgoing(nid))
        for l in nbrs:
            other = l.origin_id if mode == "up" else l.target_id
            if other not in seen:
                seen.add(other)
                frontier.append(other)
    return [g.node(i) for i in sorted(seen)]


# ---------------------------------------------------------------- 切分


def split_pack(args, path: Optional[str] = None) -> Dict[str, Any]:
    if path is None:                       # 独立调用时才自己解析（会走 cli 的路径规则）
        from ..cli import resolve_workflow
        path = resolve_workflow(args.workflow)
    g = Graph.load(path)
    seeds = parse_node_selector(args.nodes, g)
    mode = getattr(args, "closure", None) or "strict"
    members = expand_closure(g, seeds, mode)
    mset = {n.id for n in members}
    if not mset:
        raise CwfError("选出来的节点是空的")
    notes = [n for n in members if n.is_note]
    members = [n for n in members if not n.is_note]
    mset = {n.id for n in members}

    # 内部连线
    inner = [l for l in g.links if l.origin_id in mset and l.target_id in mset]
    # 对外接口
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    for l in g.links:
        if l.target_id in mset and l.origin_id not in mset:
            tgt = g.node(l.target_id)
            slot = tgt.inputs[l.target_slot] if l.target_slot < len(tgt.inputs) else None
            name = slot.name if slot else f"in{l.target_slot}"
            used = {i["name"] for i in inputs}
            nm = name
            k = 2
            while nm in used:
                nm = f"{name}_{k}"
                k += 1
            inputs.append({"name": nm, "type": (slot.type if slot else "*"),
                           "node": l.target_id, "slot": l.target_slot,
                           "desc": f"{tgt.title or tgt.type}.{name}"})
        if l.origin_id in mset and l.target_id not in mset:
            src = g.node(l.origin_id)
            slot = src.outputs[l.origin_slot] if l.origin_slot < len(src.outputs) else None
            name = slot.name if slot else f"out{l.origin_slot}"
            used = {o["name"] for o in outputs}
            nm = name
            k = 2
            while nm in used:
                nm = f"{name}_{k}"
                k += 1
            outputs.append({"name": nm, "type": (slot.type if slot else "*"),
                            "node": l.origin_id, "slot": l.origin_slot,
                            "desc": f"{src.title or src.type}.{name}"})

    # 归一化坐标
    x0 = min(n.pos[0] for n in members)
    y0 = min(n.pos[1] for n in members)
    off = (-x0, -y0)
    nodes_json = []
    for n in sorted(members, key=lambda z: z.id):
        d = n.to_json()
        d["pos"] = [round(n.pos[0] + off[0], 2), round(n.pos[1] + off[1], 2)]
        n2 = Node.from_json(d)
        n2.pos = (d["pos"][0], d["pos"][1])
        nodes_json.append(n2.to_json())

    note_json = []
    for n in notes:
        d = n.to_json()
        d["pos"] = [round(n.pos[0] + off[0], 2), round(n.pos[1] + off[1], 2)]
        note_json.append(d)

    bounds = g.bounds()
    return {
        "cwf_pack": PACK_VERSION,
        "name": args.name,
        "description": getattr(args, "description", "") or "",
        "source": os.path.basename(path),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inputs": inputs,
        "outputs": outputs,
        "nodes": nodes_json,
        "links": [l.to_json() for l in inner],
        "notes": note_json,
        "source_size": [round(bounds[2] - bounds[0]), round(bounds[3] - bounds[1])],
    }


# ---------------------------------------------------------------- 存取


def pack_dir(root: str) -> str:
    return os.path.abspath(root)


def save_pack(spec: Dict[str, Any], root: str) -> str:
    d = pack_dir(root)
    os.makedirs(d, exist_ok=True)
    name = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", spec["name"]).strip("_") or "pack"
    path = os.path.join(d, name + ".cwfpack.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    return path


def load_pack(name: str, root: str) -> Dict[str, Any]:
    d = pack_dir(root)
    cand = os.path.join(d, name)
    if os.path.exists(cand):
        path = cand
    elif os.path.exists(cand + ".cwfpack.json"):
        path = cand + ".cwfpack.json"
    else:
        hits = [p for p in os.listdir(d) if p.endswith(".cwfpack.json")
                and name.lower() in p.lower()] if os.path.isdir(d) else []
        if len(hits) == 1:
            path = os.path.join(d, hits[0])
        elif not hits:
            raise CwfError(f"模块库里没有 {name!r}（目录 {d}）")
        else:
            raise CwfError(f"{name!r} 匹配到多个模块：{hits}")
    with open(path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    if spec.get("cwf_pack") != PACK_VERSION:
        raise CwfError(f"{path} 的模块版本是 {spec.get('cwf_pack')}，"
                       f"本工具只认 {PACK_VERSION}")
    spec["_path"] = path
    return spec


def list_packs(root: str) -> List[Dict[str, Any]]:
    d = pack_dir(root)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".cwfpack.json"):
            continue
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                spec = json.load(f)
        except Exception:
            continue
        out.append({
            "name": spec.get("name", fn),
            "description": spec.get("description", ""),
            "nodes": len(spec.get("nodes") or []),
            "inputs": len(spec.get("inputs") or []),
            "outputs": len(spec.get("outputs") or []),
            "source": spec.get("source", ""),
            "file": os.path.join(d, fn),
        })
    return out


# ---------------------------------------------------------------- 实例化


def instantiate(g: Graph, name: str, root: str, alias_prefix: Optional[str] = None,
                prefix: Optional[str] = None,
                rename: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """把模块塞进一张图，返回新节点 id、接口别名。"""
    spec = load_pack(name, root)
    prefix = prefix if prefix is not None else spec["name"]
    base_alias = alias_prefix or re.sub(r"[^\w\u4e00-\u9fff]+", "", prefix) or "m"

    # 新 id 映射
    id_map: Dict[int, int] = {}
    for d in spec["nodes"]:
        old = int(d["id"])
        id_map[old] = g.next_id
        g.next_id += 1
    link_map: Dict[int, int] = {}
    for l in spec.get("links") or []:
        old = int(l[0])
        link_map[old] = g.next_link
        g.next_link += 1

    # 模块整体落在当前画布右下方
    if g.nodes:
        x0, y0, x1, _ = g.bounds()
        base = (x1 + 160.0, y0)
    else:
        base = (0.0, 0.0)

    created: List[int] = []
    for d in spec["nodes"]:
        d = copy.deepcopy(d)
        old_id = int(d["id"])
        d["id"] = id_map[old_id]
        # 连线 id 重映射
        for s in d.get("inputs") or []:
            if s.get("link") is not None and s["link"] in link_map:
                s["link"] = link_map[s["link"]]
            elif s.get("link") is not None:
                s["link"] = None
        for s in d.get("outputs") or []:
            lk = s.get("links")
            if isinstance(lk, list):
                s["links"] = [link_map[x] for x in lk if x in link_map] or None
        pos = d.get("pos") or [0, 0]
        d["pos"] = [round(base[0] + float(pos[0]), 2), round(base[1] + float(pos[1]), 2)]
        if prefix:
            d["title"] = f"{prefix}·{d.get('title') or d.get('type')}"
        node = Node.from_json(d)
        g.nodes.append(node)
        created.append(node.id)
    g.reindex()
    # 内部连线
    for l in spec.get("links") or []:
        l = list(l)
        l[0] = link_map[int(l[0])]
        l[1] = id_map[int(l[1])]
        l[3] = id_map[int(l[3])]
        g.links.append(Link(int(l[0]), int(l[1]), int(l[2]), int(l[3]), int(l[4]),
                            l[5] if len(l) > 5 else "*"))
    # 注释
    for d in spec.get("notes") or []:
        d = copy.deepcopy(d)
        d["id"] = g.next_id
        g.next_id += 1
        pos = d.get("pos") or [0, 0]
        d["pos"] = [round(base[0] + float(pos[0]), 2), round(base[1] + float(pos[1]), 2)]
        g.nodes.append(Node.from_json(d))
    g.reindex()

    # 接口别名
    ren = {}
    for r in (rename or []):
        k, _, v = r.partition("=")
        ren[k.strip()] = v.strip()
    ii = []
    for i in spec.get("inputs", []):
        nm = ren.get(i["name"], i["name"])
        alias = f"{base_alias}.{nm}"
        node = g.node(id_map[int(i["node"])])
        setattr(node, "_pack_input", None)
        ii.append({"alias": alias, "name": nm, "type": i["type"],
                   "node": node.id, "slot": i["slot"],
                   "node_type": node.type, "desc": i.get("desc", "")})
    oo = []
    for o in spec.get("outputs", []):
        nm = ren.get(o["name"], o["name"])
        alias = f"{base_alias}.{nm}"
        node = g.node(id_map[int(o["node"])])
        oo.append({"alias": alias, "name": nm, "type": o["type"],
                   "node": node.id, "slot": o["slot"],
                   "node_type": node.type, "desc": o.get("desc", "")})
    g.reorder()
    return {"nodes": created, "prefix": prefix, "aliases": {i["alias"]: i for i in ii},
            "inputs": ii, "outputs": oo, "pack": spec["name"], "path": spec["_path"]}
