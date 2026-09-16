# -*- coding: utf-8 -*-
"""
cwf —— ComfyUI 工作流读写 / 拼装 / 排版工具（供 AI agent 调用）

设计取向：
  * 每条命令都能吐出「人读文本」和 `--json` 两种结果，agent 拿 JSON、人拿文本；
  * 所有动作默认不覆盖原文件（`--out` 不写就直接打印），避免手滑毁工作流；
  * 报错说人话，并给出下一步该敲什么命令。
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import shutil
import statistics
import sys
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

if __package__ in (None, ""):                       # 允许直接 python cli.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "cwf"

from cwf.lib import strata
from cwf.lib.dsl import parse as dsl_parse
from cwf.lib.dsl import parse_value
from cwf.lib.graph import (CwfError, Graph, Group, Link, Node, Slot,
                           VIRTUAL_GET, VIRTUAL_SET, is_frontend_only, is_note_type)
from cwf.lib.schema import (DEFAULT_SERVER, Registry, estimate_size,
                            load_object_info, registry, size_of)
from cwf.lib import pack as packmod
from cwf.lib import paths
from cwf.lib import rig

VERSION = "1.0.0"

WF_DEFAULT = paths.default_workflow_root()
HUB = os.environ.get("CWF_HUB", paths.tool_home())


# ---------------------------------------------------------------- 输出


class Out:
    """统一的输出口。--json 时只吐结构化数据，平时吐人话。"""

    def __init__(self, as_json: bool = False, quiet: bool = False):
        self.json = as_json
        self.quiet = quiet
        self.data: Dict[str, Any] = {}

    def say(self, *parts: Any) -> None:
        if not self.json:
            print(*parts)

    def raw(self, text: str = "") -> None:
        if not self.json:
            print(text)

    def put(self, key: str, val: Any) -> None:
        self.data[key] = val

    def finish(self, ok: bool = True) -> None:
        if self.json:
            self.data["ok"] = ok
            print(json.dumps(self.data, ensure_ascii=False, indent=2, default=str))


def die(msg: str, code: int = 2) -> "NoReturn":     # type: ignore
    print(f"✖ {msg}", file=sys.stderr)
    raise SystemExit(code)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


# ---------------------------------------------------------------- 路径解析


def resolve_workflow(arg: str, must_exist: bool = True) -> str:
    r"""把用户给的「工作流名」解析成真实路径。

    支持：绝对/相对路径、相对工作流根目录的路径、唯一子串（模糊匹配）。
    正反斜杠都认 —— `ws list` 输出的是 `子目录\xxx.json`，但 agent 习惯写
    `子目录/xxx.json`，两者必须都能用。
    """
    if not arg:
        raise CwfError("没给工作流名字")
    raw = str(arg).strip().strip('"').strip("'")
    p = os.path.expanduser(raw)
    norm = p.replace("/", os.sep).replace("\\", os.sep)
    looks_like_path = os.path.isabs(norm) or ("/" in p) or ("\\" in p)
    cands: List[str] = []
    if looks_like_path:
        cands.append(norm if norm.lower().endswith(".json") else norm + ".json")
    else:
        cands.append(os.path.join(WF_DEFAULT, p))
        cands.append(os.path.join(WF_DEFAULT, p + ".json"))
    for c in cands:
        if os.path.exists(c):
            return os.path.abspath(c)
    # 用户可能连工作流库根目录也写进来了（或用 / 分隔）—— 去掉前缀再试一次
    for prefix in (WF_DEFAULT, WF_DEFAULT.replace("\\", "/")):
        if raw.startswith(prefix):
            rel = raw[len(prefix):].lstrip("/\\")
            c = os.path.join(WF_DEFAULT, rel.replace("/", os.sep))
            if os.path.exists(c):
                return os.path.abspath(c)
    # 模糊匹配（用文件名去掉 .json 再搜，命中率最高）
    for needle in (raw, os.path.splitext(os.path.basename(raw))[0], os.path.basename(raw)):
        hits = search_workflows(needle, limit=5)
        if len(hits) == 1:
            return hits[0][0]
        if hits:
            raise CwfError(
                f"{arg!r} 匹配到多个工作流，请写得更具体：\n  " +
                "\n  ".join(os.path.relpath(h, WF_DEFAULT) for h, _ in hits))
    raise CwfError(f"找不到工作流 {arg!r}（找过 {cands[0]}）")


def search_workflows(needle: str, limit: int = 30) -> List[Tuple[str, float]]:
    needle_low = needle.lower()
    out: List[Tuple[str, float]] = []
    for p in globmod.glob(os.path.join(WF_DEFAULT, "**", "*.json"), recursive=True):
        base = os.path.basename(p)
        rel = os.path.relpath(p, WF_DEFAULT)
        low = rel.lower()
        if needle_low in low:
            score = 0.0
            if os.path.splitext(base)[0] == needle or rel == needle:
                score = 100.0
            elif needle_low in base.lower():
                score = 50.0
            else:
                score = 10.0
            score += max(0.0, 20.0 - len(rel) / 20.0)
            try:
                score += min(5.0, os.path.getmtime(p) / 1e10)
            except OSError:
                pass
            out.append((p, score))
    out.sort(key=lambda kv: -kv[1])
    return out[:limit]


def out_path_for(args, default_dir: Optional[str] = None) -> Optional[str]:
    return getattr(args, "out", None)


# ---------------------------------------------------------------- 通用渲染


def fmt_node_line(n: Node, reg: Registry, show_widgets: bool = True) -> str:
    tag = "" if n.mode == 0 else ("  [旁路]" if n.mode == 4 else "  [静音]")
    head = f"#{n.id:<5} {n.type:<34} {n.title or ''}{tag}"
    if not show_widgets:
        return head.rstrip()
    bits = []
    try:
        for k, v in n.widget_pairs():
            if k in ("videopreview",):
                continue
            sv = str(v)
            if len(sv) > 36:
                sv = sv[:33] + "..."
            bits.append(f"{k}={sv}")
    except Exception:
        pass
    return (head.rstrip() + ("  |  " + " ".join(bits) if bits else "")).rstrip()


def render_tree(g: Graph, out: Out, show_detail: bool = True) -> None:
    st = g.summary()
    out.raw(f"节点 {st['nodes']}（有效 {st['real_nodes']}，注释 {st['notes']}）"
            f" · 连线 {st['links']} · 分区框 {st['groups']}"
            f" · 旁路 {st['bypassed']} · 静音 {st['muted']}")
    x0, y0, x1, y1 = g.bounds()
    out.raw(f"画布范围 {x1 - x0:.0f} × {y1 - y0:.0f}  "
            f"平均每节点 {(x1 - x0) * (y1 - y0) / max(1, st['nodes']):.0f} px²")
    if g.groups:
        out.raw("分区：" + "、".join(gr.title for gr in g.groups))
    if not show_detail:
        return
    out.raw("")
    for n in sorted(g.nodes, key=lambda z: (z.pos[0], z.pos[1])):
        out.raw("  " + fmt_node_line(n, reg=None))  # type: ignore


# ================================================================ 子命令


# ---------------- ws / schema


def cmd_ws_list(args, out: Out) -> None:
    rows = []
    root = args.root or WF_DEFAULT
    for p in globmod.glob(os.path.join(root, "**", "*.json"), recursive=True):
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append({
            "path": p,
            "rel": os.path.relpath(p, root),
            "dir": os.path.dirname(os.path.relpath(p, root)) or ".",
            "name": os.path.splitext(os.path.basename(p))[0],
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    rows.sort(key=lambda r: -r["mtime"])
    if args.match:
        low = args.match.lower()
        rows = [r for r in rows if low in r["rel"].lower()]
    if args.dir:
        rows = [r for r in rows if r["dir"].lower().startswith(args.dir.lower())]
    if args.limit:
        rows = rows[:args.limit]
    out.put("root", root)
    out.put("count", len(rows))
    out.put("workflows", rows)
    out.raw(f"{root}  共 {len(rows)} 个")
    out.raw("")
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["mtime"]))
        out.raw(f"  {when}  {human_size(r['size']):>7}  {r['rel']}")


def cmd_ws_info(args, out: Out) -> None:
    path = resolve_workflow(args.workflow)
    g = Graph.load(path)
    st = g.summary()
    out.put("path", path)
    out.put("summary", st)
    out.raw(f"文件: {path}")
    out.raw(f"大小: {human_size(os.path.getsize(path))}")
    out.raw(f"节点 {st['nodes']} / 连线 {st['links']} / 分区 {st['groups']} / "
            f"旁路 {st['bypassed']}")
    out.raw("高频节点: " + "、".join(f"{t}×{c}" for t, c in st["top_types"][:8]))
    term = g.terminals()
    if term:
        out.raw("终结点: " + "、".join(f"#{n.id} {n.title or n.type}" for n in term[:6]))
    orphan = g.orphans()
    if orphan:
        out.raw(f"⚠ 孤儿节点 {len(orphan)} 个（没接线）: " +
                "、".join(f"#{n.id} {n.type}" for n in orphan[:6]))


def cmd_ws_resolve(args, out: Out) -> None:
    """不做任何修改，只回答「这个工作流里有什么、缺什么」。"""
    g = _load(args, out)
    reg = _reg(args)
    missing = _missing_types(g, reg)
    out.put("missing_node_types", missing)
    out.put("summary", g.summary())
    if missing:
        out.raw(f"⚠ 有 {len(missing)} 种节点类型在本机 ComfyUI 里找不到：")
        for t, c in missing:
            out.raw(f"    {t}×{c}")
        out.raw("  （节点没装 → 工作流打不开；也可能是版本不一致改了名）")
    else:
        out.raw(f"✓ {len(g)} 个节点的类型在本机 ComfyUI 里全部存在")
    models = _missing_models(g, reg)
    if models:
        out.raw("")
        out.raw(f"⚠ 引用了 {len(models)} 个本机没有的模型文件：")
        for m, (cnt, who) in sorted(models.items())[:40]:
            out.raw(f"    {m}   (用在 {who})")
    else:
        out.raw("✓ 引用的模型文件名都在本机模型库里")
    if args.json:
        out.put("missing_models", {k: v[0] for k, v in models.items()})


def _missing_types(g: Graph, reg: Registry) -> List[Tuple[str, int]]:
    if not len(reg):
        return []
    bad: Dict[str, int] = {}
    for n in g.nodes:
        if n.type in reg or is_frontend_only(n.type) or is_note_type(n.type):
            continue
        # aux_id 指向某个插件包 → 装过，只是没有后端节点（前端 JS 注册）
        aux = str((n.properties or {}).get("aux_id") or "")
        if aux:
            continue
        bad[n.type] = bad.get(n.type, 0) + 1
    return sorted(bad.items(), key=lambda kv: -kv[1])


MODEL_WIDGETS = {
    "ckpt_name": "checkpoints", "lora_name": "loras", "vae_name": "vae",
    "unet_name": "unet", "clip_name": "text_encoders", "model_name": "unet",
    "control_net_name": "controlnet", "clip_name1": "text_encoders",
    "clip_name2": "text_encoders", "clip_name3": "text_encoders",
    "style_model_name": "style_models", "image": "images", "video": None,
}


def _missing_models(g: Graph, reg: Registry) -> Dict[str, Tuple[int, str]]:
    """检查工作流里引用的模型文件名是否真实存在。"""
    if not len(reg):
        return {}
    cache: Dict[str, List[str]] = {}
    bad: Dict[str, Tuple[int, str]] = {}
    for n in g.nodes:
        try:
            pairs = list(n.widget_pairs())
        except Exception:
            continue
        for k, v in pairs:
            kind = MODEL_WIDGETS.get(k)
            if not kind or not isinstance(v, str) or not v:
                continue
            if kind not in cache:
                cache[kind] = reg.model_files(kind)
            files = cache[kind]
            if not files:
                continue
            norm = {os.path.basename(f).lower(): f for f in files}
            if norm.get(os.path.basename(v).lower()) is None:
                key = f"{kind}/{v}"
                cnt, who = bad.get(key, (0, ""))
                bad[key] = (cnt + 1, f"#{n.id} {n.type}")
    return bad


# ---------------- schema


def cmd_schema_search(args, out: Out) -> None:
    reg = _reg(args, refresh=args.refresh)
    hits = reg.search(args.query, limit=args.limit)
    out.put("count", len(hits))
    out.put("results", [{
        "type": s.type, "category": s.category, "display": s.display_name,
        "inputs": [i.name for i in s.widget_inputs],
        "link_inputs": [i.name for i in s.link_inputs],
        "outputs": [o.name for o in s.outputs],
    } for s in hits])
    out.raw(f"命中 {len(hits)} 个（共 {len(reg)} 种节点，来源 {reg.source}）")
    for s in hits:
        out.raw(f"  {s.type}")
        out.raw(f"      {s.category or '-'} | 出: {[o.name + ':' + o.type for o in s.outputs]}")
        if s.widget_inputs:
            out.raw(f"      控件: {[i.name for i in s.widget_inputs]}")


def cmd_schema_show(args, out: Out) -> None:
    reg = _reg(args)
    s = reg.get(args.type)
    if s is None:
        hits = reg.search(args.type, limit=6)
        tip = "、".join(h.type for h in hits) if hits else "（一个都没找到）"
        die(f"没有节点类型 {args.type!r}。你是不是想找：{tip}")
    out.put("type", s.type)
    out.put("category", s.category)
    out.put("required_links", [{"name": i.name, "type": i.type} for i in s.link_inputs
                               if not i.optional])
    out.put("optional_links", [{"name": i.name, "type": i.type} for i in s.link_inputs
                               if i.optional])
    out.put("widgets", [{"name": i.name, "type": i.type, "default": i.default,
                         "options": (i.options[:12] if i.options else None)}
                        for i in s.widget_inputs])
    out.put("outputs", [{"name": o.name, "type": o.type} for o in s.outputs])
    out.raw(f"{s.type}   [{s.category}]  {s.display_name or ''}")
    if s.description:
        out.raw(f"  {s.description[:200]}")
    out.raw("  输入(连线): " + ("、".join(f"{i.name}:{i.type}" for i in s.link_inputs
                                          if not i.optional) or "无"))
    opt = [i for i in s.link_inputs if i.optional]
    if opt:
        out.raw("  输入(可选): " + "、".join(f"{i.name}:{i.type}" for i in opt))
    out.raw("  控件: " + ("、".join(i.name for i in s.widget_inputs) or "无"))
    for i in s.widget_inputs:
        if i.options:
            n = len(i.options)
            out.raw(f"      {i.name}: {n} 个可选值，前几个 → {i.options[:6]}")
    out.raw("  输出: " + ("、".join(f"{o.name}:{o.type}" for o in s.outputs) or "无"))


def cmd_schema_produce(args, out: Out) -> None:
    reg = _reg(args)
    hits = reg.types_producing(args.type_, limit=args.limit)
    out.put("type", args.type_)
    out.put("count", len(hits))
    out.put("producers", [{"type": s.type, "category": s.category,
                           "outputs": [o.name for o in s.outputs]} for s in hits])
    out.raw(f"能产出 {args.type_} 的节点（{len(hits)} 个）：")
    for s in hits:
        out.raw(f"  {s.type:<44} {s.category}")
    if args.json:
        out.finish()


def cmd_schema_accept(args, out: Out) -> None:
    reg = _reg(args)
    hits = reg.types_accepting(args.type_, limit=args.limit)
    out.put("type", args.type_)
    out.put("accepting", [{"type": s.type, "category": s.category} for s in hits])
    out.raw(f"能接收 {args.type_} 的节点（{len(hits)} 个）：")
    for s in hits:
        out.raw(f"  {s.type:<44} {s.category}")
    if args.json:
        out.finish()


def cmd_schema_models(args, out: Out) -> None:
    reg = _reg(args)
    files = reg.model_files(args.kind)
    out.put("kind", args.kind)
    out.put("count", len(files))
    out.put("files", files[:400])
    out.raw(f"{args.kind} 下 {len(files)} 个文件" + (f"（显示前 {min(400, len(files))}）"
                                                  if len(files) > 400 else ""))
    for f in files[:args.limit]:
        out.raw(f"  {f}")


def cmd_schema_categories(args, out: Out) -> None:
    reg = _reg(args)
    tree = reg.category_tree(depth=args.depth)
    out.put("categories", tree)
    out.put("total_types", len(reg))

    def show(node: Dict[str, Any], depth: int, prefix: str) -> None:
        for k in sorted(node):
            if k == "_n":
                continue
            sub = node[k]
            cnt = sub.get("_n", 0)
            if depth >= args.depth:
                total = 0
                stack = [sub]
                while stack:
                    cur = stack.pop()
                    total += cur.get("_n", 0)
                    stack.extend(v for kk, v in cur.items() if kk != "_n")
                out.raw(f"{prefix}{k}  ({total})")
            else:
                show(sub, depth + 1, prefix)

    if not args.json:
        show(tree, 1, "  ")


def cmd_schema_refresh(args, out: Out) -> None:
    info, src = load_object_info(args.server, refresh=True, timeout=args.timeout)
    reg = Registry(info, src)
    out.put("source", src)
    out.put("types", len(reg))
    out.raw(f"✓ 刷新成功：{len(reg)} 种节点类型（{src}）")


def cmd_schema_stats(args, out: Out) -> None:
    reg = _reg(args)
    out.put("types", len(reg))
    out.put("source", reg.source)
    out.raw(f"节点字典：{len(reg)} 种类型（来源 {reg.source}）")


# ---------------- 节点库


def _catalog(args) -> Dict[str, Any]:
    from cwf.lib import catalog as cat_mod
    return cat_mod.build_catalog(
        getattr(args, "server", DEFAULT_SERVER),
        getattr(args, "root", None) or WF_DEFAULT,
        refresh=getattr(args, "rebuild", False) or getattr(args, "refresh", False))


def _join_name(v) -> str:
    """把 argparse 的 `nargs='+'` 结果拼回节点名。

    节点类型名里带空格太常见了 —— `Fast Groups Bypasser (rgthree)`、
    `LayerUtility: ImageScaleByAspectRatio V2`、`Mask Fill Holes`。
    不拼回来的话，用户（和 AI）就得给每个名字加引号，加漏一次就报
    「unrecognized arguments」，还看不出是名字里空格的问题。
    """
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v).strip()
    return (v or "").strip()


def cmd_render(args, out: Out) -> None:
    """把工作流离线画成图（SVG / PNG），不开浏览器也不开 ComfyUI 前端。"""
    from cwf.lib import render as rmod

    path = resolve_workflow(args.workflow)
    g = Graph.load(path)
    out.put("path", path)

    try:
        cat = _tax(args)
    except Exception:
        cat = None

    fmt = (args.format or "").lower()
    if not fmt and args.out:
        fmt = os.path.splitext(args.out)[1].lstrip(".").lower()
    if fmt not in ("svg", "png"):
        fmt = "svg"

    opts = rmod.Opts(
        scale=args.scale, max_px=args.max_px, pad=args.pad, theme=args.theme,
        color=args.color, legend=not args.no_legend, grid=not args.no_grid,
        widgets=not args.no_widgets, widget_names=not args.no_widget_names,
        notes=not args.no_notes, font=args.font,
    )
    if opts.theme not in rmod.THEMES:
        die(f"没有主题 {opts.theme!r}。可选：{'、'.join(rmod.THEMES)}")
    if args.max_px is None:
        # SVG 是矢量：放大不糊，没必要为了"像素上限"把整张图缩下去。
        # PNG 是位图：缩下去字就糊了，所以默认给个上限保护。
        opts.max_px = 0 if fmt == "svg" else 4200

    title = args.title or os.path.splitext(os.path.basename(path))[0]

    if not args.out:
        die("要指定输出文件，例如：\n"
            f"  cwf render {args.workflow} --out preview.svg\n"
            f"  cwf render {args.workflow} --out preview.png --scale 1.5\n"
            "（这个工具默认不落盘，得你说了才写）")

    dest = os.path.abspath(args.out)

    if fmt == "png":
        try:
            import PIL                                    # noqa: F401
        except Exception:
            die("这台机器上没有 Pillow，出不了 PNG。\n"
                "  要么改用 SVG：`--out preview.svg`（纯标准库，一样能看），\n"
                "  要么装一个：`pip install pillow`")
        w, h, sc = rmod.render_png(g, dest, opts, cat, title)
        out.put("written", dest)
        out.put("width", w)
        out.put("height", h)
        out.put("stats", sc.stats)
        out.raw(f"✓ 已渲染 {dest}")
        ratio = w / max(sc.w, 1)
        out.raw(f"  {w}×{h} px（画布 {int(sc.w)}×{int(sc.h)}，缩放 ×{ratio:.2f}）")
        if ratio < 0.75:
            out.raw(f"  ⚠ 缩到 ×{ratio:.2f} 了，字会偏小。想要大字：")
            out.raw(f"      · 出 SVG（矢量，放多大都清晰）"
                    f"：`--out out.svg`")
            out.raw(f"      · 或者放开上限：`--max-px 0 --scale 1.5`")
    else:
        svg, sc, s = rmod.render_svg(g, opts, cat, title)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(svg)
        out.put("written", dest)
        out.put("width", int(sc.w * s))
        out.put("height", int(sc.h * s))
        out.put("stats", sc.stats)
        out.raw(f"✓ 已渲染 {dest}")
        out.raw(f"  画布 {int(sc.w)}×{int(sc.h)} px，导出 ×{s:.2f}"
                f" → {int(sc.w * s)}×{int(sc.h * s)}")

    out.raw(f"  {sc.stats['nodes']} 节点 / {sc.stats['links']} 连线 / "
            f"{sc.stats['groups']} 分区框 · 按功能分了 {sc.stats['classes']} 类")
    if sc.legend:
        top = "、".join(f"{lbl}×{n}" for _k, lbl, n in sc.legend[:5])
        out.raw(f"  主要构成：{top}")
    if args.open:
        try:
            os.startfile(dest)          # type: ignore[attr-defined]
            out.raw("  已用系统默认程序打开")
        except Exception as exc:
            out.raw(f"  （自动打开失败：{exc}）")


def cmd_place(args, out: Out) -> None:
    """手动摆节点位置 —— 让 agent（或你）直接说了算，而不是交给自动排版。

    三种用法可以混着来：

      1. 绝对指定   `cwf place 某流 "#12=100,200" "标题子串=0,0"`
      2. 相对微调   `cwf place 某流 "#12+=0,-200"`（在当前坐标上挪）
      3. 列计划     `cwf place 某流 --col "主模型 正向" --col "一采"`（一行一列）

    选择器跟其它命令完全一致（`#id` / `type:名字` / `title:子串` / `~正则` / 中文别名）。
    """
    from cwf.lib import strata as st

    path = resolve_workflow(args.workflow)
    g = Graph.load(path)
    out.put("path", path)
    before = {n.id: tuple(n.pos) for n in g.nodes}

    moved: Dict[int, Tuple[float, float]] = {}

    # ---- 1/2. 逐个位置的绝对与相对指定
    for spec in (args.specs or []):
        s = str(spec).strip()
        rel = "+=" in s
        sep = "+=" if rel else "="
        if sep not in s:
            die(f"写不来 {spec!r}。要么 `选择器=X,Y`，要么 `选择器+=dX,dY`")
        sel, _, val = s.partition(sep)
        sel = sel.strip()
        try:
            if "," in val:
                a, b = val.split(",", 1)
                nx, ny = float(a.strip()), float(b.strip())
            else:
                nx = ny = float(val.strip())
        except ValueError:
            die(f"{spec!r} 里的坐标不是数字。要写成 `#12=100,200`")
        targets = g.find_all(sel)
        if not targets:
            die(f"选择器 {sel!r} 没选中任何节点。（`cwf cat 某流` 能看到所有节点）")
        for n in targets:
            cx, cy = n.pos
            moved[n.id] = (cx + nx, cy + ny) if rel else (nx, ny)

    # ---- 3. 列计划：一列一行，从左到右
    cols: List[List[str]] = []
    raw_cols = list(args.col or [])
    if args.plan:
        if not os.path.exists(args.plan):
            die(f"找不到计划文件 {args.plan}")
        with open(args.plan, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("col "):
                    line = line[4:]
                raw_cols.append(line)
    for line in raw_cols:
        toks = [t for t in re.split(r"[\s,]+", line) if t]
        if toks:
            cols.append(toks)

    if cols:
        x = 0.0
        col_report: List[Dict[str, Any]] = []
        for ci, toks in enumerate(cols):
            members: List[Node] = []
            for t in toks:
                hit = g.find_all(t)
                if not hit:
                    die(f"列计划第 {ci + 1} 列里的 {t!r} 没选中任何节点")
                members.extend(hit)
            seen: Set[int] = set()
            uniq = [n for n in members if not (n.id in seen or seen.add(n.id))]
            colw = max((n.width for n in uniq), default=0.0)
            y = 0.0
            for n in uniq:
                # 列内水平居中：窄节点跟宽节点对齐中轴，比左对齐整齐
                moved[n.id] = (x + (colw - n.width) / 2.0, y)
                y += n.height + args.gap_v
            x += colw + args.gap_h
            col_report.append({"col": ci + 1, "nodes": [n.id for n in uniq],
                               "width": colw})
        out.put("columns", col_report)

    # ---- 落位（先把所有目标算完再写，免得相对位移用到被改过的坐标）
    for nid, (nx, ny) in moved.items():
        n = g.maybe(nid)
        if n is not None:
            n.pos = (float(nx), float(ny))

    # ---- 压紧：把现有空隙一律收到 gap_h / gap_v
    if args.pack or args.fold > 1:
        n_cols = _pack_graph(g, args.gap_h, args.gap_v, args.fold)
        out.put("columns_after", n_cols)
        if n_cols:
            out.raw(f"  压紧后 {n_cols} 列")
        else:
            out.raw("  压紧没好处（原图不是列对齐的，硬压反而更宽），保持原样")

    # ---- 兜底：保证交出去的时候一个重叠都没有
    #
    # 这是**工具的责任**，不是用户的。手写的列计划、绝对坐标、或者原来的
    # 乱版式，都可能让两个节点压在一起；重叠是唯一"看见了就必须手动修"的
    # 版式问题，让用户回 ComfyUI 里拖一遍就失去用这个工具的意义了。
    pushed = 0
    if not args.no_fix_overlap:
        try:
            pushed = st.resolve_overlaps(
                g, st.LayoutOptions(h_gap=args.gap_h, v_gap=args.gap_v))
        except Exception as exc:
            out.raw(f"  （消重叠失败，已跳过：{exc}）")
    if pushed:
        out.raw(f"  推开 {pushed} 对重叠节点（这是硬保证，不用你手动拖）")

    # ---- 分区框跟着重算（挪了节点还留着旧框，框会套错地方）
    if not args.no_regroup:
        try:
            st.regroup(g, st.LayoutOptions(h_gap=args.gap_h, v_gap=args.gap_v))
        except Exception as exc:
            out.raw(f"  （分区框重算失败，已跳过：{exc}）")

    # 坐标归位要**克制**。
    #
    # 无条件归一化到原点会改掉 `#3=3000,3000` 这种明确指定的绝对坐标；
    # 只处理"负坐标"也不行 —— 把某个节点往上挪 40 px 就会让全图偏移，
    # `+=0,-40` 的结果变成"它没动、别人往下走了"，非常反直觉。
    # 所以只在**整个图落到很远的负半轴**时才拉回来（那种情况多半是被拖飞了，
    # 打开图会看到一片空白）。小小的负数原样保留。
    if g.nodes:
        dx = min(n.pos[0] for n in g.nodes)
        dy = min(n.pos[1] for n in g.nodes)
        dx = dx if dx < -1000 else 0.0
        dy = dy if dy < -1000 else 0.0
        if dx or dy:
            for n in g.nodes:
                n.pos = (round(n.pos[0] - dx, 1), round(n.pos[1] - dy, 1))
            out.raw(f"  （整图在负坐标区，已整体移回：{dx:.0f}, {dy:.0f}）")

    # 报尺寸要算**跨度**，不能直接用 max(right)/max(bottom)。
    # 图里只要有一个节点在负坐标，max(right) 就把那段负区间也算进宽度了，
    # 报出来的数字跟实际占位对不上（实测报 6901×527，实际是 6130×1778）。
    if g.nodes:
        w = max(n.right for n in g.nodes) - min(n.pos[0] for n in g.nodes)
        h = max(n.bottom for n in g.nodes) - min(n.pos[1] for n in g.nodes)
    else:
        w = h = 0.0
    changed = sum(1 for n in g.nodes if tuple(n.pos) != before.get(n.id))

    from cwf.lib import strata as _st
    ov = sum(1 for i, a in enumerate(g.nodes) for b in g.nodes[i + 1:]
             if a.width > 0 and b.width > 0
             and min(a.right, b.right) > max(a.pos[0], b.pos[0])
             and min(a.bottom, b.bottom) > max(a.pos[1], b.pos[1]))
    out.put("moved", changed)
    out.put("overlaps", ov)
    out.put("size", [round(w), round(h)])
    out.raw(f"✓ 挪了 {changed} 个节点（共 {len(g.nodes)} 个）")
    out.raw(f"  节点重叠 {ov} 处" + ("（0 = 不用再进 ComfyUI 手拖）" if ov == 0 else " ⚠"))
    out.raw(f"  新画布 {w:.0f} × {h:.0f} px"
            + (f"　宽高比 {w / h:.2f}" if h else ""))
    if cols:
        out.raw(f"  列计划：{len(cols)} 列，列间距 {args.gap_h:.0f}、"
                f"行间距 {args.gap_v:.0f}")

    # 落盘规则与其它写命令一致：**没说就不写**。
    # （这里踩过坑：一开始无条件 `_save(g, path, ...)`，结果不管有没有
    #   给 --out，都把结果写回了输入文件本身。）
    if args.in_place:
        backup = path + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        out.put("backup", backup)
        out.raw(f"  原文件已备份 → {os.path.basename(backup)}")
        _save(g, path, out, fmt=args.format)
    elif args.out:
        _save(g, args.out, out, fmt=args.format)
    else:
        out.raw("（未指定 --out / --in-place，没有写文件）")


def _pack_graph(g: Graph, gap_h: float, gap_v: float, fold: int = 1) -> int:
    """压紧版式：按 x 聚成列，列间只留 gap_h、列内只留 gap_v。

    `fold > 1` 时再把**相邻的 N 列并成一列**（列内按原来的 y 顺序往下堆）。
    这是"别把图拉那么长"最有效的一招 —— 一条 7 列的直线链折成 4 列，
    宽度直接掉三成，而相对顺序一点没变。

    不改变节点之间的上下/左右**相对顺序**，只是把空隙收到统一值。
    返回最终列数。
    """
    ns = [n for n in g.nodes if n.width > 0]
    if not ns:
        return 0

    # 先记下原样：压完如果反而更大，就退回去。
    # 踩过的坑：`--pack` 假设原图已经是"列对齐"的，而随手摆的图不是 ——
    # 一张 39 节点、原本 4750×1063 的图，压完变成 10013×1263，宽了一倍多。
    # 压紧这件事**只允许变好**，不许变坏。
    def _span(nodes):
        if not nodes:
            return (0.0, 0.0)
        w = max(n.right for n in nodes) - min(n.pos[0] for n in nodes)
        h = max(n.bottom for n in nodes) - min(n.pos[1] for n in nodes)
        return (w, h)

    orig_pos = [(n, n.pos) for n in ns]
    ow, oh = _span(ns)

    # 分列判据：按**中心 x** 聚类。
    #
    # ⚠ 这里踩过一个滚雪球的坑：原来比的是「本列当前的最右边」，而列是一边
    # 装一边变宽的 —— 装进一个宽节点后，右边界推到很远，于是右边所有节点
    # 都被吸进同一列。实测一张 33 节点的图被压成 390×4630 的**一列**。
    # 改用中心距：同列的中心 x 几乎相同，不同列至少差一个节点宽度。
    ns.sort(key=lambda n: n.pos[0] + n.width / 2.0)
    widths = sorted(n.width for n in ns)
    med_w = widths[len(widths) // 2]
    thr = max(120.0, med_w * 0.55)
    cols: List[List[Node]] = []
    for n in ns:
        cx = n.pos[0] + n.width / 2.0
        if cols:
            prev_cx = cols[-1][0].pos[0] + cols[-1][0].width / 2.0
            if abs(cx - prev_cx) < thr:
                cols[-1].append(n)
                continue
        cols.append([n])

    fold = max(1, int(fold))
    if fold > 1 and len(cols) > 1:
        merged: List[List[Node]] = []
        for i in range(0, len(cols), fold):
            group: List[Node] = []
            for c in cols[i:i + fold]:
                group.extend(c)
            merged.append(group)
        cols = merged

    x = 0.0
    for col in cols:
        colw = max(n.width for n in col)
        col.sort(key=lambda n: n.pos[1])
        y = 0.0
        for n in col:
            n.pos = (round(x + (colw - n.width) / 2.0, 1), round(y, 1))
            y += n.height + gap_v
        x += colw + gap_h

    nw, nh = _span(ns)
    if max(ow, oh) > 0 and max(nw, nh) > max(ow, oh) + 1.0:
        for n, pos in orig_pos:          # 变差了，原样还回去
            n.pos = pos
        return 0
    return len(cols)


def cmd_nodes_stats(args, out: Out) -> None:
    """节点库总览：一共多少种节点、按功能怎么分、分别来自哪些插件包。"""
    from cwf.lib import taxonomy as tm
    cat = _tax(args)
    t = cat["totals"]
    tx = cat.get("taxonomy") or {}
    out.put("totals", t)
    out.put("taxonomy", tx)
    out.put("by_native_category", cat["by_category"])
    out.put("by_package", cat["by_package"])
    out.put("built", cat["built_str"])
    out.put("source", cat["source"])
    src = "缓存" if cat.get("_from_cache") else "刚刚重建"
    out.raw(f"本机节点库（{src}，建于 {cat['built_str']}）")
    out.raw("")
    out.raw(f"  节点类型总数     {t['types']:>6}")
    out.raw(f"  实际用过的类型   {t['with_usage']:>6}")
    out.raw(f"  插件包数量       {t['packages']:>6}")
    out.raw(f"  扫描工作流       {t['workflows_scanned']:>6} 个"
            f"（{t['node_instances']} 个节点实例）")
    out.raw("")
    if tx.get("by_top"):
        out.raw("── 按功能分类（这才是「能干什么」）────────────")
        out.raw(f"  {'大类':<8} {'种数':>6} {'用过':>6}   说明")
        for k in tm.TOP_ORDER:
            n = tx["by_top"].get(k, 0)
            if not n:
                continue
            out.raw(f"  {tm.L1[k]:<8} {n:>6} {tx['by_top_used'].get(k, 0):>6}   "
                    f"{tm.L1_WHAT.get(k, '')}")
        out.raw("")
        out.raw(f"  自动判定覆盖率 {tx.get('coverage', 0) * 100:.1f}%"
                f"（未归类 {tx['by_top'].get('misc', 0)} 种）"
                f"  置信度分布 {tx.get('by_conf')}")
        if tx.get("overrides"):
            out.raw(f"  你手动覆盖了 {tx['overrides']} 条")
        out.raw("  `cwf nodes tree` 展开看小类与具体节点")
        out.raw("")
    out.raw("── 按原生分类（多数插件的这个字段其实就是插件名）──")
    for k, v in list(cat["by_category"].items())[:args.limit]:
        out.raw(f"  {k:<24} {v:>5}")
    out.raw("")
    out.raw("── 按插件包（贡献节点类型最多的在前） ─────")
    from cwf.lib import catalog as cat_mod
    for pkg, n_types, uses in cat_mod.package_report(cat, limit=args.limit):
        out.raw(f"  {pkg:<40} {n_types:>4} 种 / 用过 {uses} 次")


def cmd_nodes_list(args, out: Out) -> None:
    """列节点：支持中文别名、功能分类过滤、只看用过的。"""
    from cwf.lib import catalog as cat_mod
    from cwf.lib import taxonomy as tm
    from cwf.lib.terms import resolve_type

    cat = _tax(args)
    pool = cat["types"]

    # 先按功能分类（新）过滤，再按原生分类（旧的插件命名空间）过滤
    if getattr(args, "cat", None):
        try:
            top, sub = tm.norm_target(args.cat)
        except CwfError as exc:
            die(str(exc))
        pool = {t: e for t, e in pool.items()
                if e.get("tax") == top and (not sub or sub in (e.get("tax_sub") or ""))}
        if not pool:
            die(f"「{tm.L1.get(top, top)}」下"
                + (f"小类含「{sub}」的" if sub else "")
                + "没有节点。`cwf nodes tree --cat "
                + (top if not sub else f"{top}/{sub}") + "` 看看有哪些小类")
    view = {"types": pool} if pool is not cat["types"] else cat

    rows: List[Tuple[str, Dict[str, Any]]]
    if args.query:
        real = resolve_type(args.query, _reg(args))
        rows = []
        if real and real in pool:
            rows.append((real, pool[real]))
        rows += cat_mod.search(view, args.query, limit=args.limit, in_cat=args.category)
        seen = set()
        rows = [(t, v) for t, v in rows if not (t in seen or seen.add(t))][:args.limit]
    elif args.category:
        rows = cat_mod.search(view, "", limit=100000, in_cat=args.category)[:args.limit]
    else:
        rows = sorted(pool.items(),
                      key=lambda kv: (-kv[1].get("usage", 0), kv[0]))[:args.limit]

    if args.used_only:
        rows = [(t, v) for t, v in rows if v.get("usage", 0) > 0]

    out.put("count", len(rows))
    out.put("pool", len(pool))
    out.put("nodes", [{"type": t, **v} for t, v in rows])
    head = f"共 {len(rows)} 条"
    if getattr(args, "cat", None):
        head += f"（功能分类 {tm.L1.get(tm.norm_target(args.cat)[0], '')}"
        if tm.norm_target(args.cat)[1]:
            head += f"/{tm.norm_target(args.cat)[1]}"
        head += f"，池子 {len(pool)} 种）"
    if args.query:
        head += f"（搜「{args.query}」"
        real = resolve_type(args.query, _reg(args))
        if real:
            head += f" → 归一为 {real}"
        head += "）"
    out.raw(head)
    out.raw("")
    for t, v in rows:
        used = f"×{v['usage']}" if v.get("usage") else "  ·"
        mark = "" if not v.get("not_in_object_info") else "  ⚠未注册"
        out.raw(f"  {t:<44} {v.get('tax_path_zh') or v.get('category_zh') or '-':<16} "
                f"{used:>7}  {v.get('package', '')}{mark}")
    if rows:
        out.raw("")
        out.raw("  提示：`cwf nodes show 类型名` 看端口与控件；"
                "`cwf nodes classify 类型名` 看分类依据")


def cmd_nodes_show(args, out: Out) -> None:
    """一个节点的完整说明书：端口、控件、可选值、来源包、在库里用过几次。"""
    from cwf.lib.terms import resolve_type
    cat = _tax(args)
    want = _join_name(args.type)
    real = resolve_type(want, _reg(args)) or want
    v = cat["types"].get(real)
    if v is None:
        from cwf.lib import catalog as cat_mod
        hits = cat_mod.search(cat, want, limit=8)
        tip = "、".join(t for t, _ in hits) if hits else "（没有相近的）"
        die(f"节点库里没有 {want!r}。你是不是想找：{tip}")
    out.put("type", real)
    out.put("node", v)
    out.raw(f"{real}")
    if v.get("display") and v["display"] != real:
        out.raw(f"  显示名: {v['display']}")
    out.raw(f"  功能分类: {v.get('tax_path_zh') or '-'}"
            + (f"（判定规则 {v.get('tax_rule')}，"
               f"`cwf nodes classify {real}` 看依据）" if v.get("tax_rule") else ""))
    out.raw(f"  原生分类: {v.get('category') or '-'}"
            + (f"（{v.get('category_zh')}）"
               if v.get("category_zh") and v.get("category_zh") != v.get("category") else "")
            + "   ← 多数插件这里填的是插件名")
    out.raw(f"  来源:   {v.get('package') or '（工作流里没标注）'}")
    out.raw(f"  用量:   你的工作流里出现过 {v.get('usage', 0)} 次")
    if v.get("not_in_object_info"):
        out.raw("  ⚠ 本机 ComfyUI 的 /object_info 里没有它"
                + ("（前端 JS 注册的节点，属正常）" if v.get("frontend_only") else
                   "（插件可能已卸载，或只在旧工作流里存在）"))
    if v.get("desc"):
        out.raw(f"  说明:   {v['desc']}")
    if v.get("output_node"):
        out.raw("  ★ 这是输出节点（能直接出图/出片）")
    out.raw("")
    out.raw("  必接输入:  " + ("、".join(v["link_inputs"]) or "无"))
    if v.get("opt_inputs"):
        out.raw("  可选输入:  " + "、".join(v["opt_inputs"]))
    out.raw("  输出:      " + ("、".join(v["outputs"]) or "无"))
    out.raw("  控件:      " + ("、".join(v["widgets"]) or "无"))
    if len(cat["types"]) and getattr(args, "verbose", False):
        spec = _reg(args).get(real)
        if spec:
            out.raw("")
            out.raw("  ── 控件默认值与可选值 ──")
            for i in spec.widget_inputs:
                opts = ""
                if i.options:
                    opts = f"  可选 {len(i.options)} 项：{i.options[:8]}"
                out.raw(f"    {i.name:<22} {i.type:<8} 默认={i.default!r}{opts}")


def cmd_nodes_alias(args, out: Out) -> None:
    """中文别名表：你可以直接说「解码」「主模型」。"""
    from cwf.lib.terms import NODE_ALIASES, SLOT_ALIASES, resolve_type
    reg = _reg(args)
    rows = []
    for k, cands in NODE_ALIASES.items():
        real = None
        for c in cands:
            if not len(reg) or c in reg:
                real = c
                break
        rows.append({"say": k, "resolves_to": real or cands[0],
                     "exists": bool(real) or not len(reg)})
    out.put("node_aliases", rows)
    out.put("slot_aliases", SLOT_ALIASES)
    out.raw("说这个词……          就会解析成")
    out.raw("─" * 46)
    for r in rows:
        flag = "" if r["exists"] else "  （本机没装）"
        out.raw(f"  {r['say']:<18} → {r['resolves_to']}{flag}")
    out.raw("")
    out.raw("端口别名（连线时可用）：")
    for k, v in list(SLOT_ALIASES.items())[:24]:
        out.raw(f"  {k:<10} → {v}")


def cmd_nodes_pkg(args, out: Out) -> None:
    """按插件包看节点：某个包到底给你装了什么。"""
    from cwf.lib import catalog as cat_mod
    cat = _catalog(args)
    if args.package:
        q = args.package.lower()
        rows = [(t, v) for t, v in cat["types"].items()
                if q in (v.get("package") or "").lower()]
        rows.sort(key=lambda kv: (-kv[1].get("usage", 0), kv[0]))
        out.put("package", args.package)
        out.put("nodes", [{"type": t, **v} for t, v in rows[:args.limit]])
        out.put("count", len(rows))
        pkgs = sorted({v.get("package") or "" for t, v in rows})
        out.raw(f"包 {args.package!r} 贡献 {len(rows)} 种节点"
                f"（匹配到 {len(pkgs)} 个包名: {pkgs[:6]}）")
        for t, v in rows[:args.limit]:
            used = f"×{v['usage']}" if v.get("usage") else "  ·"
            out.raw(f"  {t:<44} {v.get('category_zh') or '-':<14} {used:>7}")
        return
    out.put("packages", cat_mod.package_report(cat, limit=args.limit))
    out.raw(f"本机 {len(cat['by_package'])} 个来源包（含未标注），"
            f"贡献最多的 {args.limit} 个：")
    out.raw("")
    for pkg, n_types, uses in cat_mod.package_report(cat, limit=args.limit):
        out.raw(f"  {pkg:<44} {n_types:>4} 种节点 / 累计用过 {uses} 次")


def cmd_nodes_unused(args, out: Out) -> None:
    """装了但从没在你工作流里出现过的节点类型——清理插件时的参考。"""
    cat = _catalog(args)
    rows = sorted([(t, v) for t, v in cat["types"].items()
                   if not v.get("usage") and not v.get("not_in_object_info")],
                  key=lambda kv: (kv[1].get("package") or "", kv[0]))
    out.put("count", len(rows))
    out.put("unused", [{"type": t, "package": v.get("package", ""),
                        "category": v.get("category_zh") or v.get("category", "")}
                       for t, v in rows[:args.limit]])
    by_pkg: Dict[str, int] = {}
    for t, v in rows:
        p = v.get("package") or "（未标注）"
        by_pkg[p] = by_pkg.get(p, 0) + 1
    out.raw(f"从没在你工作流里用过的节点类型：{len(rows)} 种")
    out.raw("（注意：全新装的插件本来就还没用过，不代表它没用）")
    out.raw("")
    for p, n in sorted(by_pkg.items(), key=lambda kv: -kv[1])[:args.limit]:
        out.raw(f"  {p:<44} {n:>4} 种未用")


def cmd_nodes_build(args, out: Out) -> None:
    """强制重建节点库缓存。"""
    cat = _catalog(args)
    t = cat["totals"]
    out.put("totals", t)
    out.put("taxonomy", cat.get("taxonomy"))
    out.raw(f"✓ 节点库已重建：{t['types']} 种节点类型 / {t['packages']} 个来源包"
            f"（扫了 {t['workflows_scanned']} 个工作流）")
    tx = cat.get("taxonomy") or {}
    if tx.get("by_top"):
        out.raw(f"  功能分类：{len([1 for k, v in tx['by_top'].items() if v])} 类，"
                f"自动判定覆盖率 {tx.get('coverage', 0) * 100:.1f}%"
                f"（未归类 {tx['by_top'].get('misc', 0)} 种）")
    out.raw(f"  缓存文件：{os.path.join(os.path.expanduser('~'), '.cwf', 'node_catalog.json')}")


# ---------------- 功能分类


def _tax(args) -> Dict[str, Any]:
    """取节点库 + **当场重跑分类**。

    为什么不直接用缓存里存好的分类字段：用户改一行 `categories.txt` 就该立刻
    生效，不该逼他重建缓存。7746 个节点重跑一遍规则不到一秒。

    分类器出错时必须**出声**：建库那条路径为了"节点库不能建不出来"会把异常
    吞掉、只写个 error 字段，结果就是所有节点悄悄失去分类。这里补上。
    """
    from cwf.lib import taxonomy as tm
    cat = _catalog(args)
    try:
        tm.apply_to_catalog(cat)
    except Exception as exc:
        die(f"功能分类跑不起来：{type(exc).__name__}: {exc}\n"
            f"  节点库本身没问题（{len(cat.get('types') or {})} 种节点仍在），"
            f"是 cwf/lib/taxonomy.py 的规则表有问题。")
    return cat


def _tax_index(cat: Dict[str, Any]) -> Dict[str, Dict[str, List[Tuple[str, Dict[str, Any]]]]]:
    idx: Dict[str, Dict[str, List[Tuple[str, Dict[str, Any]]]]] = {}
    for t, e in (cat.get("types") or {}).items():
        if not isinstance(e, dict):
            continue
        top = e.get("tax") or "misc"
        sub = e.get("tax_sub") or "（未细分）"
        idx.setdefault(top, {}).setdefault(sub, []).append((t, e))
    for top in idx:
        for sub in idx[top]:
            idx[top][sub].sort(key=lambda kv: (-(kv[1].get("usage") or 0), kv[0]))
    return idx


def cmd_nodes_tree(args, out: Out) -> None:
    """按功能浏览节点库：16 个大类，点开看小类和具体节点。"""
    from cwf.lib import taxonomy as tm
    cat = _tax(args)
    idx = _tax_index(cat)
    types = cat["types"]

    only_top: Optional[str] = None
    only_sub: Optional[str] = None
    if getattr(args, "cat", None):
        only_top, only_sub = tm.norm_target(args.cat)

    def stat(rows) -> Tuple[int, int]:
        return len(rows), sum(1 for _, e in rows if e.get("usage"))

    out.put("total", len(types))
    out.put("used_total", sum(1 for e in types.values() if e.get("usage")))
    if only_top:
        out.put("cat", only_top)
        out.put("cat_zh", tm.L1.get(only_top, only_top))

    total, used_total = len(types), out.data["used_total"]
    if not only_top:
        out.raw(f"本机节点库 {total} 种（你用过的 {used_total} 种）"
                f" · 按功能分 {len(tm.L1)} 类")
        out.raw("（分类依据：端口类型 + 类型名 + 原生分类路径；"
                "不是按插件名）")
        out.raw("")
        out.put("tree", {})
        for top in tm.TOP_ORDER:
            subs = idx.get(top) or {}
            if not subs:
                continue
            n, nu = stat([r for v in subs.values() for r in v])
            tops = "、".join(f"{s} {len(v)}" for s, v in
                             sorted(subs.items(), key=lambda kv: -len(kv[1]))[:4])
            out.raw(f"  {tm.L1[top]:<8} {n:>5} 种  用过 {nu:>3}   "
                    f"{len(subs):>2} 小类   {tops}")
            out.data["tree"][top] = {"zh": tm.L1[top], "n": n, "used": nu,
                                     "subs": {s: len(v) for s, v in subs.items()}}
        out.raw("")
        out.raw("  `cwf nodes tree --cat 视频` 展开某一类；"
                "`cwf nodes list --cat mask --used-only` 列具体节点")
        misc_n = len([1 for _, e in types.items() if e.get("tax") == "misc"])
        if misc_n:
            out.raw(f"  未归类 {misc_n} 种（占比 {misc_n / total * 100:.1f}%）"
                    f"—— 多数是不在 /object_info 里的老工作流残留；"
                    f"`cwf nodes setcat` 可以手动归位")
        return

    subs = idx.get(only_top) or {}
    if only_sub:
        subs = {k: v for k, v in subs.items() if only_sub in k}
    rows_all = [r for v in subs.values() for r in v]
    n, nu = stat(rows_all)
    out.raw(f"{tm.L1[only_top]}（{only_top}） {n} 种 / 用过 {nu} 种")
    out.raw(f"  {tm.L1_WHAT.get(only_top, '')}")
    out.raw("")
    limit = max(1, args.limit)
    for sub in sorted(subs, key=lambda s: -len(subs[s])):
        rows = subs[sub]
        sn, su = stat(rows)
        out.raw(f"  ├─ {sub}（{sn} 种，用过 {su}）")
        for t, e in rows[:limit]:
            tag = f"×{e['usage']}" if e.get("usage") else "·"
            pkg = (e.get("package") or "")[:26]
            out.raw(f"  │    {t[:46]:<46} {tag:>6}  {pkg}")
        if len(rows) > limit:
            out.raw(f"  │    … 还有 {len(rows) - limit} 种")
    out.raw("")
    out.raw(f"  提示：`cwf nodes show <类型>` 看端口；"
            f"`cwf nodes list --cat {only_top}` 平铺列出")


def cmd_nodes_classify(args, out: Out) -> None:
    """这个节点为什么被判成这个类 —— 判定依据全摊开，方便纠错。"""
    from cwf.lib import taxonomy as tm
    cat = _tax(args)
    t = _join_name(args.type)
    e = cat["types"].get(t)
    if e is None:
        real = None
        try:
            from cwf.lib.terms import resolve_type
            real = resolve_type(t, _reg(args))
        except Exception:
            real = None
        if real and real in cat["types"]:
            t, e = real, cat["types"][real]
        else:
            near = [k for k in cat["types"] if t.lower() in k.lower()][:6]
            die(f"节点库里没有 {args.type!r}。"
                + (f"相近的有：{'、'.join(near)}" if near else "先 `cwf nodes list` 搜搜"))
    info = tm.explain(t, e)
    out.put("type", t)
    out.put("taxonomy", info["tax"])
    out.put("path_zh", info["path"])
    out.put("view", info["view"])
    out.put("matched_rule", info["matched_rule"])
    out.raw(f"{t}")
    out.raw(f"  → {info['path']}   置信度 {info['tax']['conf']}   规则 {info['matched_rule']}")
    if info["override"]:
        out.raw(f"  （来自你的覆盖文件：{info['override']}）")
    out.raw("")
    v = info["view"]
    out.raw(f"  名称词元:  {', '.join(v['tokens']) or '（无）'}")
    out.raw(f"  输出端口:  {', '.join(v['outputs']) or '（无）'}")
    out.raw(f"  输入端口:  {', '.join(v['inputs']) or '（无）'}")
    out.raw(f"  原生分类:  {v['native_category'] or '（空）'}")
    out.raw(f"  来源包:    {v['package'] or '（未标注）'}")
    out.raw(f"  用量:      {v['usage']} 次")
    if e.get("not_in_object_info"):
        out.raw("  ⚠ 不在 /object_info 里（插件卸载残留或前端虚拟节点），"
                "端口证据为零，判定只能靠名字")
    out.raw("")
    n_try = len(info["trace"])
    if info["trace"] and info["trace"][-1]["hit"]:
        out.raw(f"  判定过程：按顺序试规则，第 {n_try} 条 `{info['matched_rule']}` 命中"
                f"（前面 {n_try - 1} 条都没命中）")
    else:
        out.raw("  判定过程：所有规则都没命中，落进「未归类」")
    out.raw(f"  想改：cwf nodes setcat {t} 图像处理/放大")


def cmd_nodes_setcat(args, out: Out) -> None:
    """手动改一个节点的功能分类（写进 categories.txt，永久生效）。"""
    from cwf.lib import taxonomy as tm
    cat = _tax(args)
    t, target = _setcat_args(args)
    if t not in cat["types"]:
        near = [k for k in cat["types"] if t.lower() in k.lower()][:6]
        die(f"节点库里没有 {t!r}，没法归类。"
            + (f"相近的有：{'、'.join(near)}" if near else ""))
    try:
        top, sub = tm.norm_target(target)
    except CwfError as exc:
        die(str(exc))
    p = tm.save_override(t, target)
    info = tm.explain(t, cat["types"][t])
    out.put("type", t)
    out.put("written", p)
    out.put("now", info["tax"])
    out.raw(f"✓ {t} → {info['path']}")
    out.raw(f"  写入 {p}")
    out.raw(f"  当前判定规则：{info['matched_rule']}（override 永远优先于自动规则）")



def _setcat_args(args) -> Tuple[str, str]:
    """拆 `nodes setcat <类型…> <分类>`。

    类型名可能带空格（`Anything Everywhere`），分类也可能（`图像处理/放大 / 缩放`），
    所以做成：给了 `--to` 就全按类型名拼；没给就把最后一个参数当分类。
    这样 `setcat KSampler 图像处理/放大` 和
    `setcat Anything Everywhere 流程与组织/通配广播` 都能用。
    """
    if getattr(args, "to", None):
        return _join_name(args.parts), args.to.strip()
    parts = list(args.parts or [])
    if len(parts) < 2:
        die("用法：cwf nodes setcat <节点类型> <分类>，"
            "例如 `cwf nodes setcat KSampler 采样与生成/采样器`；"
            "节点名带空格时用 --to 指定分类")
    return " ".join(parts[:-1]).strip(), parts[-1].strip()


def cmd_nodes_atlas(args, out: Out) -> None:
    """把全部节点按功能分类导出成可读图鉴（markdown + 机读索引）。"""
    from cwf.lib import taxonomy as tm
    cat = _tax(args)
    dest = args.out or os.path.join(os.path.expanduser("~"), ".cwf", "atlas")
    r = tm.export_atlas(cat, dest, used_only=args.used_only)
    out.put("atlas", r)
    out.raw(f"✓ 已导出 {r['total']} 种节点 → {r['dir']}")
    out.raw(f"  共 {len(r['files'])} 个文件：")
    for p in r["files"]:
        try:
            kb = os.path.getsize(p) / 1024.0
        except OSError:
            kb = 0
        out.raw(f"    {os.path.basename(p):<28} {kb:>8.1f} KB")
    out.raw("")
    out.raw("  入口是 index.md。这份是**全量快照**，节点库变了一键重导即可。")


# ---------------- 读取


def _load(args, out: Out) -> Graph:
    path = resolve_workflow(args.workflow)
    g = Graph.load(path)
    out.put("path", path)
    return g


def _reg(args, refresh: bool = False) -> Registry:
    if getattr(args, "offline", False):
        return registry(DEFAULT_SERVER, offline=True)
    return registry(getattr(args, "server", DEFAULT_SERVER), refresh=refresh)


def resolve_dest(dest: Optional[str], name: str,
                 default_dir: str = "") -> str:
    """把 `--out` 解析成真正要写的文件路径。

    `--out` 允许两种写法，都得支持，因为两种都很自然：

        --out out/我的流.json    给文件
        --out out/               给目录（此时自动拼上名字）

    目录判定：以 `/` 或 `\\` 结尾，或者本身已存在且是目录。
    没给 dest 就落到 default_dir/名字.json。

    这里踩过坑：早期直接把目录当文件写，在 Windows 上抛的是
    `PermissionError: Permission denied: 'out/'` —— 报错完全看不出
    真正原因（把目录当文件了），用户只会一脸问号。
    """
    fn = name if name.lower().endswith(".json") else name + ".json"
    if not dest:
        if not default_dir:
            return fn
        return os.path.join(default_dir, fn)
    if dest.endswith(("/", "\\")) or os.path.isdir(dest):
        os.makedirs(dest, exist_ok=True)
        return os.path.join(dest, fn)
    parent = os.path.dirname(os.path.abspath(dest))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    return dest


def _save(g: Graph, path: str, out: Out, fmt: str = "ui") -> None:
    # 兜底：万一路径是个目录，别把目录当文件写。有些调用点拿不到名字，
    # 就在这儿补一个，好过抛一个看不懂的 PermissionError。
    if path.endswith(("/", "\\")) or os.path.isdir(path):
        path = os.path.join(path, f"cwf-{int(time.time())}.json")
    real = g.save(path, fmt=fmt)
    out.put("written", real)
    out.raw(f"✓ 已写入 {real}")
    n = len(g)
    out.raw(f"  {n} 节点 / {len(g.links)} 连线 / {len(g.groups)} 分区框")


def cmd_cat(args, out: Out) -> None:
    g = _load(args, out)
    if not args.detail:
        out.put("summary", g.summary())
        render_tree(g, out, show_detail=False)
        if args.json:
            out.finish()
        return
    reg = _reg(args)
    nodes = sorted(g.nodes, key=lambda z: (z.pos[0], z.pos[1]))
    if args.filter:
        low = args.filter.lower()
        nodes = [n for n in nodes if low in (n.title or "").lower()
                 or low in n.type.lower()]
    shown = nodes[:args.limit] if args.limit else nodes
    out.put("nodes", [{
        "id": n.id, "type": n.type, "title": n.title, "mode": n.mode,
        "pos": [round(n.pos[0]), round(n.pos[1])],
        "widgets": _safe_widgets(n),
        "inputs": [{"name": s.name, "type": s.type,
                    "from": _link_desc(g, s.link)} for s in n.inputs],
        "outputs": [{"name": s.name, "type": s.type,
                     "to": [f"#{l.target_id}" for l in g.outgoing(n.id)
                            if l.origin_slot == i]}
                    for i, s in enumerate(n.outputs)],
        "group": _group_of(g, n),
    } for n in shown])
    out.raw(f"节点 {len(g)}（显示 {len(shown)}）· 连线 {len(g.links)}")
    for n in shown:
        out.raw(f"  #{n.id}  {n.type}   {n.title or ''}")
        w = _safe_widgets(n)
        if w:
            parts = []
            for k, v in w.items():
                sv = str(v).replace("\n", "⏎")
                parts.append(f"{k}={sv[:60]}")
            out.raw("        值: " + "  ".join(parts))
        ins = [f"{s.name}←{_link_desc(g, s.link, short=True)}" for s in n.inputs
               if s.link is not None]
        if ins:
            out.raw("        入: " + "  ".join(ins))
        outs = []
        for i, s in enumerate(n.outputs):
            tg = [f"#{l.target_id}.{g.node(l.target_id).inputs[l.target_slot].name}"
                  for l in g.outgoing(n.id) if l.origin_slot == i]
            if tg:
                outs.append(f"{s.name}→{'/'.join(tg)}")
        if outs:
            out.raw("        出: " + "  ".join(outs))
    if len(shown) < len(nodes):
        out.raw(f"  … 还有 {len(nodes) - len(shown)} 个（--limit 调大）")


def _safe_widgets(n: Node) -> Dict[str, Any]:
    try:
        return {k: v for k, v in n.widget_pairs() if k != "videopreview"}
    except Exception:
        return {}


def _link_desc(g: Graph, lid: Optional[int], short: bool = False) -> Optional[str]:
    if lid is None:
        return None
    l = g.link(lid)
    if l is None:
        return f"坏链接#{lid}"
    src = g.maybe(l.origin_id)
    if src is None:
        return f"#{l.origin_id}[{l.origin_slot}]"
    slot = src.outputs[l.origin_slot].name if l.origin_slot < len(src.outputs) else str(l.origin_slot)
    return f"#{src.id}.{slot}" if short else f"#{src.id} {src.title or src.type} · {slot}"


def _group_of(g: Graph, n: Node) -> Optional[str]:
    cx, cy = n.pos[0] + n.width / 2, n.pos[1] + n.height / 2
    for gr in g.groups:
        x, y, w, h = gr.bounding
        if x <= cx <= x + w and y <= cy <= y + h:
            return gr.title
    return None


def cmd_find(args, out: Out) -> None:
    reg = _reg(args)
    hits = []
    for path, score in search_workflows(args.query, limit=args.limit):
        g = Graph.load(path)
        hits.append((path, score, g.summary()))
    out.put("results", [{"path": p, "summary": s} for p, _, s in hits])
    out.raw(f"匹配 {len(hits)} 个工作流：")
    for p, _, s in hits:
        out.raw(f"  {os.path.relpath(p, WF_DEFAULT)}")
        out.raw(f"      {s['nodes']} 节点 · {s['types']} 种类型 · "
                f"{human_size(os.path.getsize(p))}")


def cmd_grep(args, out: Out) -> None:
    """在工作流库里按节点类型 / 控件值 / 标题搜内容。"""
    pat = re.compile(args.pattern, re.I)
    root = args.root or WF_DEFAULT
    rows = []
    files = globmod.glob(os.path.join(root, "**", "*.json"), recursive=True)
    for p in files:
        try:
            g = Graph.load(p)
        except Exception:
            continue
        for n in g.nodes:
            hit = None
            if pat.search(n.type) or pat.search(n.title or ""):
                hit = "类型/标题"
            else:
                for k, v in _safe_widgets(n).items():
                    if isinstance(v, str) and pat.search(v):
                        hit = f"{k}={v[:60]}"
                        break
            if hit:
                rows.append({"path": p, "rel": os.path.relpath(p, root),
                             "node": n.id, "type": n.type, "title": n.title,
                             "hit": hit})
        if args.limit and len(rows) >= args.limit:
            break
    rows = rows[:args.limit] if args.limit else rows
    out.put("count", len(rows))
    out.put("matches", rows)
    out.raw(f"命中 {len(rows)} 处" + (f"（上限 {args.limit}）" if args.limit else ""))
    for r in rows:
        out.raw(f"  {r['rel']}   #{r['node']} {r['type']}  → {r['hit']}")


def cmd_outline(args, out: Out) -> None:
    """给 agent 看的结构摘要：分阶段列出节点与关键连线。"""
    g = _load(args, out)
    reg = _reg(args)
    order = g.topo_order()
    preds: Dict[int, List[int]] = {}
    for l in g.links:
        preds.setdefault(l.target_id, []).append(l.origin_id)
    depth: Dict[int, int] = {}

    def d(nid: int, guard: int = 0) -> int:
        if nid in depth:
            return depth[nid]
        if guard > 200:
            return 0
        ps = preds.get(nid, [])
        v = 0 if not ps else max(d(p, guard + 1) for p in ps) + 1
        depth[nid] = v
        return v

    for n in g.nodes:
        d(n.id)
    stages: Dict[int, List[Node]] = {}
    for n in g.nodes:
        stages.setdefault(depth.get(n.id, 0), []).append(n)
    out.put("stages", [{
        "stage": k,
        "nodes": [{"id": n.id, "type": n.type, "title": n.title,
                   "keys": _key_widgets(n)} for n in sorted(v, key=lambda z: z.id)],
    } for k, v in sorted(stages.items())])
    out.raw(f"数据流共 {len(stages)} 阶段（{len(g)} 节点）")
    for k, v in sorted(stages.items()):
        out.raw(f"\n── 阶段 {k} " + "─" * 40)
        for n in sorted(v, key=lambda z: z.id):
            kw = _key_widgets(n)
            ks = " ".join(f"{a}={b}" for a, b in kw.items() if b not in (None, ""))
            out.raw(f"  #{n.id:<5} {n.type:<32} {n.title or '':<24} {ks[:80]}")


KEY_WIDGETS = ("text", "positive", "negative", "prompt", "steps", "cfg", "seed",
               "denoise", "ckpt_name", "lora_name", "vae_name", "unet_name",
               "clip_name", "width", "height", "batch_size", "length", "fps",
               "filename_prefix", "sampler_name", "scheduler", "model_name",
               "image", "video", "audio", "frame_load_cap", "force_rate",
               "upscale_method", "scale_by", "strength", "start_percent",
               "end_percent", "value", "shift", "noise_seed")


def _key_widgets(n: Node) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        for k, v in n.widget_pairs():
            if k in KEY_WIDGETS:
                out[k] = (str(v)[:70] + "…") if isinstance(v, str) and len(str(v)) > 70 else v
    except Exception:
        pass
    return out


def cmd_deps(args, out: Out) -> None:
    """某节点的上游 / 下游链。"""
    g = _load(args, out)
    nodes = g.find_all(args.node)
    if not nodes:
        # 报错要给出路：列出图里实际有哪些类型，并试着给相近的
        from cwf.lib.terms import suggest_for
        have = sorted({n.type for n in g.nodes})
        near = suggest_for(str(args.node), have)
        tip = (f"这张图里有相近的：{'、'.join(near)}" if near
               else f"这张图的类型清单：{'、'.join(have[:16])}")
        raise CwfError(f"{args.node!r} 没匹配到节点。{tip}\n"
                       f"（节点引用支持 #id / type:类型名 / 标题子串 / 中文别名）")
    if len(nodes) > 1:
        out.raw(f"（{args.node!r} 匹配到 {len(nodes)} 个节点，逐个列出）")
    for node in nodes:
        _deps_one(g, node, args, out)
    pass


def _deps_one(g: Graph, node: Node, args, out: Out) -> None:
    up, seen = [], set()

    def walk_up(nid: int, depth: int) -> None:
        if nid in seen or depth > args.depth:
            return
        seen.add(nid)
        for l in g.incoming(nid):
            up.append((depth, l.origin_id, l.target_id))
            walk_up(l.origin_id, depth + 1)

    dn, seen2 = [], set()

    def walk_dn(nid: int, depth: int) -> None:
        if nid in seen2 or depth > args.depth:
            return
        seen2.add(nid)
        for l in g.outgoing(nid):
            dn.append((depth, l.origin_id, l.target_id))
            walk_dn(l.target_id, depth + 1)

    walk_up(node.id, 1)
    walk_dn(node.id, 1)
    out.put("node", {"id": node.id, "type": node.type, "title": node.title})
    out.put("upstream", [{"depth": d, "from": a, "to": b} for d, a, b in up])
    out.put("downstream", [{"depth": d, "from": a, "to": b} for d, a, b in dn])
    out.raw(f"#{node.id} {node.type}  {node.title or ''}")
    out.raw(f"  上游 {len(up)} 条：")
    for d, a, b in sorted(up):
        sa = g.maybe(a)
        out.raw(f"    {'  ' * (d - 1)}← #{a} {sa.type if sa else '?'} → #{b}")
    out.raw(f"  下游 {len(dn)} 条：")
    for d, a, b in sorted(dn):
        sb = g.maybe(b)
        out.raw(f"    {'  ' * (d - 1)}→ #{b} {sb.type if sb else '?'}")


def cmd_diff(args, out: Out) -> None:
    a = Graph.load(resolve_workflow(args.a))
    b = Graph.load(resolve_workflow(args.b))
    na = {n.id: n for n in a.nodes}
    nb = {n.id: n for n in b.nodes}
    out.put("only_in_a", [{"id": i, "type": na[i].type} for i in sorted(set(na) - set(nb))])
    out.put("only_in_b", [{"id": i, "type": nb[i].type} for i in sorted(set(nb) - set(na))])
    changed = []
    for i in sorted(set(na) & set(nb)):
        x, y = na[i], nb[i]
        if x.type != y.type:
            changed.append({"id": i, "kind": "type", "a": x.type, "b": y.type})
            continue
        wx, wy = _safe_widgets(x), _safe_widgets(y)
        for k in set(wx) | set(wy):
            if wx.get(k) != wy.get(k):
                changed.append({"id": i, "type": x.type, "kind": f"widget.{k}",
                                "a": wx.get(k), "b": wy.get(k)})
        if x.mode != y.mode:
            changed.append({"id": i, "type": x.type, "kind": "mode",
                            "a": x.mode, "b": y.mode})
    la = {(l.origin_id, l.origin_slot, l.target_id, l.target_slot) for l in a.links}
    lb = {(l.origin_id, l.origin_slot, l.target_id, l.target_slot) for l in b.links}
    out.put("links_only_in_a", sorted(la - lb))
    out.put("links_only_in_b", sorted(lb - la))
    out.put("changed", changed)
    out.raw(f"A={os.path.basename(args.a)}  B={os.path.basename(args.b)}")
    out.raw(f"仅 A 有 {len(set(na) - set(nb))} 个节点，仅 B 有 {len(set(nb) - set(na))} 个")
    out.raw(f"仅 A 有 {len(la - lb)} 条连线，仅 B 有 {len(lb - la)} 条")
    out.raw(f"参数差异 {len(changed)} 处：")
    for c in changed[:60]:
        out.raw(f"  #{c['id']} {c.get('type', '')} {c['kind']}: "
                f"{str(c.get('a'))[:40]} → {str(c.get('b'))[:40]}")
    if len(changed) > 60:
        out.raw(f"  … 还有 {len(changed) - 60} 处")


# ---------------- 节点知识库（store）


def _store(args):
    from cwf.lib import store as st
    return st


def _store_ctx(args):
    """检索 store 时要用的上下文：节点字典 + 节点库（含当场重算的功能分类）。"""
    reg = _reg(args)
    cat = None
    try:
        cat = _tax(args)
    except Exception:
        cat = None
    return reg, cat


def cmd_store_find(args, out: Out) -> None:
    """检索节点：先查你沉淀过的（别名/笔记/收藏），再回落 7746 种节点字典。"""
    st = _store(args)
    reg, cat = _store_ctx(args)
    hits = st.search(args.query or "", reg=reg, cat=cat, limit=args.limit)
    out.put("count", len(hits))
    out.put("results", [h.as_dict() for h in hits])
    if not hits:
        out.raw(f"没找到和 {args.query!r} 相关的节点。")
        out.raw("  提示：`cwf store find 解码` 这类中文词也行；"
                "或 `cwf nodes list` 看全量。")
        return
    src_tag = {"alias": "别名", "note": "笔记", "pin": "收藏",
               "exact": "精确", "catalog": "字典"}
    out.raw(f"找到 {len(hits)} 个（★ = 你沉淀过的）")
    out.raw("")
    for h in hits:
        star = "★" if h.source in ("alias", "note", "pin") else " "
        alias = f"  [{h.alias}]" if h.alias else ""
        used = f" ×{h.usage}" if h.usage else ""
        out.raw(f"  {star} {h.type}{alias}")
        line2 = f"      {h.category or '-'}"
        if h.package:
            line2 += f" · {h.package}"
        if used:
            line2 += f" · 用过{used}"
        out.raw(line2)
        if h.outputs:
            out.raw(f"      出: {'、'.join(h.outputs)}")
        if h.inputs:
            out.raw(f"      入: {'、'.join(h.inputs)}")
    out.raw("")
    out.raw("  下一步：`cwf store show 类型名` 看详情 · "
            "`cwf store mark 类型名 -a 别名 -n 笔记` 沉淀它")


def cmd_store_show(args, out: Out) -> None:
    """一个节点的完整速查卡 + 你写过的笔记。"""
    st = _store(args)
    reg, cat = _store_ctx(args)
    hits = st.search(_join_name(args.type), reg=reg, cat=cat, limit=8)
    if not hits:
        from cwf.lib.terms import suggest_for
        have = sorted((cat.get("types") or {}).keys()) if cat else []
        near = suggest_for(_join_name(args.type), have)
        die(f"节点库里没有 {_join_name(args.type)!r}。"
            + (f"相近的有：{'、'.join(near)}" if near else ""))
    h = hits[0]
    out.put("node", h.as_dict())
    out.raw(st.cheat_sheet([h.type], reg, cat))
    note = st.load_note(h.type)
    if note:
        out.raw("")
        out.raw("  ── 你的笔记 ──")
        out.put("note", note)
        for line in note.splitlines():
            out.raw(f"  {line}")
    else:
        out.raw("")
        out.raw(f"  （还没有笔记。`cwf store mark {h.type} -n \"怎么接/踩过什么坑\"` 记一条）")
    aliases = {k: v for k, v in st.load_aliases().items() if v == h.type}
    if aliases:
        out.raw(f"  已有别名：{'、'.join(aliases)}")


def cmd_store_mark(args, out: Out) -> None:
    """把一个节点记进知识库：起别名 + 写笔记 + 收藏，一次搞定。"""
    st = _store(args)
    reg, cat = _store_ctx(args)
    res = st.mark(_join_name(args.node), alias=args.alias, note=args.note,
                  pin=args.pin, reg=reg, cat=cat,
                  cat_target=getattr(args, "cat", None))
    out.put("marked", res)
    out.raw(f"  ✓ 记住 {res['type']}")
    if res["alias"]:
        out.raw(f"      别名「{res['alias']}」→ 以后直接用这个名字就行")
    if res["pinned"] is True:
        out.raw("      已加入常用（cwf store list 会列出来）")
    elif res["pinned"] is False:
        out.raw("      已从常用里移除")
    if res["note"]:
        out.raw(f"      笔记写入 {res['note']}")
    if res.get("cat"):
        out.raw(f"      分类改为「{res['cat']}」（写进 categories.txt，立刻生效）")
    if not (res["alias"] or res["note"] or res["pinned"] is not None):
        out.raw("      （没有指定 -a/-c/-n/-p，什么都没记。加一个吧）")


def cmd_store_list(args, out: Out) -> None:
    """列出沉淀过的东西：别名 / 常用 / 笔记。"""
    st = _store(args)
    reg, cat = _store_ctx(args)
    s = st.stats()
    out.put("store", {k: v for k, v in s.items() if k != "alias_list"})
    out.put("aliases", s["alias_list"])
    out.raw(f"节点知识库：{s['dir']}")
    out.raw(f"  别名 {s['aliases']} 条 · 常用 {s['pins']} 个 · 笔记 {s['notes']} 条")
    if not (s["aliases"] or s["pins"] or s["notes"]):
        out.raw("")
        out.raw("  （还是空的。用 `cwf store mark 节点 -a 别名 -n 笔记 -p` 开始沉淀）")
        return
    if s["pins"]:
        out.raw("")
        out.raw("── 常用 ──")
        for t in s["pin_list"]:
            h = st._from_spec(t, reg, cat, "pin")
            out.raw(f"  {t}" + (f"  ×{h.usage}" if h.usage else ""))
            if h.inputs:
                out.raw(f"      入: {'、'.join(h.inputs)}")
            if h.outputs:
                out.raw(f"      出: {'、'.join(h.outputs)}")
    if s["alias_list"]:
        out.raw("")
        out.raw("── 别名 ──")
        for a in sorted(s["alias_list"]):
            out.raw(f"  {a} → {s['alias_list'][a]}")
    if s["note_list"]:
        out.raw("")
        out.raw("── 笔记 ──")
        for t in sorted(s["note_list"]):
            out.raw(f"  {t}")


def cmd_store_note(args, out: Out) -> None:
    """给节点写/追加笔记。"""
    st = _store(args)
    reg, cat = _store_ctx(args)
    res = st.mark(args.node, note=args.text, reg=reg, cat=cat)
    out.put("note", res)
    out.raw(f"  ✓ {res['type']} 的笔记已写入 {res['note']}")
    if args.show:
        out.raw("")
        out.raw(st.load_note(res["type"]) or "")


def cmd_store_alias(args, out: Out) -> None:
    """起/看别名。"""
    st = _store(args)
    reg, cat = _store_ctx(args)
    if not args.alias:
        aliases = st.load_aliases()
        out.put("aliases", aliases)
        out.raw(f"自定义别名 {len(aliases)} 条：")
        for a in sorted(aliases):
            out.raw(f"  {a} → {aliases[a]}")
        out.raw("")
        out.raw("内置的中文别名（解码/主模型/放大…）见 `cwf nodes alias`")
        return
    res = st.mark(args.type, alias=args.alias, reg=reg, cat=cat)
    out.put("alias", res)
    out.raw(f"  ✓ 「{args.alias}」→ {res['type']}（以后所有命令都能用这个名字）")


def cmd_store_pin(args, out: Out) -> None:
    """收藏/取消收藏常用节点。"""
    st = _store(args)
    reg, cat = _store_ctx(args)
    res = st.mark(args.type, pin=True, reg=reg, cat=cat)
    out.put("pin", res)
    out.raw(f"  {'✓ 已收藏' if res['pinned'] else '✓ 已取消收藏'} {res['type']}")
    pins = st.load_pins()
    if pins:
        out.raw(f"  常用：{'、'.join(pins)}")


def cmd_store_export(args, out: Out) -> None:
    """导出成一份 markdown（换机器直接拷，人也能读）。"""
    st = _store(args)
    reg, cat = _store_ctx(args)
    text = st.export_markdown(reg, cat)
    out.put("markdown", text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        out.raw(f"✓ 已导出到 {args.out}（{len(text)} 字符）")
    else:
        p = st.write_readme(reg, cat)
        out.raw(f"✓ 已写入 {p}")
        out.raw(text)


# ---------------- 校验


def cmd_validate(args, out: Out) -> None:
    g = _load(args, out)
    reg = _reg(args)
    errs: List[str] = []
    warns: List[str] = []
    if not len(reg):
        warns.append("ComfyUI 没连上，跳过节点类型与模型文件校验（用 --refresh 试试）")

    # 类型
    for t, c in _missing_types(g, reg):
        errs.append(f"节点类型不存在：{t} ×{c}")
    # 模型
    for m, (cnt, who) in sorted(_missing_models(g, reg).items()):
        errs.append(f"模型文件不存在：{m}（{who}）")

    # 连线完整性
    for l in g.links:
        o, t = g.maybe(l.origin_id), g.maybe(l.target_id)
        if o is None:
            errs.append(f"连线 {l.id} 的来源节点 #{l.origin_id} 不存在")
            continue
        if t is None:
            errs.append(f"连线 {l.id} 的目标节点 #{l.target_id} 不存在")
            continue
        if l.origin_slot >= len(o.outputs):
            errs.append(f"连线 {l.id}: #{o.id}({o.type}) 没有第 {l.origin_slot} 个输出")
        if l.target_slot >= len(t.inputs):
            errs.append(f"连线 {l.id}: #{t.id}({t.type}) 没有第 {l.target_slot} 个输入")
    # 悬空 link 引用。注意：很多真实工作流的 outputs[].links 里存的是过期的、
    # 甚至根本不存在的 id（连 ComfyUI 自己都不看这个字段），所以这里只报
    # 「输入指向了不存在的连线」这种真会断线的问题，输出侧的过期 id 略过。
    ids = {l.id for l in g.links}
    for n in g.nodes:
        for s in n.inputs:
            if s.link is not None and s.link not in ids:
                errs.append(f"#{n.id}({n.type}) 的输入 {s.name} 指向已不存在的连线 {s.link}")
    # 类型不匹配
    for l in g.links:
        o, t = g.maybe(l.origin_id), g.maybe(l.target_id)
        if not o or not t or l.origin_slot >= len(o.outputs) or l.target_slot >= len(t.inputs):
            continue
        so = o.outputs[l.origin_slot].type
        si = t.inputs[l.target_slot].type
        if so not in ("*",) and si not in ("*", "COMBO", "") and so.upper() != si.upper():
            warns.append(f"类型可疑：#{o.id}.{o.outputs[l.origin_slot].name}({so}) → "
                         f"#{t.id}.{t.inputs[l.target_slot].name}({si})")
    # 必要输入缺失
    if len(reg):
        for n in g.nodes:
            if n.is_note or n.mode == 2:
                continue
            spec = reg.get(n.type)
            if spec is None:
                continue
            for i in spec.link_inputs:
                if i.optional or i.type.upper() in ("IMAGEUPLOAD", "AUDIOUPLOAD",
                                                    "VIDEOUPLOAD"):
                    continue
                s = n.input(i.name)
                if s is None or s.link is None:
                    if n.mode == 4:
                        continue
                    warns.append(f"#{n.id}({n.type}) 的必要输入 {i.name} 没接线")
    # 孤儿 / 无输出
    for n in g.orphans():
        if not n.is_virtual:
            warns.append(f"孤儿节点 #{n.id}({n.type}) 没有任何连线")
    if not g.terminals():
        warns.append("这张图没有任何终结点（SaveImage / PreviewImage 之类），跑起来看不到结果")

    out.put("errors", errs)
    out.put("warnings", warns)
    out.put("error_count", len(errs))
    out.put("warning_count", len(warns))
    out.raw(f"{os.path.basename(g.path or '')}：{len(errs)} 个错误，{len(warns)} 个警告")
    for e in errs:
        out.raw(f"  ✖ {e}")
    for w in warns:
        out.raw(f"  ⚠ {w}")
    if not errs and not warns:
        out.raw("  ✓ 没发现问题")
    if errs and args.strict:
        raise SystemExit(1)
    if args.json:
        out.finish(ok=not errs)


# ---------------- 排版


def _layout_opts(args) -> strata.LayoutOptions:
    o = strata.LayoutOptions.from_flags(
        compact=getattr(args, "compact", False),
        loose=getattr(args, "loose", False))
    for name in ("h_gap", "v_gap", "group_pad", "margin", "grid"):
        v = getattr(args, name.replace("-", "_"), None)
        if v is not None:
            setattr(o, name, float(v))
    if getattr(args, "no_groups", False):
        o.allow_groups = False
    if getattr(args, "direction", None):
        o.direction = args.direction
    if getattr(args, "strays", None):
        o.strays = args.strays
    if getattr(args, "keep_notes", False):
        o.notes = "keep"
    return o


def cmd_layout(args, out: Out) -> None:
    g = _load(args, out)
    reg = _reg(args)
    before = g.bounds()
    opts = _layout_opts(args)
    rep = strata.layout(g, opts, reg)
    after = g.bounds()
    out.put("report", rep.as_dict())
    out.put("before", {"w": before[2] - before[0], "h": before[3] - before[1]})
    out.put("after", {"w": after[2] - after[0], "h": after[3] - after[1]})
    out.raw(f"排版完成：{rep.layers} 层 · 交叉 {rep.crossings_before} → {rep.crossings}")
    out.raw(f"画布 {before[2]-before[0]:.0f}×{before[3]-before[1]:.0f} → "
            f"{after[2]-after[0]:.0f}×{after[3]-after[1]:.0f}"
            f"（每节点面积 {rep.area_per_node:.0f} px²）")
    out.raw(f"分区框 {rep.groups} 个 · 隔离 {rep.strays} · 注释 {rep.notes}")
    if rep.issues:
        out.raw("⚠ 自检发现问题：")
        for i in rep.issues:
            out.raw(f"    {i}")
    else:
        out.raw("✓ 自检通过：无节点重叠，分区框互不压边")
    if args.out:
        _save(g, args.out, out)
    else:
        out.raw("（未指定 --out，没有写文件；加上 --out 路径 或 --in-place 保存）")
    out.finish()


def cmd_beautify(args, out: Out) -> None:
    """排版 + 重命名标题 + 重排序号，一条命令把图收拾干净。"""
    g = _load(args, out)
    reg = _reg(args)
    rep = strata.layout(g, _layout_opts(args), reg)
    renamed = 0
    if not args.no_title and not args.no_titles:
        for n in g.nodes:
            if n.is_note:
                continue
            if n.title and n.title.strip():
                continue
            spec = reg.get(n.type)
            pretty = (spec.display_name if spec else None) or n.type
            n.title = pretty
            renamed += 1
    if getattr(args, "renumber", False):
        for i, n in enumerate(sorted(g.nodes, key=lambda z: (z.pos[0], z.pos[1])), 1):
            if n.is_note:
                continue
            base = (n.title or n.type).split(". ", 1)[-1]
            n.title = f"{i:02d}. {base}"
    g.reorder()
    out.put("report", rep.as_dict())
    out.put("retitled", renamed)
    out.raw(f"✓ 美化完成：{rep.layers} 层 / {rep.crossings} 交叉 / {rep.groups} 分区框"
            + (f" / 补了 {renamed} 个节点标题" if renamed else ""))
    if rep.issues:
        for i in rep.issues[:6]:
            out.raw(f"  ⚠ {i}")
    started = time.time()
    if args.in_place:
        if g.path is None:
            die("这个图没有来源路径，不能 --in-place")
        backup = g.path + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(g.path, backup)
        out.put("backup", backup)
        out.raw(f"  原文件已备份 → {backup}")
        _save(g, g.path, out)
    elif args.out:
        _save(g, args.out, out)
    else:
        out.raw("（未指定 --out / --in-place，没有写文件）")
    out.raw(f"  用时 {time.time() - started:.2f}s")
    out.finish()


# ---------------- 编辑


def _edit_target(args, out: Out) -> Tuple[Graph, Optional[str]]:
    """载入待编辑的图，返回 (graph, 保存路径)。"""
    g = _load(args, out)
    save_to = None
    if getattr(args, "in_place", False):
        save_to = g.path
    elif getattr(args, "out", None):
        save_to = args.out
    return g, save_to


def _finish_edit(g: Graph, save_to: Optional[str], args, out: Out) -> None:
    reg = _reg(args)
    if getattr(args, "layout", False):
        rep = strata.layout(g, _layout_opts(args), reg)
        out.put("layout", rep.as_dict())
        out.raw(f"  ↻ 已重新排版（{rep.layers} 层，{rep.crossings} 交叉）")
    g.reorder()
    out.put("summary", g.summary())
    if save_to:
        if getattr(args, "in_place", False) and g.path:
            backup = g.path + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(g.path, backup)
            out.put("backup", backup)
            out.raw(f"  原文件备份 → {backup}")
        _save(g, save_to, out)
    else:
        out.raw("（未指定 --out / --in-place，没有写文件）")
    out.finish()


def cmd_set(args, out: Out) -> None:
    g, save_to = _edit_target(args, out)
    done = []
    for spec in args.set:
        # 从右往左切，节点选择器吃最左边那段，这样
        # `type:CLIPTextEncode:text=xxx` 这种带冒号的选择器也不会切错
        target, sep, kv = spec.rpartition(":")
        key, eq, val = kv.partition("=")
        if not target or not key or not sep or not eq:
            die(f"--set 要写成 节点:控件=值，收到 {spec!r}\n"
                f"  节点可以是 #12 / 标题子串 / type:KSampler / title:采样 / ~正则")
        nodes = g.find_all(target)
        if not nodes:
            die(f"{target!r} 没匹配到任何节点（可用 type:类型名 / #id / 标题子串）")
        # 先全体预检：只要有一个节点没这个控件就整批不动 —— 避免「改了一半崩了」
        key = key.strip()
        missing = [f"#{n.id}({n.type})" for n in nodes if not n.has_widget(key)]
        if missing:
            raise CwfError(
                f"{target!r} 里有 {len(missing)} 个节点没有控件 {key!r}：{missing[:6]}；"
                f"换个更精确的选择器（#id / title:标题）或换个控件名"
                f"（本次没有改动任何内容）")
        for node in nodes:
            try:
                node.set_widget(key, parse_value(val))
            except Exception as e:
                raise CwfError(f"改 #{node.id}({node.type}).{key} 失败：{e}")
            msg = f"#{node.id} {node.type}.{key} = {val}"
            done.append(msg)
            out.raw(f"  ✓ {msg}")
        if len(nodes) > 1:
            out.raw(f"    （{target!r} 匹配到 {len(nodes)} 个节点，全部改了）")
    out.put("changes", done)
    _finish_edit(g, save_to, args, out)


def cmd_rename(args, out: Out) -> None:
    g, save_to = _edit_target(args, out)
    spec = args.rename if isinstance(args.rename, str) else " ".join(args.rename)
    if "=" not in spec:
        die('--rename 要写成 "节点=新标题"，例如 --rename "5. 采样=主采样器"')
    target, _, new_title = spec.partition("=")
    nodes = g.find_all(target)
    if not nodes:
        die(f"{target!r} 没匹配到任何节点")
    for node in nodes:
        node.title = new_title.strip()
        out.raw(f"  ✓ #{node.id} {node.type} → {node.title}")
    out.put("renamed", [{"id": n.id, "title": n.title} for n in nodes])
    _finish_edit(g, save_to, args, out)


def cmd_mode(args, out: Out) -> None:
    g, save_to = _edit_target(args, out)
    mode = {"normal": 0, "on": 0, "mute": 2, "静音": 2, "bypass": 4, "旁路": 4}[args.mode]
    nodes = g.find_all(args.node)
    if not nodes:
        die(f"{args.node!r} 没匹配到任何节点（可用 type:类型名 / #id / 标题子串）")
    for node in nodes:
        node.mode = mode
        out.raw(f"  ✓ #{node.id} {node.type} → {args.mode}（mode={mode}）")
    out.put("mode", mode)
    out.put("changed", len(nodes))
    _finish_edit(g, save_to, args, out)


def cmd_add(args, out: Out) -> None:
    g, save_to = _edit_target(args, out)
    reg = _reg(args)
    spec = reg.get(args.type)
    if spec is None and len(reg):
        hits = reg.search(args.type, limit=5)
        tip = "、".join(h.type for h in hits) if hits else "（无）"
        die(f"没有节点类型 {args.type!r}。相近的：{tip}")
    kwargs = {}
    for spec_s in (args.arg or []):
        k, _, v = spec_s.partition("=")
        kwargs[k] = parse_value(v)
    b = __import__("cwf.lib.dsl", fromlist=["Builder"]).Builder(reg, base=g)
    n = b.make_node(args.type, title=args.title, widgets=kwargs)
    if args.title:
        n.title = args.title
    out.put("node", {"id": n.id, "type": n.type, "title": n.title})
    out.raw(f"  ✓ 新增 #{n.id} {n.type} {n.title or ''}")
    if kwargs:
        out.raw(f"     参数: {kwargs}")
    _finish_edit(g, save_to, args, out)


def cmd_remove(args, out: Out) -> None:
    g = _load(args, out)
    nodes = [g.find(t) for t in args.node]
    keep = getattr(args, "keep_wiring", False)
    info = []
    for n in nodes:
        info.append({"id": n.id, "type": n.type, "title": n.title})
        g.remove_node(n, keep_wiring=keep)
    out.put("removed", info)
    out.raw(f"  ✓ 删除 {len(info)} 个节点" + ("（已焊接上下游）" if keep else ""))
    for i in info:
        out.raw(f"      - #{i['id']} {i['type']}")
    _finish_edit(g, g.path if args.in_place else args.out, args, out)


def cmd_connect(args, out: Out) -> None:
    g = _load(args, out)
    src, dst = args.pair
    a = g.find(src.split(".")[0].split("[")[0])
    b = g.find(dst.split(".")[0].split("[")[0])
    sa = src.split(".", 1)[1] if "." in src else 0
    sb = dst.split(".", 1)[1] if "." in dst else 0
    lk = g.connect(a, sa, b, sb)
    out.put("link", {"id": lk.id, "from": lk.origin_id, "to": lk.target_id,
                     "type": lk.type})
    out.raw(f"  ✓ #{a.id}.{sa} → #{b.id}.{sb}  ({lk.type})")
    _finish_edit(g, g.path if args.in_place else args.out, args, out)


def cmd_disconnect(args, out: Out) -> None:
    g = _load(args, out)
    if args.link:
        n = g.disconnect(link_id=args.link)
    elif args.to:
        tgt = args.to[0]
        node = g.find(tgt.split(".")[0].split("[")[0])
        slot = tgt.split(".", 1)[1] if "." in tgt else None
        n = g.disconnect(dst=node, dst_slot=slot)
    else:
        die("要 --link 编号 或 --to 节点.输入")
    out.put("disconnected", n)
    out.raw(f"  ✓ 断开 {n} 条连线")
    _finish_edit(g, g.path if args.in_place else args.out, args, out)


def _src_node(g: Graph, ref: str) -> Node:
    """解析端点里的 `节点[.端口]` —— 节点部分支持全部选择器写法。"""
    node_part = ref.split(".", 1)[0].split("[", 1)[0].strip()
    hits = g.find_all(node_part)
    if not hits:
        raise CwfError(f"{ref!r} 里的节点 {node_part!r} 没匹配到（可用 type:类型名 / #id / 标题子串）")
    if len(hits) > 1:
        # 多个候选时，优先挑有连线的（插节点场景下这条更符合意图）
        hits.sort(key=lambda n: -(len(g.incoming(n.id)) + len(g.outgoing(n.id))))
    return hits[0]


def cmd_insert(args, out: Out) -> None:
    """把新节点插进一条已有连线上：A→B 变成 A→新→B。"""
    g = _load(args, out)
    reg = _reg(args)
    spec = reg.get(args.type)
    if spec is None and len(reg):
        hits = reg.search(args.type, limit=5)
        tip = "、".join(h.type for h in hits) if hits else "（无）"
        die(f"没有节点类型 {args.type!r}。相近的：{tip}")
    src = _src_node(g, args.on[0])
    dst = _src_node(g, args.on[1])
    lk = next((l for l in g.links if l.origin_id == src.id and l.target_id == dst.id), None)
    if lk is None:
        die(f"#{src.id} 没有直接连到 #{dst.id}，没法插进去（先 cwf connect）")
    kwargs = {}
    for s in (args.arg or []):
        k, _, v = s.partition("=")
        kwargs[k] = parse_value(v)
    from cwf.lib.dsl import Builder
    b = Builder(reg, base=g)
    n = b.make_node(args.type, title=args.title, widgets=kwargs)
    out_slot_type = src.outputs[lk.origin_slot].type
    in_slot = None
    for i, s in enumerate(n.inputs):
        if s.type in (out_slot_type, "*") or out_slot_type in ("*",):
            in_slot = i
            break
    if in_slot is None:
        die(f"{args.type} 没有能接收 {out_slot_type} 的输入；"
            f"可选: {[s.name + ':' + s.type for s in n.inputs]}")
    out_slot = None
    want = dst.inputs[lk.target_slot].type
    for i, s in enumerate(n.outputs):
        if s.type in (want, "*") or want in ("*", "COMBO"):
            out_slot = i
            break
    if out_slot is None:
        die(f"{args.type} 没有能产出 {want} 的输出")
    src_slot, dst_slot = lk.origin_slot, lk.target_slot
    g.disconnect(link_id=lk.id)
    g.connect(src, src_slot, n, in_slot)
    g.connect(n, out_slot, dst, dst_slot)
    n.pos = ((src.right + dst.pos[0]) / 2 - n.width / 2,
             (src.pos[1] + dst.pos[1]) / 2)
    out.put("node", {"id": n.id, "type": n.type})
    out.raw(f"  ✓ 已插入 #{n.id} {n.type}：#{src.id} → #{n.id} → #{dst.id}")
    _finish_edit(g, g.path if args.in_place else args.out, args, out)


def cmd_splice(args, out: Out) -> None:
    """删掉一个节点但保留数据流（把它的上下游接直）。"""
    g = _load(args, out)
    node = g.find(args.node)
    info = {"id": node.id, "type": node.type}
    g.remove_node(node, keep_wiring=True)
    out.put("removed", info)
    out.raw(f"  ✓ 已摘掉 #{info['id']} {info['type']}，上下游已接直")
    _finish_edit(g, g.path if args.in_place else args.out, args, out)


def cmd_mute_group(args, out: Out) -> None:
    g = _load(args, out)
    targets = []
    for t in args.node:
        targets.append(g.find(t))
    mode = 4 if args.mode in ("bypass", "旁路") else 2
    for n in targets:
        n.mode = mode
    out.put("count", len(targets))
    out.raw(f"  ✓ {len(targets)} 个节点 → {'旁路' if mode == 4 else '静音'}")
    _finish_edit(g, g.path if args.in_place else args.out, args, out)


# ---------------- 构建 (DSL)


def cmd_build(args, out: Out) -> None:
    src = args.file
    text = sys.stdin.read() if src in ("-", None) else open(src, "r", encoding="utf-8").read()
    reg = _reg(args)
    if not len(reg) and not args.offline:
        die("ComfyUI 没连上，建图需要节点字典。先启动 ComfyUI，或 `cwf schema refresh`")
    base = None
    if args.base:
        base = Graph.load(resolve_workflow(args.base))
        out.raw(f"基于已有工作流：{base.path}")
    res = dsl_parse(text, reg, base=base)
    g = res.graph
    for w in res.warnings:
        out.raw(f"  ⚠ {w}")
    out.put("warnings", res.warnings)
    out.put("nodes", [{"id": n.id, "type": n.type, "title": n.title}
                      for n in g.nodes])
    out.raw(f"✓ DSL 解析完成：新增 {len(g) - (len(base) if base else 0)} 个节点，"
            f"共 {len(g)} 节点 / {len(g.links)} 连线")
    if not args.no_layout:
        rep = strata.layout(g, _layout_opts(args), reg)
        out.put("layout", rep.as_dict())
        out.raw(f"  ↻ 自动排版：{rep.layers} 层 · 交叉 {rep.crossings} · "
                f"{rep.width:.0f}×{rep.height:.0f} · 分区框 {rep.groups}")
        if rep.issues:
            for i in rep.issues[:5]:
                out.raw(f"  ⚠ {i}")
    if args.validate:
        errs = _quick_validate(g, reg)
        out.put("validate", errs)
        for e in errs:
            out.raw(f"  ✗ {e}")
        if not errs:
            out.raw("  ✓ 校验通过")
    dest = resolve_dest(args.out, args.name or f"cwf-{int(time.time())}",
                        os.path.join(WF_DEFAULT, "cwf_new"))
    if args.print_only:
        out.put("json_preview", g.to_ui())
        out.raw("（--print-only：没有写文件）")
    else:
        _save(g, dest, out)
    out.finish()


def _quick_validate(g: Graph, reg: Registry) -> List[str]:
    errs: List[str] = []
    for t, c in _missing_types(g, reg):
        errs.append(f"节点类型不存在：{t} ×{c}")
    for l in g.links:
        o, t = g.maybe(l.origin_id), g.maybe(l.target_id)
        if o is None or t is None:
            errs.append(f"连线 {l.id} 指向不存在的节点")
            continue
        if l.origin_slot >= len(o.outputs):
            errs.append(f"连线 {l.id}: #{o.id} 没有第 {l.origin_slot} 个输出")
        if l.target_slot >= len(t.inputs):
            errs.append(f"连线 {l.id}: #{t.id} 没有第 {l.target_slot} 个输入")
    if len(reg):
        for n in g.nodes:
            spec = reg.get(n.type)
            if spec is None or n.is_note or n.mode == 2:
                continue
            for i in spec.link_inputs:
                if i.optional or i.type.upper() in ("IMAGEUPLOAD", "AUDIOUPLOAD",
                                                    "VIDEOUPLOAD"):
                    continue
                s = n.input(i.name)
                if (s is None or s.link is None) and n.mode != 4:
                    errs.append(f"#{n.id}({n.type}) 必要输入 {i.name} 没接线")
    return errs


def cmd_new(args, out: Out) -> None:
    """交互/脚本式建图：cwf new --node Type a=1 --node Type2 b=2 --link ..."""
    raise CwfError("请改用 `cwf build`（DSL）或 `cwf add`（加单个节点）")


def cmd_scaffold(args, out: Out) -> None:
    """吐出可直接编辑的 DSL 骨架——给 agent 一个起手模板。"""
    tpl = TEMPLATES.get(args.template)
    if tpl is None:
        die(f"没有模板 {args.template!r}。可用: {', '.join(TEMPLATES)}")
    out.raw(tpl)
    if args.json:
        out.put("template", args.template)
        out.put("dsl", tpl)
        out.finish()


TEMPLATES: Dict[str, str] = {
    "txt2img": """# 文生图骨架 —— 改提示词和模型名就能跑
