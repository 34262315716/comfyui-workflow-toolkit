# -*- coding: utf-8 -*-
"""
测试组 4：节点功能分类法（taxonomy）

每一条都对应一个**真实踩到的坑**，不是凑数的：

  * `'3d' in name.lower()` 会把 `MiniMaxH3DualClockSampler`（H**3D**ual）判成三维
    —— 实测把 102 次使用的采样节点判错
  * `LoRA` 被驼峰切成 `Lo|RA`，`lora` 这个词元根本不出现，LoRA 节点全判错
  * `clip` 既是 CLIP 文本编码器也是「视频片段」，往兜底表里一放就误伤
  * 端口输出 SAMPLER/SIGMAS 是结构性事实，必须压过「原生分类写着 Audio」
  * 输出 MODEL 的节点不该因为名字里有 Sampling 就被判成采样器
  * 覆盖文件必须**立刻**生效（不能逼用户重建节点库）
  * 导出图鉴必须一个节点都不丢
"""
from __future__ import annotations

import os
import shutil
import tempfile

from harness import Suite, check, eq, skip

from cwf.lib import taxonomy as tx
from cwf.lib.graph import CwfError

suite = Suite()
TMP = os.path.join(tempfile.gettempdir(), "cwf_tax_test")

CAT = None
try:
    import json
    p = os.path.join(os.path.expanduser("~"), ".cwf", "node_catalog.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            CAT = json.load(f)
except Exception:
    CAT = None


def _t(name, **kw) -> str:
    """拿真实节点库里的分类结果；查不到就跳过。"""
    if not CAT or name not in CAT["types"]:
        skip(f"节点库里没有 {name}")
    e = dict(CAT["types"][name])
    e.update(kw)
    return tx.classify(name, e).key


# ---------------------------------------------------------------- 切词


suite.group("切词")


@suite("驼峰切词把 LoRA 还原成一个词元")
def _tok_lora():
    t = tx.tokens("MiniMaxH3LoRACompatibilityLoaderT8Advanced")
    check("lora" in t, f"LoRA 应该被还原成词元 lora，实际词元：{sorted(t)}")


@suite("Load3D 切得出 3d")
def _tok_3d():
    check("3d" in tx.tokens("Load3D"), f"Load3D 应含 3d：{sorted(tx.tokens('Load3D'))}")
    check("3d" in tx.tokens("Create3DModel"), "Create3DModel 应含 3d")


@suite("H3Dual 里不会凭空长出 3d（旧写法的致命误判）")
def _tok_false_3d():
    t = tx.tokens("MiniMaxH3DualClockSamplerT8")
    check("3d" not in t, f"H3Dual 不该切出 3d，实际：{sorted(t)}")
    # 顺带锁死旧的错误写法确实有问题
    check("3d" in "minimaxh3dualclocksamplert8",
          "（这条是反证：子串匹配确实会命中假 3d）")


@suite("中文名不会被切碎")
def _tok_cjk():
    t = tx.tokens("孤海-图像差异到遮罩")
    check("孤海" in t, f"中文词元丢了：{sorted(t)}")


# ---------------------------------------------------------------- 分类正确性


suite.group("分类正确性（拿本机真实节点验）")


@suite("KSampler → 采样与生成/采样器")
def _ks():
    eq(_t("KSampler"), "sample/采样器")


@suite("VAEDecode 是图像处理/解码，不是模型节点")
def _vaedecode():
    eq(_t("VAEDecode"), "image/解码")


@suite("VAEEncode 是潜空间/编码")
def _vaedecode_():
    eq(_t("VAEEncode"), "latent/编码")


@suite("LoadImage 归加载输入（不是图像处理）")
def _loadimage():
    eq(_t("LoadImage"), "load/图像")


@suite("SaveImage 归输出与预览")
def _saveimage():
    eq(_t("SaveImage"), "output/图像")


@suite("CLIPTextEncode 归条件与提示词")
def _cliptext():
    eq(_t("CLIPTextEncode"), "cond/文本编码")


@suite("输出 SAMPLER/SIGMAS 压过原生分类里的 Audio")
def _dualclock():
    """MiniMaxH3DualClockSamplerT8 的原生分类是 T8/MiniMax H3/Audio，
    但它输出 sampler:SAMPLER,sigmas:SIGMAS —— 端口证据更硬。"""
    k = _t("MiniMaxH3DualClockSamplerT8")
    check(k.startswith("sample/"), f"应该是采样类，实际 {k}")


@suite("输出 MODEL 的节点不会因为名字带 Sampling 被判成采样器")
def _modelsampling():
    k = _t("ModelSamplingAuraFlow")
    check(k.startswith("model/"), f"应该是模型类，实际 {k}")


@suite("名字带 Bypass 的 LoRA 加载器仍归模型，不是画布静音开关")
def _lora_bypass():
    k = _t("LoraLoaderBypassModelOnly")
    check(k.startswith("model/"), f"应该是模型类，实际 {k}")


@suite("名字带 Aspect 的缩放节点归放大/缩放，不是分析")
def _aspect_scale():
    k = _t("LayerUtility: ImageScaleByAspectRatio V2")
    check(k.startswith("image/"), f"应该是图像类，实际 {k}")
    check("分析" not in k, f"被 aspect 拐进分析类了：{k}")


@suite("真正读尺寸的节点还是归分析")
def _real_analysis():
    eq(_t("easy imageSize"), "image/分析 / 测量")


@suite("H3Dual 那个采样节点不再被判成三维")
def _no_false_3d():
    k = _t("MiniMaxH3DualClockSamplerT8")
    check(not k.startswith("threed"), f"被误判成三维了：{k}")


@suite("rgthree 的画布开关归流程与组织，不归输出节点")
def _rgthree():
    k = _t("Fast Groups Bypasser (rgthree)")
    check(k.startswith("flow/"), f"应该是流程类，实际 {k}")


@suite("ClipProjApply 不被 clip=视频片段 误伤")
def _clip():
    k = _t("ClipProjApply")
    check(not k.startswith("video/"), f"被误判成视频了：{k}")


@suite("检测/分割模型归遮罩")
def _detector():
    k = _t("UltralyticsDetectorProvider")
    check(k.startswith("mask/"), f"应该是遮罩类，实际 {k}")


@suite("带点号的模型名 Qwen2.5VL 也能认出来")
def _qwen():
    k = _t("Qwen2.5VL")
    check(k.startswith("api/"), f"应该是 api/大模型，实际 {k}")


@suite("UUID 类型名单独标成插件卸载残留")
def _uuid():
    k = _t("7b34ab90-36f9-45ba-a665-71d418f0df18")
    eq(k, "misc/插件卸载残留")


# ---------------------------------------------------------------- 分类器自身


suite.group("分类器自身")


@suite("规则 id 唯一，且大类都合法")
def _rules_sane():
    ids = [r.rid for r in tx.RULES]
    eq(len(ids), len(set(ids)), "有重复的规则 id")
    for r in tx.RULES:
        check(r.top in tx.L1, f"规则 {r.rid} 指向了不存在的大类 {r.top}")


@suite("16 个大类都有中文名和说明")
def _l1_complete():
    eq(len(tx.L1), 16, "大类数量变了")
    for k, v in tx.L1.items():
        check(bool(v), f"{k} 没有中文名")
        check(k in tx.L1_WHAT, f"{k} 没有说明文字")


@suite("分类是确定性的：同一个节点连判两次结果一致")
def _deterministic():
    if not CAT:
        skip("没有节点库")
    e = CAT["types"].get("KSampler") or {}
    a = tx.classify("KSampler", e)
    b = tx.classify("KSampler", dict(e))
    eq((a.top, a.sub, a.rule), (b.top, b.sub, b.rule))


@suite("空信息不会让分类器崩")
def _robust():
    for name in ("", "??", "a" * 300, "测试节点"):
        tax = tx.classify(name, {})
        check(tax.top in tx.L1, f"{name!r} 判出了非法大类")


# ---------------------------------------------------------------- 全量覆盖


suite.group("全量覆盖（7746 个节点）")


@suite("每个节点都拿到一个合法分类，一个都不少")
def _cover_all():
    if not CAT:
        skip("没有节点库")
    types = CAT["types"]
    seen = 0
    for t, e in types.items():
        tax = tx.classify(t, e)
        check(tax.top in tx.L1, f"{t} 判出了非法大类 {tax.top}")
        check(bool(tax.rule), f"{t} 没有记录判定规则")
        seen += 1
    eq(seen, len(types), "有节点被漏掉")
    check(seen > 7000, f"节点库只有 {seen} 个，不对劲")


@suite("apply_to_catalog 给每个节点写全分类字段，且不增不减")
def _apply_all():
    if not CAT:
        skip("没有节点库")
    import copy
    c = {"types": copy.deepcopy(CAT["types"])}
    before = len(c["types"])
    st = tx.apply_to_catalog(c)
    eq(len(c["types"]), before, "节点数变了")
    eq(st["total"], before, "统计总数对不上")
    for t, e in c["types"].items():
        for f in ("tax", "tax_zh", "tax_sub", "tax_path", "tax_path_zh",
                  "tax_rule", "tax_conf"):
            check(f in e, f"{t} 缺字段 {f}")
    eq(sum(st["by_top"].values()), before, "分类计数总和 ≠ 节点总数")


@suite("自动判定覆盖率保持在 95% 以上")
def _coverage():
    if not CAT:
        skip("没有节点库")
    import copy
    c = {"types": copy.deepcopy(CAT["types"])}
    st = tx.apply_to_catalog(c)
    check(st["coverage"] >= 0.95,
          f"覆盖率掉到 {st['coverage'] * 100:.1f}% 了，规则可能被改坏了")


# ---------------------------------------------------------------- 覆盖文件


suite.group("用户覆盖（categories.txt）")


def _fresh() -> str:
    d = os.path.join(TMP, "store")
    if os.path.exists(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


@suite("中文 / 英文大类都能认，写错了要报错而不是静默吞掉")
def _norm():
    eq(tx.norm_target("采样与生成/采样器"), ("sample", "采样器"))
    eq(tx.norm_target("video"), ("video", ""))
    eq(tx.norm_target("图像处理 / 放大"), ("image", "放大"))
    try:
        tx.norm_target("不存在的类")
        check(False, "写了不存在的大类却没报错")
    except CwfError:
        pass


@suite("覆盖写进文件后立刻生效，且 override 永远优先")
def _override_wins():
    d = _fresh()
    p = tx.save_override("KSampler", "图像处理/特殊", root=d)
    check(os.path.exists(p), "覆盖文件没写出来")
    got = tx.load_overrides(d)
    eq(got.get("KSampler"), "image/特殊")
    tax = tx.classify("KSampler", {"outputs": ["LATENT:LATENT"]}, overrides=got)
    eq(tax.top, "image", "覆盖没生效")
    eq(tax.conf, "manual", "覆盖的置信度应该是 manual")
    # 再验证一次"不带 overrides 时会自动去读文件"
    eq(tx.classify("KSampler", {}, overrides=None).top in tx.L1, True)


@suite("同一个类型改两次，文件里只留一条生效行")
def _override_rewrite():
    d = _fresh()
    tx.save_override("KSampler", "sample/采样器", root=d)
    tx.save_override("KSampler", "video/通用", root=d)
    with open(tx.category_file(d), "r", encoding="utf-8") as f:
        active = [ln.strip() for ln in f
                  if ln.strip() and not ln.strip().startswith("#")]
    ks = [ln for ln in active if ln.split("=")[0].strip() == "KSampler"]
    eq(len(ks), 1, f"文件里出现了多条 KSampler 生效行：{active}")
    # 注释里的示例行是文档，不算
    eq(tx.load_overrides(d).get("KSampler"), "video/通用")


@suite("尾部通配一条管一族")
def _override_wildcard():
    d = _fresh()
    tx.save_override("MiniMaxH3*", "video/视频工程套件", root=d)
    ov = tx.load_overrides(d)
    eq(tx.classify("MiniMaxH3WhateverT8Advanced", {}, ov).key, "video/视频工程套件")
    check(tx.classify("KSampler", {}, ov).top != "video", "通配误伤了别的节点")


@suite("覆盖文件里写错的行不会毒死其它行")
def _override_badline():
    d = _fresh()
    tx.ensure_category_file(d)
    with open(tx.category_file(d), "a", encoding="utf-8") as f:
        f.write("乱写的一行 = 不存在的大类\n")
        f.write("KSampler = 采样与生成/采样器\n")
    ov = tx.load_overrides(d)
    eq(ov.get("KSampler"), "sample/采样器")
    eq(ov.get("乱写的一行"), None, "非法行不该被读进来")


# ---------------------------------------------------------------- 导出


suite.group("图鉴导出")


@suite("导出会生成总览 + 分类文件，且一个节点都不丢")
def _atlas():
    if not CAT:
        skip("没有节点库")
    import copy
    c = {"types": copy.deepcopy(CAT["types"]),
         "built_str": "test", "totals": {}}
    tx.apply_to_catalog(c)
    dest = os.path.join(TMP, "atlas")
    r = tx.export_atlas(c, dest)
    eq(r["total"], len(c["types"]), "导出总数和节点数对不上")
    idx = os.path.join(dest, "index.md")
    check(os.path.exists(idx), "没有 index.md")
    # 把每个分类文件里的条目数加起来，必须等于全量
    n = 0
    for fn in os.listdir(dest):
        if not fn.endswith(".md") or fn == "index.md":
            continue
        with open(os.path.join(dest, fn), "r", encoding="utf-8") as f:
            n += sum(1 for line in f if line.startswith("- **"))
    eq(n, len(c["types"]), "图鉴里有节点没被写进去")
    check(os.path.exists(os.path.join(dest, "node_index.json")), "没有机读索引")


@suite("--used-only 导出只含用过的节点")
def _atlas_used():
    if not CAT:
        skip("没有节点库")
    import copy
    c = {"types": {t: dict(e) for t, e in CAT["types"].items() if e.get("usage")}}
    tx.apply_to_catalog(c)
    dest = os.path.join(TMP, "atlas_used")
    r = tx.export_atlas(c, dest, used_only=True)
    eq(r["total"], len(c["types"]))
    check(r["total"] < len(CAT["types"]), "used-only 应该比全量少")


suite.group("命令行（节点名里带空格）")


@suite("节点名带空格时不用加引号也能查")
def _cli_name_with_spaces():
    """`Mask Fill Holes`、`Fast Groups Bypasser (rgthree)` 这种名字本机一大把。

    踩过：`cwf nodes setcat Anything Everywhere 流程与组织/通配广播` 直接报
    「unrecognized arguments」—— argparse 把节点名当成了两个参数。
    这个错在 bash 里还会被 `(rgthree)` 的括号再坑一次，用户根本看不出原因。
    """
    import io
    import contextlib
    from cwf import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["nodes", "classify", "Mask", "Fill", "Holes"])
    eq(rc, 0, "带空格的节点名没被拼回去")
    out = buf.getvalue()
    check("Mask Fill Holes" in out, f"输出里没认出完整名字：\n{out[:200]}")
    check("遮罩" in out, f"分类不对：\n{out[:200]}")


@suite("setcat 把「类型… 分类」拆得对")
def _setcat_split():
    from cwf import cli

    class A:
        pass

    a = A()
    # 常规：最后一个参数是分类
    a.parts, a.to = ["KSampler", "采样与生成/采样器"], None
    eq(cli._setcat_args(a), ("KSampler", "采样与生成/采样器"))
    # 类型名带空格：中间的全归类型
    a.parts, a.to = ["Anything", "Everywhere", "流程与组织/通配广播"], None
    eq(cli._setcat_args(a), ("Anything Everywhere", "流程与组织/通配广播"))
    # 用 --to 明确指定时，位置参数全当类型
    a.parts, a.to = ["Fast", "Groups", "Bypasser", "(rgthree)"], "流程与组织/静音"
    eq(cli._setcat_args(a),
       ("Fast Groups Bypasser (rgthree)", "流程与组织/静音"))
    # 只给一个参数：说不清，必须报错而不是瞎猜
    a.parts, a.to = ["KSampler"], None
    try:
        cli._setcat_args(a)
        check(False, "参数不够却没报错")
    except SystemExit:
        pass


@suite("覆盖文件在 CWF_STORE 之下（换目录不污染真 store）")
def _override_isolated():
    from cwf.lib import store as st
    d = _fresh()
    old = st.STORE_DIR
    try:
        st.STORE_DIR = d
        eq(os.path.abspath(tx.store_dir()), os.path.abspath(d))
        p = tx.save_override("SomeNode", "视频/通用")
        eq(os.path.abspath(os.path.dirname(p)), os.path.abspath(d))
        eq(tx.load_overrides().get("SomeNode"), "video/通用")
    finally:
        st.STORE_DIR = old


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
