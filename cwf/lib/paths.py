# -*- coding: utf-8 -*-
"""路径探测 —— 本文件不写死任何机器相关目录。

ComfyUI 的安装方式太杂：官方 zip、git clone、Desktop、便携版、各种整合包，
工作流目录可能藏在任意深度。所以这里不猜「ComfyUI 装在哪」，而是直接找
目标物：形如 ``.../user/default/workflows`` 的目录。

搜索策略（有界，不会扫全盘）：

1. 环境变量 ``CWF_WORKFLOWS`` / ``CWF_COMFY`` 最优先，随时可覆盖
2. 从若干候选根**广度优先**下探，深度上限 4 层
3. 下探时的剪枝规则：
   - 第 0、1 层自由下探（这样 ``D:\\第三方目录\\ComfyUI`` 这种能进去）
   - 第 2 层起只走「名字里带 comfy」的分支
   - 系统目录、缓存目录、依赖目录一律跳过
   - 单层目录数超上限就放弃该层，避免撞上巨型目录树
4. 找不到就返回空串 —— 调用方负责提示「请设置 CWF_WORKFLOWS」，
   而不是悄悄用一个不存在的默认值
"""
from __future__ import annotations

import os
import sys
from collections import deque
from typing import List, Optional

#: 从 ComfyUI 根目录到工作流目录的相对后缀
WF_SUFFIX = os.path.join("user", "default", "workflows")

#: 下探深度上限。<ComfyUI>/user/default/workflows 需要 4 层。
MAX_DEPTH = 4

#: 单层目录条目上限。列目录本身很便宜，这里只是防「一层几万条」的极端情况；
#: 真正控制搜索规模的是「第 2 层起只走带 comfy 线索的分支」这条规则。
MAX_ENTRIES = 4000

#: 一次探测最多访问多少个目录（兜底，保证最坏情况也有界）
MAX_VISITS = 6000

#: 一律不进这些目录（系统 / 缓存 / 依赖 / 版本控制）
SKIP_DIRS = {
    # Windows 系统
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
    "windows.old", "msys64", "drivers", "intel", "amd", "nvidia",
    # 缓存 / 运行时
    "appdata", "temp", "tmp", "cache", "caches", "logs", "crashdumps",
    "node_modules", "site-packages", "__pycache__", ".git", ".svn", ".hg",
    ".venv", "venv", "env", ".conda", "conda-meta", ".cache", ".npm",
    ".cargo", ".rustup", ".gradle", ".m2", ".vscode", ".idea",
    # 明显无关
    "scoop", "chocolatey", "winsxs", "assembly", "installer",
}

#: 名字里带这些词的目录值得优先下探
COMFY_HINTS = ("comfy", "comfyui")

#: 探测结果缓存（cli 在 import 期就会调用，别重复扫）
_CACHE: dict = {}


def _is_dir(p: str) -> bool:
    try:
        return os.path.isdir(p)   # 会跟随符号链接 / junction
    except OSError:
        return False


def _looks_like_comfy(root: str) -> bool:
    """判断一个目录是不是 ComfyUI 根（有 main.py 或 comfy 或 models 等）。"""
    if not _is_dir(root):
        return False
    for marker in ("main.py", "comfy", "models", "custom_nodes", "user"):
        if os.path.exists(os.path.join(root, marker)):
            return True
    return False


def _candidate_roots() -> List[str]:
    """列出「可能装着 ComfyUI」的起点目录，靠前的优先。"""
    home = os.path.expanduser("~")
    out: List[str] = [
        home,
        os.path.join(home, "Documents"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "comfy"),
        os.path.join(home, "ComfyUI"),
        os.path.join(home, "ai"),
        os.path.join(home, "AI"),
        os.path.join(home, "workspace"),
    ]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        local = os.environ.get("LOCALAPPDATA")
        if appdata:
            out.append(os.path.join(appdata, "ComfyUI"))
        if local:
            out.append(os.path.join(local, "Programs"))
            out.append(os.path.join(local, "ComfyUI"))
        for drive in ("C:", "D:", "E:", "F:", "G:"):
            out.append(drive + os.sep)
    else:
        out += ["/opt", "/usr/local", "/srv", "/data", "/mnt", "/media", "/home"]
    return [p for p in out if _is_dir(p)]


