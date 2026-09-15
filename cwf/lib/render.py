# -*- coding: utf-8 -*-
"""
cwf.render —— 离线把工作流画成图

不开浏览器、不开 ComfyUI 前端、不联网，直接把工作流 JSON 渲染成
**SVG**（纯标准库）或 **PNG**（本机有 Pillow 时）。

## 为什么是 SVG 优先

cwf 承诺零第三方依赖 —— 换台 ComfyUI 机器拷过去就能跑。所以默认产物是 SVG：
纯文本、任何浏览器能开、无限缩放、还方便 diff。PNG 走 Pillow（ComfyUI 自己的
venv 必然有），属于"有则更好"，不是必需。

两种产物**共用同一套几何与布局代码**，只在最后的绘制原语上分叉
（`SvgCanvas` / `PilCanvas`），所以形状、位置、颜色完全一致，不会出现
"导出的图跟预览不一样"。

## 画得像不像 ComfyUI 前端

尺寸常量直接对齐 ComfyUI 用的那套（`TITLE_H=30`、`SLOT_H=20`、圆角 8），
标题栏、端口圆点、控件条、连线贝塞尔、分区框、便签都按前端的画法来。
但**有意做了两处偏离**，都是为了让静态图更可读：

1. **标题栏按功能分类上色**（那 16 个大类的配色），而不是清一色深灰 ——
   一眼就能看出「哪段在加载模型、哪段在采样、哪段在出图」。
   想要跟前端一样的素色用 `--color comfy`；节点自己带 `color`/`bgcolor`
   时永远以工作流里的为准。
2. **控件条显示 `名字: 值`**，而不是只显示值。静态图没有悬停提示，
   只写个 `28` 谁都看不出那是步数。要纯前端观感用 `--no-widget-names`。

## 不是什么东西

* 不是把 ComfyUI 前端截图 —— 没有下拉框、没有小地图、没有节点选中态；
* 不执行任何 JS，不做节点内部的动态控件（比如动态生成的端口）；
* 渲染的是**工作流里已经存下来的样子**，不替你排版。图乱就是原图乱，
  先 `cwf beautify` 再 `cwf render`。
"""
from __future__ import annotations

import html
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .graph import CwfError, Graph, Node

# ---------------------------------------------------------------- 常量

#: 与 ComfyUI / LiteGraph 对齐的那几个数（改这几个值就能整体调版式）
TITLE_H = 30.0
SLOT_H = 20.0
RADIUS = 8.0
SLOT_R = 5.0

#: 16 个功能大类 → 标题栏配色。刻意拉开色相，让图上能"看出段落"。
PALETTE: Dict[str, str] = {
    "load":   "#3E6E9E",
    "model":  "#7A5CA8",
    "cond":   "#B4733A",
    "sample": "#A8433C",
    "latent": "#9C4E7E",
    "image":  "#2F8574",
    "mask":   "#4E8F3E",
    "video":  "#2A7291",
    "audio":  "#8E7A34",
    "threed": "#5F5F92",
    "text":   "#6E6E6E",
    "num":    "#48626E",
    "output": "#AD4C44",
    "flow":   "#5A5A5A",
    "api":    "#7E4E9C",
    "misc":   "#4A4A4A",
}

#: 连线颜色。前 8 个是 ComfyUI 前端自带的类型色，后面是给自定义类型补的。
LINK_COLORS: Dict[str, str] = {
    "MODEL": "#B39DDB",
    "CLIP": "#FFD500",
    "VAE": "#FF6E6E",
    "CONDITIONING": "#FFA931",
    "LATENT": "#FF9CF9",
    "IMAGE": "#64B5F6",
    "MASK": "#81C784",
    "CONTROL_NET": "#6EE7B7",
    "CLIP_VISION": "#F0C36D",
    "STYLE_MODEL": "#C0E36D",
    "AUDIO": "#E8A33D",
    "VIDEO": "#5AC8E8",
    "SIGMAS": "#C9A0DC",
    "SAMPLER": "#FF8A65",
    "GUIDER": "#FFB74D",
    "NOISE": "#B0BEC5",
    "STRING": "#9CCC65",
    "INT": "#90A4AE",
    "FLOAT": "#90A4AE",
    "BOOLEAN": "#90A4AE",
}
DEFAULT_LINK = "#9A9A9A"

#: 纯前端噪音的控件名（按钮、隐藏标记），画出来只会占地方。
#: 刻意比 schema.UI_ONLY_WIDGETS 窄：那份是给"估节点高度"用的，
#: 连 `image`/`video` 这类**装着真实文件名**的控件都算进去了，渲染时不能照抄。
RENDER_NOISE = {
    "videopreview", "upload", "choose image to upload", "choose video to upload",
    "choose file to upload", "choose audio to upload", "hidden", "paused", "params",
}

#: 自定义类型（本机工作流里一大堆 T8_*、COMFYTV_* 之类）的备用色，
#: 按类型名哈希取，保证同一类型每次渲染颜色一致。
EXTRA_LINK_COLORS = ["#E57373", "#BA68C8", "#4DD0E1", "#AED581", "#FFB74D",
                     "#F06292", "#7986CB", "#A1887F", "#4DB6AC", "#DCE775"]