@title 文生图
模型   CheckpointLoaderSimple  ckpt_name=改成你的模型.safetensors  标题="1. 主模型"
正向   CLIPTextEncode          text="1girl, solo, masterpiece, best quality"  标题="2. 正向提示词"
负向   CLIPTextEncode          text="lowres, bad anatomy, worst quality"      标题="3. 负向提示词"
潜图   EmptyLatentImage        width=832 height=1216 batch_size=1           标题="4. 空潜空间"
采样   KSampler                seed=0 steps=28 cfg=6.5 sampler_name=euler scheduler=normal denoise=1.0  标题="5. 采样"
解码   VAEDecode               # 只要一个参数都没有时连端口也省了
保存   SaveImage               filename_prefix=CWF/txt2img

模型.MODEL -> 采样.model
模型.CLIP  -> 正向.clip
模型.CLIP  -> 负向.clip
模型.VAE   -> 解码.vae
正向.CONDITIONING -> 采样.positive
负向.CONDITIONING -> 采样.negative
潜图.LATENT -> 采样.latent
采样.LATENT -> 解码.samples
解码.IMAGE  -> 保存.images
""",
    "img2img": """# 图生图骨架
@title 图生图
模型   CheckpointLoaderSimple  ckpt_name=改成你的模型.safetensors
原图   LoadImage               image=输入图.png
正向   CLIPTextEncode          text="a beautiful photo"
负向   CLIPTextEncode          text="blurry, lowres"
编码   VAEEncode
采样   KSampler                seed=0 steps=24 cfg=6.0 denoise=0.6
解码   VAEDecode
保存   SaveImage               filename_prefix=CWF/img2img

