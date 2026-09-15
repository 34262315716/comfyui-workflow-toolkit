# -*- coding: utf-8 -*-
"""
cwf.graph —— ComfyUI 工作流的规范图模型

一份 JSON 里其实住着两套格式：
  * store / UI 格式：前端画布格式（nodes + links + groups + extra），是这个工具的主战场
  * api 格式：提交给 /prompt 的执行图（node_id -> {class_type, inputs}）

本模块把两种格式都读进同一套 Node/Graph 对象，改动后再按需要写回其中一种。
设计原则：**读进来什么怪东西都不许丢**。未知字段一律原样背在 Node.raw 上，
写回时原样吐出，这样排版一个别人做的工作流不会把它的元数据洗掉。
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 常量

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "NUMBER"}
HIDDEN_TYPES = {"UNIQUE_ID", "PROMPT", "EXTRA_PNGINFO", "DYNPROMPT", "AUTH_TOKEN", "API_KEY"}

#: 这些节点是「虚拟跳线 / 纯装饰」，不参与数据流，但排版时要认出来
VIRTUAL_SET = {"SetNode", "SetNodeT8", "Set_Node"}
VIRTUAL_GET = {"GetNode", "GetNodeT8", "Get_Node"}
REROUTE = {"Reroute", "Reroute (rgthree)", "RerouteNode", "ReroutePrimitive|pysssss"}
NOTE_TYPES = {"Note", "孤海注释", "MarkdownNote", "注释", "PreviewNote", "注释节点"}
NOTE_HINTS = ("note", "注释", "comment", "markdown", "标签")

#: 由前端 JS 注册、**不会**出现在 /object_info 里的节点，校验时别报「类型不存在」
FRONTEND_ONLY = VIRTUAL_SET | VIRTUAL_GET | REROUTE | NOTE_TYPES | {
    "PrimitiveNode", "PrimitiveNode|pysssss", "Fast Groups Bypasser (rgthree)",
    "Fast Groups Muter (rgthree)", "Bookmark (rgthree)", "Label (rgthree)",
    "Any Switch (rgthree)", "Any Switch (rgthree)",
}


def is_frontend_only(t: str) -> bool:
    if t in FRONTEND_ONLY:
        return True
    if t.endswith("(rgthree)") and ("switch" in t.lower() or "bypass" in t.lower()
                                    or "muter" in t.lower() or "bookmark" in t.lower()):
        return True
    return False

#: 输入类型为这些的，基本可以断定是「连线端口」而非控件
SCALAR_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN"}


class CwfError(Exception):
    """带退出码的用户级错误。"""

    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


def _as_pair(v: Any, default: Tuple[float, float] = (0.0, 0.0)) -> Tuple[float, float]:
    """pos / size 的容错解析：数组、元组、{'x':..,'y':..}、{'0':..,'1':..} 全吃。"""
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        try:
            return (float(v[0]), float(v[1]))
        except (TypeError, ValueError):
            return default
    if isinstance(v, dict):
        for kx, ky in (("x", "y"), ("0", "1"), (0, 1), ("width", "height"),
                       ("w", "h")):
            if kx in v and ky in v:
                try:
                    return (float(v[kx]), float(v[ky]))
                except (TypeError, ValueError):
                    return default
        nums = [x for x in v.values() if isinstance(x, (int, float))]
        if len(nums) >= 2:
            return (float(nums[0]), float(nums[1]))
    return default


def _as_list(v: Any) -> Optional[List[Any]]:
    """widgets_values 的容错解析：字典形式按 key 排序还原成列表。"""
    if v is None:
        return None
    if isinstance(v, list):
        return list(v)
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, dict):
        def key(k: Any) -> Tuple[int, Any]:
            try:
                return (0, int(k))
            except (TypeError, ValueError):
                return (1, str(k))
        return [v[k] for k in sorted(v.keys(), key=key)]
    return [v]


#: 节点字典的注入点。schema.registry() 会把自己塞进来，让 Node 能查到控件名。
#: 用列表当可变容器，避免模块间循环 import。
_REGISTRY_PROVIDER: List[Any] = []


def bind_registry(reg: Any) -> None:
    _REGISTRY_PROVIDER.clear()
    _REGISTRY_PROVIDER.append(reg)


def _active_registry() -> Any:
    """拿当前节点字典。没显式绑定过就顺手问 schema 要一个（进程内缓存，开销极小）。"""
    if _REGISTRY_PROVIDER and _REGISTRY_PROVIDER[0] is not None:
        return _REGISTRY_PROVIDER[0]
    try:
        from . import schema as _schema
        if _schema._REG is not None:
            bind_registry(_schema._REG)
            return _schema._REG
    except Exception:
        pass
    return None


def is_note_type(t: str) -> bool:
    if t in NOTE_TYPES:
        return True
    low = (t or "").lower()
    return any(h in low for h in ("note", "注释", "comment", "markdown"))


# ---------------------------------------------------------------- 槽位


@dataclass
class Slot:
    """一个输入或输出端口。"""

    name: str
    type: str = "*"
    link: Optional[int] = None          # 输入：单条连线 id；输出：忽略
    links: Optional[List[int]] = None   # 输出：多条连线 id
    label: Optional[str] = None
    localized: Optional[str] = None
    is_widget: bool = False             # 这个输入是「被转成端口」的控件
    widget_name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    # ---------- 读写 json
    @classmethod
    def from_json(cls, d: Dict[str, Any], kind: str) -> "Slot":
        w = d.get("widget") or {}
        s = cls(
            name=d.get("name", "?"),
            type=d.get("type", "*"),
            label=d.get("label"),
            localized=d.get("localized_name"),
            is_widget=bool(w),
            widget_name=(w.get("name") if isinstance(w, dict) else None),
            raw=copy.deepcopy(d),
        )
        if kind == "in":
            s.link = d.get("link")
        else:
            lk = d.get("links")
            s.links = list(lk) if isinstance(lk, list) else None
        return s

    def to_json(self, kind: str) -> Dict[str, Any]:
        d = copy.deepcopy(self.raw)
        d["name"] = self.name
        d["type"] = self.type
        if self.label is not None:
            d["label"] = self.label
        if self.localized is not None:
            d["localized_name"] = self.localized
        if self.is_widget:
            w = d.get("widget") if isinstance(d.get("widget"), dict) else {}
            w = dict(w)
            w.setdefault("name", self.widget_name or self.name)
            d["widget"] = w
        else:
            d.pop("widget", None)
        if kind == "in":
            d["link"] = self.link
        else:
            d["links"] = self.links if self.links else None
        return d

    @property
    def display(self) -> str:
        return self.localized or self.label or self.name


# ---------------------------------------------------------------- 节点


@dataclass
class Node:
    id: int
    type: str
    title: Optional[str] = None
    pos: Tuple[float, float] = (0.0, 0.0)
    size: Tuple[float, float] = (390.0, 60.0)
    mode: int = 0                                  # 0 正常 / 2 静音 / 4 旁路
    inputs: List[Slot] = field(default_factory=list)
    outputs: List[Slot] = field(default_factory=list)
    widgets: Any = None                            # widgets_values（list 或 dict 两种形态）
    widgets_named: Optional[Dict[str, Any]] = None # widgets_values_named
    _wform: str = field(default="none", repr=False)
    properties: Dict[str, Any] = field(default_factory=dict)
    color: Optional[str] = None
    bgcolor: Optional[str] = None
    flags: Dict[str, Any] = field(default_factory=dict)
    order: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    _wnames: Optional[List[str]] = field(default=None, repr=False)

    # 运行期附加信息（不进 JSON）
    module: Optional[str] = None                   # 来自哪个模块包
    short_id: Optional[str] = None                 # DSL 里的短名

    # ---------- 基本属性
    @property
    def label(self) -> str:
        return self.title or self.type

    @property
    def is_note(self) -> bool:
        return is_note_type(self.type) and not self.inputs and not self.outputs

    @property
    def is_virtual(self) -> bool:
        return self.type in VIRTUAL_SET or self.type in VIRTUAL_GET

    @property
    def is_reroute(self) -> bool:
        return self.type in REROUTE

    @property
    def width(self) -> float:
        return float(self.size[0])

    @property
    def height(self) -> float:
        return float(self.size[1])

    @property
    def right(self) -> float:
        return self.pos[0] + self.width

    @property
    def bottom(self) -> float:
        return self.pos[1] + self.height

    @property
    def box(self) -> Tuple[float, float, float, float]:
        return (self.pos[0], self.pos[1], self.right, self.bottom)

    # ---------- 端口查找
    def input(self, name: str) -> Optional[Slot]:
        for s in self.inputs:
            if s.name == name or s.display == name or s.label == name:
                return s
        return None

    def output(self, name: str) -> Optional[Slot]:
        for s in self.outputs:
            if s.name == name or s.display == name or s.label == name:
                return s
        return None

    def input_index(self, name: str) -> int:
        for i, s in enumerate(self.inputs):
            if s.name == name or s.display == name or s.label == name:
                return i
        return -1

    def output_index(self, name: str) -> int:
        for i, s in enumerate(self.outputs):
            if s.name == name or s.display == name or s.label == name:
                return i
        raise CwfError(f"节点 #{self.id} ({self.type}) 没有名为 {name!r} 的输出；"
                       f"可用输出: {[s.name for s in self.outputs]}")

    # ---------- 控件
    def widget_pairs(self):
        """(名字, 值) 的迭代器。

        list 形态的 widgets_values 是**位置数组**，名从哪来？
        按优先级：自己带的名字 → 节点字典(schema)里的控件顺序 → 当前 inputs
        里那些「被转成端口的控件」。这一步很关键：名字对不上，改参数就会改错地方。
        """
        if self._wform == "dict" and isinstance(self.widgets, dict):
            for k, v in self.widgets.items():
                yield (str(k), v)
            return
        if self._wform == "list" and isinstance(self.widgets, list):
            names = self._wnames
            if not names:
                reg = _active_registry()
                spec = reg.get(self.type) if reg is not None else None
                if spec is not None:
                    names = [i.name for i in spec.widget_inputs]
                elif self.inputs:
                    names = [s.name for s in self.inputs if s.is_widget]
            if not names:
                names = []
            for i, v in enumerate(self.widgets):
                yield ((names[i] if i < len(names) else f"w{i}"), v)
            return
        if self.widgets_named:
            for k, v in self.widgets_named.items():
                yield (str(k), v)

    def widget_names(self) -> List[str]:
        return [k for k, _ in self.widget_pairs()]

    def widget_value(self, name: str) -> Any:
        low = str(name).lower()
        for k, v in self.widget_pairs():
            if k == name or k.lower() == low:
                return v
        names = self.widget_names()
        raise CwfError(f"节点 #{self.id} ({self.type}) 没有名为 {name!r} 的控件值；"
                       f"可用控件: {names}")

    def has_widget(self, name: str) -> bool:
        low = str(name).lower()
        return any(k == name or k.lower() == low for k, _ in self.widget_pairs())

    def set_widget(self, name: str, value: Any) -> None:
        """写控件值。dict 形态直接改 key；list 形态按名字对位改。"""
        low = str(name).lower()
        if self._wform == "dict" and isinstance(self.widgets, dict):
            for k in list(self.widgets.keys()):
                if k == name or str(k).lower() == low:
                    self.widgets[k] = value
                    if self.widgets_named is not None:
                        self.widgets_named[k] = value
                    return
            raise CwfError(f"节点 #{self.id} ({self.type}) 没有控件 {name!r}；"
                           f"可用: {list(self.widgets.keys())}")
        if self._wform == "list" and isinstance(self.widgets, list):
            names = [k for k, _ in self.widget_pairs()]
            for i, k in enumerate(names):
                if k == name or str(k).lower() == low:
                    if i < len(self.widgets):
                        self.widgets[i] = value
                    else:
                        self.widgets.append(value)
                    if self.widgets_named is not None:
                        self.widgets_named[k] = value
                    return
            raise CwfError(f"节点 #{self.id} ({self.type}) 没有控件 {name!r}；可用: {names}")
        if self.widgets_named is not None:
            for k in list(self.widgets_named.keys()):
                if k == name or str(k).lower() == low:
                    self.widgets_named[k] = value
                    return
        hint = ""
        try:
            pairs = list(self.widget_pairs())
        except Exception:
            pairs = []
        if pairs:
            hint = f"；可用: {[k for k, _ in pairs]}"
        raise CwfError(f"节点 #{self.id} ({self.type}) 没有控件值可写{hint}")

    def widget_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.widget_pairs()}

    # ---------- 转换
    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Node":
        raw_widgets = d.get("widgets_values")
        if isinstance(raw_widgets, dict):
            form, widgets = "dict", copy.deepcopy(raw_widgets)
        elif isinstance(raw_widgets, (list, tuple)):
            form, widgets = "list", list(raw_widgets)
        elif raw_widgets is None:
            form, widgets = "none", None
        else:
            form, widgets = "list", [raw_widgets]
        named = copy.deepcopy(d.get("widgets_values_named"))
        wnames: Optional[List[str]] = None
        if isinstance(named, dict) and named:
            wnames = [str(k) for k in named.keys()]
        n = cls(
            id=int(d.get("id", 0)),
            type=d.get("type", "Unknown"),
            title=d.get("title"),
            pos=_as_pair(d.get("pos"), (0.0, 0.0)),
            size=_as_pair(d.get("size"), (390.0, 60.0)),
            mode=int(d.get("mode", 0) or 0),
            inputs=[Slot.from_json(x, "in") for x in (d.get("inputs") or [])],
            outputs=[Slot.from_json(x, "out") for x in (d.get("outputs") or [])],
            widgets=widgets,
            widgets_named=named,
            _wform=form,
            _wnames=wnames,
            properties=copy.deepcopy(d.get("properties") or {}),
            color=d.get("color"),
            bgcolor=d.get("bgcolor"),
            flags=copy.deepcopy(d.get("flags") or {}),
            order=d.get("order"),
            raw=copy.deepcopy(d),
        )
        return n

    def to_json(self) -> Dict[str, Any]:
        d = copy.deepcopy(self.raw)
        d["id"] = self.id
        d["type"] = self.type
        d["pos"] = [round(float(self.pos[0]), 2), round(float(self.pos[1]), 2)]
        d["size"] = [round(float(self.size[0]), 2), round(float(self.size[1]), 2)]
        d["mode"] = self.mode
        d["flags"] = self.flags or {}
        if self.title:
            d["title"] = self.title
        else:
            d.pop("title", None)
        if self.inputs:
            d["inputs"] = [s.to_json("in") for s in self.inputs]
        else:
            d.pop("inputs", None)
        if self.outputs:
            d["outputs"] = [s.to_json("out") for s in self.outputs]
        else:
            d.pop("outputs", None)
        if self.widgets is not None:
            d["widgets_values"] = self.widgets
        elif "widgets_values" in d:
            d.pop("widgets_values", None)
        if self.widgets_named is not None:
            d["widgets_values_named"] = self.widgets_named
        if self.properties:
            d["properties"] = self.properties
        if self.color:
            d["color"] = self.color
        if self.bgcolor:
            d["bgcolor"] = self.bgcolor
        if self.order is not None:
            d["order"] = self.order
        return d


# ---------------------------------------------------------------- 连线


@dataclass
class Link:
    id: int
    origin_id: int
    origin_slot: int
    target_id: int
    target_slot: int
    type: str = "*"

    def to_json(self) -> List[Any]:
        return [self.id, self.origin_id, self.origin_slot, self.target_id, self.target_slot, self.type]


# ---------------------------------------------------------------- 分区框


@dataclass
class Group:
    id: int
    title: str
    bounding: Tuple[float, float, float, float]   # x, y, w, h
    color: Optional[str] = None
    flags: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Group":
        b = d.get("bounding") or [0, 0, 0, 0]
        return cls(int(d.get("id", 0)), d.get("title", ""),
                   (float(b[0]), float(b[1]), float(b[2]), float(b[3])),
                   d.get("color"), copy.deepcopy(d.get("flags") or {}), copy.deepcopy(d))

    def to_json(self) -> Dict[str, Any]:
        d = copy.deepcopy(self.raw)
        d.update({
            "id": self.id, "title": self.title,
            "bounding": [round(float(x), 2) for x in self.bounding],
            "flags": self.flags or {},
        })
        if self.color:
            d["color"] = self.color
        return d

    @property
    def box(self) -> Tuple[float, float, float, float]:
        x, y, w, h = self.bounding
        return (x, y, x + w, y + h)


# ---------------------------------------------------------------- 图


class Graph:
    """一张 ComfyUI 工作流。"""

    def __init__(self, nodes: Sequence[Node] = (), links: Sequence[Link] = (),
                 groups: Sequence[Group] = (), extra: Optional[Dict] = None,
                 meta: Optional[Dict] = None, path: Optional[str] = None):
        self.nodes: List[Node] = list(nodes)
        self.links: List[Link] = list(links)
        self.groups: List[Group] = list(groups)
        self.extra: Dict[str, Any] = extra if extra is not None else {}
        self.meta: Dict[str, Any] = meta or {}
        self.path: Optional[str] = path
        self._index: Dict[int, Node] = {n.id: n for n in self.nodes}
        self.next_id: int = (max((n.id for n in self.nodes), default=0) + 1)
        self.next_link: int = (max((l.id for l in self.links), default=0) + 1)
        self.next_group: int = (max((g.id for g in self.groups), default=0) + 1)

    # ---------- 容器协议
    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self):
        return iter(self.nodes)

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, Node):
            return key.id in self._index
        return int(key) in self._index

    def __getitem__(self, key: Any) -> Node:
        if isinstance(key, Node):
            return key
        return self.node(int(key))

    # ---------- 查询
    def node(self, nid: int) -> Node:
        try:
            return self._index[int(nid)]
        except (KeyError, ValueError):
            raise CwfError(f"找不到节点 #{nid}")

    def maybe(self, nid: Any) -> Optional[Node]:
        try:
            return self._index.get(int(nid))
        except (TypeError, ValueError):
            return None

    def by_title(self, needle: str) -> List[Node]:
        low = needle.lower()
        return [n for n in self.nodes
                if needle in (n.title or "") or low in (n.title or "").lower()]

    def by_type(self, t: str) -> List[Node]:
        return [n for n in self.nodes if n.type == t or n.type.lower() == t.lower()]

    def find(self, token: Any) -> Node:
        """把引用解析成节点。依次尝试：

        `123` / `#123`  按 id
        标题全等 → 类型全等 → 标题子串 → 「02. 采样」这种去掉序号后再比 → 类型子串
        仍然歧义就报错并列候选，绝不瞎猜。
        """
        if isinstance(token, Node):
            return token
        if isinstance(token, int):
            return self.node(token)
        t = str(token).strip()
        if t.startswith("#") and t[1:].isdigit():
            return self.node(int(t[1:]))
        if t.isdigit():
            return self.node(int(t))

        def unnum(s: str) -> str:
            return re.sub(r"^\s*\d+\s*[.、:：\-]\s*", "", s or "").strip()

        exact = [n for n in self.nodes if n.title == t]
        if len(exact) == 1:
            return exact[0]
        exact_t = [n for n in self.nodes if n.type == t]
        if len(exact_t) == 1:
            return exact_t[0]
        un = unnum(t)
        if un:
            hit = [n for n in self.nodes if unnum(n.title or "") == un]
            if len(hit) == 1:
                return hit[0]
        low = t.lower()
        hits = [n for n in self.nodes if low in (n.title or "").lower()]
        if len(hits) == 1:
            return hits[0]
        if un:
            hits = [n for n in self.nodes if un.lower() in unnum(n.title or "").lower()]
            if len(hits) == 1:
                return hits[0]
        if not hits:
            hits = [n for n in self.nodes if low in n.type.lower()]
            if len(hits) == 1:
                return hits[0]
        if not hits:
            # 最后试中文别名：用户说「解码」，图上写的是 VAEDecode
            try:
                from .terms import resolve_type
                real = resolve_type(t, _active_registry())
                if real:
                    hits = [n for n in self.nodes if n.type == real]
                    if len(hits) == 1:
                        return hits[0]
                    if len(hits) > 1:
                        raise CwfError(
                            f"{t!r} 对应 {real}，但图上有 {len(hits)} 个，请用 #id 指定：" +
                            "、".join(f"#{n.id} {n.label}" for n in hits[:8]))
            except CwfError:
                raise
            except Exception:
                pass
        if not hits:
            raise CwfError(f"找不到匹配 {t!r} 的节点")
        raise CwfError(f"{t!r} 匹配到多个节点，请用 #id 指定：" +
                       "、".join(f"#{n.id} {n.label}" for n in hits[:8]))

    def find_all(self, token: Any) -> List[Node]:
        """选择器 → 一批节点（可能 0 个或多个）。与 find() 的区别是不怕歧义。

        支持：
            `#12` / `12`        按 id
            `type:KSampler`     按类型（精确）
            `title:采样`         按标题子串
            `~正则`              按标题正则
            `short:竖图`         按 DSL 里的短名
            其他                先试 find()，不行再按标题/短名/类型子串模糊

        短名（`properties.cwf_short`）是 DSL 建的图里最靠得住的抓手：
        一张图里常有四个 `EmptyLatentImage`，只有短名能区分谁是谁。
        """
        if isinstance(token, Node):
            return [token]
        t = str(token).strip()
        if t.startswith("#") and t[1:].isdigit():
            n = self.maybe(int(t[1:]))
            return [n] if n else []
        if t.isdigit():
            n = self.maybe(int(t))
            return [n] if n else []
        if ":" in t:
            kind, val = t.split(":", 1)
            k = kind.strip().lower()
            val = val.strip()
            if k in ("type", "类型"):
                from .terms import resolve_type
                real = resolve_type(val, _active_registry())
                hits = self.by_type(real or val)
                if not hits and real:
                    hits = self.by_type(val)
                return hits
            if k in ("title", "标题", "name"):
                return [n for n in self.nodes if val.lower() in (n.title or "").lower()]
            if k in ("short", "短名", "alias"):
                return [n for n in self.nodes
                        if val.lower() == str(n.properties.get("cwf_short") or "").lower()]
        if t.startswith("~"):
            pat = re.compile(t[1:])
            return [n for n in self.nodes if pat.search(n.title or "")]
        try:
            return [self.find(t)]
        except CwfError:
            low = t.lower()
            hits = [n for n in self.nodes
                    if low in (n.title or "").lower() or low in n.type.lower()
                    or low == str(n.properties.get("cwf_short") or "").lower()]
            if hits:
                return hits
            from .terms import resolve_type
            real = resolve_type(t, _active_registry())
            if real:
                return self.by_type(real)
            return []

    def reindex(self) -> None:
        self._index = {n.id: n for n in self.nodes}
        self.next_id = max(self.next_id, max((n.id for n in self.nodes), default=0) + 1)
        self.next_link = max(self.next_link, max((l.id for l in self.links), default=0) + 1)
        self.next_group = max(self.next_group, max((g.id for g in self.groups), default=0) + 1)

    # ---------- 结构统计
    @property
    def real_nodes(self) -> List[Node]:
        return [n for n in self.nodes if not n.is_note]

    def incoming(self, nid: int) -> List[Link]:
        return [l for l in self.links if l.target_id == nid]

    def outgoing(self, nid: int) -> List[Link]:
        return [l for l in self.links if l.origin_id == nid]

    def orphans(self) -> List[Node]:
        """既不进也不出、又不是注释的节点（多半是忘了接线）。"""
        return [n for n in self.real_nodes
                if not self.incoming(n.id) and not self.outgoing(n.id)
                and not (n.is_virtual and n.type in VIRTUAL_GET)]

    def terminals(self) -> List[Node]:
        """终结点：没有下游输出，且有上游输入（SaveImage / PreviewImage 一类）。"""
        return [n for n in self.real_nodes
                if not self.outgoing(n.id) and self.incoming(n.id)]

    def virtual_map(self) -> Dict[str, Tuple[Optional[Node], List[Node]]]:
        """SetNode / GetNode 的虚拟跳线映射：名字 -> (发源 SetNode, [GetNode...])。"""
        sets: Dict[str, Optional[Node]] = {}
        gets: Dict[str, List[Node]] = {}
        for n in self.nodes:
            key = None
            if isinstance(n.widgets, list) and n.widgets and isinstance(n.widgets[0], str):
                key = n.widgets[0]
            elif isinstance(n.widgets_named, dict):
                for v in n.widgets_named.values():
                    if isinstance(v, str):
                        key = v
                        break
            if key is None:
                continue
            if n.type in VIRTUAL_SET:
                sets.setdefault(key, n)
            elif n.type in VIRTUAL_GET:
                gets.setdefault(key, []).append(n)
        return {k: (sets.get(k), gets.get(k, [])) for k in (set(sets) | set(gets))}

    # ---------- 载入 / 保存
    @classmethod
    def from_ui(cls, data: Dict[str, Any], path: Optional[str] = None) -> "Graph":
        nodes = [Node.from_json(d) for d in data.get("nodes") or []]

        # 顶层 links 数组经常是陈旧的（id 与端口对不上，甚至整个缺项）——
        # ComfyUI 前端真正依赖的是「节点上的 link 字段」，所以以节点侧为权威，
        # 用顶层数组补齐类型信息，对不上就修，缺了就从节点侧反推出来。
        top: Dict[int, Link] = {}
        for item in data.get("links") or []:
            if isinstance(item, dict):
                lk = Link(int(item.get("id", 0)), int(item.get("origin_id", 0)),
                          int(item.get("origin_slot", 0)), int(item.get("target_id", 0)),
                          int(item.get("target_slot", 0)), item.get("type", "*"))
            elif isinstance(item, (list, tuple)) and len(item) >= 5:
                lk = Link(int(item[0]), int(item[1]), int(item[2]), int(item[3]),
                          int(item[4]), str(item[5]) if len(item) > 5 else "*")
            else:
                continue
            top[lk.id] = lk

        # 输出侧：link id -> (节点, 槽位)
        origin_of: Dict[int, Tuple[Node, int]] = {}
        for n in nodes:
            for i, s in enumerate(n.outputs):
                for lid in (s.links or []):
                    origin_of.setdefault(int(lid), (n, i))

        links: List[Link] = []
        for n in nodes:
            for i, s in enumerate(n.inputs):
                if s.link is None:
                    continue
                lid = int(s.link)
                base = top.get(lid)
                org = origin_of.get(lid)
                if org is None:
                    if base is not None:
                        # 只有顶层记录：用它，但目标以节点为准（节点一定是对的）
                        links.append(Link(lid, base.origin_id, base.origin_slot,
                                          n.id, i, base.type))
                    continue
                src, sidx = org
                typ = "*"
                if sidx < len(src.outputs) and src.outputs[sidx].type not in (None, "*"):
                    typ = src.outputs[sidx].type
                elif s.type not in (None, "*"):
                    typ = s.type
                elif base is not None and base.type:
                    typ = base.type
                links.append(Link(lid, src.id, sidx, n.id, i, typ))

        # 顶层的孤立记录（没有节点引用的）丢掉；重复 id 去重
        seen: Dict[int, Link] = {}
        for l in links:
            seen.setdefault(l.id, l)
        clean: List[Link] = []
        for l in seen.values():
            s = next((x for x in nodes if x.id == l.origin_id), None)
            if s is not None and l.origin_slot < len(s.outputs):
                sl = s.outputs[l.origin_slot]
                if sl.links is None:
                    sl.links = []
                if l.id not in sl.links:
                    sl.links.append(l.id)
            clean.append(l)

        groups = [Group.from_json(g) for g in data.get("groups") or []]
        extra = copy.deepcopy(data.get("extra") or {})
        meta = {
            "id": data.get("id") or str(uuid.uuid4()),
            "revision": data.get("revision", 0),
            "version": data.get("version", 0.4),
            "last_node_id": data.get("last_node_id"),
            "last_link_id": data.get("last_link_id"),
            "config": copy.deepcopy(data.get("config") or {}),
        }
        return cls(nodes, clean, groups, extra, meta, path)

    @classmethod
    def from_api(cls, data: Dict[str, Any], path: Optional[str] = None) -> "Graph":
        """把 /prompt 的 api 格式（常用于从队列里捞回工作流）转成 UI 图。"""
        nodes: List[Node] = []
        links: List[Link] = []
        next_link = 1
        # 先建节点骨架
        id_map = {k: int(k) for k in data.keys() if str(k).lstrip("-").isdigit()}
        for key, spec in data.items():
            if not isinstance(spec, dict) or "class_type" not in spec:
                continue
            nid = int(key)
            kw = spec.get("inputs") or {}
            ins: List[Slot] = []
            outs: List[Slot] = []
            widgets: List[Any] = []
            for name, val in kw.items():
                if isinstance(val, list) and len(val) == 2 and str(val[0]) in id_map:
                    ins.append(Slot(name=name, type="*", link=None))
                else:
                    ins.append(Slot(name=name, type="*", is_widget=True, widget_name=name))
                    widgets.append(val)
            nodes.append(Node(id=nid, type=spec["class_type"], title=spec.get("_meta", {}).get("title"),
                              inputs=ins, outputs=outs, widgets=widgets,
                              properties={"Node name for S&R": spec["class_type"]}))
            nodes[-1].short_id = None
        g = cls(nodes, links, [], {}, {"id": str(uuid.uuid4()), "version": 0.4}, path)
        g.reindex()
        # 再接线
        for key, spec in data.items():
            if not isinstance(spec, dict) or "class_type" not in spec:
                continue
            nid = int(key)
            for name, val in (spec.get("inputs") or {}).items():
                if isinstance(val, list) and len(val) == 2 and str(val[0]) in id_map:
                    src = g.node(int(val[0]))
                    slot = int(val[1])
                    while len(src.outputs) <= slot:
                        src.outputs.append(Slot(name=f"out{len(src.outputs)}", type="*", links=[]))
                    tgt_slot = g.node(nid).input_index(name)
                    if tgt_slot < 0:
                        g.node(nid).inputs.append(Slot(name=name, type="*"))
                        tgt_slot = len(g.node(nid).inputs) - 1
                    g.connect(src, slot, g.node(nid), tgt_slot, link_id=next_link)
                    next_link += 1
        return g

    def to_ui(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.meta.get("id") or str(uuid.uuid4()),
            "revision": self.meta.get("revision", 0),
            "last_node_id": max((n.id for n in self.nodes), default=0),
            "last_link_id": max((l.id for l in self.links), default=0),
            "nodes": [n.to_json() for n in self.nodes],
            "links": [l.to_json() for l in self.links],
            "groups": [g.to_json() for g in self.groups],
            "config": self.meta.get("config") or {},
            "extra": self.extra,
            "version": self.meta.get("version", 0.4),
        }
        return data

    def to_api(self) -> Dict[str, Any]:
        """转成 /prompt 用的 api 格式。"""
        out: Dict[str, Any] = {}
        for n in self.nodes:
            if n.is_note or n.mode == 2:
                continue
            if n.type in VIRTUAL_SET or n.type in VIRTUAL_GET or n.is_reroute:
                continue
            ins: Dict[str, Any] = {}
            for nm, val in n.widget_pairs():
                if nm == "videopreview" or val is None:
                    continue
                ins[nm] = val
            for i, s in enumerate(n.inputs):
                if s.link is None:
                    continue
                lk = self.link(s.link)
                if lk is None:
                    continue
                ins[s.name] = [str(lk.origin_id), lk.origin_slot]
            out[str(n.id)] = {"class_type": n.type, "inputs": ins,
                              "_meta": {"title": n.title or n.type}}
        return out

    def link(self, lid: int) -> Optional[Link]:
        for l in self.links:
            if l.id == lid:
                return l
        return None

    @classmethod
    def load(cls, path: str) -> "Graph":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise CwfError(f"找不到文件：{path}")
        except json.JSONDecodeError as e:
            raise CwfError(f"{os.path.basename(path)} 不是合法 JSON：{e}")
        if isinstance(data, dict) and "nodes" in data:
            return cls.from_ui(data, path)
        if isinstance(data, dict) and "prompt" in data and isinstance(data["prompt"], dict):
            return cls.from_api(data["prompt"], path)
        if isinstance(data, dict) and all(
                isinstance(v, dict) and "class_type" in v
                for v in data.values() if isinstance(v, dict)) and data:
            return cls.from_api(data, path)
        raise CwfError(f"{os.path.basename(path)} 既不是 UI 格式也不是 API 格式的工作流")

    def save(self, path: str, fmt: str = "ui") -> str:
        data = self.to_ui() if fmt == "ui" else self.to_api()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.path = path
        return path

    def snapshot(self) -> "Graph":
        return Graph.loads(self.dumps())

    def dumps(self) -> str:
        return json.dumps(self.to_ui(), ensure_ascii=False, indent=2)

    @classmethod
    def loads(cls, s: str) -> "Graph":
        return cls.from_ui(json.loads(s))

    # ---------- 编辑原语
    def add_node(self, type_: str, pos: Tuple[float, float] = (0, 0), title: Optional[str] = None,
                 widgets: Optional[List[Any]] = None, nid: Optional[int] = None, **kw) -> Node:
        nid = int(nid) if nid is not None else self.next_id
        self.next_id = max(self.next_id, nid + 1)
        if nid in self._index:
            raise CwfError(f"节点 id #{nid} 已被占用")
        n = Node(id=nid, type=type_, title=title, pos=pos, widgets=widgets, **kw)
        self.nodes.append(n)
        self._index[n.id] = n
        return n

    def remove_node(self, node: Any, keep_wiring: bool = False) -> None:
        """删掉一个节点。keep_wiring=True 时把「上游 → 本节点 → 下游」重新接直，
        典型的用法是拔掉一个中间处理器但不破坏主链。"""
        n = self.find(node) if not isinstance(node, Node) else node
        rewires: List[Tuple[Link, List[Link]]] = []
        if keep_wiring:
            upstream = {l.target_slot: l for l in self.incoming(n.id)}
            for out_l in self.outgoing(n.id):
                same_type = [u for u in upstream.values()
                             if u.type == out_l.type or u.type == "*" or out_l.type == "*"]
                if same_type:
                    rewires.append((out_l, same_type))
        for l in list(self.links):
            if l.origin_id == n.id or l.target_id == n.id:
                self.disconnect(link_id=l.id)
        for out_l, ups in rewires:
            up = ups[0]
            src = self.maybe(up.origin_id)
            if src is None:
                continue
            try:
                self.connect(src, up.origin_slot, out_l.target_id, out_l.target_slot)
            except CwfError:
                pass
        self.nodes = [x for x in self.nodes if x.id != n.id]
        self._index.pop(n.id, None)

    def remove_nodes(self, nodes: Iterable[Any]) -> int:
        ids = {self.find(x).id if not isinstance(x, Node) else x.id for x in nodes}
        for l in list(self.links):
            if l.origin_id in ids or l.target_id in ids:
                self.disconnect(link_id=l.id)
        self.nodes = [n for n in self.nodes if n.id not in ids]
        for i in ids:
            self._index.pop(i, None)
        return len(ids)

    def connect(self, src: Any, src_slot: Any, dst: Any, dst_slot: Any,
                link_id: Optional[int] = None) -> Link:
        s = self.find(src) if not isinstance(src, Node) else src
        d = self.find(dst) if not isinstance(dst, Node) else dst
        oi = src_slot if isinstance(src_slot, int) else s.output_index(str(src_slot))
        if oi >= len(s.outputs):
            raise CwfError(f"节点 #{s.id} ({s.type}) 只有 {len(s.outputs)} 个输出，"
                           f"拿不到第 {oi} 个（0 起算）")
        di = dst_slot if isinstance(dst_slot, int) else d.input_index(str(dst_slot))
        if di < 0 or di >= len(d.inputs):
            raise CwfError(f"节点 #{d.id} ({d.type}) 没有输入 {dst_slot!r}；"
                           f"可用: {[x.name for x in d.inputs]}")
        old = d.inputs[di].link
        if old is not None:
            self.disconnect(link_id=old)
        lid = int(link_id) if link_id is not None else self.next_link
        self.next_link = max(self.next_link, lid + 1)
        typ = s.outputs[oi].type or d.inputs[di].type
        if typ == "*" and d.inputs[di].type != "*":
            typ = d.inputs[di].type
        lk = Link(lid, s.id, oi, d.id, di, typ)
        self.links.append(lk)
        d.inputs[di].link = lid
        if s.outputs[oi].links is None:
            s.outputs[oi].links = []
        if lid not in s.outputs[oi].links:
            s.outputs[oi].links.append(lid)
        return lk

    def disconnect(self, link_id: Optional[int] = None, src: Any = None, src_slot: Any = None,
                   dst: Any = None, dst_slot: Any = None) -> int:
        """三种用法：按 link id、按来源、按目标。返回断掉的条数。"""
        if link_id is not None:
            targets = [l for l in self.links if l.id == int(link_id)]
        elif dst is not None:
            d = self.find(dst) if not isinstance(dst, Node) else dst
            if dst_slot is not None:
                di = dst_slot if isinstance(dst_slot, int) else d.input_index(str(dst_slot))
                targets = [l for l in self.links if l.target_id == d.id and l.target_slot == di]
            else:
                targets = [l for l in self.links if l.target_id == d.id]
        elif src is not None:
            s = self.find(src) if not isinstance(src, Node) else src
            if src_slot is not None:
                oi = src_slot if isinstance(src_slot, int) else s.output_index(str(src_slot))
                targets = [l for l in self.links if l.origin_id == s.id and l.origin_slot == oi]
            else:
                targets = [l for l in self.links if l.origin_id == s.id]
        else:
            raise CwfError("disconnect 需要 link_id / src / dst 之一")
        for l in targets:
            s = self.maybe(l.origin_id)
            if s and s.outputs and l.origin_slot < len(s.outputs):
                sl = s.outputs[l.origin_slot]
                if sl.links and l.id in sl.links:
                    sl.links.remove(l.id)
                    if not sl.links:
                        sl.links = None
            d = self.maybe(l.target_id)
            if d and l.target_slot < len(d.inputs) and d.inputs[l.target_slot].link == l.id:
                d.inputs[l.target_slot].link = None
            self.links = [x for x in self.links if x.id != l.id]
        return len(targets)

    def clear_all_links(self) -> int:
        n = len(self.links)
        for node in self.nodes:
            for s in node.inputs:
                s.link = None
            for s in node.outputs:
                s.links = None
        self.links = []
        return n

    def clone_node(self, node: Any, offset: Tuple[float, float] = (0, 0),
                   new_id: Optional[int] = None) -> Node:
        src = self.find(node) if not isinstance(node, Node) else node
        raw = copy.deepcopy(src.raw)
        raw["id"] = new_id if new_id is not None else self.next_id
        the_new = Node.from_json(raw)
        the_new.pos = (src.pos[0] + offset[0], src.pos[1] + offset[1])
        the_new.short_id = None
        for s in the_new.inputs:
            s.link = None
        for s in the_new.outputs:
            s.links = None
        self.next_id = max(self.next_id, the_new.id + 1)
        self.nodes.append(the_new)
        self._index[the_new.id] = the_new
        return the_new

    def reorder(self) -> None:
        """按拓扑序刷 order 字段（ComfyUI 用它决定初始执行顺序）。"""
        seen: Dict[int, int] = {}
        order = 0
        for nid in self.topo_order():
            node = self.maybe(nid)
            if node is None:
                continue
            node.order = order
            seen[nid] = order
            order += 1
        for n in self.nodes:
            if n.id not in seen:
                n.order = order
                order += 1

    def topo_order(self) -> List[int]:
        indeg = {n.id: 0 for n in self.nodes}
        adj: Dict[int, List[int]] = {n.id: [] for n in self.nodes}
        for l in self.links:
            if l.origin_id in indeg and l.target_id in indeg:
                adj[l.origin_id].append(l.target_id)
                indeg[l.target_id] += 1
        q = [n.id for n in self.nodes if indeg[n.id] == 0]
        out: List[int] = []
        while q:
            nid = q.pop(0)
            out.append(nid)
            for t in adj[nid]:
                indeg[t] -= 1
                if indeg[t] == 0:
                    q.append(t)
        if len(out) != len(self.nodes):                     # 有环：剩下的按 id 补
            out += [n.id for n in self.nodes if n.id not in out]
        return out

    def bounds(self, include_notes: bool = True) -> Tuple[float, float, float, float]:
        ns = self.nodes if include_notes else self.real_nodes
        if not ns:
            return (0.0, 0.0, 0.0, 0.0)
        x0 = min(n.pos[0] for n in ns)
        y0 = min(n.pos[1] for n in ns)
        x1 = max(n.right for n in ns)
        y1 = max(n.bottom for n in ns)
        return (x0, y0, x1, y1)

    def translate(self, dx: float, dy: float, notes_too: bool = True) -> None:
        for n in self.nodes:
            if n.is_note and not notes_too:
                continue
            n.pos = (n.pos[0] + dx, n.pos[1] + dy)
        for g in self.groups:
            x, y, w, h = g.bounding
            g.bounding = (x + dx, y + dy, w, h)

    def normalize_origin(self, margin: float = 0.0, notes_too: bool = True) -> None:
        x0, y0, _, _ = self.bounds(include_notes=notes_too)
        self.translate(margin - x0, margin - y0, notes_too=notes_too)

    # ---------- 统计
    def summary(self) -> Dict[str, Any]:
        types: Dict[str, int] = {}
        for n in self.nodes:
            types[n.type] = types.get(n.type, 0) + 1
        return {
            "nodes": len(self.nodes),
            "real_nodes": len(self.real_nodes),
            "notes": len([n for n in self.nodes if n.is_note]),
            "links": len(self.links),
            "groups": len(self.groups),
            "bypassed": len([n for n in self.nodes if n.mode == 4]),
            "muted": len([n for n in self.nodes if n.mode == 2]),
            "orphans": len(self.orphans()),
            "terminals": len(self.terminals()),
            "types": len(types),
            "top_types": sorted(types.items(), key=lambda kv: -kv[1])[:12],
        }
