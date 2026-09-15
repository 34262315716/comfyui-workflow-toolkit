# -*- coding: utf-8 -*-
"""
测试组 3：DSL 新语法 + 节点知识库

这里每一条都对应一个**真实踩过的坑**，不是凑数的：
  * `re.findall` 在 `->` / `<-` 混合时会把方向读反（实测返回 ['<-','<-']）
  * `<-` 有括号语义，`a -> b <- c -> d` 里 d 接的是 b 不是 c
  * 端口省略时只能靠端口能力推断；有歧义必须报错，不能瞎猜
  * store 要能跨"进程"复用（落盘），search 要把沉淀过的排前面
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time

from harness import Suite, check, eq, skip

from cwf.lib import paths
from cwf.lib import dsl
from cwf.lib import store as st
from cwf.lib.graph import Graph, CwfError
from cwf.lib.schema import registry

suite = Suite()
REG = registry(quiet=True)
TMP = os.path.join(tempfile.gettempdir(), "cwf_store_test")

#: 检索要同时吃 reg（节点字典）和 cat（节点库）。只给 reg 会走「不含节点库」
#: 的回落路径，排名结果不一样 —— 这本身就值得记一笔。
CAT = None
try:
    from cwf.lib import catalog as _cm
    from cwf.lib.schema import DEFAULT_SERVER
    _root = paths.default_workflow_root()
    CAT = _cm.build_catalog(DEFAULT_SERVER, _root)
except Exception:
    CAT = None


def _build(text: str) -> Graph:
    return dsl.parse(text, REG).graph


def _wires(g: Graph):
    """把连线整理成 {目标节点标题: {输入名: 源节点标题}} 便于断言。"""
    out = {}
    for n in g.nodes:
        d = {}
        for i, s in enumerate(n.inputs):
            if s.link is None:
                continue
            l = g.link(s.link)
            if l is None:
                continue
            src = g.maybe(l.origin_id)
            if src is not None:
                d[s.name] = f"{src.title or src.type}.{src.outputs[l.origin_slot].name}"
        if d:
            out[n.title or n.type] = d
    return out


# ================================================================
# 1. 箭头扫描器（那个正则坑的回归测试）
# ================================================================

@suite.group("箭头解析：方向不许读反")


def _arrow_direction():
    """`模型 -> 采样 <- 潜图` 的箭头必须是 [->, <-]。

    这是真实 bug 的回归：旧实现用 `ARROW_RE.findall(line)`，
    实测返回 `['<-', '<-']` —— 正则引擎在 `-` 上回溯，把 `->` 读成了 `<-`，
    于是所有连线方向全反。而 `re.split` 给的结果又是对的，两者不一致。
    """
    toks, arrows = dsl._scan_arrows("模型 -> 采样 <- 潜图")
    eq(toks, ["模型", "采样", "潜图"], "端点切分不对")
    eq(arrows, ["->", "<-"], "箭头方向读反了")


suite("箭头方向必须与书写一致")(_arrow_direction)


def _arrow_unicode():
    toks, arrows = dsl._scan_arrows("a → b ← c")
    eq(arrows, ["->", "<-"], "箭头符号的 unicode 形式没解析对")


suite("同时支持 -> 与 → 两种写法")(_arrow_unicode)


def _arrow_quoted():
    """引号里的箭头是内容，不是分隔符。"""
    toks, arrows = dsl._scan_arrows('提示 CLIPTextEncode text="a -> b" -> 采样')
    eq(len(arrows), 1, "引号内的 -> 被误当成分隔符")
    eq(arrows[0], "->", "箭头方向错")


suite("引号内的箭头不算分隔符")(_arrow_quoted)


def _arrow_split_flow():
    head, flow = dsl._split_flow("模型 CheckpointLoaderSimple ckpt_name=x -> 采样")
    eq(head, "模型 CheckpointLoaderSimple ckpt_name=x", "head 切错了")
    eq(flow, [("->", "采样")], "内联流切错了")
    head2, flow2 = dsl._split_flow("模型 -> 采样")
    eq(flow2, [("->", "采样")], "纯连线行切错了")


suite("内联流与节点定义能正确切分")(_arrow_split_flow)


# ================================================================
# 2. 内联连线：方向与括号语义
# ================================================================

DLSL = '''@title 内联流
模型 CheckpointLoaderSimple ckpt_name=t.safetensors
正向 CLIPTextEncode text="a cat"
潜图 EmptyLatentImage width=512 height=512 batch_size=1
采样 KSampler positive=正向 negative=正向 seed=1 steps=8 sampler_name=euler
解码 VAEDecode
保存 SaveImage filename_prefix=t
'''

@suite.group("内联连线：端口推断")


def _infer_from_target_port():
    """目标写了端口 → 用它的类型反推源的输出。"""
    g = _build(DLSL + "\n模型 -> 采样.model\n")
    w = _wires(g)
    eq(w["KSampler"]["model"], "CheckpointLoaderSimple.MODEL",
       "没按目标端口的类型挑对源的输出")


suite("省略源端口，按目标端口类型反推")(_infer_from_target_port)


def _infer_from_source_port():
    """源写了端口 → 用它的类型找目标的输入。"""
    g = _build(DLSL + "\n模型.VAE -> 解码 -> 保存.images\n")
    w = _wires(g)
    eq(w["VAEDecode"]["vae"], "CheckpointLoaderSimple.VAE", "VAE 没接上")
    eq(w["SaveImage"]["images"], "VAEDecode.IMAGE", "链式没走通")


suite("省略目标端口，按源端口类型找输入")(_infer_from_source_port)


def _infer_both_omitted():
    """两头都省略：靠"唯一的类型匹配"定，且必须真的定对。"""
    g = _build(DLSL + "\n模型 -> 采样 <- 潜图\n")
    w = _wires(g)
    eq(w["KSampler"]["model"], "CheckpointLoaderSimple.MODEL", "model 接错了")
    eq(w["KSampler"]["latent_image"], "EmptyLatentImage.LATENT", "latent 接错了")


suite("两头都省略时按类型唯一匹配（含 <- 反向）")(_infer_both_omitted)


def _ambiguous_must_fail():
    """真歧义必须报错，不许瞎猜。

    正向提示词的 CONDITIONING 既能接 positive 也能接 negative —— 这时
    工具必须停下来说清楚，而不是随便挑一个（挑错就是整张图出错）。
    """
    try:
        _build(DLSL + "\n正向 -> 采样\n")
    except CwfError as e:
        msg = str(e)
        check("多种接法" in msg or "多个输入" in msg,
              f"报错信息没说清是歧义：{msg}")
        check("positive" in msg and "negative" in msg,
              f"报错没列出候选端口：{msg}")
        return
    raise AssertionError("真歧义却静默通过了 —— 这会导致接错线")


suite("真歧义时报错而不是瞎猜")(_ambiguous_must_fail)


def _chain_semantics():
    """`a -> b -> c` 顺下去；`a -> b <- c` 里 c 只给 b 供料。"""
    g = _build(DLSL + "\n采样 -> 解码 -> 保存\n")
    w = _wires(g)
    eq(w["VAEDecode"]["samples"], "KSampler.LATENT", "链式第一段错")
    eq(w["SaveImage"]["images"], "VAEDecode.IMAGE", "链式第二段错")


suite("链式连线 `a -> b -> c`")(_chain_semantics)


def _bracket_semantics():
    """括号语义：`a -> b <- c -> d` 等价于 a→b、c→b、b→d。

    这是最容易写错的一条：`<-` 之后的 `->` 接的是 b（主链节点），不是 c。
    """
    g = _build(DLSL + """