THEMES = {
    "dark": {
        "bg": "#1E1E1E", "grid": "#2A2A2A", "node": "#353535", "node_edge": "#191919",
        "title_text": "#FFFFFF", "body_text": "#DDDDDD", "dim_text": "#9A9A9A",
        "widget": "#424242", "widget_edge": "#2E2E2E", "legend_bg": "#242424",
        "legend_edge": "#3A3A3A",
    },
    "light": {
        "bg": "#F2F2F2", "grid": "#E2E2E2", "node": "#FFFFFF", "node_edge": "#BBBBBB",
        "title_text": "#FFFFFF", "body_text": "#333333", "dim_text": "#777777",
        "widget": "#EDEDED", "widget_edge": "#D5D5D5", "legend_bg": "#FFFFFF",
        "legend_edge": "#CCCCCC",
    },
}


@dataclass
class Opts:
    scale: float = 1.0
    max_px: int = 4200
    pad: float = 48.0
    theme: str = "dark"
    color: str = "function"          # function | comfy | none
    legend: bool = True
    grid: bool = True
    widgets: bool = True
    widget_names: bool = True
    notes: bool = True
    title: Optional[str] = None
    font: float = 1.0


# ---------------------------------------------------------------- 画布抽象


def _rgb(color: str) -> Tuple[int, int, int]:
    c = (color or "#888888").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except ValueError:
        return (136, 136, 136)


_FONT_STACK = ("Microsoft YaHei, Segoe UI, PingFang SC, Noto Sans CJK SC, "
               "Hiragino Sans GB, sans-serif")


class Canvas:
    """画布接口。两个实现共用同一套调用，保证 SVG 和 PNG 长得一样。"""

    def rect(self, x, y, w, h, fill=None, stroke=None, sw=1.0, alpha=1.0): ...
    def rrect(self, x, y, w, h, r, fill=None, stroke=None, sw=1.0, alpha=1.0,
              corners=None): ...
    def line(self, x1, y1, x2, y2, stroke, sw=1.0, alpha=1.0, dash=None): ...
    def bezier(self, p0, c0, c1, p1, stroke, sw=2.0, alpha=1.0): ...
    def circle(self, cx, cy, r, fill=None, stroke=None, sw=1.0, alpha=1.0): ...
    def text(self, x, y, s, size, fill, anchor="start", alpha=1.0, bold=False): ...


class SvgCanvas(Canvas):
    """纯标准库：所有图元写成 SVG 文本。"""

    def __init__(self, w: float, h: float, scale: float = 1.0,
                 font_scale: float = 1.0):
        self.w, self.h, self.scale = w, h, scale
        self.font_scale = font_scale
        self.parts: List[str] = []

    def _s(self, v: float) -> float:
        return v * self.scale

    def _a(self, alpha: float, kind: str = "fill") -> str:
        return "" if alpha >= 0.999 else f' {kind}-opacity="{alpha:.3f}"'

    def rect(self, x, y, w, h, fill=None, stroke=None, sw=1.0, alpha=1.0):
        s = (f'<rect x="{self._s(x):.2f}" y="{self._s(y):.2f}" '
             f'width="{self._s(w):.2f}" height="{self._s(h):.2f}"')
        s += (f' fill="{fill}"{self._a(alpha)}' if fill else " fill=\"none\"")
        if stroke:
            s += f' stroke="{stroke}" stroke-width="{self._s(sw):.2f}"'
        self.parts.append(s + "/>")

    def rrect(self, x, y, w, h, r, fill=None, stroke=None, sw=1.0, alpha=1.0,
              corners=None):
        r = max(0.0, min(r, w / 2.0, h / 2.0))
        if corners is None:
            corners = (True, True, True, True)
        tl, tr, br, bl = [r if c else 0.0 for c in corners]
        X, Y = self._s(x), self._s(y)
        W, H = self._s(w), self._s(h)
        T, R_, B, L = self._s(tl), self._s(tr), self._s(br), self._s(bl)
        d = (f"M {X + L:.2f} {Y:.2f} H {X + W - R_:.2f} "
             f"A {R_:.2f} {R_:.2f} 0 0 1 {X + W:.2f} {Y + R_:.2f} "
             f"V {Y + H - B:.2f} A {B:.2f} {B:.2f} 0 0 1 {X + W - B:.2f} {Y + H:.2f} "
             f"H {X + L:.2f} A {L:.2f} {L:.2f} 0 0 1 {X:.2f} {Y + H - L:.2f} "
             f"V {Y + T:.2f} A {T:.2f} {T:.2f} 0 0 1 {X + L:.2f} {Y:.2f} Z")
        s = f'<path d="{d}"'
        s += (f' fill="{fill}"{self._a(alpha)}' if fill else ' fill="none"')
        if stroke:
            s += f' stroke="{stroke}" stroke-width="{self._s(sw):.2f}"'
        self.parts.append(s + "/>")

    def line(self, x1, y1, x2, y2, stroke, sw=1.0, alpha=1.0, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{self._s(x1):.2f}" y1="{self._s(y1):.2f}" '
            f'x2="{self._s(x2):.2f}" y2="{self._s(y2):.2f}" '
            f'stroke="{stroke}" stroke-width="{self._s(sw):.2f}"'
            f'{self._a(alpha, "stroke")}{d}/>')

    def bezier(self, p0, c0, c1, p1, stroke, sw=2.0, alpha=1.0):
        d = (f"M {self._s(p0[0]):.2f} {self._s(p0[1]):.2f} C "
             f"{self._s(c0[0]):.2f} {self._s(c0[1]):.2f}, "
             f"{self._s(c1[0]):.2f} {self._s(c1[1]):.2f}, "
             f"{self._s(p1[0]):.2f} {self._s(p1[1]):.2f}")
        self.parts.append(f'<path d="{d}" fill="none" stroke="{stroke}" '
                          f'stroke-width="{self._s(sw):.2f}"'
                          f'{self._a(alpha, "stroke")} stroke-linecap="round"/>')

    def circle(self, cx, cy, r, fill=None, stroke=None, sw=1.0, alpha=1.0):
        s = f'<circle cx="{self._s(cx):.2f}" cy="{self._s(cy):.2f}" r="{self._s(r):.2f}"'
        s += (f' fill="{fill}"{self._a(alpha)}' if fill else ' fill="none"')
        if stroke:
            s += f' stroke="{stroke}" stroke-width="{self._s(sw):.2f}"'
        self.parts.append(s + "/>")

    def text(self, x, y, s, size, fill, anchor="start", alpha=1.0, bold=False):
        if not s:
            return
        weight = ' font-weight="600"' if bold else ""
        self.parts.append(
            f'<text x="{self._s(x):.2f}" y="{self._s(y):.2f}" '
            f'font-family="{_FONT_STACK}" font-size="{self._s(size):.2f}"'
            f' fill="{fill}"{self._a(alpha)} text-anchor="{anchor}"'
            f'{weight}>{html.escape(str(s))}</text>')

    def to_svg(self, w: Optional[float] = None, h: Optional[float] = None) -> str:
        W = self._s(w if w is not None else self.w)
        H = self._s(h if h is not None else self.h)
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" '
            f'height="{H:.0f}" viewBox="0 0 {W:.2f} {H:.2f}">\n'
            + "\n".join(self.parts) + "\n</svg>\n")