def _bfs_workflow_dirs(base: str) -> List[str]:
    """从 base 出发广度优先找 ``user/default/workflows``，返回找到的目录。"""
    found: List[str] = []
    seen = set()
    visits = 0
    queue = deque([(base, 0, False)])
    while queue:
        if visits >= MAX_VISITS:
            break
        d, depth, in_comfy = queue.popleft()
        visits += 1
        try:
            key = os.path.realpath(d)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)

        # 先看本目录是不是就是目标
        wf = os.path.join(d, WF_SUFFIX)
        if _is_dir(wf):
            found.append(wf)
            continue

        if depth >= MAX_DEPTH:
            continue

        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        if len(entries) > MAX_ENTRIES:
            # 极端巨型目录：带 comfy 线索的优先，剩下的按序补足到上限
            hinted = [e for e in entries
                      if any(h in e.lower() for h in COMFY_HINTS)]
            rest = [e for e in entries if e not in set(hinted)]
            entries = (hinted + rest)[:MAX_ENTRIES]

        # 本层是否已经在 comfy 线索里
        here = in_comfy or any(h in os.path.basename(d).lower() for h in COMFY_HINTS)

        for e in entries:
            if e.startswith(".") or e.lower() in SKIP_DIRS:
                continue
            p = os.path.join(d, e)
            if not _is_dir(p):
                continue
            child_comfy = here or any(h in e.lower() for h in COMFY_HINTS)
            # 第 0、1 层自由下探；更深只走带线索的分支
            if depth >= 2 and not child_comfy:
                continue
            queue.append((p, depth + 1, child_comfy))
    return found


def find_workflow_roots() -> List[str]:
    """返回所有探测到的工作流库根目录（去重，靠前的优先）。"""
    if "wf_roots" in _CACHE:
        return _CACHE["wf_roots"]
    hits: List[str] = []

    def push(p: str) -> None:
        if p and p not in hits:
            hits.append(p)

    for cand in _candidate_roots():
        # 候选本身可能直接就是工作流目录（比如用户把 CWF_WORKFLOWS 的父级放这）
        if _is_dir(os.path.join(cand, WF_SUFFIX)):
            push(os.path.join(cand, WF_SUFFIX))
        for wf in _bfs_workflow_dirs(cand):
            push(wf)
    _CACHE["wf_roots"] = hits
    return hits


def find_comfy_roots() -> List[str]:
    """返回探测到的 ComfyUI 安装根目录（工作流目录往上退三层）。"""
    roots: List[str] = []
    for wf in find_workflow_roots():
        # .../user/default/workflows → 上退三层
        root = os.path.dirname(os.path.dirname(os.path.dirname(wf)))
        if root and root not in roots and _looks_like_comfy(root):
            roots.append(root)
    for cand in _candidate_roots():
        if "comfyui" in os.path.basename(cand).lower() and _looks_like_comfy(cand):
            if cand not in roots:
                roots.append(cand)
    return roots


def default_workflow_root() -> str:
    """工作流库根。探测不到时返回空串。"""
    env = os.environ.get("CWF_WORKFLOWS")
    if env:
        return env
    hits = find_workflow_roots()
    return hits[0] if hits else ""


def comfy_root() -> str:
    """ComfyUI 安装根（用于定位 models/ custom_nodes/ 等）。找不到返回空串。"""
    env = os.environ.get("CWF_COMFY")
    if env:
        return env
    roots = find_comfy_roots()
    return roots[0] if roots else ""


def tool_home() -> str:
    """cwf 工具自身所在目录（仓库根）。"""
    env = os.environ.get("CWF_HOME")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))       # cwf/lib
    return os.path.dirname(os.path.dirname(here))           # 仓库根


def user_cache_dir() -> str:
    """用户级缓存/配置目录（节点库、别名、分区表都放这）。"""
    env = os.environ.get("CWF_CACHE")
    if env:
        return env
    if sys.platform == "win32":
        base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache")
    return os.path.join(base, ".cwf")


def describe() -> str:
    """人话描述当前探测结果，出错时给用户看。"""
    root = default_workflow_root()
    if root:
        return "工作流库: %s" % root
    return ("没找到 ComfyUI 工作流目录。请设置环境变量 CWF_WORKFLOWS 指向它，例如:\n"
            "  Windows:      set CWF_WORKFLOWS=D:\\ComfyUI\\user\\default\\workflows\n"
            "  Linux/macOS:  export CWF_WORKFLOWS=~/ComfyUI/user/default/workflows")


def reset_cache() -> None:
    """清掉探测缓存（测试用）。"""
    _CACHE.clear()
