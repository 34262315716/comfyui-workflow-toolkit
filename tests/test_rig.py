# -*- coding: utf-8 -*-
"""
测试组 10：负载量化（cwf rig / load / fit）

这一组跟别的组不太一样：**核心断言是拿真实数据对出来的**，不是自己跟自己对。

2026-09-16 在一台真机上做的实测校准：

  ComfyUI 日志里 `prepared for dynamic VRAM loading. NNNNMB Staged`
  与磁盘字节数的对应关系 ——

     19995 MB  ←→  19996 MB   差 +0.0%
     14955 MB  ←→  14957 MB   差 +0.0%
       576 MB  ←→    577 MB   差 +0.2%

  所以"权重项 = 磁盘字节数"这个假设是立得住的，测试就把这条钉死。

另外三条是**踩过的坑**，全部来自同一个工作流的真实输出：

  1. LoRA 被当成独立常驻权重重复计算 → 估高 8.9%
  2. `model_name` 这个控件名在 UpscaleModelLoader 里指 upscale_models，
     按控件名一概而论会漏掉真实存在的模型 → 误报"模型不存在"
  3. extra_model_paths.yaml 的多行块（`key: |`）解析丢了整块 →
     每张工作流的模型全判成不存在
"""
from __future__ import annotations

import os
import tempfile
import textwrap

from harness import Suite, check, eq

from cwf.lib import rig

suite = Suite()
TMP = os.path.join(tempfile.gettempdir(), "cwf_rig_test")


# ================================================================
# 1. extra_model_paths.yaml —— 这块丢了，整个功能就是废的
# ================================================================

@suite.group("extra_model_paths.yaml 解析")
def _extra_yaml():
    text = textwrap.dedent("""\
        comfyui:
            base_path: /srv/comfy
            checkpoints: models/checkpoints
            unet: models/unet
            text_encoders: |
                models/text_encoders
                models/clip

            # 中间夹一行注释，不能打断上面的多行块
            vae: models/vae
        """)
    got = rig._parse_extra_yaml(text)
    sec = got.get("comfyui")
    check(sec is not None, "顶层 section 没解析出来")
    eq(sec.get("base_path"), "/srv/comfy", "base_path")
    eq(sec.get("unet"), "models/unet", "单行值")
    eq(sec.get("vae"), "models/vae", "注释后的值")
    # 关键：多行块一条都不能少
    eq(sec.get("text_encoders"), ["models/text_encoders", "models/clip"],
       "多行块（`key: |`）必须整块保留")


suite.case("多行块的值一条不丢（曾因 `if block:` 假值丢掉整块）")(_extra_yaml)


def _extra_multi_section():
    text = textwrap.dedent("""\
        comfyui:
            base_path: /a
            loras: models/loras
        another:
            base_path: /b
            loras: models/loras
        """)
    got = rig._parse_extra_yaml(text)
    eq(sorted(got.keys()), ["another", "comfyui"], "两个 section 都要在")


suite.case("支持多个顶层 section")(_extra_multi_section)


# ================================================================
# 2. 控件名 → 目录：同名控件在不同节点里含义不同
# ================================================================

@suite.group("模型目录判定")
def _folder_mapping():
    # UpscaleModelLoader 的 model_name 指 upscale_models，不是 unet
    f = rig.NODE_FOLDERS["UpscaleModelLoader"]["model_name"]
    eq(f, ("upscale_models",), "UpscaleModelLoader.model_name 该看 upscale_models")

    # UNETLoader 的 unet_name 要同时搜 unet 和 diffusion_models
    f = rig.NODE_FOLDERS["UNETLoader"]["unet_name"]
    check("unet" in f and "diffusion_models" in f, "UNETLoader 要搜两个目录")

    # ALL_MODEL_KEYS 要把节点级映射里的键也算进去，
    # 否则 model_name 这类只出现在 NODE_FOLDERS 里的控件会被整体漏掉
    check("model_name" in rig.ALL_MODEL_KEYS,
          "model_name 只在 NODE_FOLDERS 里出现过，必须也算模型控件")