class PilCanvas(Canvas):
    """有 Pillow 时走这条路出 PNG。图元与 SVG 一一对应。"""

    def __init__(self, w: float, h: float, scale: float = 1.0,
                 font_scale: float = 1.0, bg: str = "#1E1E1E"):
        from PIL import Image, ImageDraw                     # 延迟导入
        self.scale, self.font_scale = scale, font_scale
        self.w = max(1, int(round(w * scale)))
        self.h = max(1, int(round(h * scale)))
        self.img = Image.new("RGB", (self.w, self.h), _rgb(bg))
        self.d = ImageDraw.Draw(self.img, "RGBA")
        self._fonts: Dict[Tuple[int, bool], Any] = {}

    def _s(self, v: float) -> float:
        return v * self.scale

    def font(self, size: float, bold: bool):
        from PIL import ImageFont
        key = (max(6, int(round(size * self.scale * self.font_scale))), bool(bold))
        if key in self._fonts:
            return self._fonts[key]
        f = None
        for name in (("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "arialbd.ttf")
                     if bold else ("msyh.ttc", "simhei.ttf", "segoeui.ttf", "arial.ttf")):
            p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, key[0])
                    break
                except Exception:
                    f = None
        if f is None:
            f = ImageFont.load_default()
        self._fonts[key] = f
        return f

    def _fill(self, color, alpha):
        r, g, b = _rgb(color)
        return (r, g, b, int(max(0.0, min(1.0, alpha)) * 255))

    def rect(self, x, y, w, h, fill=None, stroke=None, sw=1.0, alpha=1.0):
        box = [self._s(x), self._s(y), self._s(x + w), self._s(y + h)]
        self.d.rectangle(box, fill=self._fill(fill, alpha) if fill else None,
                         outline=self._fill(stroke, 1.0) if stroke else None,
                         width=max(1, int(round(sw * self.scale))))

    def rrect(self, x, y, w, h, r, fill=None, stroke=None, sw=1.0, alpha=1.0,
              corners=None):
        box = [self._s(x), self._s(y), self._s(x + w), self._s(y + h)]
        rad = self._s(r)
        kw: Dict[str, Any] = {}
        if corners is not None and not all(corners):
            tl, tr, br, bl = corners
            kw = {"corners": (tl, tr, br, bl)}
        self.d.rounded_rectangle(
            box, radius=rad if not kw else rad,
            fill=self._fill(fill, alpha) if fill else None,
            outline=self._fill(stroke, 1.0) if stroke else None,
            width=max(1, int(round(sw * self.scale))), **kw)

    def line(self, x1, y1, x2, y2, stroke, sw=1.0, alpha=1.0, dash=None):
        self.d.line([self._s(x1), self._s(y1), self._s(x2), self._s(y2)],
                    fill=self._fill(stroke, alpha),
                    width=max(1, int(round(sw * self.scale))))

    def bezier(self, p0, c0, c1, p1, stroke, sw=2.0, alpha=1.0):
        # 三段折线逼近足够顺滑，而且比自写贝塞尔光栅化省事、不会出锯齿
        pts = []
        steps = 26
        for i in range(steps + 1):
            t = i / steps
            u = 1 - t
            x = (u ** 3) * p0[0] + 3 * u * u * t * c0[0] + 3 * u * t * t * c1[0] + (t ** 3) * p1[0]
            y = (u ** 3) * p0[1] + 3 * u * u * t * c0[1] + 3 * u * t * t * c1[1] + (t ** 3) * p1[1]
            pts.append((self._s(x), self._s(y)))
        self.d.line(pts, fill=self._fill(stroke, alpha),
                    width=max(1, int(round(sw * self.scale))), joint="curve")

    def circle(self, cx, cy, r, fill=None, stroke=None, sw=1.0, alpha=1.0):
        box = [self._s(cx - r), self._s(cy - r), self._s(cx + r), self._s(cy + r)]
        self.d.ellipse(box, fill=self._fill(fill, alpha) if fill else None,
                       outline=self._fill(stroke, 1.0) if stroke else None,
                       width=max(1, int(round(sw * self.scale))))

    def text(self, x, y, s, size, fill, anchor="start", alpha=1.0, bold=False):
        if not s:
            return
        f = self.font(size, bold)
        w = self.d.textlength(str(s), font=f)
        X = self._s(x)
        if anchor == "middle":
            X -= w / 2.0
        elif anchor == "end":
            X -= w
        self.d.text((X, self._s(y)), str(s), font=f,
                    fill=self._fill(fill, alpha), anchor="lm")

    def save(self, path: str) -> Tuple[int, int]:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.img.save(path)
        return self.w, self.h


