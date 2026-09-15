# -*- coding: utf-8 -*-
"""
cwf.schema —— ComfyUI 节点字典（/object_info）与尺寸推算

为什么要这一层：
  * 「从零拼流」时必须知道 NodeType 到底有哪些输入端口、哪些是控件、控件默认值是什么，
    否则生成的工作流打不开；
  * 「自动排版」时必须知道每个节点大概多高多宽，否则排出来会重叠。
节点尺寸用 ComfyUI 前端的经验公式推算（标题 + 输出行 + 输入行 + 控件行），
误差通常十几个像素，排版时另有安全间距兜底。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .graph import CwfError, WIDGET_TYPES, HIDDEN_TYPES

DEFAULT_SERVER = os.environ.get("CWF_SERVER", "http://127.0.0.1:8188")
CACHE_DIR = os.environ.get("CWF_CACHE", os.path.join(os.path.expanduser("~"), ".cwf"))
CACHE_TTL = int(os.environ.get("CWF_CACHE_TTL", "86400"))

#: ComfyUI 前端几何常量（与 frontend 1.5x 实测接近）
TITLE_H = 30.0
SLOT_H = 20.0
WIDGET_H = 20.0
WIDGET_PAD = 4.0
TEXTAREA_LINES_DEFAULT = 4
MIN_W = 210.0
DEFAULT_W = 390.0

#: 只服务于界面的伪控件，不占节点高度（VHS 系节点塞在 widgets_values 里的播放器状态）
UI_ONLY_WIDGETS = {"videopreview", "choose video to upload", "choose image to upload",
                   "upload", "image", "video", "audio", "file"}


@dataclass
class InputSpec:
    name: str
    type: str = "*"
    kind: str = "link"          # link | widget
    default: Any = None
    options: Optional[List[Any]] = None
    optional: bool = False
    force_input: bool = False
    multiline: bool = False
    localized: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_widget(self) -> bool:
        return self.kind == "widget"


@dataclass
class OutputSpec:
    name: str
    type: str = "*"
    is_list: bool = False
    localized: Optional[str] = None


@dataclass
class NodeSpec:
    type: str
    category: str = ""
    inputs: List[InputSpec] = field(default_factory=list)
    outputs: List[OutputSpec] = field(default_factory=list)
    output_node: bool = False
    display_name: Optional[str] = None
    description: str = ""
    api_node: bool = False
    python_module: str = ""            # 真正的来源：custom_nodes.<包名> / comfy_extras.xxx
    deprecated: bool = False
    experimental: bool = False
    search_aliases: List[str] = field(default_factory=list)

    @property
    def package(self) -> str:
        """从 python_module 里推出插件包名。"""
        m = self.python_module or ""
        for pre in ("custom_nodes.", "comfyui_"):
            if m.startswith(pre):
                m = m[len(pre):]
                break
        if m.startswith("comfy_extras") or m.startswith("nodes") or m in ("", "nodes"):
            return "comfy-core"
        return m.split(".")[0] if m else ""

    @property
    def widget_inputs(self) -> List[InputSpec]:
        return [i for i in self.inputs if i.is_widget]

    @property
    def link_inputs(self) -> List[InputSpec]:
        return [i for i in self.inputs if not i.is_widget]

    def widget_defaults(self) -> List[Any]:
        return [i.default for i in self.widget_inputs]

    def find_input(self, name: str) -> Optional[InputSpec]:
        low = name.lower()
        for i in self.inputs:
            if i.name == name or (i.localized and i.localized == name):
                return i
        for i in self.inputs:
            if i.name.lower() == low:
                return i
        return None

    @property
    def title(self) -> str:
        return self.display_name or self.type


# ---------------------------------------------------------------- 缓存


def _cache_path(server: str) -> str:
    h = hashlib.md5(server.encode("utf-8")).hexdigest()[:10]
    return os.path.join(CACHE_DIR, f"object_info.{h}.json")


def _version_stamp(server: str) -> Optional[str]:
    """用 /system_stats 的版本号当缓存钥匙，ComfyUI 一升级就自动失效。"""
    try:
        import urllib.request
        with urllib.request.urlopen(server.rstrip("/") + "/system_stats", timeout=4) as r:
            d = json.loads(r.read().decode("utf-8"))
        sysd = d.get("system", {})
        return f"{sysd.get('comfyui_version')}|{sysd.get('required_frontend_version')}"
    except Exception:
        return None


def _http_json(url: str, timeout: int = 90) -> Any:
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "cwf/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def save_object_info(info: Dict[str, Any], server: str = DEFAULT_SERVER,
                     stamp: Optional[str] = None) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = _cache_path(server)
    payload = {"_stamp": stamp or _version_stamp(server), "_ts": time.time(), "info": info}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return p


def load_object_info(server: str = DEFAULT_SERVER, *, refresh: bool = False,
                     allow_stale: bool = True, timeout: int = 90) -> Tuple[Dict[str, Any], str]:
    """拿到 object_info。返回 (info, 来源说明)。
    优先读缓存；缓存过期或 refresh 时才打服务；服务不可用且允许 stale 就用旧缓存。"""
    p = _cache_path(server)
    cached = None
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = None
    if cached and not refresh:
        age = time.time() - cached.get("_ts", 0)
        if age < CACHE_TTL and cached.get("info"):
            return cached["info"], f"cache {p} ({int(age)}s 前)"
    if refresh or not cached:
        try:
            info = _http_json(server.rstrip("/") + "/object_info", timeout=timeout)
            stamp = _version_stamp(server)
            save_object_info(info, server, stamp)
            return info, f"live {server}"
        except Exception as e:
            if cached and cached.get("info"):
                return cached["info"], f"stale cache（服务不可用：{e}）"
            raise CwfError(f"读不到 {server}/object_info：{e}\n"
                           f"（ComfyUI 没启动？可以先 `cwf ws resolve` 离线离线解析已有工作流）")
    return cached["info"], f"cache {p}"


# ---------------------------------------------------------------- Registry


class Registry:
    """节点类型字典。没连服务时降级成「空字典」，所有查询都安全返回 None。"""

    def __init__(self, info: Optional[Dict[str, Any]] = None, source: str = "empty"):
        self.source = source
        self._specs: Dict[str, NodeSpec] = {}
        self._raw = info or {}
        if info:
            for tname, d in info.items():
                if not isinstance(d, dict):
                    continue
                try:
                    self._specs[tname] = self._parse(tname, d)
                except Exception:
                    continue

    # ---------- 解析
    @staticmethod
    def _parse(tname: str, d: Dict[str, Any]) -> NodeSpec:
        spec = NodeSpec(
            type=tname,
            category=d.get("category", "") or "",
            output_node=bool(d.get("output_node")),
            display_name=(d.get("display_name") or d.get("name") or tname),
            description=(d.get("description") or "")[:400],
            api_node=bool(d.get("api_node")),
            python_module=d.get("python_module") or "",
            deprecated=bool(d.get("deprecated")),
            experimental=bool(d.get("experimental")),
            search_aliases=list(d.get("search_aliases") or []),
        )
        itypes = d.get("input") or {}
        order = d.get("input_order") or {}
        req_names = list(order.get("required") or [])
        opt_names = list(order.get("optional") or [])

        def add_group(group: str, names: List[str]):
            for nm in names:
                blob = (itypes.get(group) or {}).get(nm)
                if blob is None:
                    continue
                if not (isinstance(blob, (list, tuple)) and blob):
                    continue
                typ = blob[0]
                opts = blob[1] if len(blob) > 1 and isinstance(blob[1], dict) else {}
                # 有些节点写成 ((type, opts_dict),) 或 ([...],{...}) 的嵌套形态，
                # 这里统一扒到最里面那层。
                while isinstance(typ, (list, tuple)) and len(typ) == 1:
                    if isinstance(typ[0], (list, tuple, str)):
                        inner = typ[0]
                        if isinstance(inner, (list, tuple)) and len(inner) >= 2 \
                                and isinstance(inner[1], dict):
                            typ = inner[0]
                            opts = {**inner[1], **opts}
                        else:
                            typ = inner
                    else:
                        break
                raw: Dict[str, Any] = {"type": typ, "opts": opts}
                spec_input: Optional[InputSpec] = None
                if isinstance(typ, list):                     # COMBO
                    spec_input = InputSpec(name=nm, type="COMBO", kind="widget",
                                           default=(typ[0] if typ else None), options=list(typ),
                                           optional=(group == "optional"),
                                           localized=opts.get("localized_name"), raw=raw)
                    if opts.get("control_after_generate"):
                        spec_input.raw["control_after_generate"] = True
                elif isinstance(typ, str) and typ in HIDDEN_TYPES:
                    continue                                   # 隐藏输入不进图
                elif isinstance(typ, str) and typ in ("INT", "FLOAT", "STRING", "BOOLEAN"):
                    multiline = bool(opts.get("multiline"))
                    dv = blob[1] if len(blob) > 1 and not isinstance(blob[1], dict) else opts.get("default")
                    if typ == "BOOLEAN":
                        dv = bool(dv) if dv is not None else False
                    spec_input = InputSpec(name=nm, type=typ, kind="widget", default=dv,
                                           optional=(group == "optional"), multiline=multiline,
                                           force_input=bool(opts.get("forceInput")),
                                           localized=opts.get("localized_name"), raw=raw)
                    if spec_input.force_input:
                        spec_input.kind = "link"
                        spec_input.default = None
                else:
                    spec_input = InputSpec(name=nm, type=str(typ), kind="link",
                                           optional=(group == "optional"),
                                           force_input=bool(opts.get("forceInput")),
                                           localized=opts.get("localized_name"), raw=raw)
                if spec_input is not None:
                    spec.inputs.append(spec_input)

        add_group("required", req_names)
        add_group("optional", opt_names)

        outs = d.get("output") or []
        out_names = d.get("output_name") or []
        out_list = d.get("output_is_list") or []
        for i, ot in enumerate(outs):
            spec.outputs.append(OutputSpec(
                name=(out_names[i] if i < len(out_names) and out_names[i] else
                      (ot if isinstance(ot, str) else f"out{i}")),
                type=(ot if isinstance(ot, str) else "COMBO"),
                is_list=bool(out_list[i]) if i < len(out_list) else False,
            ))
        return spec

    # ---------- 查询
    def get(self, tname: str) -> Optional[NodeSpec]:
        return self._specs.get(tname)

    def require(self, tname: str) -> NodeSpec:
        s = self.get(tname)
        if s is None:
            raise CwfError(f"ComfyUI 里没有节点类型 {tname!r}。"
                           f"（拼写？节点未安装？或者用 `cwf schema search {tname[:12]}` 找找）")
        return s

    def __contains__(self, tname: str) -> bool:
        return tname in self._specs

    def __iter__(self):
        return iter(self._specs)

    def keys(self):
        return self._specs.keys()

    def __len__(self) -> int:
        return len(self._specs)

    def search(self, query: str, limit: int = 40, in_outputs: bool = False) -> List[NodeSpec]:
        q = query.lower()
        hits: List[Tuple[int, NodeSpec]] = []
        for t, s in self._specs.items():
            score = 0
            if t.lower() == q or (s.display_name or "").lower() == q:
                score = 100
            elif t.lower().startswith(q):
                score = 80
            elif q in t.lower():
                score = 60
            elif q in (s.display_name or "").lower():
                score = 55
            elif q in s.category.lower():
                score = 30
            elif q in (s.description or "").lower():
                score = 20
            if score and in_outputs:
                for o in s.outputs:
                    if o.type.lower() == q:
                        score += 40
                        break
                else:
                    score = 0
            if score:
                hits.append((score, s))
        hits.sort(key=lambda kv: (-kv[0], kv[1].type))
        return [s for _, s in hits[:limit]]

    def types_producing(self, type_: str, limit: int = 60) -> List[NodeSpec]:
        t = type_.upper()
        out = []
        for s in self._specs.values():
            if any(o.type.upper() == t for o in s.outputs):
                out.append(s)
        out.sort(key=lambda s: (not s.output_node, s.category, s.type))
        return out[:limit]

    def types_accepting(self, type_: str, limit: int = 60) -> List[NodeSpec]:
        t = type_.upper()
        out = []
        for s in self._specs.values():
            if any(i.type.upper() in (t, "*") for i in s.inputs if not i.is_widget):
                out.append(s)
        out.sort(key=lambda s: (s.category, s.type))
        return out[:limit]

    def model_files(self, kind: str) -> List[str]:
        """拿某类模型可用文件名，例如 'checkpoints' / 'loras' / 'vae' / 'unet'。"""
        combo_dir = self._raw.get("CheckpointLoaderSimple")
        names: List[str] = []
        mapping = {
            "checkpoints": ("CheckpointLoaderSimple", 0),
            "loras": ("LoraLoader", 0),
            "vae": ("VAELoader", 0),
            "unet": ("UNETLoader", 0),
            "diffusion_models": ("UNETLoader", 0),
            "text_encoders": ("CLIPLoader", 0),
            "clip": ("CLIPLoader", 0),
            "clip_vision": ("CLIPVisionLoader", 0),
            "controlnet": ("ControlNetLoader", 0),
            "style_models": ("StyleModelLoader", 0),
            "embeddings": ("CLIPTextEncode", None),
        }
        if kind in mapping:
            tname, idx = mapping[kind]
            d = self._raw.get(tname)
            if d and idx is not None:
                try:
                    blob = d["input"]["required"]
                    key = list(blob.keys())[idx]
                    val = blob[key][0]
                    if isinstance(val, list):
                        names = list(val)
                except Exception:
                    names = []
        elif kind == "images":
            d = self._raw.get("LoadImage")
            try:
                names = list(d["input"]["required"]["image"][0])
            except Exception:
                names = []
        elif kind == "all":
            names = sorted(self._specs.keys())
        return names

    def category_tree(self, depth: int = 2) -> Dict[str, Any]:
        tree: Dict[str, Any] = {}
        for s in self._specs.values():
            parts = [p for p in s.category.split("/") if p][:depth]
            cur = tree
            for p in parts:
                cur = cur.setdefault(p, {})
            cur.setdefault("_n", 0)
            cur["_n"] += 1
        return tree


# ---------------------------------------------------------------- 尺寸推算


def estimate_size(type_: str, widgets_values: Any, title: Optional[str],
                  spec: Optional[NodeSpec], n_inputs: int = 0, n_outputs: int = 0,
                  collapsed: bool = False, wdict: Optional[Dict[str, Any]] = None) -> Tuple[float, float]:
    """推算节点在画布上的像素尺寸。

    wdict 是「控件名 -> 值」的映射（优先用 Node.widget_dict() 的结果），
    因为多行文本框的高度取决于实际文本长度，而这个信息只有按名字取才拿得到。
    """
    if collapsed:
        return (MIN_W, TITLE_H + 6)

    if spec is not None:
        link_ins = len(spec.link_inputs)
        n_out = len(spec.outputs)
    else:
        link_ins, n_out = n_inputs, n_outputs

    if wdict is None:
        if isinstance(widgets_values, dict):
            wdict = {str(k): v for k, v in widgets_values.items()}
        elif isinstance(widgets_values, list) and spec is not None:
            names = [i.name for i in spec.widget_inputs]
            wdict = {nm: (widgets_values[i] if i < len(widgets_values) else None)
                     for i, nm in enumerate(names)}
        else:
            wdict = {}

    # 控件行高度：多行文本框按行数算
    lines = 0.0
    for i, (nm, val) in enumerate(wdict.items()):
        if nm in UI_ONLY_WIDGETS:
            continue
        is_text = False
        want_lines = TEXTAREA_LINES_DEFAULT
        if spec is not None:
            si = spec.find_input(nm)
            if si is not None and (si.multiline or si.raw.get("opts", {}).get("multiline")):
                is_text = True
        if isinstance(val, str):
            want_lines = max(2, min(20, val.count("\n") + 1,
                                    (len(val) // 70) + 1 if len(val) > 70 else 2))
        if is_text:
            lines += WIDGET_H * want_lines + WIDGET_PAD + 8
        else:
            lines += WIDGET_H + WIDGET_PAD

    h = TITLE_H + max(0, link_ins) * SLOT_H + max(0, n_out) * SLOT_H + lines + WIDGET_PAD
    h = max(46.0, min(1600.0, h))

    w = DEFAULT_W
    if title:
        w = max(w, min(560.0, 68.0 + len(title) * 9.5))
    if spec is not None and spec.display_name:
        w = max(w, min(560.0, 68.0 + len(spec.display_name) * 9.5))
    return (round(w, 1), round(h, 1))


def size_of(node, reg: Optional[Registry] = None) -> Tuple[float, float]:
    """给一个 Node 推算尺寸。已知实际尺寸且节点类型不在字典里时，沿用原值。"""
    spec = reg.get(node.type) if reg else None
    if node.is_note:
        # 注释类节点保持原尺寸（它们本来就是版式的一部分）
        return (max(120.0, node.width), max(60.0, node.height))
    if node.is_reroute:
        return (30.0, 30.0)
    if spec is None:
        # 字典里没有：沿用原尺寸，但至少修掉明显不合理的值
        w = node.width if 120 <= node.width <= 800 else DEFAULT_W
        h = node.height if 40 <= node.height <= 900 else estimate_size(
            node.type, node.widgets, node.title, None, len(node.inputs), len(node.outputs))[1]
        return (round(w, 1), round(h, 1))
    collapsed = bool(node.flags.get("collapsed"))
    try:
        wdict = node.widget_dict()
    except Exception:
        wdict = None
    w, h = estimate_size(node.type, node.widgets, node.title, spec,
                         len(node.inputs), len(node.outputs), collapsed=collapsed,
                         wdict=wdict)
    return (w, h)


# ---------------------------------------------------------------- 单例


_REG: Optional[Registry] = None


def registry(server: str = DEFAULT_SERVER, *, refresh: bool = False,
             offline: bool = False, quiet: bool = True) -> Registry:
    """进程内共享的 Registry。offline=True 时只用缓存，没有就返回空字典。"""
    global _REG
    if _REG is not None and not refresh:
        return _REG
    if offline:
        try:
            info, src = load_object_info(server, refresh=False, allow_stale=True)
            if not info:
                raise CwfError("empty")
            _REG = Registry(info, src)
        except Exception:
            _REG = Registry({}, "offline-empty")
        from . import graph as _graph
        _graph.bind_registry(_REG)
        return _REG
    try:
        info, src = load_object_info(server, refresh=refresh)
        _REG = Registry(info, src)
    except CwfError:
        if not quiet:
            raise
        _REG = Registry({}, "unavailable")
    from . import graph as _graph
    _graph.bind_registry(_REG)
    return _REG
