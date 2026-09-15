# -*- coding: utf-8 -*-
"""
cwf 自测骨架 —— 零依赖，python tests/run_tests.py 直接跑。

为什么不用 pytest：本工具承诺只用标准库，测试也照这个规矩来。
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cwf.lib import paths

WF_ROOT = paths.default_workflow_root()


def _discover_samples() -> List[str]:
    """从用户自己的工作流库里挑几张「有代表性的真图」当样本。

    刻意不写死任何文件名 —— 这是通用工具，测试不该依赖某一个人的
    工作流命名习惯。挑法：按字节大小取大中小三档各若干张，
    这样既覆盖复杂大图，也覆盖小图。挑不到就返回空表，相关用例自动跳过。
    """
    if not WF_ROOT or not os.path.isdir(WF_ROOT):
        return []
    found: List[Tuple[int, str]] = []
    for dirpath, dirnames, filenames in os.walk(WF_ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.lower().endswith(".json"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(full)
            except OSError:
                continue
            # 太小的多半是空壳/片段，对布局测试没意义
            if sz < 2048:
                continue
            found.append((sz, os.path.relpath(full, WF_ROOT)))
    if not found:
        return []
    found.sort(reverse=True)
    n = len(found)
    picks: List[str] = []
    # 大中小三档
    for idx in (0, n // 4, n // 2, (3 * n) // 4, n - 1):
        rel = found[idx][1]
        if rel not in picks:
            picks.append(rel)
    return picks


#: 用作「有代表性的真图」的样本（自动发现；没有工作流库时为空，用例自动跳过）
SAMPLE_WORKFLOWS = _discover_samples()


class Failure(AssertionError):
    pass


class Case:
    """一个测试用例。"""

    def __init__(self, name: str, fn: Callable[[], Any], group: str = ""):
        self.name = name
        self.fn = fn
        self.group = group


class Suite:
    def __init__(self):
        self.cases: List[Case] = []
        self._group = ""

    def group(self, name: str) -> "Suite":
        self._group = name
        return self

    def __call__(self, arg):
        """两种写法都支持：
            @suite("用例名")   → 返回装饰器（用例名以显式给的为准）
            @suite             → 直接用函数名当用例名
        """
        if callable(arg):
            name = arg.__name__.strip("_").replace("_", " ")
            # 若这个函数已经被显式命名注册过（先 @suite("名") 再套一层），就不再重复注册
            if not any(c.fn is arg for c in self.cases):
                self.cases.append(Case(name, arg, self._group))
            return arg
        return self.case(str(arg))

    def case(self, name: str):
        def deco(fn: Callable[[], Any]) -> Callable[[], Any]:
            self.cases.append(Case(name, fn, self._group))
            return fn
        return deco


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def eq(a: Any, b: Any, msg: str = "") -> None:
    if a != b:
        raise Failure(f"{msg + ': ' if msg else ''}期望 {b!r}，实际 {a!r}")


#: 没有工作流库时的跳过说明。用例需要真实工作流当样本，
#: 缺了就该**跳过**而不是失败 —— 否则新克隆仓库的人会看到一片红灯，
#: 误以为工具是坏的。
NO_LIBRARY_MSG = (
    "跳过：没找到 ComfyUI 工作流库。设 CWF_WORKFLOWS 指向 "
    "<ComfyUI>/user/default/workflows 后重跑，即可覆盖这批用例。")


def require_library() -> None:
    """没有工作流库就跳过当前用例。"""
    if not WF_ROOT or not os.path.isdir(WF_ROOT):
        skip(NO_LIBRARY_MSG)


def sample_paths(limit: Optional[int] = None) -> List[str]:
    """挑几张有代表性的真图。取不到就跳过当前用例。"""
    require_library()
    out = []
    for rel in SAMPLE_WORKFLOWS:
        p = os.path.join(WF_ROOT, rel)
        if os.path.exists(p):
            out.append(p)
    if not out:
        skip("跳过：工作流库里没找到合适的样本图（可能都是小文件）")
    if limit:
        out = out[:limit]
    return out


def all_workflows(max_files: int = 0) -> List[str]:
    """全库 .json 列表。库不存在或为空就跳过当前用例。"""
    require_library()
    import glob
    files = sorted(glob.glob(os.path.join(WF_ROOT, "**", "*.json"), recursive=True))
    if not files:
        skip("跳过：工作流库里一个 .json 都没有")
    return files[:max_files] if max_files else files


def has_comfy() -> bool:
    try:
        from cwf.lib.schema import registry
        return len(registry(quiet=True)) > 0
    except Exception:
        return False


def run(suite: Suite, verbose: bool = False) -> int:
    passed = 0
    failed: List[Tuple[str, str]] = []
    skipped: List[Tuple[str, str]] = []
    t_start = time.time()
    last_group = None
    for c in suite.cases:
        if c.group != last_group:
            print(f"\n── {c.group or '未分组'} " + "─" * max(0, 46 - len(c.group or "")))
            last_group = c.group
        t0 = time.time()
        try:
            c.fn()
            dt = time.time() - t0
            passed += 1
            print(f"  ✓ {c.name}" + (f"  ({dt*1000:.0f}ms)" if dt > 0.4 else ""))
        except Skipped as e:
            skipped.append((c.name, str(e)))
            print(f"  ○ {c.name}  跳过：{e}")
        except Failure as e:
            failed.append((c.name, str(e)))
            print(f"  ✗ {c.name}\n        {e}")
        except Exception as e:
            failed.append((c.name, f"{type(e).__name__}: {e}"))
            print(f"  ✗ {c.name}  （异常）")
            if verbose:
                traceback.print_exc()
            else:
                tb = traceback.format_exc().strip().splitlines()
                for line in tb[-4:]:
                    print(f"        {line.strip()}")
    dt = time.time() - t_start
    print("\n" + "═" * 56)
    print(f"通过 {passed} · 失败 {len(failed)} · 跳过 {len(skipped)} · 用时 {dt:.1f}s")
    if failed:
        print("\n失败清单：")
        for name, msg in failed:
            print(f"  ✗ {name}: {msg}")
    return 1 if failed else 0


class Skipped(Exception):
    pass


def skip(reason: str) -> None:
    raise Skipped(reason)