模型.MODEL -> 采样.model
模型.CLIP  -> 正向.clip
模型.CLIP  -> 负向.clip
模型.VAE   -> 编码.vae
模型.VAE   -> 解码.vae
原图.IMAGE -> 编码.pixels
正向.CONDITIONING -> 采样.positive
负向.CONDITIONING -> 采样.negative
编码.LATENT -> 采样.latent
采样.LATENT -> 解码.samples
解码.IMAGE  -> 保存.images
""",
    "h3_ref2va": """# MiniMax H3 参考生视频骨架（按本机节点名调整）
@title H3 参考生视频
unet   UNETLoader             unet_name=改成你的H3模型.safetensors weight_dtype=default
clip   CLIPLoader             clip_name=改成你的文本编码器.safetensors type=minimax
vae    VAELoader              vae_name=改成你的视频vae.safetensors
音频vae VAELoader             vae_name=改成你的音频vae.safetensors
参考   LoadImage              image=参考图.png
提示   CLIPTextEncode         text="整段自然语言提示词写在这里"

# 跳线示例：模型/VAE 复用到多个地方时，用 $名字 存一次、取多次
unet.MODEL -> $model
vae.VAE    -> $video_vae
音频vae.VAE -> $audio_vae
clip.CLIP  -> $clip

