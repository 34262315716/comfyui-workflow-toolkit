# -*- coding: utf-8 -*-
"""ComfyUI 生成图的元数据读取与统计。

ComfyUI 把生成的图存成 PNG，并在 **PNG 的文本块（tEXt / iTXt / zTXt）** 里
嵌进两个东西：

* ``prompt``   —— API 格式的节点图：``{节点id: {"class_type": ..., "inputs": {...}}}``
* ``workflow`` —— 前端格式的完整工作流（可直接拖回画布还原）

这个模块只做**读**：把图里的参数挖出来，并按「采样器 × 调度器」这类组合聚合，
回答「我历史上哪套搭配用得最多 / 效果最好」这种问题。

几点刻意的取舍：

1. **纯标准库**，不依赖 PIL。PNG 文本块就在文件头部，按 chunk 读几百 KB 就够，
   没必要为了读元数据去解整张图（1599 张图用 PIL 全会慢一个量级）。
2. **两种键名都认**。除 ``prompt``/``workflow`` 外，还认 WebUI 风格的
   ``parameters``（少数整合包会写），以及 ComfyUI 新版的其它别名。
3. **只做有据可查的抽取**。取不到就留空，不猜、不编默认值 —— 调用方看到 None
   就知道「这张图没记这个」，而不是被假数据误导。
"""
from __future__ import annotations

import json
import os
import struct
import zlib
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from cwf.lib import paths

#: PNG 文件签名
_PNG_SIG = b"\x89PNG\r\n\x1a\n"

#: 元数据可能出现的键，按优先级排列（前面的先认）
META_KEYS = ("prompt", "workflow", "parameters", "Comment", "Description")

#: 采样器类节点的 class_type 特征词
_SAMPLER_HINTS = ("Sampler", "sampler", "Guider", "Scheduler")

#: 加载器类节点特征词
_LOADER_HINTS = ("Loader", "LoraLoader", "Load")

#: 采样参数里值得抽的键
_SAMPLER_FIELDS = (
    "seed", "noise_seed", "steps", "cfg", "sampler_name", "scheduler",
    "denoise", "add_noise", "start_at_step", "end_at_step",
)


# ---------------------------------------------------------------- PNG 读取


def _png_text_chunks(path: str) -> Dict[str, str]:
    """读 PNG 的文本块，返回 ``{关键词: 文本}``。

    只读 chunk 元数据，不碰图像数据；遇到 IDAT 直接停（文本块按规范必须在其之前）。
    """
    out: Dict[str, str] = {}
    try:
        with open(path, "rb") as f:
            if f.read(len(_PNG_SIG)) != _PNG_SIG:
                return out
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                length, ctype = struct.unpack(">I4s", head)
                if ctype == b"IDAT":
                    break
                if ctype not in (b"tEXt", b"zTXt", b"iTXt"):
                    if length:
                        f.seek(length + 4, os.SEEK_CUR)
                    else:
                        f.seek(4, os.SEEK_CUR)
                    continue
                data = f.read(length)
                f.seek(4, os.SEEK_CUR)          # 跳过 CRC
                try:
                    if ctype == b"tEXt":
                        # 只按第一个 NUL 切：文本里可能含 NUL，不能全切
                        i = data.index(b"\x00")
                        out[data[:i].decode("latin-1")] = data[i + 1:].decode("latin-1")
                    elif ctype == b"zTXt":
                        i = data.index(b"\x00")
                        kw = data[:i].decode("latin-1")
                        body = data[i + 1:]
                        if body and body[0] == 0:     # 压缩方法，0 = deflate
                            body = body[1:]
                        out[kw] = zlib.decompress(body).decode("latin-1")
                    elif ctype == b"iTXt":
                        parts = data.split(b"\x00", 5)
                        if len(parts) >= 6:
                            kw = parts[0].decode("latin-1")
                            compressed = parts[1] == b"\x01"
                            text = parts[5]
                            if compressed:
                                text = zlib.decompress(text)
                            out[kw] = text.decode("utf-8", errors="replace")
                except (ValueError, zlib.error):
                    continue
    except OSError:
        return out
    return out


def read_meta(path: str) -> Optional[Dict[str, Any]]:
    """读一张图的元数据。没有可解析的元数据就返回 None。"""
    chunks = _png_text_chunks(path)
    if not chunks:
        return None

    meta: Dict[str, Any] = {"raw_keys": sorted(chunks.keys())}

    for key in ("prompt", "workflow"):
        if key in chunks:
            try:
                meta[key] = json.loads(chunks[key])
            except (ValueError, TypeError):
                meta[key] = None

    # WebUI 风格的 parameters（纯文本，不是 JSON）
    if "parameters" in chunks:
        meta["parameters"] = chunks["parameters"]

    if meta.get("prompt") is None and meta.get("workflow") is None \
            and "parameters" not in meta:
        return None
    return meta