# ---------------------------------------------------------------- 文本度量

#: 没有字体引擎也要能排版（SVG 端拿不到真实字宽），所以自己估。
#: 中日韩按 1 em、拉丁按 0.55 em —— 对本图鉴这种短标签足够准。
_WIDE = re.compile(r"[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF"
                   r"\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6\u3000-\u303F]")


def _luma(color: str) -> float:
    """相对亮度（0=黑，1=白）。用来决定"这块底色上该写深字还是浅字"。"""
    r, g, b = _rgb(color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def text_width(s: str, size: float) -> float:
    w = 0.0
    for ch in str(s):
        w += 1.0 if _WIDE.match(ch) else 0.55
    return w * size


def ellipsize(s: str, max_w: float, size: float, keep: str = "head") -> str:
    """截断到宽度内。

    keep='head' 保留**开头**（节点标题、模型名 —— 区分度高的部分在前面）；
    keep='tail' 保留**结尾**（长路径的文件名）。超宽时补一个省略号。
    """
    s = str(s)
    if max_w <= 0:
        return ""
    if text_width(s, size) <= max_w:
        return s
    ell = "…"
    ew = text_width(ell, size)
    if keep == "tail":                       # 长路径：尾巴（文件名）更有信息量
        out = ""
        for ch in reversed(s):
            if text_width(ch, size) + text_width(out, size) + ew > max_w:
                break
            out = ch + out
        return ell + out
    out = ""
    for ch in s:
        if text_width(out + ch, size) + ew > max_w:
            break
        out += ch
    return out + ell


def wrap_text(s: str, max_w: float, size: float, max_lines: int = 12) -> List[str]:
    """按宽度硬换行（中英混排都按字符宽度估）。"""
    lines: List[str] = []
    for para in str(s).splitlines() or [""]:
        cur = ""
        for ch in para:
            if text_width(cur + ch, size) > max_w:
                lines.append(cur)
                cur = ch
                if len(lines) >= max_lines:
                    return lines
            else:
                cur += ch
        lines.append(cur)
        if len(lines) >= max_lines:
            break
    return lines[:max_lines]


def fmt_value(v: Any, limit: int = 64) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(fmt_value(x, 12) for x in v[:4]) + \
               ("…]" if len(v) > 4 else "]")
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}" for k in list(v)[:3]) + \
               ("…}" if len(v) > 3 else "}")
    t = str(v)
    if "\n" in t:
        t = t.split("\n", 1)[0] + " ⏎"
    return t[:limit] + ("…" if len(t) > limit else "")


# ---------------------------------------------------------------- 场景


@dataclass
class NodeBox:
    node: Node
    x: float
    y: float
    w: float
    h: float
    title: str
    subtitle: str = ""
    tax: str = "misc"
    title_color: str = "#555555"
    in_slots: List[Tuple[int, str, str, bool]] = field(default_factory=list)
    out_slots: List[Tuple[int, str, str]] = field(default_factory=list)
    widget_rows: List[Tuple[str, str, bool]] = field(default_factory=list)
    row_h: float = SLOT_H

    def in_pos(self, slot: int) -> Tuple[float, float]:
        """输入端口圆点坐标（节点左边缘）。序号越界时退回边缘中点，
        而不是抛异常 —— 坏掉的老工作流也要能画出来看。"""
        for i, (idx, _n, _t, _w) in enumerate(self.in_slots):
            if idx == slot:
                return (self.x, self.y + TITLE_H + (i + 0.5) * self.row_h)
        return (self.x, self.y + TITLE_H + self.row_h * 0.5)

    def out_pos(self, slot: int) -> Tuple[float, float]:
        for i, (idx, _n, _t) in enumerate(self.out_slots):
            if idx == slot:
                return (self.x + self.w, self.y + TITLE_H + (i + 0.5) * self.row_h)
        return (self.x + self.w, self.y + TITLE_H + self.row_h * 0.5)