suite.case("同名控件按节点类型区分目录")(_folder_mapping)


# ================================================================
# 3. LoRA 是补丁，不额外占常驻权重
# ================================================================

@suite.group("负载估算")
def _lora_is_patch():
    check("lora_name" in rig.PATCH_KEYS, "LoRA 必须算补丁")


suite.case("LoRA 归为补丁（曾重复计算导致估高 8.9%）")(_lora_is_patch)


def _latent_math():
    """潜空间 → 激活项的算式，手算一遍对得上。"""
    # 800×1440、4 通道、单帧：latent = 100×180×4×1×2 = 144000 字节
    w, h, ch, frames = 800, 1440, 4, 1
    lw, lh = int(w / rig.LATENT_DOWN), int(h / rig.LATENT_DOWN)
    expect = lw * lh * ch * frames * 2
    eq(expect, 144000, "手算的潜空间字节数")

    # 视频不能按总帧数线性放大 —— 分块跑，峰值只跟一块有关
    check(rig.FRAME_CHUNK_CAP >= 16, "分块上限要是个合理值")
    eff = min(637, rig.FRAME_CHUNK_CAP)
    eq(eff, rig.FRAME_CHUNK_CAP,
       "637 帧的片子必须被截到分块上限，否则激活项会算成天文数字")


suite.case("潜空间算式对得上，且视频按分块而不是总帧数算")(_latent_math)


def _evaluate_verdicts():
    """余量判定的档位：够 / 偏紧 / 不够，边界要对。"""
    dev = rig.Device(vram_total=8 * rig.GIB, ram_total=32 * rig.GIB)

    # 小负载 → 轻松
    small = rig.LoadReport()
    small.width, small.height = 512, 512
    f = rig.evaluate(small, dev)
    eq(f.vram_ok, True, "小负载该放得下")
    check(f.level in ("轻松", "够用", "偏紧"), "小负载不该判成不够")

    # 权重远超内存 → 不够，且瓶颈点名内存
    big = rig.LoadReport()
    big.models = [rig.ModelRef(node_id=1, node_type="UNETLoader",
                               widget="unet_name", value="x.safetensors",
                               size=40 * rig.GIB, found=True)]
    big.width, big.height = 1024, 1024
    big.weights = 40 * rig.GIB
    f = rig.evaluate(big, dev)
    eq(f.ram_ok, False, "40 GB 权重塞不进 32 GB 内存")
    eq(f.level, "不够", "该判不够")
    eq(f.bottleneck, "内存", "瓶颈该点名内存")
    check(f.streaming_likely, "权重超过显存时应当判定为必然流式加载")

    # 没设备数据时不能瞎判
    f = rig.evaluate(small, rig.Device())
    eq(f.level, "未知", "拿不到设备数据就该说未知，不能编")


suite.case("余量判定：档位与瓶颈点得对，没数据时不瞎判")(_evaluate_verdicts)


# ================================================================
# 4. 设备画像
# ================================================================

@suite.group("设备画像")
def _device_basics():
    d = rig.Device(vram_total=8 * rig.GIB, vram_free=4 * rig.GIB,
                   compute_cap=8.9, ram_total=32 * rig.GIB)
    eq(d.arch, "Ada Lovelace (sm_89)", "compute_cap 8.9 该认成 Ada")
    check(d.vram_usable < d.vram_total,
          "可用显存必须扣掉驱动/CUDA 上下文的固定开销")
    check(d.vram_usable > d.vram_total * 0.8,
          "扣得太狠也不对（真实机器上实测约 94%）")
    eq(rig.Device(compute_cap=0.0).arch, "未知", "拿不到算力就说未知")


suite.case("设备画像：架构识别与可用显存折算")(_device_basics)


def _size_helpers():
    eq(rig.gb(rig.GIB), "1.00 GB", "单位换算")
    eq(rig._fmt_delta(rig.GIB), "+1.00 GB", "正余量带 + 号")
    eq(rig._fmt_delta(-rig.GIB), "-1.00 GB", "负余量带 - 号")


suite.case("数值格式：GB 与正负余量")(_size_helpers)