# ---------------------------------------------------------------- 参数抽取


def _titled(node: Dict[str, Any]) -> str:
    md = node.get("_meta")
    if isinstance(md, dict):
        return str(md.get("title") or "")
    return ""


def _resolve_model_name(prompt: Dict[str, Any], ref: Any) -> Optional[str]:
    """顺着连线引用一路回溯到模型文件名。

    采样器到模型之间**不一定是一条直链**。实测常见的三种形态：

    * ``KSampler.model -> LoraLoader.model -> UNETLoader``    直链
    * ``KSampler.model -> Lora Stack.model -> ...``          堆叠 LoRA
    * ``KSampler.model -> Any Switch (rgthree).any_02 -> ...`` 选择器中转

    第三种是坑：switch 类节点的输入口名是 ``any_01/any_02/...`` 这种动态名，
    而且 **只有已接线的那个口会写进 JSON**。所以不能只认 ``model`` 这一个键，
    要把该节点所有「连线型」输入都当成候选出口。

    用显式栈做 DFS 并记访问集，避免 switch 之间的环把函数挂住。
    """
    stack: List[Any] = [ref]
    seen: set = set()
    while stack:
        cur = stack.pop(0)
        if not isinstance(cur, list) or not cur:
            continue
        nid = str(cur[0])
        if nid in seen:
            continue
        seen.add(nid)
        node = prompt.get(nid)
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        # 本节点自带模型文件名？直接命中
        for k in ("unet_name", "ckpt_name", "model_name"):
            v = inputs.get(k)
            if isinstance(v, str) and v and v.lower() != "none":
                return v
        # 否则把所有连线型输入当作出口继续下探
        # 明确的 model 链优先，其余（switch 的 any_xx 等）随后
        ups = [(k, v) for k, v in inputs.items() if isinstance(v, list) and v]
        ups.sort(key=lambda kv: 0 if "model" in kv[0].lower() else 1)
        for _k, v in ups:
            if str(v[0]) not in seen:
                stack.append(v)
    return None


def extract(prompt: Dict[str, Any]) -> Dict[str, Any]:
    """从 API 格式的 prompt 里抽出采样参数、模型链、提示词。"""
    samp: Dict[str, Any] = {}
    models: Dict[str, Any] = {}
    loras: List[Dict[str, Any]] = []
    texts: List[str] = []
    loader_names: List[str] = []

    for nid, node in prompt.items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type", ""))
        inputs = node.get("inputs") or {}
        simple = {k: v for k, v in inputs.items() if not isinstance(v, (list, dict))}

        # ---- 采样器
        if any(h in ct for h in _SAMPLER_HINTS) and not samp:
            got = {k: simple[k] for k in _SAMPLER_FIELDS if k in simple}
            if got:
                got["_node"] = f"#{nid} {ct}"
                got["_title"] = _titled(node)
                got["model_file"] = _resolve_model_name(prompt, inputs.get("model"))
                samp = got

        # ---- 各类加载器
        if any(h in ct for h in _LOADER_HINTS):
            for k, v in simple.items():
                if isinstance(v, str) and v and v.lower() not in ("none", "default") \
                        and any(t in k.lower() for t in
                                ("name", "model", "clip", "vae", "type")):
                    loader_names.append(v)
            for k, v in simple.items():
                kl = k.lower()
                if "unet" in kl or "ckpt" in kl:
                    models.setdefault("unet", v)
                elif "clip" in kl or "text_encoder" in kl:
                    models.setdefault("clip", v)
                elif "vae" in kl:
                    models.setdefault("vae", v)

        # ---- LoRA（单节点式 与 堆叠式都收）
        if "LoraLoader" in ct or "lora" in ct.lower():
            ln = inputs.get("lora_name")
            if isinstance(ln, str) and ln and ln.lower() != "none":
                st = inputs.get("strength_model", inputs.get("strength", 1.0))
                loras.append({"name": ln, "strength": st, "node": f"#{nid} {ct}"})
            for i in range(1, 13):
                ln = inputs.get(f"lora_0{i}") or inputs.get(f"lora_{i:02d}")
                if isinstance(ln, str) and ln and ln.lower() != "none":
                    st = inputs.get(f"strength_0{i}",
                                    inputs.get(f"strength_{i:02d}", 1.0))
                    loras.append({"name": ln, "strength": st,
                                  "node": f"#{nid} {ct}"})

        # ---- 提示词
        if "TextEncode" in ct or ct == "CLIPTextEncode":
            t = inputs.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t.strip())

    return {
        "sampler": samp,
        "models": models,
        "loras": loras,
        "texts": texts,
        "loaders": sorted(set(loader_names)),
    }