@dataclass
class Scene:
    boxes: Dict[int, NodeBox] = field(default_factory=dict)
    links: List[Tuple[Tuple[float, float], Tuple[float, float],
                       Tuple[float, float], Tuple[float, float], str]] = \
        field(default_factory=list)
    groups: List[Any] = field(default_factory=list)
    notes: List[NodeBox] = field(default_factory=list)
    legend: List[Tuple[str, str, int]] = field(default_factory=list)
    w: float = 100.0
    h: float = 100.0
    off_x: float = 0.0
    off_y: float = 0.0
    stats: Dict[str, Any] = field(default_factory=dict)


def _title_of(n: Node, reg=None) -> Tuple[str, str]:
    """(标题栏文字, 副标题)。副标题只在标题被改过时才出现 —— 跟前端一个意思。"""
    if n.title and n.title != n.type:
        return n.title, n.type
    return n.type, ""


def _tax_of(n: Node, cat: Optional[Dict[str, Any]], tax_mod) -> str:
    if tax_mod is None:
        return "misc"
    e = ((cat or {}).get("types") or {}).get(n.type)
    if e and e.get("tax"):
        return e["tax"]
    try:
        return tax_mod.classify(n.type, e or {}).top
    except Exception:
        return "misc"


def build_scene(g: Graph, cat: Optional[Dict[str, Any]] = None,
                opts: Optional[Opts] = None) -> Scene:
    opts = opts or Opts()
    try:
        from . import taxonomy as tax_mod
    except Exception:
        tax_mod = None

    th = THEMES.get(opts.theme) or THEMES["dark"]
    sc = Scene()
    sc.groups = list(g.groups)
    counts: Dict[str, int] = {}

    for n in g.nodes:
        if _is_note(n) and opts.notes:
            continue
        w = float(n.size[0]) or 210.0
        h = float(n.size[1]) or 60.0
        tax = _tax_of(n, cat, tax_mod) if opts.color == "function" else "misc"
        counts[tax] = counts.get(tax, 0) + 1

        # 标题栏配色：工作流里写死的 color 永远优先
        if n.color:
            tcol = n.color
        elif opts.color == "comfy" or opts.color == "none":
            tcol = "#353535"
        else:
            tcol = PALETTE.get(tax, PALETTE["misc"])

        title, sub = _title_of(n)

        ins = [(i, s) for i, s in enumerate(n.inputs) if not s.is_widget]
        wslots = [(i, s) for i, s in enumerate(n.inputs) if s.is_widget]
        outs = [(i, s) for i, s in enumerate(n.outputs)]

        # 节点在节点库里时，我们能知道它"真正"有几个控件。多出来的值是前端
        # 自己塞的记账数据（LoadImage 就多塞了 `image` 和空串两个），
        # 名字解析不出来（退化成 w1/w2），画出来就是一行莫名的 "image"。
        # 能证明是垃圾的才丢；节点不在库里（插件没装）时全部保留 —— 那时
        # 那些值就是唯一的信息。
        known_widgets: Optional[List[str]] = None
        try:
            from .graph import _active_registry
            _r = _active_registry()
            _spec = _r.get(n.type) if (_r is not None and len(_r)) else None
            if _spec is not None:
                known_widgets = [i.name for i in _spec.widget_inputs]
        except Exception:
            known_widgets = None

        wrows: List[Tuple[str, str, bool]] = []
        if opts.widgets:
            try:
                for k, v in n.widget_pairs():
                    key = str(k)
                    # 前端自用的按钮/标记，画出来是噪音。
                    # 注意**不能**直接用 schema.UI_ONLY_WIDGETS —— 那一份是为
                    # 「估算节点高度」准备的，连 `image` 也算噪音；可是在
                    # LoadImage 里 `image` 装的正是选中的文件名，是真信息。
                    if key.lower() in RENDER_NOISE:
                        continue
                    txt = fmt_value(v)
                    if re.fullmatch(r"w\d+", key):
                        # 名字是占位符：空值必丢；已知真实控件清单时，占位行
                        # 就是前端记账数据，也丢
                        if txt in ("", "—") or known_widgets is not None:
                            continue
                    multiline = isinstance(v, str) and "\n" in v
                    wrows.append((key, txt, multiline))
            except Exception:
                pass

        n_rows = max(len(ins) + len(wslots), len(outs), 1)
        body = max(8.0, h - TITLE_H)
        wid_rows = len(wrows) if opts.widgets else 0
        total_rows = n_rows + wid_rows
        row_h = SLOT_H
        if total_rows * SLOT_H > body:
            row_h = max(11.0, body / total_rows)

        box = NodeBox(
            node=n, x=float(n.pos[0]), y=float(n.pos[1]), w=w, h=h,
            title=title, subtitle=sub, tax=tax, title_color=tcol,
            in_slots=[(i, s.name, s.type, False) for i, s in ins]
            + [(i, s.name, s.type, True) for i, s in wslots],
            out_slots=[(i, s.name, s.type) for i, s in outs],
            widget_rows=wrows, row_h=row_h)
        sc.boxes[n.id] = box

    # ---- 连线
    for l in g.links:
        a, b = sc.boxes.get(l.origin_id), sc.boxes.get(l.target_id)
        if a is None or b is None:
            continue                              # 连到便签/已删节点的，跳过
        ox, oy = a.out_pos(l.origin_slot)
        ix, iy = b.in_pos(l.target_slot)
        dx = max(28.0, abs(ix - ox) * 0.5)
        sc.links.append(((ox, oy), (ox + dx, oy), (ix - dx, iy), (ix, iy),
                         link_color(l.type)))

    # ---- 便签：没有端口，只有一段文字，单独画
    if opts.notes:
        for n in g.nodes:
            if not _is_note(n):
                continue
            txt = ""
            try:
                pairs = list(n.widget_pairs())
                if pairs:
                    txt = str(pairs[0][1] or "")
            except Exception:
                pass
            sc.notes.append(NodeBox(
                node=n, x=float(n.pos[0]), y=float(n.pos[1]),
                w=float(n.size[0]) or 240.0, h=float(n.size[1]) or 120.0,
                title=n.title or "便签", tax="flow",
                title_color=n.color or "#B58B00"))
    _note_ids = {b.node.id for b in sc.notes}

    # ---- 画布外框
    #
    # 注意这里的兜底写法：**不能**用 `max([...] + [0.0])` 这种"顺手塞个 0"的写法。
    # 真实工作流的节点经常整体落在负坐标区（画布往左下拖过），那时 0.0 反而
    # 成了最大边界，画布被凭空撑大一倍多 —— 实测一张 3800×2657 的图被撑成
    # 9774×4510，右边和下边全是大片空白。
    # 兜底只在**真的什么都没有**时才生效。
    ns = list(sc.boxes.values()) + list(sc.notes)
    xs0 = [b.x for b in ns] + [gp.bounding[0] for gp in sc.groups]
    ys0 = [b.y for b in ns] + [gp.bounding[1] for gp in sc.groups]
    xs1 = [b.x + b.w for b in ns] + [gp.bounding[0] + gp.bounding[2]
                                     for gp in sc.groups]
    ys1 = [b.y + b.h for b in ns] + [gp.bounding[1] + gp.bounding[3]
                                     for gp in sc.groups]
    x0 = min(xs0) if xs0 else 0.0
    y0 = min(ys0) if ys0 else 0.0
    x1 = max(xs1) if xs1 else 0.0
    y1 = max(ys1) if ys1 else 0.0

    # 图例先算出来 —— 底边要给它留一条**专属带**。
    # 踩过的坑：图例以前是画在画布左下角的浮层，节点正好落在那里就被压住了。
    # 让人少看一眼图不要紧，把节点挡掉就不行，所以画布直接长高一条。
    l1 = _l1()
    sc.legend = [(k, l1.get(k, k), v)
                 for k, v in sorted(counts.items(), key=lambda kv: -kv[1]) if v]
    band = legend_band(len(sc.legend), opts)

    sc.off_x = opts.pad - x0
    sc.off_y = opts.pad - y0
    sc.w = (x1 - x0) + opts.pad * 2
    sc.h = (y1 - y0) + opts.pad * 2 + band
    # 空工作流也要给个能看的画布，不能是 0 像素
    sc.w = max(sc.w, 320.0)
    sc.h = max(sc.h, 200.0)

    sc.stats = {"nodes": len(sc.boxes), "links": len(sc.links),
                "groups": len(sc.groups), "classes": len(sc.legend)}
    return sc