# ================================================================
# 5. 日志挖掘 —— 实测数据的来源
# ================================================================

@suite.group("日志挖掘（实测数据来源）")
def _mine_logs():
    d = os.path.join(TMP, "logs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "comfyui.log")
    with open(p, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
            [2026-09-16 13:34:41.782] VAE load device: cuda:0
            [2026-09-16 13:34:43.076] Requested to load MiniMaxH3VideoVAE
            [2026-09-16 13:34:43.172] Model MiniMaxH3VideoVAE prepared for dynamic VRAM loading. 2677MB Staged. 0 patches attached. Force pre-loaded 128 weights: 692 KB.
            [2026-09-16 13:36:48.970] 0 models unloaded.
            [2026-09-16 13:36:49.237] Model MiniMaxH3 prepared for dynamic VRAM loading. 19995MB Staged. 208 patches attached.
            [2026-09-16 13:37:00.000] 100%|##########| 8/8 [04:39<00:00, 34.94s/it]
            [2026-09-16 13:40:00.000] Prompt executed in 595.67 seconds
            [2026-09-16 13:41:00.000] 3 models unloaded.
            """))
    facts = rig.mine_logs([p])
    eq(facts.staged.get("MiniMaxH3"), 19995, "抓到 MiniMaxH3 的实测占用")
    eq(facts.staged.get("MiniMaxH3VideoVAE"), 2677, "抓到 VAE 的实测占用")
    eq(len(facts.sit), 1, "抓到一条迭代速度")
    check(abs(facts.sit[0] - 34.94) < 0.01, "速度值要准")
    check(abs(facts.prompt_seconds[-1] - 595.67) < 0.01, "整单耗时")
    # "0 models unloaded" 不算卸载事件，只看非零的
    eq(facts.unload_events, 1, "只有 3 models unloaded 才算一次卸载")


suite.case("从 ComfyUI 日志里挖出真实占用、速度与卸载事件")(_mine_logs)


def _compare_with_log():
    """估算与实测的对照 —— 这是整个模块可信度的来源。

    关键性质：**配对不上就不说话**。日志里存的是这台机器跑过的所有运行的
    记录，拿一条 37 GB 视频运行的记录去"校准"一张 5 GB 的图，会算出
    -86% 的偏差 —— 那个数字纯属胡说八道，比不给校准更糟。
    """
    from cwf.lib import rig as R

    def mk(size_gb, name="m"):
        m = R.ModelRef(node_id=1, node_type="UNETLoader", widget="unet_name",
                       value=name, size=int(size_gb * R.GIB), found=True)
        return m

    load = R.LoadReport()
    load.models = [mk(19.53, "unet.safetensors"),
                   mk(14.61, "te.safetensors")]
    load.weights = sum(m.size for m in load.models)

    # 日志里正好跑过这两个 → 必须配对成功并给出偏差
    facts = R.LogFacts(staged={"MiniMaxH3": 19995, "MiniMaxH3TEModel_": 14956})
    pairs = R.match_staged(load, facts)
    eq(len(pairs), 2, "大小一致的记录应当配上")
    lines = R.compare_with_log(load, facts)
    check(lines, "配对成功时必须给出对照结论")
    check("差" in lines[1], "结论里要有偏差百分比")

    # 日志里是**另一次无关运行** → 一条都配不上，必须闭嘴
    other = R.LogFacts(staged={"SomeOtherModel": 38123})
    eq(R.match_staged(load, other), [], "大小对不上的记录不该硬配")
    eq(R.compare_with_log(load, other), [],
       "拿无关运行记录来校准是胡说八道，必须什么都不说")

    # 一边没有数据同样闭嘴
    eq(R.compare_with_log(R.LoadReport(), facts), [], "估算为空时不硬凑")
    eq(R.compare_with_log(load, R.LogFacts()), [], "没有实测时不硬凑")


suite.case("校准按大小配对；配对不上就闭嘴（曾拿无关运行算出 -86%）")(_compare_with_log)


def main() -> int:
    from harness import run
    return run(suite, verbose="-v" in os.sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
