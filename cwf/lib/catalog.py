# -*- coding: utf-8 -*-
"""
cwf.catalog —— 本机节点库（node catalog）

回答三个问题：
  1. 我电脑上到底有多少种节点？
  2. 每种节点是干嘛的、怎么接线、有哪些控件？
  3. 这些节点分别来自哪个插件包？

数据从两处拿：
  * `/object_info`：节点类型、分类、端口、控件、可选值 —— 这是权威定义；
  * 你的工作流 JSON：每个节点上都带 `properties.cnr_id` / `aux_id`，
    这是**唯一**能反查「这个节点属于哪个插件包」的线索（object_info 里没有包名）。

结果缓存在 ~/.cwf/node_catalog.json，带版本戳 + 工作流库指纹，
ComfyUI 升级或工作流库变了才重建。
"""
from __future__ import annotations

import glob
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .graph import CwfError, Graph, is_frontend_only, is_note_type
from .schema import CACHE_DIR, Registry, load_object_info
from .terms import zh_category

CATALOG_PATH = os.path.join(CACHE_DIR, "node_catalog.json")
CATALOG_VERSION = 4


# ---------------------------------------------------------------- 扫描工作流


def scan_workflows(root: str, limit: int = 4000) -> Dict[str, Any]:
    """扫工作流库，统计「类型 → 插件包」的对应关系与使用频次。"""
    pkg_of: Dict[str, Dict[str, int]] = {}
    usage: Dict[str, int] = {}
    packages: Dict[str, Dict[str, Any]] = {}
    files = 0
    nodes = 0
    for p in glob.glob(os.path.join(root, "**", "*.json"), recursive=True)[:limit]:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if not isinstance(d, dict) or "nodes" not in d:
            continue
        files += 1
        for nd in d.get("nodes") or []:
            t = nd.get("type")
            if not t or is_note_type(t):
                continue
            nodes += 1
            usage[t] = usage.get(t, 0) + 1
            props = nd.get("properties") or {}
            pkg = props.get("cnr_id") or props.get("aux_id") or ""
            if not pkg:
                pkg = "comfy-core" if not props.get("aux_id") else "unknown"
            pkg_of.setdefault(t, {})
            pkg_of[t][pkg] = pkg_of[t].get(pkg, 0) + 1
            entry = packages.setdefault(str(pkg), {"nodes": set(), "uses": 0})
            entry["nodes"].add(t)
            entry["uses"] += 1
    return {
        "files": files,
        "nodes": nodes,
        "pkg_of": pkg_of,
        "usage": usage,
        "packages": {k: {"types": sorted(v["nodes"]), "n_types": len(v["nodes"]),
                         "uses": v["uses"]} for k, v in packages.items()},
    }


def _fingerprint(root: str) -> str:
    try:
        n = 0
        latest = 0.0
        for p in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
            n += 1
            try:
                latest = max(latest, os.path.getmtime(p))
            except OSError:
                pass
        return f"{n}:{int(latest)}"
    except Exception:
        return "0:0"


# ---------------------------------------------------------------- 建库