#: 图例的行高 / 表头高，画和量必须用同一套数
LEGEND_ROW_H = 17.0
LEGEND_HEAD_H = 26.0
LEGEND_MAX_ROWS = 17


def legend_rows(n_items: int) -> int:
    return min(n_items, LEGEND_MAX_ROWS)


def legend_height(n_items: int) -> float:
    return LEGEND_HEAD_H + legend_rows(n_items) * LEGEND_ROW_H + 12


def legend_band(n_items: int, opts: "Opts") -> float:
    """画布底部要给图例留多高。不画图例、或没有条目时返回 0。"""
    if not opts.legend or n_items <= 0:
        return 0.0
    return legend_height(n_items) + opts.pad * 0.4


def _is_note(n: Node) -> bool:
    return n.type == "Note" or "note" in (n.type or "").lower()


def link_color(t: str) -> str:
    if not t:
        return DEFAULT_LINK
    up = str(t).upper()
    if up in LINK_COLORS:
        return LINK_COLORS[up]
    # 自定义类型（T8_*、COMFYTV_* 之类）：按名字定色，保证同一个类型每次同色
    h = 0
    for ch in up:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return EXTRA_LINK_COLORS[h % len(EXTRA_LINK_COLORS)]


# ---------------------------------------------------------------- 绘制


def _l1() -> Dict[str, str]:
    """16 个大类的中文名（写在工作流的图例上）。"""
    try:
        from .taxonomy import L1
        return L1
    except Exception:
        return {}


