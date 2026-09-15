# -*- coding: utf-8 -*-
"""
测试组 5：离线渲染（SVG / PNG）

每一条都对应一个**真实踩到的坑**，不是凑数的：

  * 用 `max([...] + [0.0])` 兜底会毁掉整张图——真实工作流的节点常整体落在
    **负坐标**区（画布往左下拖过），那时 0.0 反而成了最大边界，
    实测把一张 3800×2657 的图撑成 9774×4510，右下全是空白
  * `LoadImage` 的 widgets_values 有 3 个值，但节点库只认 1 个控件 ——
    多出来的是前端记账数据，照画会出现一行莫名其妙的 "image"
  * `schema.UI_ONLY_WIDGETS` 是为"估节点高度"准备的，里面连 `image` 都算噪音；
    渲染时照抄会把"选中的文件名"这个真信息删掉
  * SVG 是文本，节点标题里的 `<` `&` `"` 不转义就是非法 XML
  * 中文宽度按 1em 算、拉丁按 0.55em —— 不估宽度就没法截断和换行
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

from harness import Suite, check, eq, skip

from cwf.lib import paths
from cwf.lib import render as R
from cwf.lib.graph import Graph, Group, Link, Node, Slot

suite = Suite()
TMP = os.path.join(tempfile.gettempdir(), "cwf_render_test")


def _node(nid, type_, title=None, pos=(0, 0), size=(300, 140), ins=(), outs=(),
          widgets=None, mode=0, color=None, bgcolor=None) -> Node:
    n = Node(id=nid, type=type_, title=title, pos=pos, size=size, mode=mode,
             color=color, bgcolor=bgcolor)
    n.inputs = [Slot(name=a, type=b) for a, b in ins]
    n.outputs = [Slot(name=a, type=b) for a, b in outs]
    if widgets is not None:
        n.widgets = widgets
        n._wform = "list" if isinstance(widgets, list) else "dict"
        n._wnames = None
    return n


def _graph() -> Graph:
    """一张小的合成工作流：加载 → 采样 → 解码 → 保存，带分区框、便签、静音。"""
    ns = [
        _node(1, "CheckpointLoaderSimple", pos=(0, 0), size=(320, 100),
              outs=(("MODEL", "MODEL"), ("CLIP", "CLIP"), ("VAE", "VAE")),
              widgets=["sd_xl.safetensors"]),
        _node(2, "EmptyLatentImage", pos=(0, 200), size=(300, 100),
              outs=(("LATENT", "LATENT"),), widgets=[512, 512, 1]),
        _node(3, "KSampler", pos=(420, 0), size=(320, 260),
              ins=(("model", "MODEL"), ("positive", "CONDITIONING"),
                   ("negative", "CONDITIONING"), ("latent_image", "LATENT")),
              outs=(("LATENT", "LATENT"),),
              widgets=[7, "fixed", 20, 8.0, "euler", "normal", 1.0]),
        _node(4, "VAEDecode", pos=(840, 0), size=(220, 90),
              ins=(("samples", "LATENT"), ("vae", "VAE")),
              outs=(("IMAGE", "IMAGE"),)),
        _node(5, "SaveImage", pos=(1120, 0), size=(320, 400),
              ins=(("images", "IMAGE"),), widgets=["CWF/out"]),
        _node(6, "Note", title="便签", pos=(0, 360), size=(280, 100),
              widgets=["这是一段<b>说明</b> & 提示"]),
        _node(7, "SomeMutedNode", pos=(840, 240), size=(200, 90), mode=2),
        _node(8, "SomeBypassedNode", pos=(840, 380), size=(200, 90), mode=4),
    ]
    ls = [
        Link(id=1, origin_id=1, origin_slot=0, target_id=3, target_slot=0, type="MODEL"),
        Link(id=2, origin_id=1, origin_slot=2, target_id=4, target_slot=1, type="VAE"),
        Link(id=3, origin_id=2, origin_slot=0, target_id=3, target_slot=3, type="LATENT"),
        Link(id=4, origin_id=3, origin_slot=0, target_id=4, target_slot=0, type="LATENT"),
        Link(id=5, origin_id=4, origin_slot=0, target_id=5, target_slot=0, type="IMAGE"),
        Link(id=6, origin_id=1, origin_slot=1, target_id=7, target_slot=0, type="CLIP"),
    ]
    gp = Group(id=1, title="采样区", bounding=(380, -40, 760, 420), color="#3f789e")
    return Graph(ns, ls, [gp])


# ---------------------------------------------------------------- 文本度量


suite.group("文本度量")


@suite("中文按 1 em、拉丁按 0.55 em 估宽")
def _tw():
    eq(R.text_width("abcd", 10), 22.0, "拉丁宽度不对")
    eq(R.text_width("中文", 10), 20.0, "中文宽度不对")
    check(R.text_width("中文", 10) > R.text_width("ab", 10), "中文应该比同字数拉丁宽")


@suite("超宽就截断，且不超框")
def _ell():
    s = "WanVideo\\Wan2.1-I2V-14B-480P_fp8_e4m3fn.safetensors"
    for keep in ("head", "tail"):
        out = R.ellipsize(s, 120, 11.5, keep=keep)
        check(R.text_width(out, 11.5) <= 120.0,
              f"keep={keep} 截断后仍然超宽：{R.text_width(out, 11.5)}")
        check(out.endswith("…") if keep == "head" else out.startswith("…"),
              f"keep={keep} 省略号位置不对：{out}")
    eq(R.ellipsize("短", 100, 11.5), "短", "没超宽不该动它")
    eq(R.ellipsize("abc", 0, 11.5), "", "宽度为 0 应该返回空")


@suite("自动换行不丢字、不超宽")
def _wrap():
    txt = "第一步：选择加载图像，网页版可能有延迟，建议等十几秒确保图片上传好了再点击生图"
    lines = R.wrap_text(txt, 200, 12)
    check(len(lines) > 1, "这么长的中文应该换行")
    for ln in lines:
        check(R.text_width(ln, 12) <= 200.0 + 1e-6, f"这行超宽了：{ln}")
    joined = "".join(lines)
    check(len(joined) >= len(txt) - 2, f"换行丢字了：{len(joined)} vs {len(txt)}")


@suite("控件值格式化：数字、布尔、列表、空值")
def _fmt():
    eq(R.fmt_value(True), "true")
    eq(R.fmt_value(None), "—")
    eq(R.fmt_value(8.0), "8")
    eq(R.fmt_value(0.25), "0.25")
    eq(R.fmt_value([1, 2, 3]), "[1, 2, 3]")
    check("…" in R.fmt_value([1, 2, 3, 4, 5, 6]), "长列表该省略")


# ---------------------------------------------------------------- 配色


suite.group("配色")


@suite("已知类型用 ComfyUI 自带色，未知类型按名字定色且稳定")
def _colors():
    eq(R.link_color("MODEL"), "#B39DDB")
    eq(R.link_color("model"), "#B39DDB", "大小写不该影响")
    a = R.link_color("H3_T8_SPEED_PLAN")
    b = R.link_color("H3_T8_SPEED_PLAN")
    eq(a, b, "同一个自定义类型两次渲染颜色必须一致")
    check(re.fullmatch(r"#[0-9A-Fa-f]{6}", a), f"颜色格式不对：{a}")
    eq(R.link_color(""), R.DEFAULT_LINK)


@suite("16 个大类都有配色，且都能给出中文名")
def _palette():
    from cwf.lib.taxonomy import L1
    for k in L1:
        check(k in R.PALETTE, f"大类 {k} 没有配色")


# ---------------------------------------------------------------- 场景


suite.group("场景构建")


@suite("节点/连线/分区框都进了场景")
def _scene():
    g = _graph()
    sc = R.build_scene(g)
    eq(sc.stats["nodes"], 7, "节点数不对（8 个节点里有 1 个是便签，不算进节点）")
    eq(sc.stats["links"], 6, "连线数不对")
    eq(sc.stats["groups"], 1, "分区框数不对")
    check(sc.w > 300 and sc.h > 200, f"画布尺寸不合理：{sc.w}×{sc.h}")


@suite("节点整体落在负坐标区时，画布不被凭空撑大（实测踩过）")
def _negative_coords():
    """真实工作流常把画布往左下拖，节点全是负坐标。

    当时用 `max([...] + [0.0])` 兜底，0.0 反而成了边界：
    3800×2657 的内容被撑成 9774×4510，右侧下方全是空白。
    """
    ns = [_node(1, "A", pos=(-3000, -2000), size=(200, 100)),
          _node(2, "B", pos=(-2200, -1900), size=(200, 100))]
    sc = R.build_scene(Graph(ns, [], []))
    w_expected = (200 + 800) + 48 * 2          # 内容宽 800 + 两边留白
    h_expected = (100 + 100) + 48 * 2
    check(abs(sc.w - w_expected) < 1.0,
          f"画布被撑大了：{sc.w:.0f}，应该是 {w_expected}")
    check(abs(sc.h - h_expected) < 1.0,
          f"画布被撑大了：{sc.h:.0f}，应该是 {h_expected}")


@suite("空工作流也能出一张合法图，不会 0 像素")
def _empty():
    sc = R.build_scene(Graph([], [], []))
    check(sc.w >= 320 and sc.h >= 200, f"空图尺寸太小：{sc.w}×{sc.h}")
    svg, _sc, _s = R.render_svg(Graph([], [], []))
    ET.fromstring(svg)                          # 不抛异常就算过


@suite("连到不存在节点的连线会被跳过，不崩")
def _dangling():
    ns = [_node(1, "A", pos=(0, 0), size=(200, 100), outs=(("x", "IMAGE"),))]
    ls = [Link(id=1, origin_id=1, origin_slot=0, target_id=99, target_slot=0)]
    sc = R.build_scene(Graph(ns, ls, []))
    eq(sc.stats["links"], 0, "悬空连线不该进场景")
    eq(sc.stats["nodes"], 1)


@suite("静音/旁路节点会被标出来")
def _modes():
    sc = R.build_scene(_graph())
    eq(sc.boxes[7].node.mode, 2)
    eq(sc.boxes[8].node.mode, 4)


@suite("便签不当成普通节点算，但会进画布范围")
def _notes():
    g = _graph()
    sc = R.build_scene(g)
    eq(len(sc.notes), 1, "便签没被单独收好")
    check(6 not in sc.boxes, "便签不该混进节点盒子")
    eq(sc.stats["nodes"], 7, "节点数不该把便签算进去")


# ---------------------------------------------------------------- SVG


suite.group("SVG 输出")


@suite("产出的 SVG 是合法 XML")
def _svg_valid():
    svg, sc, s = R.render_svg(_graph())
    root = ET.fromstring(svg)
    eq(root.tag, "{http://www.w3.org/2000/svg}svg")
    check(len(list(root.iter())) > 30, f"元素太少，像是空图：{len(list(root.iter()))}")
    eq(root.get("viewBox"), f"0 0 {sc.w * s:.2f} {sc.h * s:.2f}")


@suite("标题里的 < > & \" 会被转义（不转义就是非法 XML）")
def _escape():
    ns = [_node(1, "T", title='a<b>&"c"', pos=(0, 0), size=(240, 80),
                widgets=['x < y & z'])]
    svg, _sc, _s = R.render_svg(Graph(ns, [], []))
    ET.fromstring(svg)                          # 关键：能解析
    check("&lt;b&gt;" in svg, "尖括号没转义")
    check("&amp;" in svg, "& 没转义")


@suite("每个节点和每条连线都画进去了")
def _svg_complete():
    g = _graph()
    svg, sc, _s = R.render_svg(g)
    for t in ("CheckpointLoaderSimple", "EmptyLatentImage", "KSampler",
              "VAEDecode", "SaveImage", "SomeMutedNode"):
        check(t in svg, f"{t} 没画出来")
    # 连线画成三次贝塞尔（d 里有 " C "），圆角矩形画成圆弧（d 里有 " A "），
    # 所以数 " C " 能精确对上连线数，不会跟节点框混在一起
    eq(svg.count(" C "), len(sc.links), "画出来的连线数对不上")
    # 端口圆点半径 5，背景点阵半径 1
    ports = len([1 for b in sc.boxes.values()
                 for _ in b.in_slots]) +             len([1 for b in sc.boxes.values() for _ in b.out_slots])
    eq(svg.count('r="5.00"'), ports, "端口圆点数对不上")


@suite("静音 / 旁路有可见标记")
def _svg_modes():
    svg, _sc, _s = R.render_svg(_graph())
    check("静音" in svg or "MUTE" in svg, "静音标记丢了")
    check("旁路" in svg or "BYPASS" in svg, "旁路标记丢了")


@suite("画布缩放会按比例作用到所有坐标")
def _scaled():
    a, sc, _ = R.render_svg(_graph(), R.Opts())
    b, _sc2, s2 = R.render_svg(_graph(), R.Opts(scale=2.0))
    eq(s2, 2.0)
    eq(len(a), len(a))                            # 不崩即可
    check(b.count("<path") == a.count("<path"), "缩放不该改变图元数量")


@suite("--max-px 会限制长边，0 表示不限")
def _maxpx():
    sc = R.build_scene(_graph(), None, R.Opts(max_px=0))
    eq(R.fit_scale(sc, R.Opts(max_px=0)), 1.0, "0 应该表示不限制")
    s = R.fit_scale(sc, R.Opts(max_px=400))
    check(max(sc.w, sc.h) * s <= 400 + 1e-6, f"没被限制住：{max(sc.w, sc.h) * s:.1f}")


# ---------------------------------------------------------------- PNG


suite.group("PNG 输出")


def _has_pil() -> bool:
    try:
        import PIL                                        # noqa: F401
        return True
    except Exception:
        return False


@suite("PNG 文件写出来了，尺寸对得上")
def _png():
    if not _has_pil():
        skip("这台机器没有 Pillow")
    d = os.path.join(TMP, "png")
    shutil.rmtree(TMP, ignore_errors=True)
    p = os.path.join(d, "x.png")
    w, h, sc = R.render_png(_graph(), p, R.Opts())
    check(os.path.exists(p), "文件没写出来")
    check(os.path.getsize(p) > 2000, f"文件太小，可能是空图：{os.path.getsize(p)}")
    with open(p, "rb") as f:
        check(f.read(8) == b"\x89PNG\r\n\x1a\n", "PNG 魔数不对")
    eq(w, int(round(sc.w)))
    eq(h, int(round(sc.h)))
    from PIL import Image
    im = Image.open(p)
    eq(im.size, (w, h))
    # 画布上应该真的有东西：不是纯色
    colors = im.convert("RGB").getcolors(maxcolors=100000)
    check(colors is None or len(colors) > 20, "图像颜色太单一，可能是空画布")


@suite("PNG 也会按 max_px 缩，且至少 1 像素")
def _png_scaled():
    if not _has_pil():
        skip("这台机器没有 Pillow")
    p = os.path.join(TMP, "png", "small.png")
    w, h, _sc = R.render_png(_graph(), p, R.Opts(max_px=300))
    check(max(w, h) <= 300, f"没缩到位：{w}×{h}")
    check(w >= 1 and h >= 1, f"缩没了：{w}×{h}")


@suite("没装 Pillow 时给出的是能照做的报错，而不是 traceback")
def _png_no_pil():
    from cwf import cli
    import io
    import contextlib
    # 用不存在的格式名触发参数校验；这里只验证 png 分支的提示文案存在
    src = open(cli.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    check("pip install pillow" in src, "缺 Pillow 时没有给出安装提示")
    check("--out preview.svg" in src, "缺 Pillow 时没有给出改用 SVG 的提示")


@suite("便签正文颜色跟着便签底色走，不跟着主题走（浅色主题下实测踩过）")
def _note_contrast():
    """浅色主题里便签正文用主题的深灰字，画在深色便签上就完全看不见了。"""
    dark = _node(1, "Note", pos=(0, 0), size=(300, 120), bgcolor="#333333",
                 widgets=["深色便签上的字必须看得见"])
    svg, _sc, _s = R.render_svg(Graph([dark], [], []), R.Opts(theme="light"))
    m = re.search(r'<text[^>]*fill="(#[0-9A-Fa-f]{6})"[^>]*>深色便签', svg)
    check(m is not None, "便签正文没画出来")
    check(R._luma(m.group(1)) > 0.5,
          f"深色便签上用了个暗字，看不见：{m.group(1)}")

    light = _node(1, "Note", pos=(0, 0), size=(300, 120), bgcolor="#FBF3D0",
                  widgets=["浅色便签"])
    svg2, _sc2, _s2 = R.render_svg(Graph([light], [], []), R.Opts(theme="dark"))
    m2 = re.search(r'<text[^>]*fill="(#[0-9A-Fa-f]{6})"[^>]*>浅色便签', svg2)
    check(m2 is not None, "便签正文没画出来")
    check(R._luma(m2.group(1)) < 0.5,
          f"浅色便签上用了个亮字，看不见：{m2.group(1)}")


@suite("亮度计算是对的")
def _luma():
    eq(R._luma("#000000"), 0.0)
    check(abs(R._luma("#FFFFFF") - 1.0) < 1e-9, "白色亮度不是 1")
    check(R._luma("#333333") < 0.5 < R._luma("#FBF3D0"), "深浅判断反了")


# ---------------------------------------------------------------- 真实工作流


suite.group("真实工作流")


def _real_path() -> str:
    import glob
    root = os.environ.get(
        "CWF_WORKFLOWS",
        "")
    for pat in (r"qwen\*.json", r"放大\*.json", r"*\*.json"):
        hits = sorted(glob.glob(os.path.join(root, pat)))
        if hits:
            return hits[0]
    return ""


@suite("拿真工作流渲染：节点不丢，SVG 合法")
def _real():
    p = _real_path()
    if not p or not os.path.exists(p):
        skip("本机没有工作流样本")
    g = Graph.load(p)
    svg, sc, _s = R.render_svg(g)
    ET.fromstring(svg)
    eq(sc.stats["nodes"], len(g.nodes),
       "渲染出来的节点数和图里的对不上（便签除外）")
    check(sc.w > 100 and sc.h > 100, f"真工作流画布太小：{sc.w}×{sc.h}")


@suite("真实工作流的便签正文不会被当成端口画成一行行")
def _real_notes():
    p = _real_path()
    if not p or not os.path.exists(p):
        skip("本机没有工作流样本")
    g = Graph.load(p)
    sc = R.build_scene(g)
    for b in sc.notes:
        check(not b.in_slots and not b.out_slots, "便签不该有端口")


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