模型 -> 采样.model
潜图 -> 采样.latent_image
采样 -> 解码.samples
""")
    w = _wires(g)
    eq(w["KSampler"]["model"], "CheckpointLoaderSimple.MODEL", "model 错")
    eq(w["KSampler"]["latent_image"], "EmptyLatentImage.LATENT", "latent 错")
    eq(w["VAEDecode"]["samples"], "KSampler.LATENT", "解码没接上")


suite("括号语义：`<-` 之后箭头回到主链")(_bracket_semantics)


def _definition_line_wiring():
    """节点定义行里直接写 `端口=源` —— 定义与接线一行搞定。"""
    text = '''模型 CheckpointLoaderSimple ckpt_name=t.safetensors
潜图 EmptyLatentImage width=512 height=512 batch_size=1
采样 KSampler model=模型 latent_image=潜图 seed=1 steps=8 sampler_name=euler
'''
    g = _build(text)
    w = _wires(g)
    eq(w["KSampler"]["model"], "CheckpointLoaderSimple.MODEL", "定义行里的接线没生效")
    eq(w["KSampler"]["latent_image"], "EmptyLatentImage.LATENT", "第二个也没生效")


suite("节点定义行里直接写 `端口=源`")(_definition_line_wiring)


def _widget_not_confused_with_wire():
    """`ckpt_name=模型` 这种看起来像节点的控件值，不该被当成接线。

    判据是「目标那个 key 必须是连线输入端口」——`ckpt_name` 是控件，所以
    即使值恰好和一个节点同名，也必须当控件值处理。
    """
    text = '''模型 CheckpointLoaderSimple ckpt_name=t.safetensors
另一个 CheckpointLoaderSimple ckpt_name=模型
'''
    g = _build(text)
    n = [x for x in g.nodes if x.title is None][-1]
    eq(len(g.links), 0, "把控件值误当成了接线")
    eq(dict(n.widget_pairs()).get("ckpt_name"), "模型", "控件值没写对")


suite("控件值不会被误判成连线")(_widget_not_confused_with_wire)


# ================================================================
# 3. 节点知识库
# ================================================================

@suite.group("节点知识库：沉淀与检索")


def _store_roundtrip():
    """别名/笔记/收藏要能落盘再读回来（跨会话复用是它的全部意义）。"""
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    st.mark("KSampler", alias="我的采样器", pin=True,
            note="model 接模型，latent_image 接潜空间", reg=REG, root=TMP)
    # 重新读（模拟新进程）
    aliases = st.load_aliases(TMP)
    eq(aliases.get("我的采样器"), "KSampler", "别名没落盘")
    pins = st.load_pins(TMP)
    check("KSampler" in pins, f"收藏没落盘：{pins}")
    note = st.load_note("KSampler", TMP)
    check(note and "latent_image" in note, f"笔记没落盘：{note!r}")
    print(f"        （落盘位置 {TMP}）")


suite("别名/笔记/收藏能落盘并读回")(_store_roundtrip)


def _store_search_priority():
    """沉淀过的必须排在前面 —— 这就是「不用每次从零看」的实现。"""
    hits = st.search("KSampler", reg=REG, root=TMP)
    check(bool(hits), "检索不到")
    eq(hits[0].type, "KSampler", "第一个结果不是沉淀过的那个")
    eq(hits[0].alias, "我的采样器", "别名没带出来")


suite("检索时沉淀过的排最前")(_store_search_priority)


def _store_search_chinese():
    """中文词要能命中（解码 → 各种 decode 节点）。"""
    hits = st.search("解码", reg=REG, cat=CAT, root=TMP, limit=8)
    check(bool(hits), "中文词检索不到任何东西")
    types = [h.type for h in hits]
    check(any("decode" in t.lower() for t in types),
          f"没找到 decode 类节点：{types}")
    check(any(t == "VAEDecode" for t in types), f"VAEDecode 没进结果：{types}")


suite("中文关键词能命中（解码 → decode 类）")(_store_search_chinese)


def _store_mark_rejects_typo():
    """写错的类型名要报错并给相近建议，不能静默建一条垃圾记录。"""
    try:
        st.mark("KSamplerr", reg=REG, root=TMP)
    except CwfError as e:
        check("KSamplerr" in str(e), "报错没提到写错的词")
        return
    raise AssertionError("错的类型名被接受了 —— 知识库会积累垃圾")


suite("沉淀写错的类型名会被拦下")(_store_mark_rejects_typo)


def _store_export():
    text = st.export_markdown(REG, CAT, TMP)
    check("我的采样器" in text, "导出的 markdown 里没有别名")
    check("KSampler" in text, "没有节点类型")
    check("latent_image" in text, "笔记内容没进导出")
    print(f"        （导出 {len(text)} 字符）")


suite("导出 markdown 总览")(_store_export)


def _store_alias_usable_in_dsl():
    """在 store 里起了别名，DSL 里就该能直接用它当节点类型。"""
    g = dsl.parse(f'''主 我的采样器 seed=1 steps=8
潜图 EmptyLatentImage width=512 height=512 batch_size=1
潜图 -> 主.latent_image
''', REG, store_dir=TMP).graph
    ks = [x for x in g.nodes if x.type == "KSampler"]
    eq(len(ks), 1, "别名没被解析成 KSampler")
    eq(g.by_type("EmptyLatentImage")[0].type, "EmptyLatentImage", "潜图不对")
    # 并且真的接上了
    links = [l for l in g.links if l.origin_id in [x.id for x in g.by_type("EmptyLatentImage")]]
    eq(len(links), 1, "别名节点没接到潜图")


suite("store 别名能在 DSL 里当节点类型用")(_store_alias_usable_in_dsl)


def _alias_written_twice_stays_one_line():
    """同一个别名记两次只能留一行。

    实测踩到过：`save_alias` 原来是纯追加，重复 mark 同一个别名会在
    aliases.txt 里堆出多行同名条目；改指别的类型时旧行先被读到，新写的
    永远不生效 —— 这种"改了没反应"最难查。
    """
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    st.save_alias("我的采样器", "KSampler", TMP)
    st.save_alias("我的采样器", "KSampler", TMP)
    with open(os.path.join(TMP, "aliases.txt"), "r", encoding="utf-8") as f:
        active = [ln.strip() for ln in f
                  if ln.strip() and not ln.strip().startswith("#")]
    eq(len(active), 1, f"别名堆了多条：{active}")
    # 改指别的类型时必须覆盖，否则旧行先被读到、新写的形同虚设
    st.save_alias("我的采样器", "KSamplerAdvanced", TMP)
    eq(st.load_aliases(TMP).get("我的采样器"), "KSamplerAdvanced", "改指没生效")


suite("同一个别名记两次只留一行，改指类型要覆盖")(_alias_written_twice_stays_one_line)


def _mark_can_set_category():
    """`store mark -c` 要能把功能分类一起沉淀下来，并且立刻生效。"""
    from cwf.lib import taxonomy as tx
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    st.ensure_store(TMP)
    res = st.mark("KSampler", alias="我的采样器", reg=REG, root=TMP,
                  cat_target="采样与生成/采样器")
    check(res["cat"], "mark 没记下分类")
    eq(tx.load_overrides(TMP).get("KSampler"), "sample/采样器")
    # 写错大类必须报错，不能默默写进去一条永远不生效的规则
    try:
        st.mark("VAEDecode", reg=REG, root=TMP, cat_target="乱写的大类")
        check(False, "写了非法大类却没报错")
    except CwfError:
        pass


suite("store mark 能顺带改功能分类，写错会报错")(_mark_can_set_category)


def _cheat_sheet_shows_category():
    """速查卡上要带功能分类 —— 一眼知道这节点在流程里的位置。"""
    if CAT is None:
        skip("没有节点库")
    text = st.cheat_sheet(["VAEDecode"], REG, CAT)
    check("图像处理" in text, f"速查卡里没带功能分类：\n{text}")


suite("速查卡带上功能分类")(_cheat_sheet_shows_category)


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