def draw(scene: Scene, canvas: Canvas, opts: Opts, title: str = "") -> None:
    th = THEMES.get(opts.theme) or THEMES["dark"]
    ox, oy = scene.off_x, scene.off_y

    # 背景
    canvas.rect(0, 0, scene.w, scene.h, fill=th["bg"])
    if opts.grid:
        step = 40.0
        gx = ox - (scene.off_x - opts.pad) % step
        x = ox % step
        while x < scene.w:
            y = oy % step
            while y < scene.h:
                canvas.circle(x, y, 1.0, fill=th["grid"])
                y += step
            x += step

    # 分区框（在节点下面）
    for gp in scene.groups:
        gx, gy, gw, gh = gp.bounding
        col = gp.color or "#3f789e"
        canvas.rrect(gx + ox, gy + oy, gw, gh, 10, fill=col, alpha=0.10)
        canvas.rrect(gx + ox, gy + oy, gw, gh, 10, stroke=col, sw=1.4, alpha=0.55)
        canvas.rrect(gx + ox, gy + oy, gw, 26, 10, fill=col, alpha=0.28)
        canvas.text(gx + ox + 10, gy + oy + 13, ellipsize(gp.title, gw - 20, 14),
                    14, th["title_text"], alpha=0.92, bold=True)

    # 连线
    for p0, c0, c1, p1, col in scene.links:
        canvas.bezier((p0[0] + ox, p0[1] + oy), (c0[0] + ox, c0[1] + oy),
                      (c1[0] + ox, c1[1] + oy), (p1[0] + ox, p1[1] + oy),
                      col, sw=2.0, alpha=0.92)

    # 节点（便签最后画，压在最上面）
    for box in scene.boxes.values():
        if _is_note(box.node):
            continue
        _draw_node(canvas, box, opts, th, ox, oy)
    for box in scene.notes:
        _draw_note(canvas, box, opts, th, ox, oy)

    # 图例 / 标题
    if opts.legend:
        _draw_legend(canvas, scene, opts, th, title)


def _draw_node(c: Canvas, b: NodeBox, opts: Opts, th: Dict[str, str],
               ox: float, oy: float) -> None:
    n = b.node
    x, y, w, h = b.x + ox, b.y + oy, b.w, b.h
    fs = 13.0 * opts.font

    # 阴影 + 本体
    c.rrect(x + 2, y + 3, w, h, RADIUS, fill="#000000", alpha=0.28)
    c.rrect(x, y, w, h, RADIUS, fill=n.bgcolor or th["node"],
            stroke=th["node_edge"], sw=1.0)
    # 标题栏（只有上方两角是圆的）
    c.rrect(x, y, w, TITLE_H, RADIUS, fill=b.title_color, alpha=1.0,
            corners=(True, True, False, False))
    c.line(x, y + TITLE_H, x + w, y + TITLE_H, th["node_edge"], sw=1.0)

    # 标题文字
    tmax = w - 20
    tsize = 14.0 * opts.font
    c.text(x + 10, y + TITLE_H / 2, ellipsize(b.title, tmax, tsize), tsize,
           th["title_text"], bold=True)
    if b.subtitle:
        sub = ellipsize(b.subtitle, tmax, 10.5 * opts.font)
        c.text(x + w - 10, y + TITLE_H / 2, sub, 10.5 * opts.font,
               th["title_text"], anchor="end", alpha=0.75)

    # 节点状态（静音 / 旁路）
    if n.mode == 2:
        c.rrect(x, y, w, h, RADIUS, fill="#7B2FBE", alpha=0.22)
        c.text(x + w / 2, y + h - 9, "静音 MUTE", 11, "#E0C8FF", anchor="middle")
    elif n.mode == 4:
        c.rrect(x, y, w, h, RADIUS, fill="#B03A2E", alpha=0.20)
        c.text(x + w / 2, y + h - 9, "旁路 BYPASS", 11, "#FFCFC8", anchor="middle")

    # 端口
    rh = b.row_h
    sfs = 11.5 * opts.font
    for i, (_idx, name, _t, is_w) in enumerate(b.in_slots):
        cy = y + TITLE_H + (i + 0.5) * rh
        c.circle(x, cy, SLOT_R, fill="#2E2E2E", stroke="#BBBBBB", sw=1.2)
        if not is_w:
            c.text(x + 9, cy, ellipsize(name, w * 0.5 - 14, sfs), sfs,
                   th["body_text"], alpha=0.92)
    for i, (_idx, name, _t) in enumerate(b.out_slots):
        cy = y + TITLE_H + (i + 0.5) * rh
        c.circle(x + w, cy, SLOT_R, fill="#2E2E2E", stroke="#BBBBBB", sw=1.2)
        c.text(x + w - 9, cy, ellipsize(name, w * 0.5 - 14, sfs), sfs,
               th["body_text"], anchor="end", alpha=0.92)

    # 控件条
    if opts.widgets and b.widget_rows:
        top = y + TITLE_H + len(b.in_slots) * rh
        for k, (name, val, multi) in enumerate(b.widget_rows):
            cy = top + (k + 0.5) * rh
            if cy > y + h - 3:
                break
            c.rrect(x + 8, cy - rh / 2 + 2, w - 16, rh - 4, 4,
                    fill=th["widget"], stroke=th["widget_edge"], sw=1.0)
            inner = w - 24
            # 控件名解析不出来时会退化成 w0/w1/w2（节点不在节点库里就会这样，
            # 比如插件没装或老工作流）。显示 `w3 true` 纯属噪音，不如只显示值。
            show_name = opts.widget_names and not re.fullmatch(r"w\d+", str(name))
            if show_name:
                nfs = sfs * 0.86
                nw = min(text_width(name, nfs) + 8, inner * 0.5)
                c.text(x + 12, cy + 0.5, ellipsize(name, nw, nfs), nfs,
                       th["dim_text"], alpha=0.8)
                c.text(x + 12 + nw, cy,
                       ellipsize(val, inner - nw, sfs), sfs,
                       th["body_text"])
            else:
                c.text(x + 12, cy, ellipsize(val, inner, sfs), sfs,
                       th["body_text"])