def png_size(path: str) -> Optional[Tuple[int, int]]:
    """只读 IHDR 拿宽高（比解整张图快得多）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(33)
        if len(head) < 33 or head[:8] != _PNG_SIG:
            return None
        w, h = struct.unpack(">II", head[16:24])
        return int(w), int(h)
    except OSError:
        return None


def summary(path: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一张图的完整摘要，供查询与聚合共用。"""
    if meta is None:
        meta = read_meta(path)

    rec: Dict[str, Any] = {
        "file": path,
        "name": os.path.basename(path),
        "size": png_size(path),
        "bytes": None,
        "sampler": {},
        "models": {},
        "loras": [],
        "texts": [],
        "raw_keys": [],
    }
    try:
        rec["bytes"] = os.path.getsize(path)
    except OSError:
        pass

    if not meta:
        rec["has_meta"] = False
        return rec

    rec["has_meta"] = True
    rec["raw_keys"] = meta.get("raw_keys", [])

    prompt = meta.get("prompt")
    if isinstance(prompt, dict):
        rec.update({k: v for k, v in extract(prompt).items()})
        rec["nodes"] = len(prompt)

    wf = meta.get("workflow")
    if isinstance(wf, dict):
        # 前端格式：找最外层标题，并统计节点数
        rec["workflow_title"] = wf.get("name") or wf.get("title") or ""
        cards = wf.get("nodes")
        if isinstance(cards, list):
            rec["workflow_nodes"] = len(cards)

    if "parameters" in meta:
        rec["webui_parameters"] = meta["parameters"]
    return rec


# ---------------------------------------------------------------- 遍历与聚合


def iter_images(root: str, match: Optional[str] = None) -> Iterator[str]:
    """递归遍历图片目录，按修改时间倒序产出（新的先看）。"""
    hits: List[Tuple[float, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.lower().endswith((".png", ".webp")):
                continue
            if match and match.lower() not in fn.lower():
                continue
            p = os.path.join(dirpath, fn)
            try:
                hits.append((os.path.getmtime(p), p))
            except OSError:
                continue
    for _t, p in sorted(hits, reverse=True):
        yield p


def default_output_dir(explicit: Optional[str] = None) -> str:
    """定位 ComfyUI 的输出目录。

    优先级：显式参数 > ``CWF_OUTPUT`` 环境变量 > 从 ComfyUI 根推断。
    和 ``paths.py`` 一个路子 —— **不写死任何机器相关路径**，找不到就返回空串，
    让调用方提示用户去设环境变量，而不是拿一个不存在的默认值糊弄过去。
    """
    if explicit:
        return explicit
    env = os.environ.get("CWF_OUTPUT")
    if env and os.path.isdir(env):
        return env
    root = paths.comfy_root()
    if root:
        for cand in (os.path.join(root, "output"), os.path.join(root, "Output")):
            if os.path.isdir(cand):
                return cand
    return ""


def combo_of(rec: Dict[str, Any]) -> Optional[str]:
    """把一条记录的「采样器 × 调度器」拼成组合键。"""
    s = rec.get("sampler") or {}
    name = s.get("sampler_name")
    sched = s.get("scheduler")
    if name is None and sched is None:
        return None
    return "%s + %s" % (name if name is not None else "?",
                        sched if sched is not None else "?")


def aggregate(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """把一批摘要聚合成组合统计。"""
    combos: Counter = Counter()
    meta_of: Dict[str, Dict[str, Counter]] = defaultdict(
        lambda: {"steps": Counter(), "cfg": Counter(), "models": Counter(),
                 "loras": Counter(), "schedulers": Counter(), "samples": Counter()})
    no_meta = 0
    total = 0

    for rec in records:
        total += 1
        if not rec.get("has_meta"):
            no_meta += 1
            continue
        key = combo_of(rec)
        if key is None:
            no_meta += 1
            continue
        combos[key] += 1
        box = meta_of[key]
        s = rec.get("sampler") or {}
        for field in ("steps", "cfg"):
            v = s.get(field)
            if v is not None:
                box[field][str(v)] += 1
        mf = s.get("model_file")
        if mf:
            box["models"][mf] += 1
        for lora in rec.get("loras") or []:
            box["loras"][lora["name"]] += 1
        size = rec.get("size")
        if size:
            box["samples"][f"{size[0]}x{size[1]}"] += 1

    return {
        "total": total,
        "with_meta": total - no_meta,
        "without_meta": no_meta,
        "combos": [
            {
                "combo": key,
                "count": cnt,
                "steps": dict(meta_of[key]["steps"].most_common(3)),
                "cfg": dict(meta_of[key]["cfg"].most_common(3)),
                "models": [m for m, _ in meta_of[key]["models"].most_common(4)],
                "loras": [m for m, _ in meta_of[key]["loras"].most_common(4)],
                "samples": [m for m, _ in meta_of[key]["samples"].most_common(3)],
            }
            for key, cnt in combos.most_common()
        ],
    }
