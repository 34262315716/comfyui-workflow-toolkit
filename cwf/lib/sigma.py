# -*- coding: utf-8 -*-
"""sigma 表计算与安全校验 —— 手写 sigmas 的辅助工具。

给「自定义 sigma」这件事做三件手工很容易出错的事：

1. **查范围** —— 手写 sigma 最大的坑是范围不匹配模型。模型的 sigma_max/sigma_min
   是训练时定死的，写错就是初始噪声不足或成品带噪。这里按模型族直接算出真实范围。
2. **抄底子** —— 别从零手写。这里能把任意内置调度器的序列打印成可直接粘进
   ``ManualSigmas`` 的一行字符串，改几个数即可。
3. **防踩雷** —— 低步数下每步跨幅巨大（4 步时一步要跨 0.4），乱调会把好配置调坏。
   这里给出每步跨幅与安全可调区间。

另附接力采样切分：把一条序列按 sigma 值切成两段，保证**上段尾 = 下段头**，
避免同一段 sigma 被采两遍（接力采样最经典的坑）。

设计取舍：只依赖 ``comfy`` 包本身，**不需要 ComfyUI 服务在线、不需要加载权重**
（各采样类都支持空构造，实测可用）。
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

#: 模型族 → (说明, 采样类名, 认模型的文件名关键词)
#: 采样类取自 comfy.model_base.model_sampling() 的 model_type 分派表。
FAMILIES: Dict[str, Dict[str, Any]] = {
    "flux": {
        "desc": "Flux / Krea2 系（flow 模型，shift 可调）",
        "cls": "ModelSamplingFlux",
        "shift_default": 1.15,
        "shift_param": "shift",
        "keywords": ("krea2", "flux", "krea"),
    },
    "flow": {
        "desc": "通用 flow 匹配模型（SD3 / Qwen-Image / H3 等）",
        "cls": "ModelSamplingDiscreteFlow",
        "shift_default": None,
        "keywords": ("sd3", "qwen_image", "qwen_image_edit", "h3", "minimax",
                     "z_image", "z-image", "pixal", "boogu", "yue2", "krea2raw"),
    },
    "av": {
        "desc": "音视频联合 flow（MiniMax H3 系；sigma 范围与 flow 同，但另有 audio_shift）",
        "cls": "ModelSamplingAV",
        "shift_default": None,
        "keywords": ("minimax-h3", "minimax_h3", "h3_singularity", "ref2va"),
    },
    "discrete": {
        "desc": "老式 DDPM/SD 系（anima / SDXL / Illustrious 等）",
        "cls": "ModelSamplingDiscrete",
        "shift_default": None,
        "keywords": ("anima", "wai", "illustrious", "sdxl", "pony", "dream",
                     "hassaku", "redcraft"),
    },
    "edm": {
        "desc": "EDM 系（离散 sigma，量级 120）",
        "cls": "ModelSamplingContinuousEDM",
        "shift_default": None,
        "keywords": ("edm",),
    },
}

#: 采样类名 → 实际类（延迟导入，避免 import 期就拖起 torch）
_CLASS_CACHE: Dict[str, Any] = {}


def _comfy_root() -> str:
    from cwf.lib import paths
    return paths.comfy_root()


def _ensure_comfy_importable() -> bool:
    """把 ComfyUI 根塞进 sys.path，这样能 import comfy.model_sampling。"""
    import sys
    root = _comfy_root()
    if not root or not os.path.isdir(root):
        return False
    if root not in sys.path:
        sys.path.insert(0, root)
    return True


def _get_class(cls_name: str):
    if cls_name in _CLASS_CACHE:
        return _CLASS_CACHE[cls_name]
    if not _ensure_comfy_importable():
        raise RuntimeError("找不到 ComfyUI 根目录，无法 import comfy.model_sampling。"
                           "用 CWF_COMFY 环境变量指向 ComfyUI 安装根。")
    import comfy.model_sampling as MS
    cls = getattr(MS, cls_name, None)
    if cls is None:
        raise RuntimeError(f"comfy.model_sampling 里没有 {cls_name}")
    _CLASS_CACHE[cls_name] = cls
    return cls


def guess_family(name: str) -> Optional[str]:
    """按模型文件名猜它属于哪个族。猜不出来返回 None（调用方提示手工指定）。"""
    low = (name or "").lower()
    best: Optional[Tuple[int, str]] = None
    for fam, spec in FAMILIES.items():
        for kw in spec["keywords"]:
            if kw in low:
                score = len(kw)
                if best is None or score > best[0]:
                    best = (score, fam)
    return best[1] if best else None


def build_sampling(family: str, shift: Optional[float] = None):
    """构造该族的 model_sampling 对象。shift 只对支持的族有效。"""
    spec = FAMILIES.get(family)
    if spec is None:
        raise KeyError(f"未知模型族 {family!r}；可用：{', '.join(FAMILIES)}")
    cls = _get_class(spec["cls"])
    ms = cls(None)
    if shift is not None and spec.get("shift_param") == "shift":
        try:
            ms.set_parameters(shift=float(shift))
        except TypeError:
            ms.set_parameters(float(shift))
    return ms


def sigma_range(family: str, shift: Optional[float] = None) -> Tuple[float, float]:
    """返回 (sigma_max, sigma_min)。"""
    ms = build_sampling(family, shift)
    return float(ms.sigma_max), float(ms.sigma_min)


def calculate(family: str, scheduler: str, steps: int,
              shift: Optional[float] = None) -> List[float]:
    """算某调度器的 sigma 序列（含末尾的 0）。"""
    if not _ensure_comfy_importable():
        raise RuntimeError("找不到 ComfyUI 根目录")
    import sys
    root = _comfy_root()
    if root in sys.path:
        old = os.getcwd()
        try:
            os.chdir(root)          # comfy 内部有相对路径假设
            import comfy.samplers as CS
        finally:
            os.chdir(old)
    else:
        import comfy.samplers as CS
    if scheduler not in CS.SCHEDULER_NAMES:
        raise KeyError(f"未知调度器 {scheduler!r}；可用：{', '.join(CS.SCHEDULER_NAMES)}")
    ms = build_sampling(family, shift)
    return [float(v) for v in CS.calculate_sigmas(ms, scheduler, int(steps)).cpu().tolist()]


def scheduler_names() -> List[str]:
    if not _ensure_comfy_importable():
        return []
    import sys
    root = _comfy_root()
    if root in sys.path:
        old = os.getcwd()
        try:
            os.chdir(root)
            import comfy.samplers as CS
        finally:
            os.chdir(old)
    else:
        import comfy.samplers as CS
    return list(CS.SCHEDULER_NAMES)


def spans(sigmas: List[float]) -> List[float]:
    """每步降幅（正数表示下降）。"""
    return [round(sigmas[i] - sigmas[i + 1], 4) for i in range(len(sigmas) - 1)]


def to_string(sigmas: List[float]) -> str:
    """转成能直接粘进 ManualSigmas 控件的一行。"""
    out = []
    for v in sigmas:
        if v == 0:
            out.append("0")
        elif abs(v) < 0.001:
            out.append(f"{v:.4f}".rstrip("0").rstrip("."))
        else:
            out.append(f"{v:.3f}".rstrip("0").rstrip("."))
    return ", ".join(out)


def parse_sigmas(text: str) -> List[float]:
    """从字符串里抠出所有浮点数（与 ManualSigmas 节点同款正则）。"""
    return [float(i) for i in re.findall(r"[-+]?(?:\d*\.*\d+)", text or "")]


def inspect(sigmas: List[float], family: Optional[str] = None,
            shift: Optional[float] = None) -> Dict[str, Any]:
    """体检一条手写序列：范围对不对、末尾干不干净、哪几步最激进。"""
    issues: List[str] = []
    notes: List[str] = []

    if len(sigmas) < 2:
        issues.append("序列至少要有两个值（起点 + 终点）。")
        return {"issues": issues, "notes": notes, "spans": []}

    head, tail = sigmas[0], sigmas[-1]
    d = spans(sigmas)

    if family:
        try:
            smax, smin = sigma_range(family, shift)
        except Exception as e:
            notes.append(f"查不到 {family} 的范围（{e}），跳过范围校验。")
            smax = smin = None
        if smax is not None:
            if head < smax * 0.98:
                issues.append(
                    f"起点 {head:.4f} 低于该模型的上限 {smax:.4f} —— "
                    f"初始噪声不足，画面可能缺少大结构。建议从 {smax:.4f} 起。")
            if head > smax * 1.02:
                issues.append(
                    f"起点 {head:.4f} 高于该模型的上限 {smax:.4f} —— 可能过噪/崩坏。")
            if tail > max(smin * 5, 0.01):
                issues.append(
                    f"末尾停在 {tail:.4f}，而该模型能降到的下限是 {smin:.4f} —— "
                    f"成品会残留噪点（画面发沙）。建议末尾写 0。")

    if tail != 0:
        issues.append(f"最后一个值不是 0（是 {tail}）。多数情况下应写 0。")

    for i, v in enumerate(sigmas):
        if i > 0 and v > sigmas[i - 1]:
            notes.append(f"第 {i + 1} 个值比前一个大（{sigmas[i-1]:.3f} → {v:.3f}）"
                         f"—— 非单调序列，只有部分采样器支持，建议配 euler。")
            break

    if d:
        mx = max(d)
        idx = d.index(mx)
        notes.append(f"跨度最大的是第 {idx + 1} 步：降 {mx:.3f}"
                     f"（{sigmas[idx]:.3f} → {sigmas[idx+1]:.3f}）。")
        avg = sum(d) / len(d)
        notes.append(f"平均每步降 {avg:.4f}，最大跨幅是平均的 {mx/avg:.1f} 倍。")

    return {"issues": issues, "notes": notes, "spans": d,
            "steps": len(sigmas) - 1}


def split_at(sigmas: List[float], boundary: float,
             overlap: bool = True) -> Tuple[List[float], List[float]]:
    """在某个 sigma 值处把序列切成两段，用于接力采样。

    ``overlap=True`` 时把切点同时放进两段（上段尾 = 下段头），这正是接力采样
    需要的：下一段从上一段结束的地方接着走。若不留这个共享点，中间会断一截，
    那一段就没被采样到。
    """
    pivot = None
    for i, v in enumerate(sigmas):
        if v <= boundary:
            pivot = i
            break
    if pivot is None:
        raise ValueError(f"boundary={boundary} 比序列最小值还小，切不了。")
    if pivot == 0:
        raise ValueError(f"boundary={boundary} 比序列最大值还大，切不了。")

    high = sigmas[:pivot + 1]
    low = sigmas[pivot:] if overlap else sigmas[pivot + 1:]
    return high, low


def ladders(steps_list: List[int], scheduler: str, family: str,
            shift: Optional[float] = None) -> List[Dict[str, Any]]:
    """同一个调度器在多个步数下的序列对照（看"步数越多差异越小"）。"""
    out = []
    for st in steps_list:
        s = calculate(family, scheduler, st, shift)
        out.append({"steps": st, "sigmas": s, "spans": spans(s)})
    return out


def safe_zone(sigmas: List[float], low_ratio: float = 0.35) -> Dict[str, Any]:
    """把序列分成「相对可调」与「雷区」。

    经验规则：**每步跨幅越小的地方越经得起微调**。判据用中位数而不是最大值的比例
    —— 因为均衡的序列里最大值和最小值本来就接近，"占最大值 35%" 会让好序列也
    整条落进雷区（实测踩过这个坑）。按中位数切，永远能分出两边。
    """
    d = spans(sigmas)
    if not d:
        return {"safe": [], "danger": [], "max_span": 0, "avg_span": 0}

    srt = sorted(d)
    mid = srt[len(srt) // 2]
    mx = max(d)
    safe, danger = [], []
    for i, v in enumerate(d):
        pos = f"第{i+1}步({sigmas[i]:.3f}→{sigmas[i+1]:.3f})"
        (danger if v > mid else safe).append(pos)
    return {"safe": safe, "danger": danger, "max_span": mx,
            "mid_span": mid, "avg_span": sum(d) / len(d)}
