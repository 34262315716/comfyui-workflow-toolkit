# -*- coding: utf-8 -*-
"""
cwf.taxonomy —— 节点功能分类法

## 为什么不能直接用 ComfyUI 自带的分类

`/object_info` 里每个节点确实带一个 `category` 字段，但它对插件节点来说基本等于
**插件自己的名字**，不是功能：

    Apt_Preset 395   RunningHub 380   T8 356   RES4LYF 290   😺dzNodes 246 ...

按这个分类找节点，等于按"谁做的"找，不是按"能干什么"找。只有 comfy-core 的
原生分类是真·功能路径（`model/sampling/samplers`、`image/upscaling`）。

## 所以这里自己判

三条证据，按可靠性排序：

  1. **端口类型**（最硬）—— 输出 VIDEO 的就是视频节点，跟谁写的无关。
     这是唯一跨插件通用的语义信号。
  2. **类型名**（很强）—— `LoadImage` / `MaskBlur` / `SaveVideo`，按词切分后
     逐个 token 匹配，不是拿 `in` 硬套子串。
  3. **原生分类路径**（兜底）—— 核心节点的英文功能路径，或插件分类的末段。

判定结果落地为 16 个大类 + 任意小类，**每条判定都可解释**（命中哪条规则）。
拿不准就进 `misc`，不猜。

## 你能覆盖它

规则不可能覆盖 7746 个插件节点里的每一个，也不该硬凑。
`~/.cwf/store/categories.txt` 一行一条：

    KSampler = sample/采样器
    MiniMaxH3* = video/模型        # 支持尾部通配

用户覆盖永远优先于任何规则。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .graph import CwfError
from .schema import CACHE_DIR

TAXONOMY_VERSION = 1


# ---------------------------------------------------------------- 16 个大类

#: 大类 id → 中文名。顺序 = 数据流顺序（加载→模型→条件→采样→…→输出），
#: 这样树看起来就像一条工作流的走向。
L1: Dict[str, str] = {
    "load":   "加载输入",
    "model":  "模型与适配",
    "cond":   "条件与提示词",
    "sample": "采样与生成",
    "latent": "潜空间",
    "image":  "图像处理",
    "mask":   "遮罩",
    "video":  "视频",
    "audio":  "音频",
    "threed": "三维",
    "text":   "文本与数据",
    "num":    "数值与逻辑",
    "output": "输出与预览",
    "flow":   "流程与组织",
    "api":    "网络与云",
    "misc":   "未归类",
}
TOP_ORDER: List[str] = list(L1.keys())

#: 大类 id → 它想解决什么问题（给人和 AI 看的一句话）
L1_WHAT: Dict[str, str] = {
    "load":   "把磁盘/网络上的文件读进来：图、视频、音频、模型、文本",
    "model":  "模型本体的加载、合并、打补丁、量化、转换（MODEL/CLIP/VAE/LoRA/ControlNet）",
    "cond":   "提示词编码、条件合并、区域与引导",
    "sample": "采样器、调度器、噪声、引导器 —— 真正生成的那一步",
    "latent": "潜空间的产生与变换",
    "image":  "图像的变换、合成、滤镜、放大、修复",
    "mask":   "遮罩的产生与布尔运算",
    "video":  "视频帧、合成、插帧、视频模型",
    "audio":  "音频的加载、处理、合成、识别",
    "threed": "3D 网格、相机、贴图、渲染",
    "text":   "字符串、JSON、列表、正则、模板",
    "num":    "数学、比较、布尔、开关、计数",
    "output": "保存 / 预览 图像、视频、音频、模型",
    "flow":   "便签、跳线、路由、分组、循环控制、参数控件",
    "api":    "需要联网的：云服务、大模型、下载上传",
    "misc":   "规则没能判定，等你来归位",
}

#: 中文 → id（用户写中文大类也能用）
_ZH2ID: Dict[str, str] = {}
for _k, _v in L1.items():
    _ZH2ID[_v] = _k
    _ZH2ID[_v.replace("与", "")] = _k
    _ZH2ID[_v.replace("与", "/")] = _k
del _k, _v


# ---------------------------------------------------------------- 名称切词

_TOKEN_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
#: 字母↔数字的边界也要切一刀：`Load3D` → Load|3|D，拼接后才还原出 `3d`；
#: 而 `H3Dual` → H|3|Dual，拼出来是 `3dual`，**不会**长出假的 `3d`。
_NUM_RE = re.compile(r"(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def tokens(name: str) -> Set[str]:
    """把类型名切成小写词元（含相邻词元的拼接形）。

    `WanPhantomSubjectToVideo` → {wan, phantom, subject, to, video}

    这一步很关键：用 `'wan' in name.lower()` 会撞上 `Swap`、`Want` 之类；
    切成词元后只匹配完整单词，误判率低一个数量级。

    **同时收集相邻词元的拼接形**，专门对付两种真实存在的写法：

        MiniMaxH3LoRACompatibility…  → Lo|RA 两个词元，拼接后才还原出 `lora`
        Load3D                       → load|3|d，拼接后才还原出 `3d`

    而 `MiniMaxH3DualClockSampler` 拼出来的是 `h3`+`dual`，**不会**拼出假的
    `3d` —— 这正是旧写法 `'3d' in name.lower()` 翻车的地方（`H3Dual` 里真的
    含子串 `3d`，把 102 次使用的采样节点判成了三维节点）。
    """
    s = _CAMEL_RE.sub(" ", name or "")
    s = _NUM_RE.sub(" ", s)
    parts = [t for t in _TOKEN_RE.split(s.lower()) if t]
    out: Set[str] = set(parts)
    for a, b in zip(parts, parts[1:]):
        out.add(a + b)
    return out


# ---------------------------------------------------------------- 节点视图


@dataclass
class View:
    """分类器看到的全部信息（只读快照）。"""
    name: str
    low: str
    toks: Set[str]
    outs: Set[str]
    ins: Set[str]
    cat: str
    pkg: str
    usage: int
    out_node: bool
    squash: str = ""

    def w(self, *words: str) -> bool:
        """名称词元里有任意一个完整单词。"""
        return bool(self.toks & set(words))

    def n(self, *subs: str) -> bool:
        """名称子串（给中文和小写词根用）。"""
        return any(s in self.low for s in subs)

    def o(self, *types: str) -> bool:
        """输出端口里有任意一个类型。"""
        return bool(self.outs & set(types))

    def i(self, *types: str) -> bool:
        return bool(self.ins & set(types))

    def oo(self, *types: str) -> bool:
        """输出端口**只有**这些类型（且非空）。"""
        return bool(self.outs) and self.outs <= set(types)

    def c(self, *subs: str) -> bool:
        """原生分类路径里含子串。"""
        return any(s in self.cat for s in subs)

    def cat_is(self, *paths: str) -> bool:
        """原生分类路径等于某段或以它开头。"""
        return any(self.cat == p or self.cat.startswith(p + "/") for p in paths)

    def p(self, *subs: str) -> bool:
        """来源包名含子串。"""
        return any(s in self.pkg for s in subs)

    def sq(self, *words: str) -> bool:
        """去标点后的整串包含（专治 `Qwen2.5VL`、`llama_cpp` 这种带点/下划线的名字）。"""
        return any(w in self.squash for w in words)


def _ports(items: Sequence[str]) -> Set[str]:
    out: Set[str] = set()
    for it in items or []:
        if not isinstance(it, str):
            continue
        t = it.rsplit(":", 1)[-1].strip().upper()
        if t:
            out.add(t)
    return out


def view_of(name: str, e: Dict[str, Any]) -> View:
    e = e or {}
    return View(
        name=name,
        low=(name or "").lower(),
        toks=tokens(name),
        outs=_ports(e.get("outputs") or []),
        ins=_ports((e.get("link_inputs") or []) + (e.get("opt_inputs") or [])),
        cat=(e.get("category") or "").strip().lower(),
        pkg=(e.get("package") or "").strip().lower(),
        usage=int(e.get("usage") or 0),
        out_node=bool(e.get("output_node")),
        squash=re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (name or "").lower()),
    )


# ---------------------------------------------------------------- 判定结果


@dataclass
class Tax:
    top: str
    sub: str = ""
    rule: str = ""
    conf: str = "med"          # manual / high / med / low / none

    @property
    def zh(self) -> str:
        return L1.get(self.top, self.top)

    @property
    def key(self) -> str:
        return f"{self.top}/{self.sub}" if self.sub else self.top

    @property
    def path_zh(self) -> str:
        return f"{self.zh}/{self.sub}" if self.sub else self.zh

    def as_dict(self) -> Dict[str, str]:
        return {"top": self.top, "sub": self.sub, "rule": self.rule, "conf": self.conf}


# ---------------------------------------------------------------- 规则表

@dataclass
class Rule:
    rid: str
    pred: Callable[[View], bool]
    top: str
    sub: Any = ""              # str 或 (View) -> str
    conf: str = "high"
    why: str = ""

    def fire(self, v: View) -> Optional[Tax]:
        try:
            ok = bool(self.pred(v))
        except Exception:
            return None
        if not ok:
            return None
        sub = self.sub(v) if callable(self.sub) else self.sub
        return Tax(self.top, sub or "", self.rid, self.conf)


def _sub_from_cat(v: View, mapping: Sequence[Tuple[str, str]], default: str = "通用") -> str:
    for needle, sub in mapping:
        if needle in v.cat:
            return sub
    return default


def _sample_sub(v: View) -> str:
    """采样类小类：先看端口（最硬），再看原生分类路径。"""
    if v.o("SIGMAS"):  return "调度器"
    if v.o("SAMPLER"): return "采样器"
    if v.o("GUIDER"):  return "引导器"
    if v.o("NOISE"):   return "噪声"
    if v.o("LATENT") and v.i("MODEL"): return "采样器"
    return _sub_from_cat(v, _SAMPLE_SUB)


#: 采样相关
_SAMPLE_SUB = (("samplers", "采样器"), ("scheduler", "调度器"), ("guider", "引导器"),
               ("noise", "噪声"), ("sigmas", "调度器"), ("sampling", "通用"))

RULES: List[Rule] = [
    # ============================================================ 残缺引用
    # 工作流里有些节点类型名是 UUID —— 那是插件卸载后留下的空壳，
    # 端口、分类、包名全都没有。单独标出来，别混在"未归类"里让人以为规则不行。
    Rule("misc.uuid", lambda v: bool(_UUID_RE.fullmatch(v.name)),
         "misc", "插件卸载残留", "high"),

    # ============================================================ 网络与云
    Rule("api.cloud",
         lambda v: v.p("runninghub", "rh_openapi") or "RH_OPENAPI_CONFIG" in v.ins
         or v.low.startswith("rh_") or v.c("runninghub", "openapi"),
         "api", "云服务 API", "high"),
    Rule("api.llm",
         lambda v: v.c("llm_party", "llm/") or v.w("openai", "gpt", "claude", "gemini",
                                                   "deepseek", "ollama", "mistral",
                                                   "llm", "vlm", "chatglm"),
         "api", "大模型服务", "high"),
    Rule("api.net",
         lambda v: v.w("http", "download", "upload", "webhook", "fetch", "request")
         or v.c("api node", "api/"),
         "api", "网络请求", "high"),

    # ============================================================ 采样组件（端口证据，最硬）
    # 这两条**必须**排在 视频/音频/三维 的"原生分类"规则之前：
    # 端口输出 SAMPLER / SIGMAS 的节点就是采样组件，这是结构性事实。
    # 反例：MiniMaxH3DualClockSamplerT8 的原生分类是 "T8/MiniMax H3/Audio"，
    # 但它的输出是 sampler:SAMPLER, sigmas:SIGMAS —— 它是采样节点，不是音频节点。
    Rule("smp.sigmas", lambda v: v.o("SIGMAS"), "sample", "调度器", "high"),
    Rule("smp.kind", lambda v: v.o("SAMPLER", "GUIDER", "NOISE", "SAMPLING"),
         "sample", "采样组件", "high"),

    # ============================================================ 三维
    Rule("3d.cat.mesh", lambda v: v.c("3d/mesh"), "threed", "网格 mesh", "high"),
    Rule("3d.cat.tex", lambda v: v.c("3d/texture"), "threed", "贴图 texture", "high"),
    Rule("3d.cat", lambda v: v.cat_is("3d") or v.c("3d/"), "threed", "通用", "high"),
    Rule("3d.port",
         lambda v: v.o("MESH", "FILE_3D_GLB", "FILE_3D_FBX", "LOAD3D_CAMERA",
                       "LOAD3D_IMAGE", "LOAD3D_VIDEO", "LOAD3D_OPTIONS"),
         "threed", "通用", "high"),
    Rule("3d.name",
         lambda v: v.w("mesh", "glb", "fbx", "voxel", "blender", "pbr", "uv", "render",
                       "3d")
         or v.n("三维"),
         "threed", "通用", "med"),

    # ============================================================ 输出与预览
    Rule("out.save.video",
         lambda v: v.n("save", "export", "write")
         and (v.o("VIDEO", "COMFYTV_VIDEO") or v.i("VIDEO") or v.w("video", "animation")),
         "output", "视频", "high"),
    Rule("out.save.audio",
         lambda v: v.n("save", "export", "write")
         and (v.i("AUDIO") or v.w("audio", "sound", "wav", "mp3", "flac", "music")),
         "output", "音频", "high"),
    Rule("out.save.model",
         lambda v: v.n("save", "export", "write")
         and (v.i("MODEL", "CLIP", "VAE", "CONTROL_NET") or v.w("model", "lora",
                                                                "checkpoint", "latent")),
         "output", "模型 / 潜空间", "high"),
    Rule("out.save.3d",
         lambda v: v.n("save", "export", "write") and v.w("mesh", "glb", "obj", "3d"),
         "output", "三维", "high"),
    Rule("out.save.data",
         lambda v: v.n("save", "export", "write")
         and (v.oo("STRING", "DICT", "LIST", "JSON") or v.w("text", "json", "csv",
                                                            "caption", "metadata", "data")),
         "output", "文本 / 数据", "high"),
    Rule("out.save.image", lambda v: v.n("save", "export", "write"),
         "output", "图像", "med"),

    # hmm: `write` 出现在 `WriteText`? 保留

    Rule("out.preview",
         lambda v: v.n("preview", "show", "display", "viewer") or v.n("预览", "查看"),
         "output", "预览", "high"),

    # 静音/旁路/广播这类"画布上的开关"在前端常被标成输出节点，
    # 所以必须排在上面的 out.node 之前，否则会被误当输出节点。
    Rule("flow.mute",
         lambda v: (v.w("mute", "muter", "bypass", "bypasser", "broadcast", "broadcaster")
                    or v.n("忽略", "静音", "屏蔽", "旁通"))
         # 带 bypass 开关的模型/LoRA 加载器仍然是模型节点：
         # `LoraLoaderBypassModelOnly` 该去模型类，不该被当成画布上的静音开关。
         and not v.w("loader", "lora", "model", "checkpoint", "unet", "clip", "vae",
                     "controlnet"),
         "flow", "静音 / 旁路 / 广播", "high"),
    Rule("flow.anywhere",
         lambda v: v.cat_is("everywhere") or v.n("anything everywhere"),
         "flow", "通配广播", "high"),
    Rule("out.node", lambda v: v.out_node and not v.outs, "output", "输出节点", "high"),

    # ============================================================ 流程与组织（无歧义部分）
    Rule("flow.note",
         lambda v: v.w("note", "comment", "annotat", "markdown", "label")
         or v.n("注释", "便签", "说明") and not v.outs,
         "flow", "便签注释", "high"),
    Rule("flow.reroute",
         lambda v: v.w("reroute", "router", "junction") or v.n("跳线", "中转"),
         "flow", "跳线", "high"),
    Rule("flow.setget",
         lambda v: v.low in ("setnode", "getnode", "set node", "get node")
         or v.n("setnode", "getnode", "_set_", "_get_"),
         "flow", "跳线 Set/Get", "high"),

    # ============================================================ 加载输入
    Rule("load.model",
         lambda v: v.w("load", "loader", "read", "reader")
         and v.w("model", "lora", "checkpoint", "unet", "clip", "vae", "controlnet",
                 "ipadapter", "gguf", "clipvision", "diffusion", "upscaler",
                 "style_model", "gligen", "photomaker", "instantid"),
         "model", lambda v: _load_model_sub(v), "high"),
    Rule("load.file",
         lambda v: v.w("load", "loader", "read", "reader", "import", "fetch")
         and v.o("IMAGE", "VIDEO", "AUDIO", "MASK", "MESH", "FILE_3D_GLB", "FILE_3D_FBX",
                 "STRING", "DICT", "LIST", "JSON", "COMFYTV_VIDEO", "COMFYTV_IMAGE"),
         "load", lambda v: _load_sub(v), "high"),

    # ============================================================ 视频
    Rule("vid.cat", lambda v: v.c("video"), "video", lambda v: _video_sub(v), "high"),
    Rule("vid.port",
         lambda v: v.o("VIDEO", "COMFYTV_VIDEO", "WANVIDIMAGE_EMBEDS", "WANVIDEOTEXTEMBEDS",
                       "WANVIDEOMODEL", "FRAMEPACK"),
         "video", lambda v: _video_sub(v), "high"),
    Rule("vid.frames_audio",
         lambda v: v.o("IMAGE") and v.o("AUDIO"),
         "video", lambda v: _video_sub(v), "med"),
    Rule("vid.name",
         lambda v: v.w("video", "vhs", "wan", "frames", "fps", "interpolate", "rife",
                       "film", "animate", "motion", "sequence", "temporal")
         or v.n("视频", "帧"),
         "video", lambda v: _video_sub(v), "med"),

    # ============================================================ 音频
    Rule("aud.cat", lambda v: v.c("audio"), "audio", lambda v: _audio_sub(v), "high"),
    Rule("aud.port", lambda v: v.o("AUDIO") or v.i("AUDIO"), "audio", "通用", "med"),
    Rule("aud.name",
         lambda v: v.w("audio", "sound", "tts", "speech", "voice", "vocal", "whisper",
                       "mel", "spectrogram", "music", "song", "beat", "sampler_audio")
         or v.n("音频", "语音", "音乐", "声音", "发音"),
         "audio", "通用", "med"),

    # ============================================================ 条件与提示词
    Rule("cond.encode",
         lambda v: v.o("CONDITIONING") and v.i("CLIP"),
         "cond", "文本编码", "high"),
    Rule("cond.name.encode",
         lambda v: v.n("textencode", "text_encode", "encodetext", "promptencode",
                       "encode_prompt"),
         "cond", "文本编码", "high"),
    Rule("cond.port", lambda v: v.o("CONDITIONING"), "cond", "条件处理", "high"),
    Rule("cond.name",
         lambda v: v.w("prompt", "conditioning") or v.n("conditioning", "提示词", "条件"),
         "cond", "条件处理", "med"),

    # ============================================================ 采样与生成
    Rule("smp.cat",
         lambda v: v.c("sampling", "samplers", "schedulers", "guiders")
         and not v.o("MODEL", "CLIP", "VAE"),
         "sample", _sample_sub, "high"),
    Rule("smp.name",
         lambda v: (v.w("sampler", "scheduler", "guider", "sigmas", "noise", "denoise",
                        "sampling", "steps", "cfg_scale")
                    or v.n("采样", "调度", "噪声", "步数"))
         and not v.o("MODEL", "CLIP", "VAE"),
         "sample", _sample_sub, "med"),
    Rule("smp.latent_model", lambda v: v.o("LATENT") and v.i("MODEL"),
         "sample", "采样器", "med"),

    # ============================================================ VAE 解码/编码（特例，先于 image/latent）
    Rule("img.decode",
         lambda v: v.n("decode") and v.i("LATENT") and v.o("IMAGE"),
         "image", "解码", "high"),
    Rule("lat.encode",
         lambda v: v.n("encode") and v.i("IMAGE") and v.o("LATENT"),
         "latent", "编码", "high"),

    # ============================================================ 潜空间
    Rule("lat.name", lambda v: v.w("latent") or v.n("潜空间", "潜变量"),
         "latent", lambda v: _latent_sub(v), "med"),
    Rule("lat.port", lambda v: v.o("LATENT"), "latent", lambda v: _latent_sub(v), "med"),
    Rule("lat.cat", lambda v: v.c("latent"), "latent", lambda v: _latent_sub(v), "med"),

    # ============================================================ 遮罩
    Rule("msk.detect",
         lambda v: v.w("sam", "detector", "ultralytics", "bbox", "yolo", "grounding",
                       "detect", "segment", "segs", "clip_seg")
         or v.n("检测", "分割"),
         "mask", "分割 / 检测", "med"),
    Rule("msk.name",
         lambda v: v.w("mask", "matte", "alpha", "inpaint_mask", "segm")
         or v.n("遮罩", "蒙版", "抠图"),
         "mask", lambda v: _mask_sub(v), "high"),
    Rule("msk.only",
         lambda v: v.o("MASK") and not v.o("IMAGE", "VIDEO", "LATENT", "MODEL",
                                           "CONDITIONING", "AUDIO", "STRING"),
         "mask", "通用", "med"),
    Rule("msk.cat", lambda v: v.c("mask"), "mask", lambda v: _mask_sub(v), "med"),

    # ============================================================ 图像
    Rule("img.analysis",
         lambda v: v.i("IMAGE")
         and (v.w("size", "width", "height", "resolution", "aspect", "dimension",
                  "analysis", "stats", "measure")
              or v.n("尺寸", "分辨率"))
         # 名字里带 scale/resize 的是"改尺寸"的节点，不是"读尺寸"的节点：
         # `ImageScaleByAspectRatio` 该去放大/缩放，不该被 aspect 拐进分析类。
         and not v.w("scale", "resize", "upscale", "upscaler", "crop", "resample"),
         "image", "分析 / 测量", "med"),
    Rule("img.port", lambda v: v.o("IMAGE", "COMFYTV_IMAGE"),
         "image", lambda v: _image_sub(v), "med"),
    Rule("img.name",
         lambda v: v.w("image", "img", "upscale", "resize", "scale", "crop", "blur",
                       "sharpen", "color", "filter", "inpaint", "outpaint", "segment",
                       "face", "detailer", "tile", "pixel", "composite", "blend",
                       "draw", "paint", "paste", "stitch", "grid", "thumbnail",
                       "watermark", "brightness", "contrast", "saturation", "noise_image",
                       "transform", "rotate", "flip", "pad", "border")
         or v.n("图像", "图片", "放大", "裁剪", "缩放"),
         "image", lambda v: _image_sub(v), "med"),
    Rule("img.cat", lambda v: v.c("image"), "image", lambda v: _image_sub(v), "med"),

    # ============================================================ 模型与适配
    Rule("mdl.vae", lambda v: v.o("VAE"), "model", "VAE", "high"),
    Rule("mdl.clip",
         lambda v: v.o("CLIP", "CLIP_VISION", "CLIP_VISION_OUTPUT", "CLIP_VISION_STYLE"),
         "model", "文本 / 视觉编码器", "high"),
    Rule("mdl.cnet", lambda v: v.o("CONTROL_NET") or v.w("controlnet", "control_net"),
         "model", "ControlNet", "high"),
    Rule("mdl.lora", lambda v: v.o("LORA_STACK") or v.w("lora") or v.n("罗拉"),
         "model", "LoRA", "high"),
    Rule("mdl.adapter",
         lambda v: v.o("IPADAPTER", "STYLE_MODEL", "GLIGEN", "PHOTOMAKER", "INSTANTID",
                       "UPSCALE_MODEL", "CLIP_VISION_MODEL"),
         "model", "适配器", "high"),
    Rule("mdl.model_out",
         lambda v: v.o("MODEL") and not v.i("MODEL"),
         "model", "模型加载", "high"),
    Rule("mdl.model_patch",
         lambda v: v.o("MODEL") and v.i("MODEL"),
         "model", lambda v: ("模型合并" if v.w("merge", "combine", "add", "concat")
                             else "模型补丁"), "high"),
    Rule("mdl.name",
         lambda v: v.w("model", "checkpoint", "unet", "gguf", "merge", "patch", "quant",
                       "fp8", "gguf_loader")
         or v.n("模型", "合并", "量化"),
         "model", "通用", "med"),
    Rule("mdl.cat", lambda v: v.cat_is("model", "loaders", "advanced"),
         "model", lambda v: _model_sub(v), "med"),

    # ============================================================ 文本与数据
    Rule("txt.only", lambda v: v.oo("STRING", "DICT", "LIST", "JSON", "COMBO", "OPTIONS",
                                    "BASIC_PIPE", "PIPE_LINE"),
         "text", lambda v: _text_sub(v), "med"),
    Rule("txt.name",
         lambda v: v.w("string", "text", "json", "regex", "parse", "concat", "join",
                       "split", "template", "format", "replace", "trim", "substring",
                       "md5", "base64", "csv", "yaml", "xml", "dict", "list", "csv_",
                       "search", "extract", "caption", "translate")
         or v.n("文本", "字符串", "正则", "翻译"),
         "text", lambda v: _text_sub(v), "med"),
    Rule("txt.cat", lambda v: v.cat_is("text", "utils/string"), "text", "通用", "med"),

    # ============================================================ 数值与逻辑
    Rule("num.name",
         lambda v: v.w("math", "compare", "boolean", "switch", "toggle", "counter",
                       "random", "seed", "int", "integer", "float", "number", "clamp",
                       "lerp", "calc", "logic", "gate", "greater", "less", "equal",
                       "range", "scale_int", "multiply", "divide", "sum", "average",
                       "value", "constant", "primitive")
         or v.n("数学", "数值", "比较", "开关", "计数"),
         "num", "通用", "med"),
    Rule("num.only",
         lambda v: v.oo("INT", "FLOAT", "BOOLEAN", "NUMBER", "SEED", "*"),
         "num", "通用", "med"),
    Rule("num.cat", lambda v: v.cat_is("math", "logic") or v.c("math/", "logic/"),
         "num", "通用", "med"),

    # ============================================================ 自带一整套工作流引擎的插件包
    # （比如 minimax-h3-T8：331 种节点，plan/workspace/timeline/ledger 全是它自己的
    #  内部数据类型，端口判不出来，只能按"这个包是干嘛的"整体归位）
    Rule("pkg.suite",
         lambda v: v.p("minimax-h3", "minimaxh3"),
         "video", "视频工程套件", "low"),

    # 带点号/下划线的多模态模型名（Qwen2.5VL、llama_cpp_*）切词后拿不到品牌名，
    # 按去标点的整串再判一次。
    Rule("loose.llm",
         lambda v: v.sq("qwen25vl", "qwen3vl", "qwen2vl", "qwenimagevl", "llamacpp",
                        "llama_cpp", "minicpm", "internvl", "florence", "visionlanguage"),
         "api", "大模型服务", "low"),

    # ============================================================ 流程与组织（宽口径，放最后）
    Rule("flow.control",
         lambda v: v.w("loop", "foreach", "for_each", "while", "queue", "interrupt",
                       "sleep", "delay", "trigger", "cache", "mute", "bypass", "batch_"
                       "control", "enqueue", "schedule_control")
         or v.n("循环", "队列", "缓存"),
         "flow", "循环 / 缓存", "low"),
    Rule("flow.ui",
         lambda v: v.w("widget", "panel", "ui", "layout", "align", "group", "bookmark",
                       "color_picker", "slider", "progress")
         or v.n("面板", "分组", "布局"),
         "flow", "界面 / 参数", "low"),
    Rule("flow.any",
         lambda v: v.o("*") or v.oo("*"),
         "flow", "通配 / 路由", "low"),
]


#: 最后的兜底：只在上面所有规则都没命中时才用，置信度一律 low。
#: 专治本机这 115 种「压根不在 /object_info 里」的节点 —— 前端虚拟节点、
#: 插件已卸载、老工作流残留。它们连端口都读不到，端口证据为零，
#: 只能退化成按名字猜。（顺序 = 优先级）
LOOSE: List[Tuple[str, str, Sequence[str]]] = [
    ("cond",   "条件处理", ("conditioning", "prompt", "encode", "提示词", "条件")),
    ("sample", "通用", ("sampler", "sample", "noise", "scheduler", "采样", "噪声")),
    ("latent", "通用", ("latent", "潜空间")),
    ("mask",   "通用", ("mask", "matte", "segment", "遮罩", "蒙版")),
    ("threed", "通用", ("mesh", "3d", "glb", "fbx", "三维")),
    ("audio",  "通用", ("audio", "sound", "voice", "music", "音频", "语音")),
    ("video",  "通用", ("video", "frame", "motion", "视频")),
    ("image",  "通用", ("image", "img", "photo", "picture", "pixel", "resolution",
                        "hue", "color", "compress", "thumbnail", "watermark", "style",
                        "pulid", "face", "tiled", "warp", "sharpness", "calibrat",
                        "size", "preset",
                        "图像", "图片", "放大")),
    ("model",  "通用", ("model", "unet", "lora", "checkpoint", "clip", "vae", "swap",
                        "block", "nunchaku", "dit", "attn", "attention", "quant",
                        "模型", "合并")),
    ("api",    "大模型服务", ("vision", "vlm", "llm", "cloud", "liblib", "web", "api")),
    ("text",   "通用", ("text", "string", "json", "character", "caption", "token",
                        "count", "tag", "booru",
                        "文本", "字符串")),
    ("num",    "通用", ("number", "math", "int", "float", "bool", "seed", "数值")),
    ("output", "通用", ("save", "preview", "export")),
    ("flow",   "工具", ("memory", "cache", "clean", "free", "utility", "util", "helper",
                        "tool", "editor", "track", "option", "缓存", "清理")),
    ("load",   "通用", ("load", "read", "import")),
]


def _loose(v: View) -> Optional[Tax]:
    for top, sub, words in LOOSE:
        for w in words:
            if all(ord(c) < 128 for c in w):
                if w in v.toks:
                    return Tax(top, sub, f"loose:{w}", "low")
            elif w in v.low:
                return Tax(top, sub, f"loose:{w}", "low")
    return None


# ---------------------------------------------------------------- 小类判定

def _load_model_sub(v: View) -> str:
    if v.w("lora"):        return "LoRA"
    if v.w("controlnet"):  return "ControlNet"
    if v.w("vae"):         return "VAE"
    if v.w("clip", "clipvision", "text_encoder"): return "文本编码器"
    if v.w("ipadapter", "instantid", "photomaker"): return "适配器"
    if v.w("upscaler", "upscale_model"): return "放大模型"
    return "模型加载"


def _load_sub(v: View) -> str:
    if v.i("AUDIO") or v.w("audio", "sound", "wav", "music"):  return "音频"
    if v.o("VIDEO", "COMFYTV_VIDEO") or v.w("video", "vhs", "animation"): return "视频"
    if v.o("MESH", "FILE_3D_GLB", "FILE_3D_FBX") or v.w("mesh", "3d", "glb"): return "3D"
    if v.o("IMAGE", "COMFYTV_IMAGE", "MASK") or v.w("image", "img", "mask"): return "图像"
    if v.o("STRING", "DICT", "LIST", "JSON") or v.w("text", "json", "csv"): return "文本"
    return "通用"


def _video_sub(v: View) -> str:
    if v.w("interpolate", "rife", "film", "flowframes"): return "插帧"
    if v.w("combine", "merge", "concat", "join", "sequence"): return "合成"
    if v.w("load", "reader", "import"): return "加载"
    if v.w("save", "export", "write"):  return "保存"
    if v.w("audio", "sound", "music", "speech"): return "声画同步"
    if v.w("model", "block", "lora", "sampler", "embed"): return "视频模型"
    return "通用"


def _audio_sub(v: View) -> str:
    if v.w("tts", "speech", "voice", "vocal", "talk", "speak"): return "语音合成"
    if v.w("whisper", "transcri", "asr", "recogni"): return "语音识别"
    if v.w("music", "song", "melody", "beat", "ace", "audio_gen"): return "音乐生成"
    if v.w("load", "reader"): return "加载"
    if v.w("save", "export"):  return "保存"
    if v.w("mel", "spectrogram", "fft"):  return "频谱"
    return "通用"


def _latent_sub(v: View) -> str:
    if v.w("empty", "create", "generate"):  return "创建"
    if v.w("upscale", "scale", "resize"):   return "缩放"
    if v.w("batch", "repeat", "concat"):    return "批处理"
    if v.w("keyframe", "timestep"):         return "关键帧"
    if v.w("encode"):                       return "编码"
    if v.w("decode"):                       return "解码"
    return "运算"


def _mask_sub(v: View) -> str:
    if v.w("segment", "sam", "detect", "bbox", "grounding"):  return "分割 / 检测"
    if v.w("blur", "feather", "grow", "shrink", "smooth", "morph"): return "边缘修整"
    if v.w("composite", "combine", "merge", "add", "union", "overlay"): return "布尔运算"
    if v.w("invert", "not_", "flip"):       return "取反"
    if v.w("load", "reader"):               return "加载"
    if v.w("to_image", "toimage", "image_to"): return "转换"
    return "通用"


def _image_sub(v: View) -> str:
    if v.w("upscale", "resize", "scale", "interpolat") or v.n("放大"):  return "放大 / 缩放"
    if v.w("crop", "pad", "border", "tile", "stitch") or v.n("裁剪"):   return "裁剪 / 拼接"
    if v.w("inpaint", "outpaint", "detailer", "face", "restore"):       return "修复 / 重绘"
    if v.w("mask", "alpha", "segment"):                                 return "遮罩相关"
    if v.w("blur", "sharpen", "filter", "glow", "effect", "vfx"):       return "滤镜 / 特效"
    if v.w("color", "brightness", "contrast", "saturation", "hue", "gamma"): return "色彩"
    if v.w("composite", "blend", "overlay", "paste", "merge", "batch"): return "合成 / 批处理"
    if v.w("load", "save", "preview", "export"):                        return "加载 / 保存"
    if v.w("analysis", "detect", "caption", "segment", "bbox"):         return "分析 / 检测"
    if v.w("draw", "paint", "text", "watermark"):                       return "绘制 / 文字"
    if v.w("decode"):                                                   return "解码"
    return "变换"


def _model_sub(v: View) -> str:
    if v.w("merge", "combine", "concat"):   return "模型合并"
    if v.w("patch", "lora", "control"):     return "模型补丁"
    if v.w("quant", "fp8", "gguf"):         return "量化"
    if v.w("sampling", "guider", "sigmas"): return "采样组件"
    return "通用"


def _text_sub(v: View) -> str:
    if v.w("json"):                          return "JSON"
    if v.w("regex", "match", "extract", "search", "replace"): return "正则 / 抽取"
    if v.w("concat", "join", "split", "trim", "substring", "format"): return "字符串操作"
    if v.w("caption", "translate", "prompt"): return "文案"
    if v.w("list", "dict", "csv", "yaml"):   return "结构化数据"
    return "通用"


# ---------------------------------------------------------------- 用户覆盖

CATEGORY_FILE = "categories.txt"

_OVERRIDE_HEADER = """# 节点功能分类覆盖 —— 一行一条：类型名 = 大类[/小类]
#
# 大类必须是这 16 个之一（写中文也行）：
#   加载输入 模型与适配 条件与提示词 采样与生成 潜空间 图像处理 遮罩
#   视频 音频 三维 文本与数据 数值与逻辑 输出与预览 流程与组织 网络与云 未归类
#
# 例子（# 开头是注释）：
#   KSampler = 采样与生成/采样器
#   MiniMaxH3* = 视频/视频模型        <- 支持尾部通配，一条管一族
#
# 这里的判定**永远优先于自动规则**。改完立刻生效，不用重建缓存。
"""


def store_dir(root: Optional[str] = None) -> str:
    from .store import STORE_DIR
    return os.path.abspath(root or STORE_DIR)


def category_file(root: Optional[str] = None) -> str:
    return os.path.join(store_dir(root), CATEGORY_FILE)


def ensure_category_file(root: Optional[str] = None) -> str:
    p = category_file(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f:
            f.write(_OVERRIDE_HEADER)
    return p


def norm_target(text: str) -> Tuple[str, str]:
    """把 `图像处理/放大` 或 `image/放大` 或 `图像` 规范成 (top_id, sub)。"""
    raw = (text or "").strip()
    if not raw:
        raise CwfError("分类不能为空。可用大类：" + "、".join(L1.values()))
    parts = [p.strip() for p in raw.replace("\\", "/").split("/") if p.strip()]
    head = parts[0]
    top = head if head in L1 else _ZH2ID.get(head)
    if top is None:
        low = head.lower()
        top = low if low in L1 else None
    if top is None:
        raise CwfError(
            f"不认识的大类 {head!r}。可用：" + "、".join(f"{v}({k})" for k, v in L1.items()))
    return top, "/".join(parts[1:])


def load_overrides(root: Optional[str] = None) -> Dict[str, str]:
    """读覆盖文件：{模式: 'top/sub'}。"""
    p = category_file(root)
    out: Dict[str, str] = {}
    if not os.path.exists(p):
        return out
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if not k or not v:
                    continue
                try:
                    top, sub = norm_target(v)
                except CwfError:
                    continue                      # 写错的行跳过，不影响其它行
                out[k] = f"{top}/{sub}" if sub else top
    except Exception:
        return {}
    return out


def save_override(type_: str, target: str, root: Optional[str] = None) -> str:
    p = ensure_category_file(root)
    top, sub = norm_target(target)
    val = f"{top}/{sub}" if sub else top
    lines: List[str] = []
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    hit = False
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.split("=", 1)[0].strip() == type_:
            lines[idx] = f"{type_} = {val}"
            hit = True
            break
    if not hit:
        lines.append(f"{type_} = {val}")
    ensure_category_file(root)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    return p


def _match_override(name: str, overrides: Dict[str, str]) -> Optional[str]:
    if not overrides:
        return None
    if name in overrides:
        return overrides[name]
    low = name.lower()
    best: Optional[Tuple[int, str]] = None
    for pat, val in overrides.items():
        if "*" not in pat:
            if pat.lower() == low:
                return val
            continue
        rx = re.escape(pat.lower()).replace(r"\*", ".*")
        if re.fullmatch(rx, low):
            if best is None or len(pat) > best[0]:
                best = (len(pat), val)
    return best[1] if best else None


# ---------------------------------------------------------------- 分类

_RULES_BY_ID: Dict[str, Rule] = {r.rid: r for r in RULES}


def classify(name: str, entry: Optional[Dict[str, Any]] = None,
             overrides: Optional[Dict[str, str]] = None) -> Tax:
    """判定一个节点的功能分类。overrides 为空时自动读覆盖文件。"""
    if overrides is None:
        overrides = load_overrides()
    hit = _match_override(name, overrides)
    if hit:
        top, _, sub = hit.partition("/")
        return Tax(top, sub, "override", "manual")
    v = view_of(name, entry or {})
    for r in RULES:
        t = r.fire(v)
        if t is not None:
            return t
    t = _loose(v)
    if t is not None:
        return t
    return Tax("misc", "", "fallback", "none")


def classify_name(name: str, entry: Optional[Dict[str, Any]] = None) -> Tax:
    return classify(name, entry)


def explain(name: str, entry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给 `cwf nodes classify` 用：判定 + 依据。"""
    overrides = load_overrides()
    hit = _match_override(name, overrides)
    v = view_of(name, entry or {})
    steps: List[Dict[str, Any]] = []
    tax = Tax("misc", "", "fallback", "none")
    if hit:
        top, _, sub = hit.partition("/")
        tax = Tax(top, sub, "override", "manual")
    else:
        for r in RULES:
            ok = False
            try:
                ok = bool(r.pred(v))
            except Exception:
                ok = False
            steps.append({"rule": r.rid, "hit": ok, "top": r.top})
            if ok:
                tax = r.fire(v) or tax
                break
        else:
            lt = _loose(v)
            steps.append({"rule": "loose", "hit": lt is not None,
                          "top": lt.top if lt else ""})
            if lt is not None:
                tax = lt
    return {
        "type": name,
        "tax": tax.as_dict(),
        "path": tax.path_zh,
        "view": {
            "tokens": sorted(v.toks)[:24],
            "outputs": sorted(v.outs),
            "inputs": sorted(v.ins),
            "native_category": v.cat,
            "package": v.pkg,
            "usage": v.usage,
        },
        "matched_rule": tax.rule,
        "trace": steps,
        "override": hit,
    }