def build_catalog(server: str, workflows_root: str, refresh: bool = False,
                  use_cache: bool = True) -> Dict[str, Any]:
    if use_cache and not refresh and os.path.exists(CATALOG_PATH):
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                cat = json.load(f)
            if (cat.get("version") == CATALOG_VERSION
                    and cat.get("fingerprint") == _fingerprint(workflows_root)
                    and cat.get("server") == server):
                age = time.time() - cat.get("built", 0)
                if age < 86400:
                    cat["_from_cache"] = True
                    return cat
        except Exception:
            pass

    info, src = load_object_info(server, refresh=refresh)
    reg = Registry(info, src)
    wf = scan_workflows(workflows_root)

    types: Dict[str, Any] = {}
    for t, spec in reg._specs.items():
        pkgs = wf["pkg_of"].get(t) or {}
        # 归属优先级：object_info 的 python_module（权威）→ 工作流里的 cnr_id
        pkg = spec.package
        if not pkg and pkgs:
            pkg = max(pkgs.items(), key=lambda kv: kv[1])[0]
        types[t] = {
            "category": spec.category,
            "category_zh": zh_category(spec.category),
            "category_top": (spec.category or "").split("/")[0],
            "display": spec.display_name,
            "package": pkg,
            "python_module": spec.python_module,
            "usage": wf["usage"].get(t, 0),
            "widgets": [i.name for i in spec.widget_inputs],
            "link_inputs": [f"{i.name}:{i.type}" for i in spec.link_inputs if not i.optional],
            "opt_inputs": [f"{i.name}:{i.type}" for i in spec.link_inputs if i.optional],
            "outputs": [f"{o.name}:{o.type}" for o in spec.outputs],
            "output_node": spec.output_node,
            "deprecated": spec.deprecated,
            "experimental": spec.experimental,
            "aliases": spec.search_aliases[:8],
            "desc": (spec.description or "")[:200],
        }
    # 只出现在工作流里、object_info 没有的（前端注册 / 已卸载插件）
    for t, cnt in wf["usage"].items():
        if t in types:
            continue
        pkgs = wf["pkg_of"].get(t) or {}
        types[t] = {
            "category": "", "category_zh": "", "display": t,
            "package": max(pkgs.items(), key=lambda kv: kv[1])[0] if pkgs else "",
            "usage": cnt, "widgets": [], "link_inputs": [], "opt_inputs": [],
            "outputs": [], "output_node": False, "desc": "",
            "not_in_object_info": True,
            "frontend_only": is_frontend_only(t),
        }

    by_cat: Dict[str, int] = {}
    by_pkg: Dict[str, int] = {}
    deprecated = experimental = output_nodes = 0
    for t, v in types.items():
        top = v.get("category_top") or v.get("category") or "（无分类）"
        by_cat[zh_category(top)] = by_cat.get(zh_category(top), 0) + 1
        by_pkg[v["package"] or "（未标注）"] = by_pkg.get(v["package"] or "（未标注）", 0) + 1
        deprecated += 1 if v.get("deprecated") else 0
        experimental += 1 if v.get("experimental") else 0
        output_nodes += 1 if v.get("output_node") else 0

    cat = {
        "version": CATALOG_VERSION,
        "built": time.time(),
        "built_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "server": server,
        "source": src,
        "workflows_root": workflows_root,
        "fingerprint": _fingerprint(workflows_root),
        "totals": {
            "types": len(types),
            "with_usage": sum(1 for v in types.values() if v["usage"]),
            "packages": len([k for k in by_pkg if k != "（未标注）"]),
            "workflows_scanned": wf["files"],
            "node_instances": wf["nodes"],
            "output_nodes": output_nodes,
            "deprecated": deprecated,
            "experimental": experimental,
        },
        "by_category": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
        "by_package": dict(sorted(by_pkg.items(), key=lambda kv: -kv[1])),
        "types": types,
    }

    # 功能分类（按"能干什么"而不是"谁做的"）—— 写进每个节点
    try:
        from .taxonomy import TAXONOMY_VERSION, apply_to_catalog
        tax = apply_to_catalog(cat)
        tax["version"] = TAXONOMY_VERSION
        cat["taxonomy"] = tax
    except Exception as exc:            # 分类失败不该让整个节点库建不出来
        cat["taxonomy"] = {"error": f"{type(exc).__name__}: {exc}"}

    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        payload = dict(cat)
        payload["types"] = {k: v for k, v in types.items()}
        with open(CATALOG_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(CATALOG_PATH + ".tmp", CATALOG_PATH)
    except Exception:
        pass
    cat["_from_cache"] = False
    return cat


# ---------------------------------------------------------------- 查询


def search(cat: Dict[str, Any], query: str, limit: int = 30,
           in_cat: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
    q = (query or "").strip().lower()
    hits: List[Tuple[int, str, Dict[str, Any]]] = []
    for t, v in cat["types"].items():
        if in_cat and in_cat.lower() not in (v.get("category_zh", "") + v.get("category", "")).lower():
            continue
        blob = " ".join([t, v.get("display", ""), v.get("category", ""),
                         v.get("category_zh", ""), v.get("package", ""),
                         " ".join(v.get("widgets", [])), v.get("desc", "")]).lower()
        if q and q not in blob:
            continue
        score = 0
        if t.lower() == q:
            score = 100
        elif t.lower().startswith(q):
            score = 80
        elif q and q in t.lower():
            score = 60
        elif q and q in v.get("display", "").lower():
            score = 50
        score += min(20, v.get("usage", 0))
        hits.append((score, t, v))
    hits.sort(key=lambda x: (-x[0], x[1]))
    return [(t, v) for _, t, v in hits[:limit]]


def package_report(cat: Dict[str, Any], limit: int = 60) -> List[Tuple[str, int, int]]:
    """(包名, 类型数, 使用次数)。"""
    agg: Dict[str, List[int]] = {}
    for v in cat["types"].values():
        p = v.get("package") or "（未标注）"
        a = agg.setdefault(p, [0, 0])
        a[0] += 1
        a[1] += v.get("usage", 0)
    return sorted(((p, a[0], a[1]) for p, a in agg.items()),
                  key=lambda x: -x[1])[:limit]