采样   # ← 在这里填你的 H3 采样节点类型；接线用 $model / $clip / $video_vae
保存   SaveVideo              filename_prefix=CWF/h3_ref2va
""",
    "blank": """@title 新工作流

# 一行一个节点：短名  节点类型  参数=值 ...
# 一行一条连线：源.输出 -> 目标.输入
""",
}


def cmd_dsl_check(args, out: Out) -> None:
    """只解析 DSL 不建图，用来快速验证语法。"""
    text = sys.stdin.read() if args.file in ("-", None) else \
        open(args.file, "r", encoding="utf-8").read()
    reg = _reg(args)
    try:
        res = dsl_parse(text, reg)
    except CwfError as e:
        out.put("error", str(e))
        out.raw(f"✖ {e}")
        out.finish(ok=False)
        raise SystemExit(1)
    out.put("ok", True)
    out.put("nodes", len(res.graph))
    out.put("links", len(res.graph.links))
    out.put("warnings", res.warnings)
    out.raw(f"✓ 语法通过：{len(res.graph)} 节点 / {len(res.graph.links)} 连线")
    for w in res.warnings:
        out.raw(f"  ⚠ {w}")
    out.finish()


# ---------------- 模块拆装


def cmd_pack_split(args, out: Out) -> None:
    path = resolve_workflow(args.workflow)
    spec = packmod.split_pack(args, path)
    out.put("pack", spec)
    out.raw(f"✓ 拆出模块 {spec['name']}：{len(spec['nodes'])} 节点 / "
            f"{len(spec['links'])} 内部连线")
    out.raw(f"  对外接口 入 {len(spec.get('inputs', []))} 个，"
            f"出 {len(spec.get('outputs', []))} 个")
    if args.dir:
        p = packmod.save_pack(spec, args.dir)
        out.put("written", p)
        out.raw(f"  已存到 {p}")


def cmd_pack_list(args, out: Out) -> None:
    packs = packmod.list_packs(args.dir)
    out.put("dir", os.path.abspath(args.dir))
    out.put("packs", packs)
    out.raw(f"{os.path.abspath(args.dir)}  共 {len(packs)} 个模块")
    for p in packs:
        out.raw(f"  {p['name']:<32} {p['nodes']:>3} 节点  {p.get('description', '')}")


def cmd_pack_show(args, out: Out) -> None:
    spec = packmod.load_pack(args.name, args.dir)
    out.put("pack", spec)
    out.raw(f"{spec['name']}  —— {spec.get('description', '')}")
    out.raw(f"  节点 {len(spec['nodes'])}，内部连线 {len(spec['links'])}")
    out.raw("  入口:")
    for i in spec.get("inputs", []):
        out.raw(f"    {i['name']:<20} {i['type']:<12} → 节点 {i['node']} . {i['slot']}")
    out.raw("  出口:")
    for o in spec.get("outputs", []):
        out.raw(f"    {o['name']:<20} {o['type']:<12} ← 节点 {o['node']} . {o['slot']}")


def cmd_pack_use(args, out: Out) -> None:
    """把模块实例化进一张图（新建或并入已有）。"""
    g = Graph.load(resolve_workflow(args.workflow)) if args.workflow else Graph()
    reg = _reg(args)
    res = packmod.instantiate(g, args.name, args.dir, args.as_,
                              prefix=args.prefix, rename=args.rename)
    out.put("nodes", res["nodes"])
    out.put("aliases", res.get("aliases", {}))
    out.put("inputs", res["inputs"])
    out.put("outputs", res["outputs"])
    out.raw(f"✓ 模块 {args.name} 已实例化（前缀 {res['prefix']}）")
    out.raw(f"  节点 {len(res['nodes'])} 个: #{res['nodes'][0]}…#{res['nodes'][-1]}")
    out.raw(f"  待接入口 {len(res['inputs'])} 个：")
    for i in res["inputs"]:
        out.raw(f"    {i['alias']}  ({i['type']})")
    out.raw(f"  可接出口 {len(res['outputs'])} 个：")
    for o in res["outputs"]:
        out.raw(f"    {o['alias']}  ({o['type']})")
    if args.layout:
        rep = strata.layout(g, _layout_opts(args), reg)
        out.put("layout", rep.as_dict())
        out.raw(f"  ↻ 已排版（{rep.layers} 层）")
    dest = args.out
    if dest:
        _save(g, dest, out)
    else:
        out.raw("（未指定 --out，没有写文件）")
    out.finish()


# ---------------- 运行


def _guard_cfg() -> Dict[str, Any]:
    """执行护栏的可调参数。

    默认值偏保守，但不该绑死任何人 —— 都可以用环境变量覆盖：

        CWF_VRAM_GB=24        声明显存，警告语会带上它，画布阈值也跟着放宽
        CWF_MAX_MP=8          自定义「大画布」告警阈值（百万像素）
        CWF_GUARD=off         完全不拦，只报告
        CWF_GUARD_ALLOW=controlnet,video   把某类预先放行
    """
    vram = None
    raw = os.environ.get("CWF_VRAM_GB")
    if raw:
        try:
            vram = float(raw)
        except ValueError:
            vram = None

    mp = None
    raw = os.environ.get("CWF_MAX_MP")
    if raw:
        try:
            mp = float(raw)
        except ValueError:
            mp = None
    if mp is None:
        # 显存越大越能扛大画布；不知道显存时用 2 MP（约 1440×1440）
        mp = 2.0 if vram is None else max(1.0, min(16.0, vram * 0.25))

    allow = {s.strip().lower() for s in
             os.environ.get("CWF_GUARD_ALLOW", "").split(",") if s.strip()}
    return {
        "vram": vram,
        "max_mp": mp,
        "enabled": os.environ.get("CWF_GUARD", "").strip().lower() != "off",
        "allow": allow,
    }


def _hw_guard(g: Graph, reg: Registry) -> Dict[str, Any]:
    """**执行前护栏**：这台机器大概率跑不动的活，在提交之前拦下来。

    为什么做成命令级拦截而不是「注意事项」：注意事项靠人记得住，而这里
    容不下一次记不住 —— 一次 OOM 就可能把正在跑的长任务打断，代价远大于
    多点一次 `--allow-xxx`。所以默认拒绝，要跑必须显式放行。

    判定只看工作流本身，不看你是谁：
      * **ControlNet 类节点** —— 显存和内存占用都很凶，最容易 OOM
      * **视频生成类节点** —— 长任务，崩一次损失大
      * **超大潜空间** —— 单帧像素超过阈值，多半放不下

    阈值与开关全部可用环境变量调（见 `_guard_cfg`），也支持
    `--allow-controlnet` / `--allow-video` 单次放行。
    """
    cfg = _guard_cfg()
    res: Dict[str, Any] = {"controlnet": [], "video": [], "big": [],
                           "warnings": [], "cfg": cfg}
    cn_names = ("controlnet", "control_net", "cn_apply", "acn_")
    vid_names = ("wanvideo", "wanimage", "wanphantom", "minimaxh3", "h3_t8",
                 "hunyuan", "ltxv", "ltx_", "svd", "framepack", "animatediff",
                 "cogvideo", "mochi", "savevideo", "createvideo", "vhs_",
                 "videocombine", "rife", "svfi")
    vid_ports = {"VIDEO", "COMFYTV_VIDEO", "WANVIDIMAGE_EMBEDS",
                 "WANVIDEOTEXTEMBEDS", "WANVIDEOMODEL"}

    for n in g.nodes:
        if n.is_note or n.mode == 2:
            continue
        low = (n.type or "").lower()
        ports = ({s.type.upper() for s in n.inputs}
                 | {s.type.upper() for s in n.outputs})
        if any(k in low for k in cn_names) or "CONTROL_NET" in ports:
            res["controlnet"].append(n.id)
        if any(k in low for k in vid_names) or (ports & vid_ports):
            res["video"].append(n.id)

    for n in g.nodes:
        if n.type in ("EmptyLatentImage", "EmptySD3LatentImage",
                      "EmptyFlux2LatentImage", "EmptyLatentImagePresets"):
            try:
                w = float(n.widget_value("width"))
                h = float(n.widget_value("height"))
                if w * h >= cfg["max_mp"] * 1e6:
                    res["big"].append((n.id, int(w), int(h)))
            except Exception:
                pass

    hw = (f"{cfg['vram']:g} GB 显存" if cfg["vram"] else "本机显存")

    if res["controlnet"]:
        res["warnings"].append(
            f"含 ControlNet 相关节点 {len(res['controlnet'])} 个 —— "
            f"这类节点吃显存和内存都很凶，{hw} 下容易爆")
    if res["video"]:
        res["warnings"].append(
            f"含视频生成相关节点 {len(res['video'])} 个 —— 长任务，"
            f"中途 OOM 会白跑")
    for nid, w, h in res["big"]:
        res["warnings"].append(
            f"#{nid} 的潜空间是 {w}×{h}（{w * h / 1e6:.1f} MP），"
            f"超过设定上限 {cfg['max_mp']:g} MP，{hw} 下很可能放不下")
    return res


def _bar(used: float, total: float, width: int = 22) -> str:
    """画一条占用条。used/total 同单位。"""
    if total <= 0:
        return "─" * width
    r = max(0.0, min(1.0, used / total))
    n = int(round(r * width))
    return "█" * n + "░" * (width - n)


def _rig_device(args, comfy_root: str = "") -> "rig.Device":
    """探测设备，并把 ComfyUI 日志里的实测速度挂上去。"""
    dev = rig.detect_device(getattr(args, "server", DEFAULT_SERVER),
                            offline=getattr(args, "offline", False))
    root = comfy_root or paths.comfy_root()
    if root and not getattr(args, "no_logs", False):
        rig.attach_measurements(dev, rig.mine_logs(rig.find_logs(root)))
    return dev


def cmd_rig(args, out: Out) -> None:
    """设备能力画像：显存 / 内存 / 架构 / 实测速度。"""
    root = paths.comfy_root()
    dev = _rig_device(args, root)
    out.put("device", dev.as_dict())

    out.raw("设备能力画像")
    out.raw("─" * 60)
    if not dev.vram_total and not dev.ram_total:
        out.raw("  ✖ 什么都没探测到。")
        out.raw("    N 卡装了驱动的话，确认 nvidia-smi 在 PATH 里；")
        out.raw("    或者启动 ComfyUI 后用 --server 指过去。")
        out.finish(ok=False)
        return

    out.raw(f"  显卡      {dev.gpu_name or '未知'}")
    out.raw(f"  架构      {dev.arch}")
    out.raw("")
    if dev.vram_total:
        used = dev.vram_total - dev.vram_free
        out.raw("  显存      %s / %s  %s" % (
            rig.gb(used), rig.gb(dev.vram_total),
            _bar(used, dev.vram_total)))
        out.raw("            可用 %s（扣掉驱动与 CUDA 上下文后约 %s）"
                % (rig.gb(dev.vram_free), rig.gb(dev.vram_usable)))
    if dev.ram_total:
        used = dev.ram_total - dev.ram_free
        out.raw("  内存      %s / %s  %s" % (
            rig.gb(used), rig.gb(dev.ram_total),
            _bar(used, dev.ram_total)))
        out.raw("            可用 %s" % rig.gb(dev.ram_free))

    if dev.measured_sit:
        sit = dev.measured_sit
        out.raw("")
        out.raw("  实测速度  日志里共 %d 条迭代记录" % len(sit))
        out.raw("            中位 %.2f 秒/迭代 · 最快 %.2f · 最慢 %.2f"
                % (statistics.median(sit), min(sit), max(sit)))
    if dev.measured_runs:
        secs = [t for t, _ in dev.measured_runs]
        out.raw("  整单耗时  最近 %d 次：%s"
                % (len(secs), " · ".join("%.0fs" % t for t in secs)))

    out.raw("")
    out.raw(f"  数据来源  {dev.source}")
    if dev.comfy_version:
        out.raw(f"  ComfyUI   {dev.comfy_version} · Python {dev.python_version}")
    out.finish()


def _short_path(p: str, limit: int = 46) -> str:
    p = str(p).replace("\\", "/")
    return p if len(p) <= limit else "…" + p[-(limit - 1):]


def _print_load(out: Out, load, dev, fit) -> None:
    name = os.path.basename(out.data.get("path") or "") or "工作流"
    out.raw(f"负载画像：{name}")
    out.raw("─" * 60)

    out.raw("  权重合计  %s%s" % (
        rig.gb(load.weights),
        "   ← 磁盘实测，误差 <1%" if load.weights else ""))
    uniq: Dict[str, object] = {}
    for m in load.models:
        if m.found:
            uniq.setdefault(os.path.normcase(m.path), m)
    resident = sorted([m for m in uniq.values() if m.role == "resident"],
                      key=lambda m: -m.size)
    for m in resident[:8]:
        out.raw("    %-13s %9s  %s" % (m.folder, rig.gb(m.size),
                                       _short_path(m.value)))
    if len(resident) > 8:
        out.raw("    （另有 %d 个较小的常驻文件未列出）" % (len(resident) - 8))

    if load.patches:
        tot = sum(p.size for p in load.patches)
        out.raw("")
        out.raw("  补丁合计  %s   （%d 个 LoRA，合并进基座）" % (rig.gb(tot), len(load.patches)))
        for p in sorted(load.patches, key=lambda m: -m.size)[:5]:
            out.raw("    %-13s %9s  %s" % (p.folder, rig.gb(p.size),
                                           _short_path(p.value)))
        out.raw("    * 补丁不额外增加常驻峰值：它改写基座权重，")
        out.raw("      占用仍约等于基座那份。")

    if load.missing:
        out.raw("")
        out.raw("  ⚠ %d 个引用的模型本机没有，权重合计偏小：" % len(load.missing))
        for m in load.missing[:5]:
            out.raw("      %s   (用在 #%d %s)" % (m.value, m.node_id, m.node_type))

    out.raw("")
    out.raw("  激活项    %s   （估，不确定度 ±50%%）" % rig.gb(load.activation))
    if load.width and load.height:
        out.raw("    潜空间 %d×%d = %.2f MP · %d 帧 · 架构判定 %s"
                % (load.width, load.height, load.pixels / 1e6,
                   load.frames, load.arch_hint))
    out.raw("  运行时开销 %s   （CUDA 上下文 + 驱动 + 碎片）"
            % rig.gb(load.overhead))

    if not dev or not dev.vram_total:
        out.raw("")
        out.raw("  （没拿到设备数据，只给负载。跑 `cwf rig` 看设备能力）")
        return

    cap = dev.vram_usable or dev.vram_total
    out.raw("")
    out.raw("  ── 与这台机器的关系 " + "─" * 34)
    if fit.streaming_likely:
        need_v = load.vram_streamed
        out.raw("  显存需求  %s   （流式模式：只要求装得下一块）" % rig.gb(need_v))
        out.raw("            %s  %s 可用 · 余量 %s" % (
            _bar(need_v, cap), rig.gb(cap), rig._fmt_delta(fit.vram_margin)))
        out.raw("            ⚠ 权重 %s 超过显存，必然走动态流式加载"
                % rig.gb(load.weights))
    else:
        out.raw("  显存需求  %s   （全驻模式）" % rig.gb(load.vram_full))
        out.raw("            %s  %s 可用 · 余量 %s" % (
            _bar(load.vram_full, cap), rig.gb(cap),
            rig._fmt_delta(fit.vram_margin)))

    out.raw("  内存需求  %s" % rig.gb(load.ram))
    out.raw("            %s  %s 实有 · 余量 %s" % (
        _bar(load.ram, dev.ram_total), rig.gb(dev.ram_total),
        rig._fmt_delta(fit.ram_margin)))

    icon = {"轻松": "✓", "够用": "✓", "偏紧": "⚠", "不够": "✖"}.get(fit.level, "?")
    out.raw("")
    out.raw("  %s 判定：%s%s" % (
        icon, fit.level,
        (" · 瓶颈在%s" % fit.bottleneck) if fit.bottleneck else ""))
    for a in fit.advice:
        out.raw("      · %s" % a)


def cmd_load(args, out: Out) -> None:
    """工作流负载画像：需要多少显存和内存。"""
    g = _load(args, out)
    root = paths.comfy_root()
    load = rig.estimate_load(g, root)
    dev = None
    fit = None
    if not getattr(args, "no_device", False):
        dev = _rig_device(args, root)
        fit = rig.evaluate(load, dev)

    out.put("load", load.as_dict())
    if dev:
        out.put("device", dev.as_dict())
    if fit:
        out.put("fit", {"level": fit.level, "bottleneck": fit.bottleneck,
                        "vram_margin": fit.vram_margin,
                        "ram_margin": fit.ram_margin,
                        "streaming_likely": fit.streaming_likely})

    _print_load(out, load, dev, fit)

    if not getattr(args, "no_logs", False):
        for line in rig.compare_with_log(load, rig.mine_logs(
                rig.find_logs(root))):
            out.raw("  " + line)
    out.finish()


def cmd_fit(args, out: Out) -> None:
    """负载与能力的对照 —— 和 `cwf load` 同一份输出。"""
    cmd_load(args, out)


def cmd_precheck(args, out: Out) -> None:
    """不提交执行，只过一遍"这台机器跑不跑得动"。"""
    path = resolve_workflow(args.workflow)
    g = Graph.load(path)
    reg = _reg(args)
    rep = _hw_guard(g, reg)
    out.put("path", path)
    out.put("guard", rep)
    out.raw(f"硬件预检：{os.path.basename(path)}")
    out.raw(f"  {len(g.nodes)} 节点 · {len(g.links)} 连线")
    cfg = rep.get("cfg", {})
    if cfg:
        vram = f"{cfg['vram']:g} GB" if cfg.get("vram") else "未声明"
        out.raw(f"  判定口径：显存 {vram} · 画布上限 {cfg['max_mp']:g} MP · "
                f"护栏 {'开' if cfg.get('enabled', True) else '关（CWF_GUARD=off）'}")
    out.raw("")
    if not rep["warnings"]:
        out.raw("  ✓ 没触到红线（ControlNet / 视频 / 大画布都没出现）")
        out.raw("    这个工作流可以直接跑。")
        return
    for w in rep["warnings"]:
        out.raw(f"  ⚠ {w}")
    out.raw("")
    if rep["controlnet"]:
        out.raw(f"  ControlNet 节点：{rep['controlnet'][:12]}")
        out.raw("  → 默认不跑带 ControlNet 的图。"
                "确实要跑：cwf run … --allow-controlnet")
    if rep["video"]:
        out.raw(f"  视频相关节点：{rep['video'][:12]}")
        out.raw("  → 视频是长任务，中途 OOM 会白跑；"
                "确认流程没问题再放行：--allow-video")
    out.raw("")
    out.raw("  这条护栏写在 run 里，不是靠「记得住」。")
    out.raw("  阈值不对？设 CWF_VRAM_GB / CWF_MAX_MP，或 CWF_GUARD=off 关掉。")


def cmd_run(args, out: Out) -> None:
    import urllib.request
    g = _load(args, out)
    reg = _reg(args)

    # ---- 执行护栏：先过这一关，过了才谈校验和提交
    guard = _hw_guard(g, reg)
    cfg = guard["cfg"]
    pre_allow = cfg["allow"]
    blocked = []
    if (guard["controlnet"] and not getattr(args, "allow_controlnet", False)
            and "controlnet" not in pre_allow):
        blocked.append(("allow-controlnet", guard["controlnet"],
                        "ControlNet 类节点吃显存和内存很凶，最容易 OOM"))
    if (guard["video"] and not getattr(args, "allow_video", False)
            and "video" not in pre_allow):
        blocked.append(("allow-video", guard["video"],
                        "视频是长任务，中途 OOM 会白跑；确认流程没问题再放行"))
    if not cfg["enabled"]:          # CWF_GUARD=off —— 只报告不拦
        blocked = []
    if blocked:
        for flag, ids, why in blocked:
            out.raw(f"  ⛔ {why}")
            out.raw(f"     涉及节点 {len(ids)} 个：{ids[:12]}")
        out.raw("")
        out.raw("  触到执行护栏，拒绝提交。确认要跑就显式放行：")
        for flag, _ids, _why in blocked:
            out.raw(f"    cwf run {args.workflow} --{flag}")
        out.raw("  （阈值不合适：设 CWF_VRAM_GB / CWF_MAX_MP，"
                "或 CWF_GUARD=off 整体关掉）")
        die("触到执行护栏（ControlNet / 视频），拒绝提交。"
            "放行办法见上面的提示。")
    for w in guard["warnings"]:
        out.raw(f"  ⚠ {w}")
    out.put("hw_guard", guard)

    errs = _quick_validate(g, reg)
    if errs and not args.force:
        for e in errs:
            out.raw(f"  ✖ {e}")
        die("校验没过，拒绝提交（要强行提交加 --force）")
    api = g.to_api()
    payload = {"prompt": api, "client_id": args.client_id or str(uuid.uuid4())}
    if args.extra:
        payload.update(json.loads(args.extra))
    url = args.server.rstrip("/") + "/prompt"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    out.put("submitted_to", url)
    out.put("output_nodes", [k for k, v in api.items()])
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        body = getattr(e, "read", lambda: b"")()
        detail = body.decode("utf-8", "replace")[:800] if body else str(e)
        out.put("error", detail)
        out.raw(f"✖ 提交失败：{detail}")
        out.finish(ok=False)
        raise SystemExit(1)
    out.put("response", res)
    out.raw(f"✓ 已提交，prompt_id = {res.get('prompt_id')}")
    out.raw(f"  队列号 {res.get('number')}")
    if args.wait:
        out.raw("  等待执行…")
        hist = _wait_history(args.server, res.get("prompt_id"), args.timeout)
        out.put("history", hist)
        status = (hist.get("status") or {}).get("status_str")
        out.raw(f"  执行结束：{status}")
        for node_id, o in (hist.get("outputs") or {}).items():
            for kind, items in o.items():
                for it in (items if isinstance(items, list) else []):
                    fn = it.get("filename") if isinstance(it, dict) else None
                    if fn:
                        out.raw(f"    #{node_id} {kind}: {it.get('subfolder', '')}/{fn}")
    out.finish()


def _wait_history(server: str, prompt_id: str, timeout: int) -> Dict[str, Any]:
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(server.rstrip("/") + f"/history/{prompt_id}",
                                        timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data:
                return data.get(prompt_id, {})
        except Exception:
            pass
        time.sleep(1.5)
    return {}


def _http_get_json(url: str, timeout: int = 10) -> Any:
    """GET 一个 JSON 接口。连不上时抛出人话错误而不是原始 traceback。"""
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise CwfError(f"{url} 返回 HTTP {e.code}（{e.reason}）")
    except urllib.error.URLError as e:
        raise CwfError(
            f"连不上 ComfyUI（{url}）：{e.reason}；"
            f"服务没启动？先启动 ComfyUI，或用 --server 指定别的地址"
            f"（只读的读/查/校验命令仍可用，加 --offline 走缓存字典）")
    except json.JSONDecodeError as e:
        raise CwfError(f"{url} 返回的不是 JSON：{e}")


def cmd_queue(args, out: Out) -> None:
    q = _http_get_json(args.server.rstrip("/") + "/queue", timeout=10)
    out.put("queue", {"running": len(q.get("queue_running") or []),
                      "pending": len(q.get("queue_pending") or [])})
    out.raw(f"运行中 {len(q.get('queue_running') or [])} · "
            f"排队 {len(q.get('queue_pending') or [])}")
    for item in (q.get("queue_running") or []) + (q.get("queue_pending") or []):
        try:
            pid, prompt = item[1], item[2]
            n = len(prompt)
            out.raw(f"  {pid}  {n} 个节点")
        except Exception:
            pass


# ---------------- 格式转换 / 其他


def cmd_export_api(args, out: Out) -> None:
    g = _load(args, out)
    api = g.to_api()
    out.put("api", api)
    out.put("node_count", len(api))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(api, f, ensure_ascii=False, indent=2)
        out.raw(f"✓ 已导出 API 格式 → {args.out}（{len(api)} 个节点）")
    elif args.json:
        out.finish()
    else:
        print(json.dumps(api, ensure_ascii=False, indent=2))


def cmd_import_api(args, out: Out) -> None:
    with open(args.file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "prompt" in data and isinstance(data["prompt"], dict):
        data = data["prompt"]
    g = Graph.from_api(data, args.file)
    reg = _reg(args)
    for n in g.nodes:                       # 补端口信息
        spec = reg.get(n.type)
        if spec is None:
            continue
        have = {s.name for s in n.outputs}
        for o in spec.outputs:
            if o.name not in have:
                n.outputs.append(Slot(name=o.name, type=o.type))
        n.properties.setdefault("Node name for S&R", n.type)
    rep = strata.layout(g, _layout_opts(args), reg)
    out.put("layout", rep.as_dict())
    out.put("summary", g.summary())
    out.raw(f"✓ API 图已转成 UI 图并排版：{len(g)} 节点 / {len(g.links)} 连线")
    if args.out:
        _save(g, args.out, out)
    out.finish()


def cmd_export_dsl(args, out: Out) -> None:
    """把已有工作流导出成 DSL，便于 agent 读懂和改写。"""
    g = _load(args, out)
    lines: List[str] = []
    wt = g.extra.get("workflow_title")
    if wt:
        lines.append(f"@title {wt}")
    lines.append("")
    ids = {}
    for i, n in enumerate(sorted(g.nodes, key=lambda z: (z.pos[0], z.pos[1])), 1):
        if n.is_note:
            continue
        alias = _slug(n, i)
        ids[n.id] = alias
        parts = [f"{alias:<8} {n.type}"]
        try:
            for k, v in n.widget_pairs():
                if k == "videopreview":
                    continue
                parts.append(f"{k}={_q(v)}")
        except Exception:
            pass
        if n.title and args.titles:
            parts.append(f"标题={_q(n.title)}")
        if n.mode:
            parts.append(f"mode={n.mode}")
        lines.append("  ".join(parts))
    lines.append("")
    for l in sorted(g.links, key=lambda z: (z.origin_id, z.target_id)):
        if l.origin_id not in ids or l.target_id not in ids:
            continue
        a, b = g.node(l.origin_id), g.node(l.target_id)
        so = a.outputs[l.origin_slot].name if l.origin_slot < len(a.outputs) else str(l.origin_slot)
        si = b.inputs[l.target_slot].name if l.target_slot < len(b.inputs) else str(l.target_slot)
        lines.append(f"{ids[l.origin_id]}.{so} -> {ids[l.target_id]}.{si}")
    text = "\n".join(lines) + "\n"
    out.put("dsl", text)
    out.put("lines", len(lines))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        out.raw(f"✓ 已导出 DSL → {args.out}")
    else:
        out.raw(text)


def _q(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "none"
    s = str(v)
    if len(s) > 300:
        s = s[:300]
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _slug(n: Node, i: int) -> str:
    base = (n.title or n.type).strip()
    base = re.sub(r"[\s\.\-/\\:：]+", "", base)
    base = re.sub(r"[^\w\u4e00-\u9fff]", "", base)
    base = base or n.type
    if base[0].isdigit():
        base = "n" + base
    if len(base) > 12:
        base = base[:12]
    if base in ("", "_"):
        base = f"n{i}"
    return base


def cmd_stats(args, out: Out) -> None:
    """对整张图做体检：结构指标 + 潜在问题。"""
    g = _load(args, out)
    reg = _reg(args)
    rep = strata.layout(g, strata.LayoutOptions(), reg) if args.with_layout else None
    n = max(1, len(g))
    info = {
        "nodes": len(g), "links": len(g.links), "groups_before": len(g.groups),
        "orphans": len(g.orphans()), "terminals": len(g.terminals()),
        "bypassed": len([x for x in g.nodes if x.mode == 4]),
        "notes": len([x for x in g.nodes if x.is_note]),
    }
    if rep:
        info.update(rep.as_dict())
    out.put("stats", info)
    out.raw(f"节点 {info['nodes']} · 连线 {info['links']} · 孤儿 {info['orphans']} · "
            f"终结 {info['terminals']} · 旁路 {info['bypassed']}")
    if rep:
        x0, y0, x1, y1 = g.bounds()
        out.raw(f"排版后 {x1-x0:.0f}×{y1-y0:.0f}，每节点 {rep.area_per_node:.0f} px²，"
                f"交叉 {rep.crossings}，层 {rep.layers}")
    if not g.terminals():
        out.raw("⚠ 没有终结点：这张图跑起来不会有任何输出")
    if info["orphans"]:
        out.raw(f"⚠ {info['orphans']} 个孤立节点没接线")


# ================================================================ 参数表


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cwf",
        description="ComfyUI 工作流读写 / 拼装 / 排版工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用示例：
  cwf ws list --dir H3                      列出 H3 目录下的工作流
  cwf cat 自用版++Minimax --detail           看某个工作流的节点与参数
  cwf outline 自用版++Minimax                看数据流分了几阶段
  cwf validate 某工作流                      检查节点/模型/接线是否齐
  cwf beautify 某工作流 --in-place           一键重排成整齐版式（自动备份）
  cwf build my.dsl --name 我的新流            用 DSL 拼一张新工作流
  cwf pack split 某工作流 --nodes 10-30 --name 采样模块 --dir modules
  cwf pack use 采样模块 --workflow 目标流 --in-place
""")
    p.add_argument("--version", action="version", version=f"cwf {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, out_flag: bool = True):
        sp.add_argument("--json", action="store_true", help="输出结构化 JSON")
        sp.add_argument("--server", default=DEFAULT_SERVER, help="ComfyUI 地址")
        sp.add_argument("--offline", action="store_true", help="只用缓存的节点字典")
        sp.add_argument("--refresh", action="store_true", help="强制刷新节点字典")

    # ---- ws
    ws = sub.add_parser("ws", help="工作流库操作")
    wss = ws.add_subparsers(dest="sub", required=True)
    a = wss.add_parser("list", help="列出工作流")
    a.add_argument("--root", default=None)
    a.add_argument("--dir", default=None, help="只看某子目录")
    a.add_argument("--match", default=None, help="名字包含")
    a.add_argument("--limit", type=int, default=0)
    common(a); a.set_defaults(func=cmd_ws_list)
    a = wss.add_parser("info", help="概要")
    a.add_argument("workflow"); common(a); a.set_defaults(func=cmd_ws_info)
    a = wss.add_parser("resolve", help="检查节点类型与模型文件是否都齐")
    a.add_argument("workflow"); common(a); a.set_defaults(func=cmd_ws_resolve)

    # ---- find / grep
    a = sub.add_parser("find", help="按名字模糊找")
    a.add_argument("query"); a.add_argument("--limit", type=int, default=12)
    common(a); a.set_defaults(func=cmd_find)
    a = sub.add_parser("grep", help="在工作流内容里搜类型/标题/参数")
    a.add_argument("pattern"); a.add_argument("--root", default=None)
    a.add_argument("--limit", type=int, default=60)
    common(a); a.set_defaults(func=cmd_grep)

    # ---- 读取
    a = sub.add_parser("cat", help="看工作流内容")
    a.add_argument("workflow")
    a.add_argument("-d", "--detail", action="store_true", help="输出每个节点")
    a.add_argument("--filter", default=None)
    a.add_argument("--limit", type=int, default=60)
    common(a); a.set_defaults(func=cmd_cat)

    a = sub.add_parser("outline", help="按数据流阶段列结构（给 agent 读）")
    a.add_argument("workflow"); common(a); a.set_defaults(func=cmd_outline)

    a = sub.add_parser("deps", help="某节点的上下游链")
    a.add_argument("workflow"); a.add_argument("node")
    a.add_argument("--depth", type=int, default=6)
    common(a); a.set_defaults(func=cmd_deps)

    a = sub.add_parser("diff", help="比较两个工作流")
    a.add_argument("a"); a.add_argument("b")
    common(a); a.set_defaults(func=cmd_diff)

    a = sub.add_parser("stats", help="结构体检")
    a.add_argument("workflow"); a.add_argument("--with-layout", action="store_true")
    common(a); a.set_defaults(func=cmd_stats)

    # ---- schema
    sc = sub.add_parser("schema", help="查 ComfyUI 节点字典")
    scs = sc.add_subparsers(dest="sub", required=True)
    a = scs.add_parser("search", help="搜索节点类型")
    a.add_argument("query"); a.add_argument("--limit", type=int, default=20)
    common(a); a.set_defaults(func=cmd_schema_search)
    a = scs.add_parser("show", help="看某节点类型的端口与控件")
    a.add_argument("type"); common(a); a.set_defaults(func=cmd_schema_show)
    a = scs.add_parser("produce", help="谁能产出某类型的数据")
    a.add_argument("type_"); a.add_argument("--limit", type=int, default=30)
    common(a); a.set_defaults(func=cmd_schema_produce)
    a = scs.add_parser("accept", help="谁能接收某类型的数据")
    a.add_argument("type_"); a.add_argument("--limit", type=int, default=30)
    common(a); a.set_defaults(func=cmd_schema_accept)
    a = scs.add_parser("models", help="列出某类模型文件名")
    a.add_argument("kind", help="checkpoints/loras/vae/unet/text_encoders/images/controlnet")
    a.add_argument("--limit", type=int, default=60)
    common(a); a.set_defaults(func=cmd_schema_models)
    a = scs.add_parser("categories", help="节点分类树")
    a.add_argument("--depth", type=int, default=1)
    common(a); a.set_defaults(func=cmd_schema_categories)
    a = scs.add_parser("refresh", help="重新拉取节点字典")
    a.add_argument("--timeout", type=int, default=120)
    common(a); a.set_defaults(func=cmd_schema_refresh)
    a = scs.add_parser("stats", help="字典规模")
    common(a); a.set_defaults(func=cmd_schema_stats)

    # ---- 手动摆位置
    a = sub.add_parser("place", help="手动摆节点位置（绝对坐标 / 相对微调 / 列计划）")
    a.add_argument("workflow")
    a.add_argument("specs", nargs="*",
                   help='位置，如 "#12=100,200"、"type:KSampler+=0,-300"')
    a.add_argument("--col", action="append", default=None,
                   help='一列节点，可重复。如 --col "主模型 正向" --col "一采"')
    a.add_argument("--plan", default=None,
                   help="列计划文件：一行一列（# 开头是注释）")
    a.add_argument("--gap-h", type=float, default=90.0, help="列间距，默认 90")
    a.add_argument("--gap-v", type=float, default=45.0, help="行间距，默认 45")
    a.add_argument("--pack", action="store_true",
                   help="把现有空隙全压到 --gap-h/--gap-v（只挪位置，不改顺序）")
    a.add_argument("--fold", type=int, default=1, metavar="N",
                   help="每 N 列并成一列（压短的总长度）；N=1 不折")
    a.add_argument("--no-regroup", action="store_true", help="不重算分区框")
    a.add_argument("--no-fix-overlap", action="store_true",
                   help="不自动消重叠（默认一定会消到 0，这是硬保证）")
    a.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
    a.add_argument("--in-place", action="store_true")
    a.add_argument("--format", default="ui", choices=["ui", "api"])
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_place)

    # ---- 离线渲染
    a = sub.add_parser("render", help="把工作流离线画成图（SVG / PNG）")
    a.add_argument("workflow")
    a.add_argument("--out", default=None,
                   help="输出文件，*.svg 或 *.png（默认按扩展名判断格式）")
    a.add_argument("--format", default=None, choices=["svg", "png"],
                   help="显式指定格式（--out 没有扩展名时用）")
    a.add_argument("--scale", type=float, default=1.0, help="整体缩放，默认 1.0")
    a.add_argument("--max-px", type=int, default=None,
                   help="长边像素上限，超了自动缩小。PNG 默认 4200，SVG 默认不限；0 = 不限制")
    a.add_argument("--pad", type=float, default=48.0, help="四周留白，默认 48")
    a.add_argument("--theme", default="dark", help="dark（默认）或 light")
    a.add_argument("--color", default="function",
                   choices=["function", "comfy", "none"],
                   help="标题栏配色：function 按功能分类（默认）/ comfy 素色 / none")
    a.add_argument("--title", default=None, help="图例上的标题，默认用文件名")
    a.add_argument("--font", type=float, default=1.0, help="字号缩放，默认 1.0")
    a.add_argument("--no-legend", action="store_true", help="不画左上角图例")
    a.add_argument("--no-grid", action="store_true", help="不画背景点阵")
    a.add_argument("--no-widgets", action="store_true", help="不画控件条")
    a.add_argument("--no-widget-names", action="store_true",
                   help="控件条只显示值（跟前端一样）")
    a.add_argument("--no-notes", action="store_true", help="不画便签")
    a.add_argument("--open", action="store_true", help="渲染完用系统默认程序打开")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_render)

    # ---- 节点库
    nd = sub.add_parser("nodes", help="本机节点库：有多少节点、什么性质、来自哪个包")
    nds = nd.add_subparsers(dest="sub", required=True)

    a = nds.add_parser("stats", help="总览：节点类型数 / 分类 / 来源包")
    a.add_argument("--limit", type=int, default=30)
    a.add_argument("--root", default=None, help="工作流库根目录")
    a.add_argument("--rebuild", action="store_true", help="强制重建缓存")
    common(a); a.set_defaults(func=cmd_nodes_stats)

    a = nds.add_parser("list", help="列节点（支持中文别名与功能分类过滤）")
    a.add_argument("query", nargs="?", default=None, help="关键词或中文别名")
    a.add_argument("--cat", default=None,
                   help="按**功能分类**过滤，如 视频 / video / 图像/放大")
    a.add_argument("--category", default=None,
                   help="按**原生分类**过滤（多数插件填的是插件名）")
    a.add_argument("--limit", type=int, default=40)
    a.add_argument("--used-only", action="store_true", help="只看用过的")
    a.add_argument("--root", default=None)
    a.add_argument("--rebuild", action="store_true")
    common(a); a.set_defaults(func=cmd_nodes_list)

    a = nds.add_parser("tree", help="按功能浏览节点库：16 大类 → 小类 → 节点")
    a.add_argument("--cat", default=None,
                   help="展开哪一类，支持中文/英文，可带小类，如 图像处理/放大")
    a.add_argument("--limit", type=int, default=8, help="每个小类列几个节点")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_nodes_tree)

    a = nds.add_parser("classify", help="看一个节点被分到哪类、依据是什么")
    a.add_argument("type", nargs="+", help="节点类型（带空格也不用加引号）")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_nodes_classify)

    a = nds.add_parser("setcat", help="手动改一个节点的功能分类（永久生效）")
    a.add_argument("parts", nargs="+",
                   help="节点类型 + 目标分类，如 `KSampler 采样与生成/采样器`")
    a.add_argument("--to", default=None,
                   help="目标分类（节点名带空格时用这个，免得和分类分不开）")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_nodes_setcat)

    a = nds.add_parser("atlas", help="导出全部节点的功能分类图鉴（markdown）")
    a.add_argument("--out", default=None,
                   help="输出目录，默认 ~/.cwf/atlas")
    a.add_argument("--used-only", action="store_true", help="只导你用过的")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_nodes_atlas)

    a = nds.add_parser("show", help="一个节点的说明书：分类/端口/控件/来源包/用量")
    a.add_argument("type", nargs="+", help="节点类型（带空格也不用加引号）")
    a.add_argument("-v", "--verbose", action="store_true", help="连默认值一起列")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_nodes_show)

    a = nds.add_parser("pkg", help="按插件包看节点")
    a.add_argument("package", nargs="?", default=None)
    a.add_argument("--limit", type=int, default=50)
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_nodes_pkg)

    a = nds.add_parser("unused", help="装了但从没用过的节点类型")
    a.add_argument("--limit", type=int, default=40)
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_nodes_unused)

    a = nds.add_parser("alias", help="中文别名对照表")
    common(a); a.set_defaults(func=cmd_nodes_alias)

    a = nds.add_parser("build", help="重建节点库缓存")
    a.add_argument("--root", default=None)
    a.add_argument("--rebuild", action="store_true", default=True,
                   help="强制重建（默认就是）")
    common(a); a.set_defaults(func=cmd_nodes_build)

    # ---- 节点知识库
    stp = sub.add_parser("store", help="节点知识库：沉淀摸清过的节点（别名/笔记/常用）")
    sts = stp.add_subparsers(dest="sub", required=True)

    a = sts.add_parser("find", help="检索节点：先查沉淀过的，再查节点字典")
    a.add_argument("query", nargs="?", default=None, help="关键词 / 中文别名 / 类型名")
    a.add_argument("--limit", type=int, default=15)
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_store_find)

    a = sts.add_parser("show", help="一个节点的速查卡 + 你的笔记")
    a.add_argument("type")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_store_show)

    a = sts.add_parser("mark", help="记住一个节点：起别名 + 归类 + 写笔记 + 收藏")
    a.add_argument("node", help="类型名或已有别名")
    a.add_argument("-a", "--alias", default=None, help="起个你自己的叫法")
    a.add_argument("-c", "--cat", default=None,
                   help="同时改功能分类，如 图像处理/放大")
    a.add_argument("-n", "--note", default=None, help="记一笔：怎么接、踩过什么坑")
    a.add_argument("-p", "--pin", action="store_true", help="加进常用列表")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_store_mark)

    a = sts.add_parser("note", help="给节点写/追加笔记")
    a.add_argument("node")
    a.add_argument("text", help="笔记正文")
    a.add_argument("--show", action="store_true", help="写完回显全文")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_store_note)

    a = sts.add_parser("alias", help="起别名 / 列出所有别名")
    a.add_argument("alias", nargs="?", default=None, help="别名；不给就列出全部")
    a.add_argument("type", nargs="?", default=None, help="节点类型；不给就列出全部")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_store_alias)

    a = sts.add_parser("pin", help="收藏/取消收藏常用节点（开关式）")
    a.add_argument("type")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_store_pin)

    a = sts.add_parser("list", help="列出沉淀过的东西")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_store_list)

    a = sts.add_parser("export", help="导出成 markdown 总览")
    a.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_store_export)

    # ---- 校验
    a = sub.add_parser("validate", help="校验工作流")
    a.add_argument("workflow"); a.add_argument("--strict", action="store_true")
    common(a); a.set_defaults(func=cmd_validate)

    # ---- 排版
    def layout_flags(sp):
        sp.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
        sp.add_argument("--in-place", action="store_true", help="覆盖原文件（自动备份）")
        sp.add_argument("--compact", action="store_true", help="紧凑一点")
        sp.add_argument("--loose", action="store_true", help="宽松一点")
        sp.add_argument("--no-groups", action="store_true", help="不重画分区框")
        sp.add_argument("--keep-notes", action="store_true", help="注释节点留在原地")
        sp.add_argument("--h-gap", type=float, default=None, dest="h_gap")
        sp.add_argument("--v-gap", type=float, default=None, dest="v_gap")
        sp.add_argument("--group-pad", type=float, default=None, dest="group_pad")
        sp.add_argument("--margin", type=float, default=None)
        sp.add_argument("--strays", default=None, choices=["auto", "bottom", "right", "keep"])

    a = sub.add_parser("layout", help="自动排版（不动内容）")
    a.add_argument("workflow"); layout_flags(a)
    common(a); a.set_defaults(func=cmd_layout)

    a = sub.add_parser("beautify", help="排版 + 补标题 + 重排序号")
    a.add_argument("workflow"); layout_flags(a)
    a.add_argument("--renumber", action="store_true", help="标题加 01. 02. 序号")
    a.add_argument("--no-title", action="store_true", dest="no_title",
                   help="不给缺标题的节点补标题")
    a.add_argument("--no-titles", action="store_true", dest="no_titles")
    common(a); a.set_defaults(func=cmd_beautify)

    # ---- 编辑
    def edit_flags(sp):
        sp.add_argument("workflow")
        sp.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
        sp.add_argument("--in-place", action="store_true")
        sp.add_argument("--layout", action="store_true", help="改完顺便重排")
        sp.add_argument("--compact", action="store_true")
        sp.add_argument("--loose", action="store_true")
        sp.add_argument("--no-groups", action="store_true")
        sp.add_argument("--keep-notes", action="store_true")
        sp.add_argument("--h-gap", type=float, default=None, dest="h_gap")
        sp.add_argument("--v-gap", type=float, default=None, dest="v_gap")
        sp.add_argument("--group-pad", type=float, default=None, dest="group_pad")
        sp.add_argument("--margin", type=float, default=None)
        sp.add_argument("--strays", default=None, choices=["auto", "bottom", "right", "keep"])

    a = sub.add_parser("set", help="改参数：--set 节点:控件=值")
    edit_flags(a); a.add_argument("--set", action="append", dest="set", required=True)
    common(a); a.set_defaults(func=cmd_set)

    a = sub.add_parser("rename", help='改节点标题：--rename "节点=新标题"')
    edit_flags(a)
    a.add_argument("--rename", required=True, help='写成 "节点=新标题"')
    common(a); a.set_defaults(func=cmd_rename)

    a = sub.add_parser("mode", help="正常/静音/旁路")
    edit_flags(a); a.add_argument("node")
    a.add_argument("--mode", required=True,
                   choices=["normal", "on", "mute", "静音", "bypass", "旁路"])
    common(a); a.set_defaults(func=cmd_mode)

    a = sub.add_parser("add", help="加一个节点")
    edit_flags(a); a.add_argument("--type", required=True)
    a.add_argument("--title", default=None)
    a.add_argument("--arg", action="append", help="控件参数 k=v")
    common(a); a.set_defaults(func=cmd_add)

    a = sub.add_parser("remove", help="删节点")
    edit_flags(a); a.add_argument("node", nargs="+")
    a.add_argument("--keep-wiring", action="store_true", help="把上下游接直")
    common(a); a.set_defaults(func=cmd_remove)

    a = sub.add_parser("connect", help="连线：--pair 源.输出 目标.输入")
    edit_flags(a); a.add_argument("--pair", nargs=2, action="store", required=True)
    common(a); a.set_defaults(func=cmd_connect)

    a = sub.add_parser("disconnect", help="断线")
    edit_flags(a); a.add_argument("--link", type=int, default=None)
    a.add_argument("--to", nargs=1, default=None, help="节点.输入")
    common(a); a.set_defaults(func=cmd_disconnect)

    a = sub.add_parser("insert", help="把新节点插进已有连线中间")
    edit_flags(a); a.add_argument("--type", required=True)
    a.add_argument("--on", nargs=2, action="store", required=True, help="上游 下游")
    a.add_argument("--title", default=None)
    a.add_argument("--arg", action="append")
    common(a); a.set_defaults(func=cmd_insert)

    a = sub.add_parser("splice", help="摘掉节点并焊接上下游")
    edit_flags(a); a.add_argument("node")
    common(a); a.set_defaults(func=cmd_splice)

    a = sub.add_parser("mute-group", help="批量静音/旁路")
    edit_flags(a); a.add_argument("node", nargs="+")
    a.add_argument("--mode", default="bypass", choices=["bypass", "旁路", "mute", "静音"])
    common(a); a.set_defaults(func=cmd_mute_group)

    # ---- 构建
    a = sub.add_parser("build", help="用 DSL 拼一张工作流")
    a.add_argument("file", nargs="?", default="-", help="DSL 文件，- 表示从 stdin 读")
    a.add_argument("--base", default=None, help="在已有工作流上继续拼")
    a.add_argument("--name", default=None)
    a.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
    a.add_argument("--no-layout", action="store_true")
    a.add_argument("--validate", action="store_true", help="建完顺手校验")
    a.add_argument("--print-only", action="store_true", help="只打印不写文件")
    a.add_argument("--compact", action="store_true"); a.add_argument("--loose", action="store_true")
    a.add_argument("--no-groups", action="store_true"); a.add_argument("--keep-notes", action="store_true")
    a.add_argument("--h-gap", type=float, default=None, dest="h_gap")
    a.add_argument("--v-gap", type=float, default=None, dest="v_gap")
    a.add_argument("--group-pad", type=float, default=None, dest="group_pad")
    a.add_argument("--margin", type=float, default=None)
    a.add_argument("--strays", default=None, choices=["auto", "bottom", "right", "keep"])
    common(a); a.set_defaults(func=cmd_build)

    a = sub.add_parser("scaffold", help="打印 DSL 模板")
    a.add_argument("template", nargs="?", default="txt2img", choices=sorted(TEMPLATES))
    common(a); a.set_defaults(func=cmd_scaffold)

    a = sub.add_parser("dsl-check", help="只验证 DSL 语法")
    a.add_argument("file", nargs="?", default="-")
    common(a); a.set_defaults(func=cmd_dsl_check)

    # ---- 模块
    pk = sub.add_parser("pack", help="工作流模块拆装")
    pks = pk.add_subparsers(dest="sub", required=True)
    a = pks.add_parser("split", help="从工作流里切出一个模块")
    a.add_argument("workflow")
    a.add_argument("--nodes", required=True, help="节点选择：#1,#2 或 10-30 或 标题子串")
    a.add_argument("--name", required=True)
    a.add_argument("--dir", default=None, help="存到模块库目录")
    a.add_argument("--description", default="")
    common(a); a.set_defaults(func=cmd_pack_split)
    a = pks.add_parser("list", help="列出模块库")
    a.add_argument("--dir", required=True)
    common(a); a.set_defaults(func=cmd_pack_list)
    a = pks.add_parser("show", help="看模块接口")
    a.add_argument("name"); a.add_argument("--dir", required=True)
    common(a); a.set_defaults(func=cmd_pack_show)
    a = pks.add_parser("use", help="把模块实例化进一张图")
    a.add_argument("name")
    a.add_argument("--dir", required=True)
    a.add_argument("--workflow", default=None, help="已有的图；不给就新建")
    a.add_argument("--as", dest="as_", default=None, help="别名前缀")
    a.add_argument("--prefix", default=None, help="节点标题前缀")
    a.add_argument("--rename", action="append", help="把入口改名 alias=名字")
    a.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
    a.add_argument("--in-place", action="store_true")
    a.add_argument("--layout", action="store_true")
    a.add_argument("--compact", action="store_true"); a.add_argument("--loose", action="store_true")
    a.add_argument("--no-groups", action="store_true"); a.add_argument("--keep-notes", action="store_true")
    a.add_argument("--h-gap", type=float, default=None, dest="h_gap")
    a.add_argument("--v-gap", type=float, default=None, dest="v_gap")
    a.add_argument("--group-pad", type=float, default=None, dest="group_pad")
    a.add_argument("--margin", type=float, default=None)
    a.add_argument("--strays", default=None, choices=["auto", "bottom", "right", "keep"])
    common(a); a.set_defaults(func=cmd_pack_use)

    # ---- 运行 / 转换
    a = sub.add_parser("run", help="提交到 ComfyUI 执行（带执行护栏）")
    a.add_argument("workflow")
    a.add_argument("--wait", action="store_true", help="等它跑完并列出产物")
    a.add_argument("--force", action="store_true", help="跳过校验")
    a.add_argument("--allow-controlnet", action="store_true",
                   help="放行含 ControlNet 的图（这类节点最容易 OOM，默认拒绝）")
    a.add_argument("--allow-video", action="store_true",
                   help="放行视频生成（默认拒绝：视频只跑你准备好并验证过的流程）")
    a.add_argument("--client-id", default=None)
    a.add_argument("--extra", default=None, help="额外的 JSON 字段")
    a.add_argument("--timeout", type=int, default=1800)
    common(a); a.set_defaults(func=cmd_run)

    a = sub.add_parser("rig", help="设备能力画像：显存/内存/架构/实测速度")
    a.add_argument("--no-logs", action="store_true",
                   help="不去读 ComfyUI 日志里的实测速度")
    common(a); a.set_defaults(func=cmd_rig)

    a = sub.add_parser("load", help="工作流负载画像：要多少显存和内存")
    a.add_argument("workflow")
    a.add_argument("--root", default=None)
    a.add_argument("--no-device", action="store_true", help="只算负载，不比设备")
    a.add_argument("--no-logs", action="store_true", help="不做日志校准")
    common(a); a.set_defaults(func=cmd_load)

    a = sub.add_parser("fit", help="负载 vs 能力：余量、瓶颈、能不能跑")
    a.add_argument("workflow")
    a.add_argument("--root", default=None)
    a.add_argument("--no-device", action="store_true")
    a.add_argument("--no-logs", action="store_true")
    common(a); a.set_defaults(func=cmd_fit)

    a = sub.add_parser("precheck", help="硬件预检：这台机器跑不跑得动（不提交）")
    a.add_argument("workflow")
    a.add_argument("--root", default=None)
    common(a); a.set_defaults(func=cmd_precheck)

    a = sub.add_parser("queue", help="看 ComfyUI 队列")
    common(a); a.set_defaults(func=cmd_queue)

    a = sub.add_parser("export-api", help="导出 /prompt 用的 API 格式")
    a.add_argument("workflow"); a.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
    common(a); a.set_defaults(func=cmd_export_api)

    a = sub.add_parser("import-api", help="把 API 格式转回 UI 工作流")
    a.add_argument("file"); a.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
    a.add_argument("--compact", action="store_true"); a.add_argument("--loose", action="store_true")
    a.add_argument("--no-groups", action="store_true")
    common(a); a.set_defaults(func=cmd_import_api)

    a = sub.add_parser("export-dsl", help="把工作流导出成 DSL 文本")
    a.add_argument("workflow"); a.add_argument("--out", default=None, help="输出路径：给文件就写文件，给目录就自动拼上名字")
    a.add_argument("--titles", action="store_true", default=True)
    common(a); a.set_defaults(func=cmd_export_dsl)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Out(as_json=getattr(args, "json", False))
    # 先把节点字典备好（进程内缓存，读缓存文件很快）。
    # 有了它，Node 才能把位置数组形态的 widgets_values 对上控件名。
    try:
        _reg(args)
    except Exception:
        pass
    func = getattr(args, "func", None)
    if func is None:
        if args.cmd == "new":
            die("请改用 `cwf build`（DSL）或 `cwf add`（加单个节点）")
        parser.print_help()
        return 0
    if func is cmd_new:
        die("请改用 `cwf build`（DSL）或 `cwf add`（加单个节点）")
    try:
        func(args, out)
    except CwfError as e:
        if out.json:
            out.put("error", str(e))
            out.finish(ok=False)
        print(f"✖ {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