# ---------------------------------------------------------------- 统计 / 导出


def apply_to_catalog(cat: Dict[str, Any], root: Optional[str] = None) -> Dict[str, Any]:
    """把分类写进节点库的每一个节点（原地改 cat['types']），并返回汇总。"""
    overrides = load_overrides(root)
    types = cat.get("types") or {}
    per_top: Dict[str, int] = {k: 0 for k in TOP_ORDER}
    per_top_used: Dict[str, int] = {k: 0 for k in TOP_ORDER}
    per_sub: Dict[str, Dict[str, int]] = {}
    per_conf: Dict[str, int] = {}
    for t, e in types.items():
        if not isinstance(e, dict):
            continue
        tx = classify(t, e, overrides)
        e["tax"] = tx.top
        e["tax_zh"] = tx.zh
        e["tax_sub"] = tx.sub
        e["tax_path"] = tx.key
        e["tax_path_zh"] = tx.path_zh
        e["tax_rule"] = tx.rule
        e["tax_conf"] = tx.conf
        per_top[tx.top] = per_top.get(tx.top, 0) + 1
        if e.get("usage"):
            per_top_used[tx.top] = per_top_used.get(tx.top, 0) + 1
        d = per_sub.setdefault(tx.top, {})
        key = tx.sub or "（未细分）"
        d[key] = d.get(key, 0) + 1
        per_conf[tx.conf] = per_conf.get(tx.conf, 0) + 1
    total = sum(per_top.values())
    return {
        "total": total,
        "by_top": {k: per_top.get(k, 0) for k in TOP_ORDER},
        "by_top_used": {k: per_top_used.get(k, 0) for k in TOP_ORDER},
        "by_sub": {k: dict(sorted(v.items(), key=lambda kv: -kv[1]))
                   for k, v in per_sub.items()},
        "by_conf": dict(sorted(per_conf.items(), key=lambda kv: -kv[1])),
        "overrides": len(overrides),
        "coverage": (total - per_top.get("misc", 0)) / total if total else 0.0,
    }


