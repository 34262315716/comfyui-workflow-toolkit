# -*- coding: utf-8 -*-
"""
cwf.terms —— 中文/口语说法 → 节点类型 与 端口别名

AI agent 和人都会习惯说「解码」「采样器」「主模型」，而 ComfyUI 里叫
VAEDecode / KSampler / CheckpointLoaderSimple。这一层负责翻译，
让 `cwf deps 某流 解码` 这种写法也能用。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

#: 中文/口语 → 候选节点类型（按优先级）。命中多个时按顺序取第一个存在的。
NODE_ALIASES: Dict[str, List[str]] = {
    # 加载
    "主模型": ["CheckpointLoaderSimple", "UNETLoader", "CheckpointLoader"],
    "大模型": ["CheckpointLoaderSimple", "UNETLoader"],
    "检查点": ["CheckpointLoaderSimple"],
    "底模": ["CheckpointLoaderSimple"],
    "模型加载": ["CheckpointLoaderSimple", "UNETLoader"],
    "unet": ["UNETLoader", "UnetLoaderGGUF"],
    "lora": ["LoraLoader", "LoraLoaderModelOnly", "Lora Loader Stack (rgthree)"],
    "罗拉": ["LoraLoader"],
    "vae": ["VAELoader"],
    "编码器": ["CLIPLoader", "DualCLIPLoader"],
    "文本编码器": ["CLIPLoader", "DualCLIPLoader"],
    "载入图": ["LoadImage"],
    "读图": ["LoadImage"],
    "载入视频": ["LoadVideo", "VHS_LoadVideo"],
    "读视频": ["LoadVideo", "VHS_LoadVideo"],
    "载入音频": ["LoadAudio"],
    # 提示词
    "正向": ["CLIPTextEncode", "TextEncodeQwenImageEdit"],
    "正向提示词": ["CLIPTextEncode"],
    "负向": ["CLIPTextEncode"],
    "负向提示词": ["CLIPTextEncode"],
    "提示词": ["CLIPTextEncode", "PrimitiveStringMultiline"],
    "文本编码": ["CLIPTextEncode"],
    # 采样
    "采样": ["KSampler", "SamplerCustomAdvanced", "KSamplerAdvanced"],
    "采样器": ["KSampler", "KSamplerSelect", "SamplerCustomAdvanced"],
    "潜空间": ["EmptyLatentImage", "EmptySD3LatentImage", "EmptyLatentImagePresets"],
    "空潜空间": ["EmptyLatentImage"],
    "噪声": ["RandomNoise", "Noise"],
    "调度器": ["BasicScheduler", "KSampler"],
    "引导": ["CFGGuider", "BasicGuider"],
    # 解码/输出
    "解码": ["VAEDecode", "VAEDecodeTiled"],
    "vae解码": ["VAEDecode"],
    "编码vae": ["VAEEncode"],
    "出图": ["SaveImage", "PreviewImage"],
    "保存": ["SaveImage"],
    "存图": ["SaveImage"],
    "预览": ["PreviewImage"],
    "保存视频": ["SaveVideo", "VHS_VideoCombine", "SaveWEBM"],
    "视频合成": ["CreateVideo", "VHS_VideoCombine"],
    "保存音频": ["SaveAudio", "SaveAudioMP3"],
    # 放大修复
    "放大": ["ImageScaleBy", "LatentUpscaleBy", "ImageUpscaleWithModel", "ImageScale"],
    "高清放大": ["ImageUpscaleWithModel", "LatentUpscaleBy", "ImageScaleBy"],
    "缩放": ["ImageScale", "ImageScaleBy", "LatentUpscale"],
    "修脸": ["FaceDetailer", "ReActorFaceSwap"],
    "换脸": ["ReActorFaceSwap", "InstantID"],
    # 辅助
    "跳线": ["SetNode", "GetNode"],
    "注释": ["Note", "孤海注释"],
    "开关": ["Any Switch (rgthree)", "Switch (rgthree)"],
    "数学": ["ComfyMathExpression"],
    "文本": ["PrimitiveStringMultiline", "PrimitiveString"],
    "整数": ["PrimitiveInt"],
    "浮点": ["PrimitiveFloat"],
}

#: 中文/口语 → 端口名。用于 `a.模型 -> b.模型` 这种写法。
SLOT_ALIASES: Dict[str, str] = {
    "模型": "MODEL", "大模型": "MODEL", "主模型": "MODEL",
    "条件": "CONDITIONING", "正向条件": "positive", "负向条件": "negative",
    "潜空间": "LATENT", "潜变量": "LATENT", "latent": "LATENT",
    "图像": "IMAGE", "图片": "IMAGE", "mask": "MASK", "遮罩": "MASK",
    "视频": "VIDEO", "音频": "AUDIO", "声音": "AUDIO",
    "采样器": "SAMPLER", "噪声": "NOISE", "引导": "GUIDER",
    "提示词": "text", "文本": "text", "种子": "seed", "步数": "steps",
    "宽": "width", "高": "height", "批次": "batch_size",
    "文件名前缀": "filename_prefix", "缩放倍数": "scale_by",
    "去噪": "denoise", "采样方法": "sampler_name", "调度": "scheduler",
    "采样器名": "sampler_name", "图像输出": "IMAGE", "输出图": "IMAGE",
}

#: 分类中文名（给 schema categories / nodes list 用）
CATEGORY_ZH = {
    "loaders": "加载器", "conditioning": "条件/提示词", "latent": "潜空间",
    "sampling": "采样", "image": "图像处理", "mask": "遮罩",
    "_for_testing": "测试用", "advanced": "高级", "audio": "音频",
    "video": "视频", "3d": "3D", "utils": "工具", "text": "文本",
    "model": "模型", "model_merging": "模型合并", "api node": "API 节点",
    "image/upscaling": "图像放大", "image/transform": "图像变换",
    "image/compositing": "图像合成", "image/filters": "图像滤镜",
    "image/batch": "图像批处理", "image/noise": "图像噪声",
    "image/postprocessing": "图像后处理", "loaders/video_models": "视频模型加载",
    "sampling/custom_sampling": "自定义采样", "experimental": "实验性",
}


def zh_category(cat: str) -> str:
    """把分类路径里的英文段换成中文（只换第一段，保留后面的层级）。"""
    if not cat:
        return cat
    parts = cat.split("/")
    if parts[0].lower() in CATEGORY_ZH:
        parts[0] = CATEGORY_ZH[parts[0].lower()]
    return "/".join(parts)


def resolve_type(token: str, reg) -> Optional[str]:
    """把用户写的节点类型/中文别名，解析成一个真实存在的类型名。"""
    t = (token or "").strip()
    if not t:
        return None
    if reg is not None and len(reg) and t in reg:
        return t
    low = t.lower()
    cands = NODE_ALIASES.get(t) or NODE_ALIASES.get(low) or []
    for c in cands:
        if reg is None or not len(reg) or c in reg:
            return c
    if cands:
        return cands[0]
    # 不区分大小写地找
    if reg is not None and len(reg):
        for k in reg.keys():
            if k.lower() == low:
                return k
    return None


def resolve_slot(token: str) -> str:
    """端口名别名。"""
    t = (token or "").strip()
    return SLOT_ALIASES.get(t, SLOT_ALIASES.get(t.lower(), t))


def suggest_for(token: str, have: List[str], limit: int = 6) -> List[str]:
    """在「图里实际存在的类型」里，给一个没匹配上的词找相近的。

    两条路：
      1. 中文别名 → 英文关键词（解码 → decode），再在图里找含这个词的类型
      2. 词形相似（像 KSampler → WanVideoSampler 这种同族节点）
    比单纯列一堆不相干的类型有用得多。
    """
    t = (token or "").strip()
    if not t or not have:
        return []
    low = t.lower()
    out: List[str] = []

    # 1) 中文/口语 → 英文关键词
    ZH_KEYS = {
        "解码": ("decode",), "编码": ("encode",), "采样": ("sampler", "sampling", "ksampler"),
        "采样器": ("sampler",), "模型": ("loader", "unet", "checkpoint", "model"),
        "主模型": ("checkpointloader", "unetloader"), "加载": ("load", "loader"),
        "载入": ("load", "loader"), "提示词": ("textencode", "prompt", "text"),
        "保存": ("save",), "输出": ("save", "preview", "combine"), "视频": ("video",),
        "音频": ("audio",), "图像": ("image",), "图片": ("image",), "放大": ("upscale", "scale"),
        "缩放": ("scale", "resize"), "潜空间": ("latent",), "噪声": ("noise",),
        "调度": ("scheduler", "sigmas"), "引导": ("guider", "cfg"), "遮罩": ("mask",),
        "vae": ("vae",), "lora": ("lora",), "clip": ("clip",), "跳线": ("setnode", "getnode"),
    }
    keys = ZH_KEYS.get(low, ())
    for k in keys:
        for h in have:
            if k in h.lower() and h not in out:
                out.append(h)

    # 2) 词形相似：取 token 的词干片段去匹配
    frag = re.sub(r"[^A-Za-z]", "", t).lower()
    if len(frag) >= 4:
        for h in have:
            hh = h.lower()
            if frag in hh or hh.endswith(frag) or frag.endswith(hh):
                if h not in out:
                    out.append(h)
    if not out and len(low) >= 2:
        for h in have:
            if low[:2] in h.lower() and h not in out:
                out.append(h)
    return out[:limit]


def alias_help(reg=None, limit: int = 40) -> List[Tuple[str, str]]:
    """列出可用的中文别名（只列本机真实存在的类型）。"""
    out: List[Tuple[str, str]] = []
    for k, cands in NODE_ALIASES.items():
        for c in cands:
            if reg is None or not len(reg) or c in reg:
                out.append((k, c))
                break
    return out[:limit]
