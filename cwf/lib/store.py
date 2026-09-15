# -*- coding: utf-8 -*-
"""
cwf.store —— 节点知识库（跨会话沉淀你摸清过的节点）

为什么需要这一层：
  `/object_info` 有 7746 种节点，每次要用某个节点都得重新查一遍端口和控件——
  这是最烦的摩擦。这里把「查过 / 摸清 / 常用」的节点沉淀成三样东西：

  1. **别名 alias**   `解码 = VAEDecode` —— 之后所有命令里都能直接用「解码」
  2. **笔记 note**    这个节点的端口怎么接、有什么用、踩过什么坑
  3. **收藏 pin**     常用节点的短名单，`cwf store list` 一眼看全

三条设计原则：
  * **落成纯文本**（markdown / 一行一条），出问题时你自己就能改，不用找我
  * **能反查**：store 里没有的，自动回落到 7746 种节点字典里搜
  * **能带走**：`cwf store export` 导出成一个 markdown，换机器直接拷

目录结构（默认 `~/.cwf/store/`，可用环境变量 CWF_STORE 覆盖）：
    aliases.txt          一行一条：别名 = 节点类型
    pins.txt             一行一个：节点类型
    notes/<类型>.md      单个节点的笔记
    README.md            自动生成的总览（给人看，也是导出格式）
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .graph import CwfError
from .schema import CACHE_DIR

STORE_DIR = os.environ.get("CWF_STORE", os.path.join(CACHE_DIR, "store"))


# ---------------------------------------------------------------- 读写原语


def ensure_store(root: Optional[str] = None) -> str:
    d = os.path.abspath(root or STORE_DIR)
    os.makedirs(os.path.join(d, "notes"), exist_ok=True)
    if not os.path.exists(os.path.join(d, "aliases.txt")):
        with open(os.path.join(d, "aliases.txt"), "w", encoding="utf-8") as f:
            f.write("# 节点别名 —— 一行一条：别名 = 节点类型\n"
                    "# 这个文件你可以直接用记事本改，改完立刻生效\n"
                    "#\n"
                    "# 内置的中文别名（解码/主模型/放大…）在代码里，不用写在这里；\n"
                    "# 这里只放你自己惯用的说法。\n")
    if not os.path.exists(os.path.join(d, "pins.txt")):
        with open(os.path.join(d, "pins.txt"), "w", encoding="utf-8") as f:
            f.write("# 常用节点短名单 —— 一行一个节点类型\n")
    return d


def load_aliases(root: Optional[str] = None) -> Dict[str, str]:
    """别名 → 节点类型。兼容 `别名 = 类型` 和 `别名=类型` 两种写法。"""
    d = ensure_store(root)
    out: Dict[str, str] = {}
    with open(os.path.join(d, "aliases.txt"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def save_alias(alias: str, type_: str, root: Optional[str] = None) -> str:
    """写一条别名。

    必须**就地改**而不是追加：同一个别名记两次（比如先记 KSampler，过几天又
    记一遍），追加会留下两行同名别名。文件一长，人看着乱、排查也费劲。
    改指别的类型时同样要覆盖，否则旧行先被读到，新写的永远不生效。
    """
    d = ensure_store(root)
    p = os.path.join(d, "aliases.txt")
    lines: List[str] = []
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    hit = False
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.partition("=")[0].strip() == alias:
            lines[idx] = f"{alias} = {type_}"
            hit = True
            break
    if not hit:
        lines.append(f"{alias} = {type_}")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    return p


def load_pins(root: Optional[str] = None) -> List[str]:
    d = ensure_store(root)
    out: List[str] = []
    with open(os.path.join(d, "pins.txt"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and line not in out:
                out.append(line)
    return out


def toggle_pin(type_: str, root: Optional[str] = None) -> Tuple[bool, str]:
    """收藏 / 取消收藏。返回 (现在是否已收藏, 文件路径)。"""
    d = ensure_store(root)
    pins = load_pins(d)
    p = os.path.join(d, "pins.txt")
    if type_ in pins:
        pins = [x for x in pins if x != type_]
        pinned = False
    else:
        pins.append(type_)
        pinned = True
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 常用节点短名单 —— 一行一个节点类型\n")
        for x in pins:
            f.write(x + "\n")
    return pinned, p


# ---------------------------------------------------------------- 笔记


def _safe_name(type_: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", type_).strip("_") or "node"


def note_path(type_: str, root: Optional[str] = None) -> str:
    return os.path.join(ensure_store(root), "notes", _safe_name(type_) + ".md")


def load_note(type_: str, root: Optional[str] = None) -> Optional[str]:
    p = note_path(type_, root)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def save_note(type_: str, body: str, root: Optional[str] = None,
              append: bool = True) -> str:
    d = ensure_store(root)
    p = note_path(type_, d)
    header = ""
    if not os.path.exists(p) or not append:
        header = (f"# {type_}\n\n"
                  f"> 由 `cwf store mark` 记录 · {time.strftime('%Y-%m-%d %H:%M')}\n\n")
    mode = "a" if (append and os.path.exists(p)) else "w"
    with open(p, mode, encoding="utf-8") as f:
        if mode == "w":
            f.write(header + body.rstrip() + "\n")
        else:
            f.write("\n" + body.rstrip() + "\n")
    return p


def list_notes(root: Optional[str] = None) -> List[Dict[str, Any]]:
    d = ensure_store(root)
    out = []
    nd = os.path.join(d, "notes")
    for fn in sorted(os.listdir(nd)):
        if not fn.endswith(".md"):
            continue
        p = os.path.join(nd, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        m = re.match(r"#\s*(\S+)", text)
        out.append({
            "type": m.group(1) if m else os.path.splitext(fn)[0],
            "file": p,
            "mtime": os.path.getmtime(p),
            "lines": text.count("\n") + 1,
        })
    return out


# ---------------------------------------------------------------- 搜索


BUILTIN_ZH = {
    "解码": ("decode",), "编码": ("encode",), "采样": ("sampler", "ksampler"),
    "模型": ("loader", "unet", "checkpoint"), "加载": ("load", "loader"),
    "提示词": ("textencode", "prompt"), "保存": ("save",), "视频": ("video",),
    "音频": ("audio",), "图像": ("image",), "放大": ("upscale", "scale"),
    "潜空间": ("latent",), "噪声": ("noise",), "调度": ("scheduler",),
    "遮罩": ("mask",), "换脸": ("faceswap", "reactor"), "修脸": ("facedetailer",),
}


@dataclass
class Hit:
    """一条检索结果。为什么用 dataclass 而不是 dict：cli 里到处要用，写起来顺手。"""
    type: str
    source: str                 # alias | note | pin | catalog
    display: str = ""
    category: str = ""
    package: str = ""
    usage: int = 0
    outputs: List[str] = field(default_factory=list)
    inputs: List[str] = field(default_factory=list)
    widgets: List[str] = field(default_factory=list)
    alias: str = ""
    score: int = 0
    tax: str = ""               # 功能分类（人读，如「图像处理/放大」）
    tax_key: str = ""           # 功能分类（机读，如 image/放大）

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type, "alias": self.alias, "source": self.source,
            "display": self.display, "category": self.category,
            "tax": self.tax, "tax_key": self.tax_key,
            "package": self.package, "usage": self.usage,
            "inputs": self.inputs, "outputs": self.outputs, "widgets": self.widgets,
        }


def _catalog_lookup(cat: Optional[Dict[str, Any]], type_: str) -> Optional[Dict[str, Any]]:
    if not cat:
        return None
    return (cat.get("types") or {}).get(type_)


def _spec_lookup(reg, type_: str):
    if reg is None:
        return None
    try:
        return reg.get(type_)
    except Exception:
        return None


def _from_spec(type_: str, reg, cat, source: str, alias: str = "") -> Hit:
    v = _catalog_lookup(cat, type_) or {}
    spec = _spec_lookup(reg, type_)
    h = Hit(type=type_, source=source, alias=alias)
    h.display = v.get("display") or (spec.display_name if spec else "") or ""
    h.category = v.get("category_zh") or v.get("category") or ""
    h.tax = v.get("tax_path_zh") or ""
    h.tax_key = v.get("tax_path") or ""
    h.package = v.get("package") or ""
    h.usage = int(v.get("usage") or 0)
    if spec is not None:
        h.inputs = [f"{i.name}:{i.type}" for i in spec.link_inputs if not i.optional]
        h.outputs = [f"{o.name}:{o.type}" for o in spec.outputs]
        h.widgets = [i.name for i in spec.widget_inputs]
    else:
        h.inputs = list(v.get("link_inputs") or [])
        h.outputs = list(v.get("outputs") or [])
        h.widgets = list(v.get("widgets") or [])
    return h


def search(query: str, reg=None, cat: Optional[Dict[str, Any]] = None,
           limit: int = 20, root: Optional[str] = None) -> List[Hit]:
    """检索节点。**先查知识库（别名/笔记/收藏），再回落 7746 种节点字典。**

    这个顺序很关键：你沉淀过的会排在最前面，下次不用在一堆同名节点里挑。
    """
    q = (query or "").strip()
    out: List[Hit] = []
    seen: set = set()

    def add(h: Hit) -> None:
        if h.type in seen:
            return
        seen.add(h.type)
        out.append(h)

    aliases = load_aliases(root)
    notes = {n["type"] for n in list_notes(root)}
    pins = load_pins(root)

    if not q:
        # 没给关键词：把沉淀过的东西全列出来
        for a, t in aliases.items():
            add(_from_spec(t, reg, cat, "note" if t in notes else "alias", a))
        for t in pins:
            if t not in seen:
                add(_from_spec(t, reg, cat, "pin"))
        for t in notes:
            if t not in seen:
                add(_from_spec(t, reg, cat, "note"))
        for h in out:
            h.score = 100
        return out[:limit]

    low = q.lower()

    # 1) 别名精确命中
    for a, t in aliases.items():
        if a == q or a.lower() == low:
            add(_from_spec(t, reg, cat, "alias", a))

    # 2) 节点类型精确命中（把它的别名也带上，方便回看）
    if reg is not None and t_in_reg(reg, q):
        alias_of = next((a for a, t in aliases.items() if t == q), "")
        h = _from_spec(q, reg, cat, "exact", alias_of)
        h.score = 300
        add(h)

    # 3) 收藏与笔记里匹配
    for t in pins + sorted(notes):
        if t in seen:
            continue
        if low in t.lower():
            add(_from_spec(t, reg, cat, "pin" if t in pins else "note"))

    # 4) 别名模糊命中
    for a, t in aliases.items():
        if t in seen:
            continue
        if low in a.lower():
            add(_from_spec(t, reg, cat, "alias", a))

    # 5) 中文关键词 → 英文片段
    frags: List[str] = []
    for zh, fs in BUILTIN_ZH.items():
        if zh in q:
            frags.extend(fs)
    frags.append(low)

    # 6) 节点字典全文搜
    if cat and cat.get("types"):
        for t, v in cat["types"].items():
            if t in seen:
                continue
            blob = " ".join([t, v.get("display", ""), v.get("category", ""),
                             v.get("category_zh", ""), v.get("package", ""),
                             " ".join(v.get("widgets", [])), v.get("desc", "")]).lower()
            tl = t.lower()
            score = 0
            if tl == low:
                score = 300
            elif tl.startswith(low):
                score = 200
            elif low in tl:
                # `解码` 的中文片段是 decode；节点名里含 decode 的按「越短越可能是主角」加权：
                # VAEDecode(9) 应当排在 MiniMaxH3AVDecodeSafetyT8Advanced(31) 前面
                score = 160 - min(60, len(t))
            elif any(f in tl for f in frags if len(f) >= 3):
                score = 100 - min(40, len(t))
            elif low in blob:
                score = 60
            if score:
                h = _from_spec(t, reg, cat, "catalog")
                h.score = score + min(20, h.usage)
                add(h)
    elif reg is not None:
        for spec in reg.search(q, limit=limit):
            if spec.type in seen:
                continue
            h = _from_spec(spec.type, reg, cat, "catalog")
            h.score = 100
            add(h)

    order = {"alias": 4, "exact": 5, "note": 3, "pin": 2, "catalog": 1}
    out.sort(key=lambda h: (-order.get(h.source, 0), -h.score, -h.usage, h.type))
    return out[:limit]


def t_in_reg(reg, t: str) -> bool:
    try:
        return t in reg
    except Exception:
        return False


# ---------------------------------------------------------------- 记录/标记


def mark(type_or_alias: str, alias: Optional[str] = None, note: Optional[str] = None,
         pin: bool = False, reg=None, cat: Optional[Dict[str, Any]] = None,
         root: Optional[str] = None, cat_target: Optional[str] = None) -> Dict[str, Any]:
    """把一个节点记进知识库：起别名 + 写笔记 + 收藏。

    一次调用把「值得记住的东西」全记下来，比让 AI 分三次调用省事。
    """
    d = ensure_store(root)
    aliases = load_aliases(d)

    # 解析出真实类型
    type_ = type_or_alias
    if reg is not None and t_in_reg(reg, type_):
        pass                                       # 已经是真实类型
    elif type_ in aliases:
        type_ = aliases[type_]
    else:
        from .terms import resolve_type, suggest_for
        real = resolve_type(type_, reg)
        if real and t_in_reg(reg, real):
            type_ = real
        elif cat and type_ in (cat.get("types") or {}):
            pass
        else:
            # 谁也不认识它。这里**不能**默默接受 —— 记错了类型名，
            # 知识库里就多一条永远查不到、也删不掉的垃圾。
            have = sorted((cat.get("types") or {}).keys()) if cat else \
                (sorted(reg.keys()) if reg is not None else [])
            near = suggest_for(type_or_alias, have) if have else []
            raise CwfError(
                f"节点库里没有 {type_or_alias!r}，没法记。"
                + (f"相近的有：{'、'.join(near)}" if near else
                   "用 `cwf store find <关键词>` 搜搜看"))

    result: Dict[str, Any] = {"type": type_, "alias": None, "note": None,
                              "pinned": None, "store": d, "cat": None,
                              "cat_file": None}
    if alias:
        save_alias(alias, type_, d)
        result["alias"] = alias
    if pin:
        pinned, _ = toggle_pin(type_, d)
        result["pinned"] = pinned
    if note:
        save_note(type_, note, d)
        result["note"] = note_path(type_, d)
    if cat_target:
        # 分类和别名/笔记存在同一层：都是"你对这个节点的沉淀"，不该分两处。
        from .taxonomy import norm_target, save_override
        norm_target(cat_target)                    # 写之前先验，写错就报错
        result["cat_file"] = save_override(type_, cat_target, d)
        result["cat"] = cat_target
    return result


# ---------------------------------------------------------------- 导出 / 总览


def export_markdown(reg=None, cat: Optional[Dict[str, Any]] = None,
                    root: Optional[str] = None) -> str:
    """导出一份完整的总览 markdown（换机器直接拷，人也能读）。"""
    d = ensure_store(root)
    aliases = load_aliases(d)
    pins = load_pins(d)
    notes = list_notes(d)
    lines: List[str] = []
    lines.append("# cwf 节点知识库")
    lines.append("")
    lines.append(f"> 自动生成于 {time.strftime('%Y-%m-%d %H:%M')}　·　"
                 f"别名 {len(aliases)} 条 · 收藏 {len(pins)} 个 · 笔记 {len(notes)} 个")
    lines.append("")

    lines.append("## 别名")
    lines.append("")
    lines.append("| 你说 | 实际类型 |")
    lines.append("|---|---|")
    for a in sorted(aliases):
        lines.append(f"| `{a}` | `{aliases[a]}` |")
    lines.append("")

    lines.append("## 常用（收藏）")
    lines.append("")
    for t in pins:
        h = _from_spec(t, reg, cat, "pin")
        lines.append(f"- **{t}**" + (f" — {h.display}" if h.display else ""))
        if h.outputs:
            lines.append(f"  - 出：{', '.join(h.outputs)}")
        if h.inputs:
            lines.append(f"  - 入：{', '.join(h.inputs)}")
    lines.append("")

    lines.append("## 笔记")
    lines.append("")
    for n in notes:
        lines.append(f"### `{n['type']}`")
        lines.append("")
        body = load_note(n["type"], d) or ""
        body = re.sub(r"^#\s*\S+\n", "", body)          # 去掉重复的标题
        body = re.sub(r"^>\s*由.*\n", "", body, flags=re.M)
        lines.append(body.strip())
        lines.append("")
    return "\n".join(lines)


def write_readme(reg=None, cat=None, root: Optional[str] = None) -> str:
    d = ensure_store(root)
    p = os.path.join(d, "README.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(export_markdown(reg, cat, d))
    return p


def stats(root: Optional[str] = None) -> Dict[str, Any]:
    d = ensure_store(root)
    aliases = load_aliases(d)
    pins = load_pins(d)
    notes = list_notes(d)
    return {
        "dir": d,
        "aliases": len(aliases),
        "pins": len(pins),
        "notes": len(notes),
        "alias_list": aliases,
        "pin_list": pins,
        "note_list": [n["type"] for n in notes],
    }


# ---------------------------------------------------------------- 速查卡


def cheat_sheet(types: List[str], reg=None, cat=None) -> str:
    """把若干个节点压成一张「接线速查卡」——端口、控件全在一屏里。

    这是给 AI 看的：比翻 references 快，比读 JSON 省得多。
    """
    lines: List[str] = []
    for t in types:
        h = _from_spec(t, reg, cat, "pin")
        if not h.display and not h.inputs and not h.outputs:
            lines.append(f"{t}　（节点库里没这个类型）")
            continue
        lines.append(f"{t}" + (f"  「{h.display}」" if h.display else "")
                     + (f"   [{h.tax}]" if h.tax else ""))
        if h.inputs:
            lines.append(f"    必接输入: {'、'.join(h.inputs)}")
        if h.outputs:
            lines.append(f"    输出:     {'、'.join(h.outputs)}")
        if h.widgets:
            lines.append(f"    控件:     {'、'.join(h.widgets)}")
        if h.package:
            lines.append(f"    来源包:   {h.package}"
                         + (f"　你用过的次数 {h.usage}" if h.usage else ""))
    return "\n".join(lines)
