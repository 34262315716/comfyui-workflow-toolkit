# -*- coding: utf-8 -*-
"""cwf.rig —— 设备能力 × 工作流负载，量化到 GB / 秒

## 这个模块回答什么问题

「这张工作流，我这台机器跑得动吗？余量还有多少？」

## 为什么敢给数字而不是拍脑袋

三条数据来源都是**可实测**的，不是经验系数：

1. **权重占用**：模型文件在磁盘上的字节数。
   实测校准（2026-09-16，ComfyUI 0.35.1）——
   日志里 `prepared for dynamic VRAM loading. NNNNMB Staged` 与磁盘字节对比：

       19995 MB  ←→  19996 MB   差 +0.0%
       14955 MB  ←→  14957 MB   差 +0.0%
         576 MB  ←→    577 MB   差 +0.2%

   所以权重项的误差 < 1%，而它通常占负载的 95% 以上。

2. **设备能力**：`nvidia-smi` 报显存，ComfyUI `/system_stats` 报内存与
   torch 实际占用。都是硬数字。

3. **真实速度**：ComfyUI 日志里的 `34.94s/it`、`Prompt executed in 595.67
   seconds`。测出来的比查规格表准。

## 哪些是估的（必须说清楚）

* **激活项**：潜空间和中间张量。按「分辨率 ÷ 下采样 × 通道 × 帧数 × 精度」
  算，再乘一个架构相关的中间张量系数。**不确定度 ±50%**。
  好在它通常只占负载的几个百分点（20 GB 权重 vs 100 MB 潜空间）。
* **运行时开销**：CUDA context + 驱动 + 碎片，取 0.4~1.0 GB 的区间。
* **ComfyUI 会不会流式加载**：这是最大的一处不确定。
  全驻显存和动态流式的峰值差好几倍，所以两种都报，并说明判断依据。

## 一句话总结口径

    内存 = 全部权重必须能装下（动态加载要把权重整个驻留内存）
    显存 = 要么全装下，要么按块流式（那就只要求"装得下一块"）
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 单位

GIB = 1024 ** 3
MIB = 1024 ** 2


def gb(n: float) -> str:
    """字节 → 人读的 GB（用 GiB，和显卡厂商标的 GB 口径不同，这里统一按 1024）。"""
    return "%.2f GB" % (n / GIB)


def _fmt_delta(n: float) -> str:
    return ("+" if n >= 0 else "") + "%.2f GB" % (n / GIB)


# ---------------------------------------------------------------- 设备


@dataclass
class Device:
    """这台机器的能力画像。全部是实测值，拿不到就是 None。"""

    gpu_name: str = ""
    vram_total: int = 0
    vram_free: int = 0
    compute_cap: float = 0.0

    ram_total: int = 0
    ram_free: int = 0

    torch_vram_total: int = 0      # torch 自己已分配的量（含缓存）
    torch_vram_free: int = 0

    comfy_version: str = ""
    python_version: str = ""
    source: str = ""               # 这些数字是从哪来的

    #: 实测吞吐（秒/迭代）。来自日志，测出来的比查规格表准。
    measured_sit: List[float] = field(default_factory=list)
    measured_runs: List[Tuple[float, str]] = field(default_factory=list)

    @property
    def vram_usable(self) -> int:
        """真正能用到的显存。

        显卡标称 8188 MiB，但驱动 + CUDA context 先吃掉一截，
        留 512 MB 给上下文的抖动。这是拿 nvidia-smi 的 total 与
        torch 的可用量对比得出的经验值。
        """
        return max(0, self.vram_total - 512 * MIB) if self.vram_total else 0

    @property
    def arch(self) -> str:
        cc = self.compute_cap
        if not cc:
            return "未知"
        table = [(9.0, "Hopper"), (8.9, "Ada Lovelace"), (8.6, "Ampere"),
                 (8.0, "Ampere"), (7.5, "Turing"), (7.0, "Volta"),
                 (6.1, "Pascal"), (6.0, "Pascal"), (5.2, "Maxwell")]
        for lo, name in table:
            if cc >= lo:
                return f"{name} (sm_{str(cc).replace('.', '')})"
        return f"sm_{cc}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "gpu": self.gpu_name, "arch": self.arch,
            "compute_cap": self.compute_cap,
            "vram_total": self.vram_total, "vram_free": self.vram_free,
            "ram_total": self.ram_total, "ram_free": self.ram_free,
            "torch_vram_total": self.torch_vram_total,
            "torch_vram_free": self.torch_vram_free,
            "comfy_version": self.comfy_version,
            "source": self.source,
            "measured_sit": self.measured_sit[:20],
        }


def _from_nvidia_smi() -> Optional[Dict[str, Any]]:
    """问 nvidia-smi 要显卡信息。没有 N 卡 / 没装驱动就返回 None。"""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.free,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    line = r.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        return {
            "gpu_name": parts[0],
            "vram_total": int(float(parts[1])) * MIB,
            "vram_free": int(float(parts[2])) * MIB,
            "compute_cap": float(parts[3]),
        }
    except ValueError:
        return None


def _from_system_stats(server: str, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    """问 ComfyUI 要 /system_stats。它比 nvidia-smi 多知道 torch 的真实占用。"""
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/system_stats",
                                    timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    out: Dict[str, Any] = {}
    sysd = d.get("system") or {}
    if sysd.get("ram_total"):
        out["ram_total"] = int(sysd["ram_total"])
        out["ram_free"] = int(sysd.get("ram_free") or 0)
    if sysd.get("comfyui_version"):
        out["comfy_version"] = str(sysd["comfyui_version"])
    if sysd.get("python_version"):
        out["python_version"] = str(sysd["python_version"]).split()[0]
    devs = d.get("devices") or []
    if devs:
        d0 = devs[0]
        for k_src, k_dst in (("vram_total", "vram_total"),
                             ("vram_free", "vram_free"),
                             ("torch_vram_total", "torch_vram_total"),
                             ("torch_vram_free", "torch_vram_free")):
            if d0.get(k_src) is not None:
                out[k_dst] = int(d0[k_src])
        if d0.get("name") and "gpu_name" not in out:
            nm = str(d0["name"])
            # cuda:0 NVIDIA GeForce RTX 4070 Laptop GPU : cudaMallocAsync
            nm = re.sub(r"^(cuda|mps|cpu):\d+\s*", "", nm)
            nm = nm.split(":")[0].strip()
            out["gpu_name"] = nm
    return out


def _ram_total_os() -> Tuple[int, int]:
    """不靠第三方库问操作系统的内存。返回 (总量, 可用)。"""
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys), int(st.ullAvailPhys)
        except Exception:
            pass
        return 0, 0
    if sys.platform == "darwin":
        try:
            tot = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                                     capture_output=True, text=True,
                                     timeout=5).stdout.strip())
            return tot, 0
        except Exception:
            return 0, 0
    # Linux
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.strip().split()[0]) * 1024
        return info.get("MemTotal", 0), info.get("MemAvailable", 0)
    except Exception:
        return 0, 0


def detect_device(server: str = "http://127.0.0.1:8188",
                  offline: bool = False) -> Device:
    """把设备能力凑齐。每一项都尽量拿实测值，拿不到就留空。"""
    dev = Device()
    src: List[str] = []

    smi = _from_nvidia_smi()
    if smi:
        dev.gpu_name = smi["gpu_name"]
        dev.vram_total = smi["vram_total"]
        dev.vram_free = smi["vram_free"]
        dev.compute_cap = smi["compute_cap"]
        src.append("nvidia-smi")

    if not offline:
        st = _from_system_stats(server)
        if st:
            # ComfyUI 的 vram_total 比 nvidia-smi 略大（含 cudaMalloc 池），
            # 取两者较大的那个当"标称"，free 用 system_stats 的更新鲜。
            if st.get("vram_total"):
                dev.vram_total = max(dev.vram_total, int(st["vram_total"]))
            if st.get("vram_free"):
                dev.vram_free = int(st["vram_free"])
            dev.torch_vram_total = int(st.get("torch_vram_total") or 0)
            dev.torch_vram_free = int(st.get("torch_vram_free") or 0)
            dev.ram_total = int(st.get("ram_total") or 0)
            dev.ram_free = int(st.get("ram_free") or 0)
            dev.comfy_version = st.get("comfy_version", "")
            dev.python_version = st.get("python_version", "")
            if not dev.gpu_name and st.get("gpu_name"):
                dev.gpu_name = st["gpu_name"]
            src.append("ComfyUI /system_stats")

    if not dev.ram_total:
        tot, avail = _ram_total_os()
        dev.ram_total, dev.ram_free = tot, avail
        if tot:
            src.append("操作系统 API")

    dev.source = " + ".join(src) if src else "没拿到任何实测数据"
    return dev


# ---------------------------------------------------------------- 模型解析

#: 控件名 → 该控件引用的模型可能在哪些目录里。
#:
#: ⚠ 只按控件名判断是**不够的**：`model_name` 在 `UpscaleModelLoader` 里指
#: `upscale_models`，在别处可能指 unet。这个坑实测踩过 —— 一张工作流报
#: 「4x-UltraSharpV2.safetensors 不存在」，而它明明躺在 upscale_models 里。
#: 所以真正的判定顺序是：
#:   1. NODE_FOLDERS[节点类型][控件名]  ← 最准
#:   2. MODEL_FOLDERS[控件名]           ← 次之
#:   3. 全目录搜一遍                    ← 兜底（跨目录同名文件很少见）
MODEL_FOLDERS: Dict[str, Tuple[str, ...]] = {
    "ckpt_name": ("checkpoints",),
    "unet_name": ("unet", "diffusion_models"),
    "vae_name": ("vae", "vae_approx"),
    "clip_name": ("text_encoders", "clip"),
    "clip_name1": ("text_encoders", "clip"),
    "clip_name2": ("text_encoders", "clip"),
    "clip_name3": ("text_encoders", "clip"),
    "clip_name4": ("text_encoders", "clip"),
    "lora_name": ("loras",),
    "control_net_name": ("controlnet",),
    "style_model_name": ("style_models",),
    "gligen_name": ("gligen",),
    "upscale_model_name": ("upscale_models",),
    "vae_approx_name": ("vae_approx",),
}

#: 按**节点类型**精确指定，优先级最高。同名控件在不同节点里含义不同，
#: 只有这里才说得清。没列到的节点走上面的兜底逻辑。
NODE_FOLDERS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "UpscaleModelLoader": {"model_name": ("upscale_models",)},
    "ImageUpscaleWithModel": {"model_name": ("upscale_models",)},
    "UNETLoader": {"unet_name": ("unet", "diffusion_models")},
    "UnetLoaderGGUF": {"unet_name": ("unet", "diffusion_models")},
    "CheckpointLoaderSimple": {"ckpt_name": ("checkpoints",)},
    "CheckpointLoader": {"ckpt_name": ("checkpoints",)},
    "VAELoader": {"vae_name": ("vae", "vae_approx")},
    "CLIPLoader": {"clip_name": ("text_encoders", "clip"),
                   "clip_name1": ("text_encoders", "clip"),
                   "clip_name2": ("text_encoders", "clip"),
                   "clip_name3": ("text_encoders", "clip")},
    "DualCLIPLoader": {"clip_name1": ("text_encoders", "clip"),
                       "clip_name2": ("text_encoders", "clip")},
    "TripleCLIPLoader": {"clip_name1": ("text_encoders", "clip"),
                         "clip_name2": ("text_encoders", "clip"),
                         "clip_name3": ("text_encoders", "clip")},
    "LoraLoader": {"lora_name": ("loras",)},
    "LoraLoaderModelOnly": {"lora_name": ("loras",)},
    "ControlNetLoader": {"control_net_name": ("controlnet",)},
    "DiffControlNetLoader": {"control_net_name": ("controlnet",)},
    "StyleModelLoader": {"style_model_name": ("style_models",)},
    "CLIPVisionLoader": {"clip_name": ("clip_vision",)},
    "GLIGENLoader": {"gligen_name": ("gligen",)},
}

#: 某个控件名到底算不算"模型引用"：只要在任一映射里出现过就算。
ALL_MODEL_KEYS = set(MODEL_FOLDERS)
for _nf in NODE_FOLDERS.values():
    ALL_MODEL_KEYS.update(_nf)

#: 这些控件名看着像模型，其实不是（图片文件名、文件夹名之类），要排除
NOT_MODEL_KEYS = {"image", "video", "mask", "file", "filename", "path",
                  "audio", "directory", "folder", "subfolder"}

#: 模型文件的扩展名
MODEL_EXTS = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin",
              ".gguf", ".onnx", ".pkl")

#: 这些控件引用的是**补丁**而不是独立权重。
#:
#: 依据（实测）：日志里 `Model MiniMaxH3 ... 19995MB Staged. 208 patches attached`
#: —— LoRA 被合并进基座模型的权重里，日志报的 staged 大小等于**基座文件本身**
#: 的字节数。所以把 LoRA 的磁盘大小当成独立常驻权重会重复计算。
#: 这个项目上第一次跑就差 +8.9%，差额正好是那几个 LoRA 的和。
#:
#: 注意 LoRA 不是"不占内存"：它是把基座权重改写掉，占用仍约等于基座那份。
#: 但它**不额外增加**峰值，所以不进权重合计，单独报出来给用户看。
PATCH_KEYS = {"lora_name"}


@dataclass
class ModelRef:
    """工作流里引用的一个模型文件。"""

    node_id: int
    node_type: str
    widget: str
    value: str
    folder: str = ""
    path: str = ""
    size: int = 0
    found: bool = False
    #: "resident" 独立常驻权重 / "patch" 合并进基座的补丁（LoRA）
    role: str = "resident"

    @property
    def name(self) -> str:
        return os.path.basename(self.value.replace("\\", "/"))

    def as_dict(self) -> Dict[str, Any]:
        return {"node": self.node_id, "type": self.node_type,
                "widget": self.widget, "value": self.value,
                "folder": self.folder, "size": self.size,
                "found": self.found, "role": self.role}


class ModelIndex:
    """模型库索引：把「工作流里写的文件名」查到真实路径与字节数。

    ## 为什么不能只看 <comfy_root>/models

    ComfyUI 的模型库经常**不在**安装目录里。这个项目实测就是这样：

        comfyui-def/ComfyUI/models/unet/   →  只有一个 put_unet_files_here
        extra_model_paths.yaml             →  base_path: D:\\AItool\\ComfyUI
                                              unet: models/unet   ← 真库在这

    不看 `extra_model_paths.yaml` 的话，每一张工作流都会被判成
    「所有模型都不存在」—— 这是踩过的坑，所以这里必须解析它。

    ## 查找规则

    ComfyUI 是在目标目录下**递归**找同名文件，名字里可以带子目录
    （`H3/xxx.safetensors`）。这里照做，并且额外建一份 basename 索引兜底：
    有些老工作流写的路径和实际落盘位置对不上，用 basename 再找一次，
    比直接报「文件不存在」有用得多。
    """

    def __init__(self, comfy_root: str, folders: Optional[Dict[str, List[str]]] = None):
        self.root = comfy_root
        self.folders = folders if folders is not None else model_search_paths(comfy_root)
        self._by_full: Dict[str, Tuple[str, str]] = {}   # 相对名(小写) → (根, 全路径)
        self._by_base: Dict[str, List[str]] = {}
        self._scanned: set = set()

    @property
    def usable(self) -> bool:
        return any(os.path.isdir(d) for dirs in self.folders.values() for d in dirs)

    def _scan(self, folder: str) -> None:
        if folder in self._scanned:
            return
        self._scanned.add(folder)
        for base in self.folders.get(folder, []):
            if not os.path.isdir(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if not fn.lower().endswith(MODEL_EXTS):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, base).replace("\\", "/")
                    self._by_full.setdefault(rel.lower(), (base, full))
                    self._by_base.setdefault(fn.lower(), []).append(full)

    def find(self, value: str, folders: Sequence[str]) -> Tuple[str, str, int]:
        """返回 (逻辑目录, 真实路径, 字节数)。找不到就全是空。"""
        if not value:
            return "", "", 0
        rel = str(value).strip().replace("\\", "/").lstrip("./")
        for folder in folders:
            self._scan(folder)
            hit = self._by_full.get(rel.lower())
            if hit and os.path.isfile(hit[1]):
                return folder, hit[1], _size(hit[1])
        # 兜底：按 basename 找（可能落在别的子目录、或者写的是旧路径）
        base = os.path.basename(rel).lower()
        for folder in folders:
            self._scan(folder)
            for hit in self._by_base.get(base, []):
                if os.path.isfile(hit):
                    return folder, hit, _size(hit)
        return "", "", 0


def _parse_extra_yaml(text: str) -> Dict[str, Dict[str, Any]]:
    """极简 YAML 解析 —— 只够读 extra_model_paths.yaml，不引入 PyYAML。

    这个文件的形状很简单：顶层一个 section 名，缩进里若干 `键: 值`，
    值可以是单行、也可以是用 `|` 开头的多行块。

    踩过的坑：判断"是否还在多行块里"**不能用 `if block:`** ——
    块刚开始时 `block` 是空列表，假值，于是第一行就被当成块结束了，
    整块内容静默丢失（`text_encoders` 就这么没的）。必须用独立标志位。
    """
    sections: Dict[str, Dict[str, Any]] = {}
    cur_sec: Optional[str] = None
    cur_key: Optional[str] = None
    block: List[str] = []
    block_indent = 0
    in_block = False

    def flush() -> None:
        nonlocal block, cur_key, in_block
        if in_block and cur_sec and cur_key:
            sections[cur_sec][cur_key] = list(block)
        block = []
        cur_key = None
        in_block = False

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue                       # 空行和整行注释不打断多行块
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()

        if in_block:
            if indent > block_indent:
                block.append(stripped)
                continue
            flush()

        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key, val = key.strip(), val.strip()
        if indent == 0:
            cur_sec = key
            sections.setdefault(cur_sec, {})
            continue
        if cur_sec is None:
            continue
        if val in ("|", ">"):
            cur_key = key
            block_indent = indent
            block = []
            in_block = True
            continue
        sections[cur_sec][key] = val
    flush()
    return sections


def load_extra_model_paths(comfy_root: str) -> Optional[Dict[str, List[str]]]:
    """读 extra_model_paths.yaml，返回 {逻辑目录: [真实绝对路径...]}。

    文件不存在就返回 None（调用方退回 <comfy_root>/models）。
    """
    if not comfy_root:
        return None
    yml = os.path.join(comfy_root, "extra_model_paths.yaml")
    if not os.path.isfile(yml):
        return None
    try:
        with open(yml, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    sections = _parse_extra_yaml(text)
    out: Dict[str, List[str]] = {}
    for _sec, kv in sections.items():
        base = kv.get("base_path") or comfy_root
        if isinstance(base, list):
            base = base[0] if base else comfy_root
        base = os.path.expanduser(str(base).strip().strip('"').strip("'"))
        for key, val in kv.items():
            if key == "base_path" or not key or key.startswith("is_default"):
                continue
            vals = val if isinstance(val, list) else [val]
            dirs: List[str] = []
            for v in vals:
                v = str(v).strip().strip('"').strip("'")
                if not v:
                    continue
                p = v if os.path.isabs(v) else os.path.join(base, v)
                p = os.path.normpath(os.path.expanduser(p))
                if os.path.isdir(p) and p not in dirs:
                    dirs.append(p)
            if dirs:
                out.setdefault(key, []).extend(
                    d for d in dirs if d not in out.get(key, []))
    return out or None


def model_search_paths(comfy_root: str) -> Dict[str, List[str]]:
    """逻辑目录名 → 该目录的真实搜索路径列表。

    优先用 extra_model_paths.yaml；没有再退回 <comfy_root>/models/<名字>。
    """
    extra = load_extra_model_paths(comfy_root)
    if extra:
        return extra
    base = os.path.join(comfy_root, "models") if comfy_root else ""
    names = set()
    for folders in MODEL_FOLDERS.values():
        names.update(folders)
    return {n: [os.path.join(base, n)] for n in names if base}


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def collect_models(graph: Any, comfy_root: str) -> List[ModelRef]:
    """把工作流里引用的所有模型文件找出来，带真实字节数。"""
    idx = ModelIndex(comfy_root)
    out: List[ModelRef] = []
    seen: set = set()
    for n in getattr(graph, "nodes", []):
        if getattr(n, "is_note", False):
            continue
        try:
            pairs = list(n.widget_pairs())
        except Exception:
            continue
        for k, v in pairs:
            key = str(k)
            if key in NOT_MODEL_KEYS or key not in ALL_MODEL_KEYS:
                continue
            if not isinstance(v, str) or not v.strip():
                continue
            # 目录判定顺序：节点类型精确 → 控件名 → 全目录兜底
            folders = (NODE_FOLDERS.get(n.type, {}).get(key)
                       or MODEL_FOLDERS.get(key))
            dup = (n.id, key, v)
            if dup in seen:
                continue
            seen.add(dup)
            folder, path, size = idx.find(v, folders)
            if not path and folders is not None:
                # 兜底：控件名的映射可能不对，全目录再搜一次
                folder, path, size = idx.find(v, sorted(idx.folders))
            out.append(ModelRef(node_id=n.id, node_type=n.type, widget=key,
                                value=v, folder=folder, path=path, size=size,
                                found=bool(path),
                                role="patch" if key in PATCH_KEYS else "resident"))
    return out


# ---------------------------------------------------------------- 负载估算

#: 潜空间下采样倍率（像素 → 潜空间）
LATENT_DOWN = 8.0

#: 各架构的潜空间通道数，用来算潜张量大小
LATENT_CHANNELS = {
    "flux": 16, "sd3": 16, "sd3.5": 16, "qwen": 16, "z-image": 16,
    "sd15": 4, "sdxl": 4, "sd2": 4, "v1": 4, "v2": 4,
}

#: 中间张量系数：潜张量之外还要活着的激活量，按架构估。
#: 这是**估的**（±50%），好在权重项通常占 95%+，它影响很小。
ACT_COEF = {"dit": 4.0, "unet": 6.0, "unknown": 6.0}

#: 运行时固定开销：CUDA context + 驱动 + 碎片
OVERHEAD_VRAM = (400 * MIB, 1024 * MIB)

#: 分辨率提醒阈值（百万像素）。
#:
#: ⚠ 这是**经验提醒，不是算出来的值**。大 DiT 的注意力工作集随 token 数
#: 超线性增长，而本模块的激活项是**线性**估算 —— 它算不出"这一步要跑多久"。
#: 实测上这条线比显存总量更能决定"跑不跑得动"：8 GB 卡配 20B 级视频模型，
#: 约 0.5 MP 还行，1.2 MP 就会一步卡几十分钟。
#: 阈值可用 CWF_ATTN_MP 覆盖，设 0 关掉提醒。
ATTN_MP_DEFAULT = 1.0


def attn_ceiling_mp() -> float:
    raw = os.environ.get("CWF_ATTN_MP")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return ATTN_MP_DEFAULT

#: 视频类节点常见的帧数控件名
FRAME_KEYS = ("length", "num_frames", "frames", "video_frames", "batch_size")

#: 峰值激活只跟"一次处理多少帧"有关，跟整段视频多长无关 ——
#: 视频模型都是按时间分块跑的。所以算激活时把帧数截到这个上限，
#: 否则一条 637 帧的长片会被算成天文数字。81 是常见视频 DiT 的分块长度量级。
FRAME_CHUNK_CAP = 81


@dataclass
class LoadReport:
    """一张工作流的负载画像。"""

    models: List[ModelRef] = field(default_factory=list)
    weights: int = 0                  # 所有权重合计（磁盘字节，实测）
    activation: int = 0               # 激活项（估）
    overhead: int = 0                 # 运行时开销（估）

    width: int = 0                    # 潜空间宽（像素）
    height: int = 0
    frames: int = 1
    latent_bytes: int = 0
    arch_hint: str = "unknown"

    missing: List[ModelRef] = field(default_factory=list)
    patches: List[ModelRef] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def biggest(self) -> int:
        return max((m.size for m in self.models), default=0)

    # ---- 两种运行模式的峰值
    @property
    def vram_full(self) -> int:
        """全部权重都驻留显存时的峰值（没开流式加载就是这个）。"""
        return self.weights + self.activation + (OVERHEAD_VRAM[0] + OVERHEAD_VRAM[1]) // 2

    @property
    def vram_streamed(self) -> int:
        """动态加载（按块流式）时的峰值：只要求装得下最大的一块。

        观察到 ComfyUI 的 `prepared for dynamic VRAM loading` 机制会把
        权重常驻内存、按块送显存，所以显存只需要放下"最大的那个模型的一小块"
        —— 保守按最大模型的一半算，再加激活和开销。
        """
        return self.biggest // 2 + self.activation + OVERHEAD_VRAM[0]

    @property
    def ram(self) -> int:
        """内存需求：动态加载要求权重全量驻留内存，所以就是权重合计 + 余量。"""
        return self.weights + self.activation // 4 + 512 * MIB

    def as_dict(self) -> Dict[str, Any]:
        return {
            "weights": self.weights, "activation": self.activation,
            "overhead": self.overhead, "vram_full": self.vram_full,
            "vram_streamed": self.vram_streamed, "ram": self.ram,
            "width": self.width, "height": self.height,
            "pixels": self.pixels, "frames": self.frames,
            "latent_bytes": self.latent_bytes, "arch_hint": self.arch_hint,
            "models": [m.as_dict() for m in self.models],
            "patches": [p.as_dict() for p in self.patches],
            "missing": [m.value for m in self.missing],
            "notes": self.notes,
        }


def _guess_arch(models: List[ModelRef], graph: Any) -> str:
    """从模型文件名猜架构 —— 只用来选潜空间通道数和激活系数。"""
    blob = " ".join(m.value.lower() for m in models)
    if any(k in blob for k in ("flux", "sd3", "qwen_image", "qwen-image",
                               "z-image", "zimage", "z_image", "hidream",
                               "minimax", "hunyuan", "wan", "ltx", "mochi",
                               "cogvideo", "h3")):
        return "dit"
    if any(k in blob for k in ("sdxl", "sd_xl", "xl_base", "sd15", "v1-5",
                               "v1.5", "v2-1", "anime")):
        return "unet"
    return "unknown"


def _latent_shape(graph: Any) -> Tuple[int, int, int, int, str]:
    """找出潜空间的 (宽, 高, 帧数, 通道, 来源说明)。

    从 EmptyLatentImage 一类的节点读；视频工作流还会在别处给帧数。
    找不到就返回 0，调用方据此降低置信度。
    """
    w = h = 0
    frames = 1
    ch = 4
    src = ""
    for n in getattr(graph, "nodes", []):
        t = (getattr(n, "type", "") or "").lower()
        if not any(k in t for k in ("emptylatent", "latentimage",
                                    "emptyvideo", "emptyhunyuan",
                                    "emptysd3", "emptyflux", "wanempty")):
            continue
        try:
            pairs = dict(n.widget_pairs())
        except Exception:
            continue
        if "width" in pairs and "height" in pairs:
            try:
                w = int(float(pairs["width"]))
                h = int(float(pairs["height"]))
                src = "%s#%s" % (getattr(n, "type", "?"), n.id)
            except (TypeError, ValueError):
                continue
            for fk in FRAME_KEYS:
                if fk in pairs:
                    try:
                        frames = max(1, int(float(pairs[fk])))
                    except (TypeError, ValueError):
                        pass
                    if fk != "batch_size":
                        break
            break
    # 退路：不是所有工作流都用 EmptyLatentImage。
    # 导演台/一体化这类工作流把分辨率写在自定义节点里，名字五花八门，
    # 所以直接找"任何一个同时带宽和高控件的节点"，取像素数最大的那个
    # —— 峰值由最大那张图决定。
    if not (w and h):
        best = 0
        for n in getattr(graph, "nodes", []):
            try:
                pairs = dict(n.widget_pairs())
            except Exception:
                continue
            ww = hh = 0
            for wk in ("width", "W", "img_width", "video_width"):
                if wk in pairs:
                    try:
                        ww = int(float(pairs[wk]))
                    except (TypeError, ValueError):
                        pass
                    break
            for hk in ("height", "H", "img_height", "video_height"):
                if hk in pairs:
                    try:
                        hh = int(float(pairs[hk]))
                    except (TypeError, ValueError):
                        pass
                    break
            if ww > 0 and hh > 0 and ww * hh > best:
                best, w, h = ww * hh, ww, hh
                src = "%s#%s" % (getattr(n, "type", "?"), n.id)
                for fk in FRAME_KEYS:
                    if fk in pairs:
                        try:
                            frames = max(1, int(float(pairs[fk])))
                        except (TypeError, ValueError):
                            pass
                        if fk != "batch_size":
                            break

    # 通道数按模型架构挑
    blob = " ".join(str(getattr(n, "type", "")) for n in
                    getattr(graph, "nodes", []))
    for k, v in LATENT_CHANNELS.items():
        if k in blob.lower():
            ch = v
            break
    return w, h, frames, ch, src


def estimate_load(graph: Any, comfy_root: str,
                  device: Optional[Device] = None) -> LoadReport:
    """算一张工作流的负载。权重项实测，其余按声明的方式估。"""
    rep = LoadReport()
    rep.models = collect_models(graph, comfy_root)
    # 同一个文件被多个节点引用时只算一次 —— 权重只加载一份
    by_path: Dict[str, ModelRef] = {}
    seen_patch: set = set()
    for m in rep.models:
        if not m.found:
            if m not in rep.missing:
                rep.missing.append(m)
            continue
        key = os.path.normcase(m.path)
        if m.role == "patch":
            # 补丁合并进基座，不额外增加常驻峰值；但同一份补丁只报一次
            if key not in seen_patch:
                seen_patch.add(key)
                rep.patches.append(m)
            continue
        if key not in by_path or m.size > by_path[key].size:
            by_path[key] = m
    rep.weights = sum(m.size for m in by_path.values())

    arch = _guess_arch(rep.models, graph)
    rep.arch_hint = arch

    w, h, frames, ch, src = _latent_shape(graph)
    rep.width, rep.height = w, h
    rep.frames = frames
    if w and h:
        lw = max(1, int(w / LATENT_DOWN))
        lh = max(1, int(h / LATENT_DOWN))
        eff_frames = min(frames, FRAME_CHUNK_CAP)
        rep.latent_bytes = lw * lh * ch * eff_frames * 2      # bf16
        coef = ACT_COEF.get(arch, ACT_COEF["unknown"])
        rep.activation = int(rep.latent_bytes * coef)
        note = ("潜空间 %d×%d × %d 通道（来自 %s）"
                % (w, h, ch, src))
        if frames > 1:
            note += "，总帧数 %d" % frames
            if frames > eff_frames:
                note += "（激活按分块 %d 帧算 —— 视频模型不一次性处理整段）" % eff_frames
        note += "；激活项按 ×%.1f 系数估，不确定度 ±50%%" % coef
        rep.notes.append(note)
    else:
        rep.notes.append("没找到潜空间尺寸，激活项按 0 算 —— 峰值估偏小")

    rep.overhead = sum(OVERHEAD_VRAM) // 2
    if rep.missing:
        rep.notes.append(
            "有 %d 个引用的模型在本机模型库里找不到，权重合计会偏小"
            % len(rep.missing))
    return rep


# ---------------------------------------------------------------- 关系判定

@dataclass
class Fit:
    """负载与能力的关系。"""

    level: str = ""          # 轻松 / 够用 / 偏紧 / 不够 / 未知
    vram_ok: bool = False
    ram_ok: bool = False
    vram_margin: int = 0
    ram_margin: int = 0
    bottleneck: str = ""
    advice: List[str] = field(default_factory=list)
    streaming_likely: bool = False


def evaluate(load: LoadReport, dev: Device) -> Fit:
    """把负载和能力对起来，给出余量与瓶颈。"""
    f = Fit()
    if not dev.vram_total and not dev.ram_total:
        f.level = "未知"
        f.bottleneck = "没拿到设备数据，只能看负载"
        return f

    vram_cap = dev.vram_usable or dev.vram_total
    need_vram_stream = load.vram_streamed
    need_vram_full = load.vram_full
    need_ram = load.ram

    # 权重装不进显存 → 必然走流式（ComfyUI 会自动降级）
    f.streaming_likely = load.weights > vram_cap
    need_vram = need_vram_stream if f.streaming_likely else need_vram_full

    f.vram_margin = vram_cap - need_vram
    f.ram_margin = dev.ram_total - need_ram
    f.vram_ok = f.vram_margin >= 0
    f.ram_ok = f.ram_margin >= 0

    if not f.ram_ok:
        f.level = "不够"
        f.bottleneck = "内存"
    elif not f.vram_ok:
        f.level = "不够"
        f.bottleneck = "显存"
    else:
        vr = f.vram_margin / max(vram_cap, 1)
        rr = f.ram_margin / max(dev.ram_total, 1)
        worst = min(vr, rr)
        f.bottleneck = "显存" if vr <= rr else "内存"
        if worst >= 0.30:
            f.level = "轻松"
        elif worst >= 0.12:
            f.level = "够用"
        else:
            f.level = "偏紧"

    # ---- 建议：按瓶颈给出真正能降负载的招
    if f.streaming_likely:
        f.advice.append(
            "权重 %.1f GB > 可用显存 %.1f GB，必然会走动态流式加载 —— "
            "能跑，但速度会被显存带宽拖住"
            % (load.weights / GIB, vram_cap / GIB))
    if not f.ram_ok:
        f.advice.append(
            "内存差 %.1f GB。这台机器已经在靠交换文件硬撑，"
            "速度会掉得很厉害。降负载最有效的一条是换更小的量化版本"
            % (-f.ram_margin / GIB))
    elif dev.ram_total and f.ram_margin < dev.ram_total * 0.10:
        f.advice.append(
            "内存只剩 %.1f GB 余量，跑起来容易触发交换。"
            "同时别开浏览器和大程序" % (f.ram_margin / GIB))
    if load.frames > 1 and load.activation > 128 * MIB:
        f.advice.append(
            "激活项 %s 主要来自 %d 帧 —— 缩短帧数能直接降这块"
            % (gb(load.activation), load.frames))
    if load.missing:
        f.advice.append(
            "有 %d 个模型没在本机找到，实际负载可能比这里算的高"
            % len(load.missing))

    # 分辨率提醒 —— 明说是经验值，不是本工具算出来的
    ceil_mp = attn_ceiling_mp()
    if ceil_mp and load.pixels > ceil_mp * 1e6:
        f.advice.append(
            "分辨率 %.2f MP 超过经验线 %.1f MP：大模型的注意力工作集随 token 数"
            "超线性增长，而本工具的激活项是**线性**估算，算不准这一块。"
            "实测上这条线比显存总量更能决定跑不跑得动 —— 超过后常见的表现是"
            "某一步卡住几十分钟。要调这条线：设 CWF_ATTN_MP"
            % (load.pixels / 1e6, ceil_mp))
    return f


# ---------------------------------------------------------------- 日志挖掘

_RE_STAGED = re.compile(
    r"Model\s+([A-Za-z0-9_.\-]+)\s+prepared for dynamic VRAM loading\.\s*"
    r"([0-9]+)\s*MB Staged")
_RE_UNLOAD = re.compile(r"([0-9]+) models? unloaded")
_RE_SIT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(it/s|s/it)")
_RE_PROMPT = re.compile(r"Prompt executed in ([0-9.]+) seconds")
_RE_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


@dataclass
class LogFacts:
    """从 ComfyUI 日志里挖出来的实测事实。"""

    staged: Dict[str, int] = field(default_factory=dict)   # 模型类名 → MB
    unload_events: int = 0
    sit: List[float] = field(default_factory=list)         # 秒/迭代
    prompt_seconds: List[float] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"staged_mb": self.staged, "unload_events": self.unload_events,
                "s_per_it": self.sit[-10:],
                "prompt_seconds": self.prompt_seconds[-10:]}


def find_logs(comfy_root: str, extra: Sequence[str] = ()) -> List[str]:
    """找 ComfyUI 的日志文件。"""
    pats = list(extra)
    if comfy_root:
        user = os.path.join(comfy_root, "user")
        pats += [os.path.join(user, "comfyui*.log"),
                 os.path.join(comfy_root, "comfyui*.log")]
        pats += [os.path.join(comfy_root, "logs", "*.log")]
    out: List[str] = []
    for p in pats:
        for hit in glob.glob(p):
            if hit not in out:
                out.append(hit)
    return out


def mine_logs(paths: Sequence[str], max_bytes: int = 8 * MIB) -> LogFacts:
    """从日志里挖实测数据：模型真实占用、卸载次数、迭代速度。

    大日志只读尾部 —— 我们要的是最近的运行情况，不是全部历史。
    """
    facts = LogFacts()
    for p in paths:
        try:
            sz = os.path.getsize(p)
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                if sz > max_bytes:
                    f.seek(sz - max_bytes)
                    f.readline()          # 丢掉半行
                text = f.read()
        except OSError:
            continue
        for m in _RE_STAGED.finditer(text):
            name, mb = m.group(1), int(m.group(2))
            # 同名模型取最近一次的读数
            facts.staged[name] = mb
        facts.unload_events += sum(
            1 for m in _RE_UNLOAD.finditer(text) if m.group(1) != "0")
        for m in _RE_SIT.finditer(text):
            v, unit = float(m.group(1)), m.group(2)
            if unit == "s/it" and 0 < v < 100000:
                facts.sit.append(v)
            elif unit == "it/s" and v > 0:
                facts.sit.append(1.0 / v)
        for m in _RE_PROMPT.finditer(text):
            try:
                facts.prompt_seconds.append(float(m.group(1)))
            except ValueError:
                pass
    return facts


def attach_measurements(dev: Device, facts: LogFacts) -> None:
    """把日志实测塞进设备画像。"""
    dev.measured_sit = sorted(set(round(v, 2) for v in facts.sit))
    if facts.prompt_seconds:
        dev.measured_runs = [(t, "Prompt executed") for t in
                             facts.prompt_seconds[-5:]]


# ---------------------------------------------------------------- 校准

CALIB_FILE = "calibration.json"


def calibration_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, CALIB_FILE)


def load_calibration(cache_dir: str) -> Dict[str, Any]:
    try:
        with open(calibration_path(cache_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_measurement(cache_dir: str, key: str,
                     payload: Dict[str, Any]) -> None:
    """记下一次实测。下次同配置就能用实测值代替估算值。"""
    data = load_calibration(cache_dir)
    data[key] = dict(payload, ts=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(calibration_path(cache_dir), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def match_staged(load: LoadReport, facts: LogFacts,
                 tol: float = 0.04) -> List[Tuple[str, int, "ModelRef"]]:
    """把日志里的 staged 记录按**字节大小**匹配到本次工作流的模型文件。

    返回 [(日志里的类名, MB, 匹配到的 ModelRef), ...]

    ## 为什么必须匹配，不能直接相加

    日志里存的是这台机器跑过的**所有**运行的记录（而且只留最近一次读数）。
    拿一条 37 GB 视频运行的记录去"校准"一张 5 GB 的图，会算出 -86% 的偏差
    —— 那个数字纯属胡说八道，比不给校准更糟，会直接毁掉用户对整套数字的信任。
    这个坑实测踩过。

    ## 为什么可以按大小配对

    staged 的 MB 与磁盘字节是同一口径（实测差 <0.2%，见模块开头）。
    所以大小相同基本就是同一个文件，配对可靠。
    """
    pairs: List[Tuple[str, int, "ModelRef"]] = []
    used: set = set()
    resident = [m for m in load.models if m.found and m.role == "resident"]
    for name, mb in sorted(facts.staged.items(), key=lambda kv: -kv[1]):
        target = mb * MIB
        best, best_diff = None, None
        for m in resident:
            if id(m) in used:
                continue
            d = abs(m.size - target) / max(target, 1)
            if d <= tol and (best_diff is None or d < best_diff):
                best, best_diff = m, d
        if best is not None:
            used.add(id(best))
            pairs.append((name, mb, best))
    return pairs


def compare_with_log(load: LoadReport, facts: LogFacts,
                     tol: float = 0.04) -> List[str]:
    """把估算和日志实测对一下，给出校准结论。

    这是整个模块可信度的来源：如果实测和估算差得远，就当场说出来，
    而不是让用户以为估得准。**配对不上就什么都不说** —— 宁可沉默，
    也不要拿别的运行记录来编一个偏差百分比。
    """
    pairs = match_staged(load, facts, tol)
    if not pairs:
        return []
    measured = sum(mb for _n, mb, _m in pairs) * MIB
    ours = sum(m.size for _n, _m, m in pairs)
    if not ours or not measured:
        return []
    diff = (ours - measured) / measured * 100
    out = ["校准：本机日志里认出本次用到的 %d 个模型（按文件大小配对）" % len(pairs)]
    out.append("  实测合计 %s · 本次估算 %s · 差 %+.1f%%"
               % (gb(measured), gb(ours), diff))
    for name, mb, m in pairs:
        out.append("    %-22s 日志实测 %6d MB · 我们读磁盘 %6d MB"
                   % (name, mb, m.size // MIB))
    return out