def _line(t: str, e: Dict[str, Any], with_ports: bool = True) -> str:
    pkg = e.get("package") or "?"
    use = e.get("usage") or 0
    tag = f"×{use}" if use else " ·"
    parts = [f"- **{t}**", f"`{pkg}`", tag]
    if with_ports:
        ins = ", ".join(e.get("link_inputs") or []) or "∅"
        outs = ", ".join(e.get("outputs") or []) or "∅"
        parts.append(f"{ins} → {outs}")
    if e.get("deprecated"):
        parts.append("（已弃用）")
    return " ".join(parts)


def export_atlas(cat: Dict[str, Any], out_dir: str, used_only: bool = False) -> Dict[str, Any]:
    """把全部（或只用过的）节点按新分类导成可读的图鉴。

    产出：
        <out_dir>/index.md          总览 + 16 类目录 + 怎么看
        <out_dir>/NN_大类.md         每类一个文件，按小类分节
        <out_dir>/node_index.json   机器用的紧凑索引
    """
    types = cat.get("types") or {}
    if used_only:
        types = {t: e for t, e in types.items() if e.get("usage")}
    os.makedirs(out_dir, exist_ok=True)

    grouped: Dict[str, Dict[str, List[Tuple[str, Dict[str, Any]]]]] = {}
    for t, e in types.items():
        top = e.get("tax") or "misc"
        sub = e.get("tax_sub") or "（未细分）"
        grouped.setdefault(top, {}).setdefault(sub, []).append((t, e))

    written: List[str] = []
    for idx, top in enumerate(TOP_ORDER, 1):
        subs = grouped.get(top)
        if not subs:
            continue
        n_total = sum(len(v) for v in subs.values())
        lines = [
            f"# {idx:02d} {L1[top]}",
            "",
            f"> {L1_WHAT.get(top, '')}",
            "",
            f"共 **{n_total}** 种节点，分 {len(subs)} 个小类。",
            "",
        ]
        for sub in sorted(subs, key=lambda s: (-len(subs[s]), s)):
            rows = sorted(subs[sub], key=lambda kv: (-(kv[1].get("usage") or 0), kv[0]))
            lines.append(f"## {sub}（{len(rows)}）")
            lines.append("")
            lines.extend(_line(t, e) for t, e in rows)
            lines.append("")
        p = os.path.join(out_dir, f"{idx:02d}_{_safe(top)}.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip("\n") + "\n")
        written.append(p)

    # 总览
    idx_lines = [
        "# 本机 ComfyUI 节点图鉴",
        "",
        f"建于 {cat.get('built_str', '')} · 工作流库扫描 {cat.get('totals', {}).get('workflows_scanned', '?')} 个"
        f" · 节点实例 {cat.get('totals', {}).get('node_instances', '?')} 个",
        "",
        f"**共 {len(types)} 种节点类型**"
        + ("（只列你用过的）" if used_only else "（全部）"),
        "",
        "分类是**按功能**走的，不是按插件名 —— 这样你找的是「谁能把图放大」，",
        "而不是「哪个插件里有放大」。判定依据是端口类型 + 类型名 + 原生分类路径。",
        "",
        "| # | 大类 | 种数 | 用过的 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for idx, top in enumerate(TOP_ORDER, 1):
        subs = grouped.get(top) or {}
        n = sum(len(v) for v in subs.values())
        nu = sum(1 for v in subs.values() for _, e in v if e.get("usage"))
        if not n:
            continue
        idx_lines.append(f"| {idx:02d} | [{L1[top]}]({idx:02d}_{_safe(top)}.md) | {n} | {nu} | "
                         f"{L1_WHAT.get(top, '')} |")
    idx_lines += [
        "",
        "---",
        "",
        "## 怎么用",
        "",
        "```bash",
        "cwf nodes tree                     # 不翻文件，直接看分类树",
        "cwf nodes tree --cat 图像处理        # 只看一类",
        "cwf nodes list --cat image --used-only   # 用过的图像节点",
        "cwf nodes classify VAEDecode       # 它是怎么被判成这个类的",
        "```",
        "",
        "## 判错了怎么办",
        "",
        "分类不是猜的，但 7746 个插件节点不可能条条都对。改一行就永久生效：",
        "",
        "```bash",
        "cwf nodes setcat 某节点 图像处理/放大",
        "```",
        "",
        f"写入 `{category_file()}`，纯文本，你也能直接改。",
        "",
    ]
    ip = os.path.join(out_dir, "index.md")
    with open(ip, "w", encoding="utf-8") as f:
        f.write("\n".join(idx_lines).rstrip("\n") + "\n")
    written.insert(0, ip)

    # 紧凑机读索引
    compact: Dict[str, Dict[str, Dict[str, List[Any]]]] = {}
    for t, e in types.items():
        top = e.get("tax") or "misc"
        sub = e.get("tax_sub") or ""
        compact.setdefault(top, {}).setdefault(sub, {})[t] = [
            e.get("package") or "", e.get("usage") or 0]
    jp = os.path.join(out_dir, "node_index.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump({"built": cat.get("built_str", ""), "total": len(types),
                   "classes": compact}, f, ensure_ascii=False)
    written.append(jp)
    return {"dir": out_dir, "files": written, "total": len(types),
            "classes": len([k for k in grouped if k != "misc"])}


def _safe(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", L1.get(name, name)).strip("_")