def _draw_note(c: Canvas, b: NodeBox, opts: Opts, th: Dict[str, str],
               ox: float, oy: float) -> None:
    """便签（Note 节点）：一块贴纸 + 自动换行的正文。"""
    n = b.node
    x, y, w, h = b.x + ox, b.y + oy, b.w, b.h
    txt = ""
    try:
        pairs = list(n.widget_pairs())
        if pairs:
            txt = str(pairs[0][1] or "")
    except Exception:
        pass
    # 便签的底色可能是工作流里指定的深色，也可能是默认色。
    # **正文颜色必须跟着便签底色走，不能跟着主题走** —— 浅色主题下用主题
    # 正文色（深灰）画在深色便签上，字就完全看不见了（实测踩过）。
    fill = n.bgcolor or ("#3B3520" if opts.theme == "dark" else "#FBF3D0")
    edge = n.color or ("#7A6A20" if opts.theme == "dark" else "#C9B458")
    tcol = "#F0F0F0" if _luma(fill) < 0.5 else "#2A2A2A"
    c.rrect(x + 2, y + 3, w, h, 6, fill="#000000", alpha=0.25)
    c.rrect(x, y, w, h, 6, fill=fill, stroke=edge, sw=1.2)
    fs = 12.0 * opts.font
    lines = wrap_text(txt, w - 20, fs, max_lines=max(1, int((h - 16) / (fs * 1.5))))
    for i, ln in enumerate(lines):
        c.text(x + 10, y + 12 + i * fs * 1.5, ln, fs, tcol, alpha=0.95)
    if not lines:
        c.text(x + 10, y + 12, ellipsize(b.title, w - 20, fs), fs, tcol, alpha=0.7)


def _draw_legend(c: Canvas, sc: Scene, opts: Opts, th: Dict[str, str],
                 title: str) -> None:
    fs = 12.0 * opts.font
    line_h = LEGEND_ROW_H
    head = LEGEND_HEAD_H
    rows = legend_rows(len(sc.legend))
    wid = 0.0
    for k, lbl, cnt in sc.legend[:rows]:
        wid = max(wid, text_width(f"{lbl}  {cnt}", fs) + 46)
    wid = max(wid, text_width(title or "", 14.0 * opts.font) + 24, 210.0)
    hgt = legend_height(len(sc.legend))

    # 落在 build_scene 留出的那条底带里，不会压到任何节点
    x = opts.pad * 0.4
    y = sc.h - hgt - opts.pad * 0.4
    c.rrect(x + 2, y + 2, wid, hgt, 8, fill="#000000", alpha=0.30)
    c.rrect(x, y, wid, hgt, 8, fill=th["legend_bg"], stroke=th["legend_edge"],
            sw=1.0)
    c.rrect(x, y, wid, head, 8, fill="#3A3A3A" if opts.theme == "dark" else "#E8E8E8",
            corners=(True, True, False, False))
    c.text(x + 10, y + head / 2,
           ellipsize(title or "工作流", wid - 20, 14.0 * opts.font),
           14.0 * opts.font, th["title_text"] if opts.theme == "dark" else "#222222",
           bold=True)

    for i, (k, lbl, cnt) in enumerate(sc.legend[:rows]):
        cy = y + head + (i + 0.5) * line_h
        c.rrect(x + 10, cy - 5, 20, 10, 3, fill=PALETTE.get(k, PALETTE["misc"]))
        c.text(x + 36, cy, f"{lbl}", fs, th["body_text"])
        c.text(x + wid - 12, cy, str(cnt), fs, th["dim_text"], anchor="end")

    # 右下角补一行尺寸信息，方便知道这张图对应多大的画布
    info = (f"{sc.stats.get('nodes', 0)} 节点 · {sc.stats.get('links', 0)} 连线 · "
            f"{int(sc.w)}×{int(sc.h)} px")
    c.text(sc.w - opts.pad * 0.4, sc.h - opts.pad * 0.4, info, fs,
           th["dim_text"], anchor="end")


# ---------------------------------------------------------------- 入口


def fit_scale(scene: Scene, opts: Opts) -> float:
    """画布太大就整体缩小，保证长边不超过 max_px。"""
    longest = max(scene.w, scene.h) * max(opts.scale, 0.01)
    if opts.max_px and longest > opts.max_px:
        return opts.max_px / max(scene.w, scene.h)
    return opts.scale


def render_svg(g: Graph, opts: Optional[Opts] = None,
               cat: Optional[Dict[str, Any]] = None,
               title: str = "") -> Tuple[str, Scene, float]:
    opts = opts or Opts()
    sc = build_scene(g, cat, opts)
    s = fit_scale(sc, opts)
    cv = SvgCanvas(sc.w, sc.h, s, opts.font)
    draw(sc, cv, opts, title)
    return cv.to_svg(), sc, s


def render_png(g: Graph, path: str, opts: Optional[Opts] = None,
               cat: Optional[Dict[str, Any]] = None,
               title: str = "") -> Tuple[int, int, Scene]:
    opts = opts or Opts()
    sc = build_scene(g, cat, opts)
    s = fit_scale(sc, opts)
    th = THEMES.get(opts.theme) or THEMES["dark"]
    cv = PilCanvas(sc.w, sc.h, s, opts.font, bg=th["bg"])
    draw(sc, cv, opts, title)
    w, h = cv.save(path)
    return w, h, sc
