# -*- coding: utf-8 -*-
"""
cwf.dsl —— 给 AI agent 用的「紧凑工作流描述语言」

目标：让 agent 用几行纯文本描述一张能直接跑的 ComfyUI 工作流，
不用手写那几十 KB 的 JSON。

语法（一个节点一行，缩进随意）：

    # 注释
    @title 我的文生图流                # 可选，给整张图起个名
    @meta  krea2 / 768x1280           # 可选，写进 extra.workflow_title

    模型  CheckpointLoaderSimple  ckpt=animagineXL.sd  标题="主模型"
    正向  CLIPTextEncode          text="1girl, solo"   标题="正向提示词"
    潜图  EmptyLatentImage        w=768 h=1280 batch=1
    采样  KSampler                steps=28 cfg=6.5 seed=42 sampler=euler

    # 连线： 源节点.输出 -> 目标节点.输入
    模型.MODEL  -> 采样.model
    模型.CLIP   -> 正向.clip
    正向.CONDITIONING -> 采样.positive   # 直接写端口名
    潜图.LATENT -> 采样.latent

行的两种形态：
  * 节点定义： `<id>  <NodeType>  key=value ...`
  * 连线：     包含 `->` 的行

连线写法（按优先级）：
    a.MODEL -> b.model        按输出名 / 输入名
    a[0] -> b[1]              按下标
    a.out -> b.in             别名
    a.CONDITIONING -> b       # 只有一个能收这种类型的输入，可省略目标端口
    a.MODEL -> *model         # 广播给所有叫 model 的输入
    a -> b                    # 唯一连接时省略端口

内联连线（推荐写法，一行写完节点和接线）：
    模型 -> 采样 <- 潜图          采样.model 收 模型.MODEL，采样.latent 收 潜图.LATENT
    模型 -> 采样 -> 解码 -> 保存   链式往下接
    模型[1] -> 采样.model         指定输出槽（下标从 0 起）
    模型.CLIP -> 正向 -> 采样      同一节点可以出现在多条流里
    源 -> 目标.端口=源.输出        端口名写不清时用这种显式写法

显式端口赋值（接多个同类输入时必须用）：
    采样 KSampler positive=正向 negative=负向 model=模型 latent_image=潜图

虚拟跳线：ComfyUI 的 Set/Get 在这里用 `$名字` 表示
    vae  VAELoader  vae_name=xxx.vae
    vae.VAE -> $video_vae            把 VAE 存进名为 video_vae 的跳线
    ...  $video_vae -> 解码.vae      从跳线取出来（中间可以隔很多行）
    （DSL 会自动生成真正需要的 SetNode / GetNode，按名字配对）
"""
from __future__ import annotations

import re
import shlex
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .graph import (CwfError, Graph, Link, Node, Slot, VIRTUAL_GET, VIRTUAL_SET,
                       is_note_type)
from .schema import Registry, NodeSpec, estimate_size

#: 界面上传类端口，不要写进节点 JSON（前端会自己处理）
UI_ONLY_INPUT_TYPES = {"IMAGEUPLOAD", "AUDIOUPLOAD", "VIDEOUPLOAD", "FILEUPLOAD",
                       "IMAGEUPLOADTYPE", "UPLOAD"}

#: 连线分隔符。四种箭头都认——`模型 -> 采样 <- 潜图` 里的 `<-` 也是分隔符
ARROW_RE = re.compile(r"\s*(?:->|<-|→|←)\s*")
#: 第一段是短名，之后交给 registry 做最长匹配（节点类型里可能带空格、括号）
ID_RE = re.compile(r"^([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)\s+(.*)$", re.S)
ID_GROUP, BODY_GROUP = 1, 2
#: 没有字典时的兜底：类型 = 连续的非空白（允许末尾的 (xxx)）
TYPE_FALLBACK_RE = re.compile(r"^([A-Za-z_][\w:.\-|+]*)(\s*\([^)]*\))?\s*(.*)$", re.S)


# ---------------------------------------------------------------- 值解析


def parse_value(tok: str) -> Any:
    t = tok.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        body = t[1:-1]
        body = body.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
        return body
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("none", "null", "nil"):
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _split_args(rest: str) -> List[str]:
    """把 `a=1 b="x y" c=z` 切成 token，引号内的空格不算分隔。"""
    out: List[str] = []
    cur = ""
    quote: Optional[str] = None
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            cur += ch
            if ch == quote and (i == 0 or rest[i - 1] != "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch.isspace():
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
        i += 1
    if cur:
        out.append(cur)
    if quote:
        raise CwfError(f"引号没闭合：{rest}")
    return out


# ---------------------------------------------------------------- 端点解析


@dataclass
class Endpoint:
    node: str
    slot: Optional[str] = None
    index: Optional[int] = None
    virtual: Optional[str] = None      # $名字
    broadcast: bool = False

    def __str__(self) -> str:
        if self.virtual:
            return f"${self.virtual}"
        if self.index is not None:
            return f"{self.node}[{self.index}]"
        return f"{self.node}.{self.slot}" if self.slot else self.node


def parse_endpoint(tok: str) -> Endpoint:
    t = tok.strip()
    if t.startswith("$"):
        return Endpoint(node="", virtual=t[1:])
    if t.startswith("*"):
        return Endpoint(node="", slot=t[1:], broadcast=True)
    m = re.match(r"^([^\[\].]+)\[(\d+)\]$", t)
    if m:
        return Endpoint(node=m.group(1), index=int(m.group(2)))
    if "." in t:
        head, _, tail = t.rpartition(".")
        if head:
            return Endpoint(node=head.strip(), slot=tail.strip())
    return Endpoint(node=t)


# ---------------------------------------------------------------- 构建器


@dataclass
class PendingLink:
    src: Endpoint
    dst: Endpoint
    line: int = 0
    src_is_real: bool = True      # 源端口是用户写明的，还是由目标类型推断出来的


class Builder:
    """把 DSL 文本变成一张 Graph。"""

    def __init__(self, reg: Registry, base: Optional[Graph] = None,
                 auto_size: bool = True, store_dir: Optional[str] = None):
        self.reg = reg
        self.store_dir = store_dir      # 覆盖节点知识库目录（测试用；默认走 ~/.cwf/store）
        self.g = base or Graph()
        self.auto_size = auto_size
        self.aliases: Dict[str, Node] = {}       # DSL 里的短名 -> 节点
        self.virtual_sets: Dict[str, Node] = {}  # 跳线名 -> SetNode
        self.virtual_gets: Dict[str, Node] = {}  # 跳线名 -> GetNode
        self.pending: List[PendingLink] = []
        self.warnings: List[str] = []
        self.line_of: Dict[str, int] = {}
        self.out_hint: Dict[str, Optional[int]] = {}
        self._store_aliases: Optional[Dict[str, str]] = None

    def store_alias(self, name: str) -> Optional[str]:
        """查用户在 `cwf store` 里沉淀的节点别名。"""
        if self._store_aliases is None:
            try:
                from .store import load_aliases
                self._store_aliases = load_aliases(self.store_dir)
            except Exception:
                self._store_aliases = {}
        return self._store_aliases.get(name)

    def _resolve_type_spec(self, name: str):
        """把用户写的名字解析成节点规格。顺序：

            精确类型名 → store 里的自定义别名 → 内置中文别名（解码/主模型/放大…）

        store 放在内置别名之前：那是用户自己起的名字，优先级更高。
        """
        if self.reg is None:
            return None
        s = self.reg.get(name)
        if s is not None:
            return s
        real = self.store_alias(name)
        if real:
            s = self.reg.get(real)
            if s is not None:
                return s
        try:
            from .terms import resolve_type
            real = resolve_type(name, self.reg)
            if real:
                return self.reg.get(real)
        except Exception:
            pass
        return None

    def looks_like_source(self, key: str, node_type: str, val: str) -> bool:
        """`key=val` 到底是「接一根线」还是「设一个控件值」？

        判据：key 必须是该节点的**连线输入端口**（不是控件），
        而且 val 必须指向一个已知节点（已定义的短名 / 先定义的 / store 别名）。
        两边都满足才当接线——不然就是控件值，别自作主张。
        """
        if not isinstance(val, str) or not val:
            return False
        if self.reg is None or node_type not in self.reg:
            return False
        spec = self.reg.require(node_type)
        si = spec.find_input(key)
        if si is None or si.is_widget:
            return False
        target = val.split(".")[0].split("[")[0].strip()
        if target in self.aliases:
            return True
        if target in self.store_alias_cache_keys:
            return True
        return False

    @property
    def store_alias_cache_keys(self):
        if self._store_aliases is None:
            self.store_alias("")           # 触发加载
        return self._store_aliases or {}

    # ---------- 节点类型切分
    def split_type(self, body: str) -> Tuple[str, str]:
        """从 `CheckpointLoaderSimple ckpt_name=x` 里切出类型和参数串。

        节点类型里可能带空格或括号（`Any Switch (rgthree)`），所以用注册表做
        最长匹配；没有字典时退回「第一个空白之前」。
        """
        if len(self.reg):
            best = ""
            for t in self.reg.keys():
                if body.startswith(t) and len(t) > len(best):
                    nxt = body[len(t):len(t) + 1]
                    if nxt == "" or nxt.isspace():
                        best = t
            if best:
                return best, body[len(best):].strip()
            # 没匹配上：把第一个词当类型，交给后面报错（并给出相近建议）
            first = body.split()[0] if body.split() else body
            return first, body[len(first):].strip()
        m = TYPE_FALLBACK_RE.match(body)
        if not m:
            raise CwfError(f"看不懂节点类型：{body!r}")
        return (m.group(1) + (m.group(2) or "")).strip(), (m.group(3) or "").strip()

    # ---------- 建节点
    def make_node(self, type_: str, alias: Optional[str] = None,
                  title: Optional[str] = None, widgets: Optional[Dict[str, Any]] = None,
                  mode: int = 0) -> Node:
        # 类型名解析：先生成通用的中文别名（解码→VAEDecode），
        # 再认用户在 `cwf store` 里沉淀的别名。两者都没有才算写错。
        spec = self._resolve_type_spec(type_)
        if spec is not None:
            type_ = spec.type
        nid = self.g.next_id
        node = self.g.add_node(type_, nid=nid, title=title, mode=mode)
        # 把 DSL 里的短名记进 properties。**不放进 title** —— title 是画布上
        # 显示的名字，不该被工具擅自改掉（想要的话 `cwf build --titles`）。
        # 但短名必须留下来：一张图里常常有好几个同类型节点（四个
        # EmptyLatentImage），没有短名就完全没法按名字指认，
        # `cwf place "竖图=..."` 也就无从谈起。
        if alias and alias != type_:
            node.properties.setdefault("cwf_short", alias)

        if spec is None:
            if not self.reg:
                self.warnings.append(
                    f"节点类型 {type_!r} 不在本地字典里（ComfyUI 没连上？），"
                    f"已按「无端口节点」创建，接不了线")
            elif not is_note_type(type_):
                # 例外：便签（Note / 注释 / Comment…）是**前端 JS 注册**的节点，
                # /object_info 里根本没有，但 cwf 全程支持它们（list / cat /
                # validate / 排版都按便签正确处理）。DSL 不该因为查不到规格就
                # 拒绝 —— 否则「用几行字造一张带说明的图」这件最自然的事
                # 反而做不到。
                from .terms import suggest_for
                near = suggest_for(type_, sorted(self.reg.keys()))
                tip = f"相近的有：{'、'.join(near)}" if near else "用 cwf nodes list 搜搜看"
                raise CwfError(f"ComfyUI 里没有节点类型 {type_!r}。{tip}")
            if widgets:
                node.widgets = list(widgets.values())
                node._wform = "list"
            if alias:
                self.aliases[alias] = node
            return node

        node.properties.setdefault("Node name for S&R", type_)
        node.properties.setdefault("cnr_id", spec.category.split("/")[0] if spec.category else "")

        given = dict(widgets or {})
        unknown = [k for k in given if spec.find_input(k) is None]
        for k in unknown:
            low = k.lower()
            if low in ("标题", "title", "name"):
                continue
            cands = [i.name for i in spec.inputs]
            raise CwfError(f"节点 {type_} 没有输入 {k!r}；可用: {cands}")

        # 控件
        wnames: List[str] = []
        wvals: List[Any] = []
        for i in spec.widget_inputs:
            wnames.append(i.name)
            val = given.get(i.name, i.default)
            if i.type == "COMBO" and val is None and i.options:
                val = i.options[0]
            if i.type == "BOOLEAN" and val is None:
                val = False
            if i.type in ("INT", "FLOAT") and val is None:
                val = 0 if i.type == "INT" else 0.0
            if i.type == "STRING" and val is None:
                val = ""
            wvals.append(val)
        # 允许按「本地化名」给值
        for k, v in given.items():
            si = spec.find_input(k)
            if si is not None and si.is_widget and si.name not in given:
                if si.name in wnames:
                    wvals[wnames.index(si.name)] = v
        node.widgets = wvals if wvals else None
        node._wform = "list" if wvals else "none"
        node._wnames = list(wnames) if wnames else None

        # 端口
        for i in spec.link_inputs:
            if i.optional and i.type.upper() in UI_ONLY_INPUT_TYPES:
                continue
            node.inputs.append(Slot(name=i.name, type=i.type or "*",
                                    localized=i.localized))
        for o in spec.outputs:
            node.outputs.append(Slot(name=o.name, type=o.type or "*"))

        if title:
            node.title = title
        elif spec.display_name and spec.display_name != type_:
            node.title = None
        if self.auto_size:
            node.size = estimate_size(type_, node.widgets, node.title, spec,
                                      len(node.inputs), len(node.outputs),
                                      wdict=node.widget_dict())
        node.pos = (0.0, 0.0)
        if alias:
            self.aliases[alias] = node
        return node

    # ---------- 跳线
    def get_virtual_source(self, name: str) -> Node:
        return self.virtual_sets[name]

    # ---------- 连线
    def add_link(self, src: Endpoint, dst: Endpoint, line: int = 0,
                 src_is_real: bool = True) -> None:
        self.pending.append(PendingLink(src, dst, line, src_is_real))

    def resolve_slot_index(self, node: Node, ep: Endpoint, want_output: bool,
                           want_type: Optional[str] = None) -> int:
        """把端点解析成槽位下标。

        `want_type` 是「对端端口期望的类型」，用它可以在写了 `a -> b.positive`
        时自动挑出 a 的 CONDITIONING 输出——这就是「端口名可省略」的实现。
        """
        spec = self.reg.get(node.type)
        if ep.index is not None:
            return ep.index
        if ep.slot is None:
            if want_output and want_type:
                # 按目标类型在源节点上找唯一匹配的输出
                wt = want_type.upper()
                hits = [i for i, s in enumerate(node.outputs)
                        if s.type.upper() in (wt, "*") or wt == "*"]
                exact = [i for i, s in enumerate(node.outputs) if s.type.upper() == wt]
                pick = exact or hits
                if len(pick) == 1:
                    return pick[0]
                if len(pick) > 1:
                    desc = "、".join(f"[{i}]{node.outputs[i].name}:{node.outputs[i].type}"
                                    for i in pick)
                    raise CwfError(
                        f"{node.type}#{node.id} 有多个输出能给 {want_type}：{desc}；"
                        f"写法：`{ep.node}[下标] -> 目标.端口`，或先给它加 out=下标")
                raise CwfError(
                    f"{node.type}#{node.id} 没有能产出 {want_type} 的输出（"
                    f"它有 {[s.type for s in node.outputs]}）")
            raise CwfError(f"连线没写端口：{ep}（写成 `{ep.node}.端口名` 或 `{ep.node}[0]`）")
        slots = node.outputs if want_output else node.inputs
        # 精确名
        for i, s in enumerate(slots):
            if s.name == ep.slot or s.display == ep.slot:
                return i
        # 忽略大小写
        for i, s in enumerate(slots):
            if s.name.lower() == ep.slot.lower():
                return i
        # 按类型匹配（省略端口时常用）
        up = ep.slot.upper()
        hits = [i for i, s in enumerate(slots) if s.type.upper() == up]
        if len(hits) == 1:
            return hits[0]
        # 输出侧且没给名字/下标：按目标端口的类型，在源节点上找唯一匹配的输出
        if want_output and ep.slot is None and ep.index is None:
            raise CwfError(f"连线没写端口：{ep}")
        # schema 里存在但节点上没有这个输入（可选端口没被创建）→ 补上
        if not want_output and spec is not None:
            si = spec.find_input(ep.slot)
            if si is not None:
                node.inputs.append(Slot(name=si.name, type=si.type or "*",
                                        localized=si.localized))
                return len(node.inputs) - 1
        kind = "输出" if want_output else "输入"
        raise CwfError(f"节点 #{node.id} ({node.type}) 没有{kind} {ep.slot!r}；"
                       f"可用: {[s.name for s in slots]}")

    def flush_links(self) -> int:
        """把攒下的连线真正接上。返回接了几条。"""
        n = 0
        for pl in self.pending:
            # 来源侧：跳线取值。
            #
            # ⚠ 这一步**必须排在广播前面**。广播分支要拿 `pl.src` 去跟源节点
            # 比较（"跳过源自己"），也会把 `pl.src` 交给 _connect_one；而
            # `$名字` 这种端点的 node 字段是空串，没解析成真正的 GetNode 之前
            # 走到哪儿都会炸成「找不到节点 ''」。
            if pl.src.virtual:
                if pl.src.virtual not in self.virtual_sets:
                    raise CwfError(f"第 {pl.line} 行：跳线 ${pl.src.virtual} 没有对应"
                                   f"的 `-> ${pl.src.virtual}` 写入")
                gn = self._ensure_get(pl.src.virtual, pl.line)
                pl = PendingLink(Endpoint(node=str(gn.id), index=0), pl.dst, pl.line,
                                 pl.src_is_real)

            # 目标侧：广播？
            if pl.dst.broadcast:
                targets = [(nd, pl.dst.slot) for nd in self.g.nodes
                           if any(s.name == pl.dst.slot or s.name.lower() == (pl.dst.slot or "").lower()
                                  for s in nd.inputs)]
                if not targets:
                    raise CwfError(f"第 {pl.line} 行：没有任何节点有输入 {pl.dst.slot!r}")
                for nd, slotname in targets:
                    if nd is self._src_node(pl.src):
                        continue
                    self._connect_one(pl.src, Endpoint(node=str(nd.id), slot=slotname),
                                      pl.line, pl.src_is_real)
                    n += 1
                continue

            # 目标侧：跳线写入
            if pl.dst.virtual:
                setter = self._ensure_set(pl.dst.virtual, pl.line)
                pl = PendingLink(pl.src, Endpoint(node=str(setter.id),
                                                  slot=setter.inputs[0].name
                                                  if setter.inputs else "value"),
                                 pl.line, pl.src_is_real)

            self._connect_one(pl.src, pl.dst, pl.line, pl.src_is_real)
            n += 1
        self.pending.clear()
        return n

    def _lookup(self, name: str, line: int) -> Node:
        node = self.aliases.get(name)
        if node is None:
            node = self.g.maybe(name)
        if node is None:
            raise CwfError(f"第 {line} 行：找不到节点 {name!r}。"
                           f"已定义: {sorted(self.aliases)[:20]}")
        return node

    def _connect_one(self, src: Endpoint, dst: Endpoint, line: int,
                     src_is_real: bool = True) -> None:
        """接一根线。**穷举式端口解析**。

        为什么用穷举：`a -> b` 只表示「a 与 b 之间有一根线」，真正谁喂给谁由
        **端口能力**决定（能产出 → 能接收）。而端口名又可能只写了一头。
        与其推演组合，不如把有限的可能性全试一遍：

            方向 A：a 的输出 → b 的输入   （4 种端口组合）
            方向 B：b 的输出 → a 的输入   （4 种端口组合）

        哪一组能同时定出「一个输出 + 一个能收它的输入」就用哪一组。
        多个方向都成立才叫歧义，这时才报错。
        """
        if src.node and dst.node and src.node == dst.node and not dst.broadcast:
            s = self._lookup(src.node, line)
            d = s
        else:
            s = self._lookup(src.node, line)
            d = self._lookup(dst.node, line)

        # 按箭头方向优先：`a -> b` 先当「a 的数据流到 b」解；解不出来再反向。
        # 注意「反方向也能解出来」不算歧义 —— `模型 -> 采样` 在语法上反着也能连，
        # 但用户的箭头已经把意图说清楚了。
        cands: List[Tuple[Node, int, Node, int]] = []
        for (sn, sx, dn, dx) in ((s, src, d, dst), (d, dst, s, src)):
            ways = _resolve_ways(sn, dn, sx, dx)
            if ways:
                for oi, di, _how in ways:
                    c = (sn, oi, dn, di)
                    if c not in cands:
                        cands.append(c)
                break                      # 这个方向能解，就不看反方向了

        if not cands:
            raise CwfError(self._connect_hint(s, src, d, dst, line))
        if len(cands) > 1:
            # 同一方向内有多个解：优先「类型完全一致」的
            exact = [c for c in cands
                     if c[0].outputs[c[1]].type.upper() == c[2].inputs[c[3]].type.upper()]
            if len(exact) == 1:
                cands = exact
            else:
                desc = "、".join(
                    f"{c[0].type}.{c[0].outputs[c[1]].name}({c[0].outputs[c[1]].type})"
                    f" → {c[2].type}.{c[2].inputs[c[3]].name}({c[2].inputs[c[3]].type})"
                    for c in cands)
                raise CwfError(
                    f"第 {line} 行：`{src.node} -> {dst.node}` 有多种接法，定不下来：{desc}；"
                    f"请写明端口，例如 `{src.node}.{cands[0][0].outputs[cands[0][1]].name} -> "
                    f"{dst.node}.{cands[0][2].inputs[cands[0][3]].name}`")

        so_node, oi, di_node, di = cands[0]
        so, si = so_node.outputs[oi].type, di_node.inputs[di].type
        if so not in ("*", "") and si not in ("*", "", "COMBO") and so.upper() != si.upper():
            self.warnings.append(
                f"第 {line} 行：类型可能不匹配 —— {so_node.type}#{so_node.id}"
                f".{so_node.outputs[oi].name}({so}) → {di_node.type}#{di_node.id}"
                f".{di_node.inputs[di].name}({si})")
        self.g.connect(so_node, oi, di_node, di)

    # ---- 穷举式端口解析的候选生成

    def _try_connect(self, s: Node, src: Endpoint, d: Node, dst: Endpoint,
                     line: int) -> Optional[Tuple[int, int]]:
        """兼容旧调用：返回第一个候选解。"""
        ways = _resolve_ways(s, d, src, dst)
        return (ways[0][0], ways[0][1]) if ways else None

    def _pick_input(self, d: Node, otype: str, src: Endpoint, dst: Endpoint,
                    line: int) -> int:
        """知道源的输出类型了，在目标节点上找唯一匹配的输入。"""
        hits = [i for i, x in enumerate(d.inputs) if x.type.upper() == otype.upper()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            desc = "、".join(f"[{i}]{d.inputs[i].name}:{d.inputs[i].type}" for i in hits)
            raise CwfError(
                f"第 {line} 行：{otype} 类型的数据可以接 {d.type} 的多个输入（{desc}），"
                f"请写明：`{src.node} -> {dst.node}.输入名`")
        raise CwfError(
            f"第 {line} 行：{d.type} 没有能接收 {otype} 的输入"
            f"（它有 {[f'{x.name}:{x.type}' for x in d.inputs]}）")

    def _connect_hint(self, s: Node, src: Endpoint, d: Node, dst: Endpoint,
                      line: int) -> str:
        """穷举不出任何合法接法时，把两边的端口全列出来，让人一眼看出问题。"""
        return (
            f"第 {line} 行：`{src.node} -> {dst.node}` 接不上，两端都没有能对上的端口。\n"
            f"  {src.node}（{s.type}）可输出："
            f"{'、'.join(f'{x.name}:{x.type}' for x in s.outputs) or '无'}\n"
            f"  {dst.node}（{d.type}）可接收："
            f"{'、'.join(f'{x.name}:{x.type}' for x in d.inputs) or '无'}\n"
            f"  这两个节点之间本来就接不上（类型不匹配），或者需要中间加个转换节点。")

    def _src_node(self, ep: Endpoint) -> Optional[Node]:
        """端点 → 节点。`$跳线` 直接查已建立的 SetNode。

        ⚠ 这里曾经有**两份**定义：正确的那份在类的前面，而后面还尾随着一段
        从 `_connect_one` 复制漏下的残渣（里面用着 `s`/`oi`/`d`/`di` 这些
        压根不属于本函数的变量）。Python 后定义覆盖前定义，于是正确的实现
        被静默顶掉，任何走到这条路的写法都会 NameError —— 而它只在
        「广播 `*端口` 要跳过源自己」时才被调用，平时根本碰不到。
        教训：同一个方法名在类里出现两次，光看调用点是看不出来的。
        """
        if ep.virtual:
            return self.virtual_sets.get(ep.virtual)
        return self.aliases.get(ep.node) or self.g.maybe(ep.node)

    # ---------- Set / Get
    def _ensure_set(self, name: str, line: int) -> Node:
        if name in self.virtual_sets:
            return self.virtual_sets[name]
        node = self.g.add_node("SetNode", title=f"Set_{name}",
                               properties={"Node name for S&R": "SetNode",
                                           "aux_id": "kijai/ComfyUI-KJNodes"},
                               widgets=[name], _wform="list", _wnames=["Constant"])
        node.inputs.append(Slot(name="value", type="*", label="*"))
        node.outputs.append(Slot(name="*", type="*", label="*"))
        node.size = (210.0, 50.0)
        self.virtual_sets[name] = node
        return node

    def _ensure_get(self, name: str, line: int) -> Node:
        if name in self.virtual_gets:
            return self.virtual_gets[name]
        node = self.g.add_node("GetNode", title=f"Get_{name}",
                               properties={"Node name for S&R": "GetNode",
                                           "aux_id": "kijai/ComfyUI-KJNodes"},
                               widgets=[name], _wform="list", _wnames=["Constant"])
        node.outputs.append(Slot(name="*", type="*", label="*"))
        node.size = (210.0, 34.0)
        self.virtual_gets[name] = node
        return node


# ---------------------------------------------------------------- 解析


@dataclass
class DslResult:
    graph: Graph
    aliases: Dict[str, Node]
    warnings: List[str] = field(default_factory=list)


def strip_comment(line: str) -> str:
    """去掉行尾注释的 `# ...`，但引号里的 `#` 是内容，不能碰。

    规则：`#` 前面是空白（或行首）才算注释起点，所以 `模型#1` 这种引用不受影响。
    """
    quote: Optional[str] = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def parse(text: str, reg: Registry, base: Optional[Graph] = None,
          auto_size: bool = True, store_dir: Optional[str] = None) -> DslResult:
    b = Builder(reg, base, auto_size, store_dir)
    lines = text.splitlines()
    for ln, raw in enumerate(lines, 1):
        line = strip_comment(raw).strip()
        if not line or line.startswith("//"):
            continue
        try:
            if line.startswith("@"):
                _directive(b, line, ln)
                continue
            # 先切出「内联流」，再判断这行到底是节点定义还是纯连线。
            # 判据：head 里还有箭头 → 纯连线行；否则 head 是「短名 + 类型」→ 节点行。
            # 判据只看 head 的**结构**：`短名 类型 ...` 才是节点行，其余交给连线行。
            #
            # 这里踩过坑：原来还有一条 `ARROW_RE.search(head)` 的子句，本意是
            # "head 里还有箭头就当纯连线"。可 `_split_flow` 已经在第一个**引号外**
            # 的箭头处切开了，head 里根本不可能再有引号外的箭头 —— 于是这条子句
            # 只在一种情况下触发：**引号里**写了箭头。而那是完全正常的内容
            # （便签正文、提示词里写「A → B」），结果整行被当成接线，
            # 报一句莫名其妙的"连线至少要有头和尾"。
            head, flow = _split_flow(line)
            if ID_RE.match(head):
                _node_line(b, line, ln)
            else:
                _link_line(b, line, ln)
        except CwfError as e:
            raise CwfError(f"DSL 第 {ln} 行出错：{e}")
    b.flush_links()
    b.g.reindex()
    b.g.reorder()
    return DslResult(b.g, b.aliases, b.warnings)


def _directive(b: Builder, line: str, ln: int) -> None:
    parts = line[1:].split(None, 1)
    key = parts[0].lower()
    val = parts[1].strip() if len(parts) > 1 else ""
    if key in ("title", "workflow_title"):
        b.g.extra["workflow_title"] = val
    elif key == "meta":
        b.g.extra.setdefault("cwf_meta", []).append(val)
    elif key == "noinput":                    # 预留给以后的开关
        pass
    else:
        b.warnings.append(f"第 {ln} 行：未知指令 @{key}")


def _scan_arrows(line: str):
    """把一行按箭头切成 (端点列表, 箭头列表)，箭头按出现顺序。

    ⚠ 这里**故意不用正则**。`ARROW_RE.findall("模型 -> 采样 <- 潜图")` 会返回
    `['<-', '<-']` —— 正则引擎在 `-` 上回溯，把 `->` 读成了 `<-`，方向全反。
    `re.split` 给的结果又是对的，两者不一致。连线方向是语义，不能靠这种
    带歧义的正则，所以自己扫。
    """
    toks: List[str] = []
    arrows: List[str] = []
    cur = ""
    quote: Optional[str] = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            cur += ch
            if ch == quote and (i == 0 or line[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            cur += ch
            i += 1
            continue
        if line[i:i + 2] == "->":
            toks.append(cur.strip())
            arrows.append("->")
            cur = ""
            i += 2
            continue
        if line[i:i + 2] == "<-":
            toks.append(cur.strip())
            arrows.append("<-")
            cur = ""
            i += 2
            continue
        if ch in "→←":
            toks.append(cur.strip())
            arrows.append("->" if ch == "→" else "<-")
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    toks.append(cur.strip())
    return toks, arrows


FLOW_RE = re.compile(r"\s*(?:->|<-|→|←)\s*")


def _split_flow(line: str):
    """把一行切成「节点定义部分」和「内联流部分」。

    返回 (head, [(箭头, 端点), ...])。没有箭头就返回 (line, [])。
    注意引号内的箭头不算——提示词里写 `->` 是常有的事。
    """
    quote = None
    i = 0
    cut = -1
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "-<" or ch in "→←":
            if ch == "-" and line[i:i + 2] == "->":
                cut = i
                break
            if ch == "<" and line[i:i + 2] == "<-":
                cut = i
                break
            if ch in "→←":
                cut = i
                break
        i += 1
    if cut < 0:
        return line, []
    head = line[:cut].rstrip()
    toks, arrows = _scan_arrows(line[cut:])
    flow = [(a, t) for a, t in zip(arrows, toks[1:]) if t]
    return head, flow


def _node_line(b: Builder, line: str, ln: int) -> None:
    head, flow = _split_flow(line)
    m = ID_RE.match(head)
    if not m:
        raise CwfError(f"看不懂这行：{line!r}")
    alias = m.group(ID_GROUP)
    body = m.group(BODY_GROUP).strip()
    if not body:
        raise CwfError(f"只写了短名 {alias!r}，没写节点类型")
    if alias in b.aliases:
        raise CwfError(f"节点短名 {alias!r} 重复了（每行的第一个词必须唯一）")
    type_, rest = b.split_type(body)

    kwargs: Dict[str, Any] = {}
    port_in: List[Tuple[str, str]] = []
    out_hint: Optional[int] = None
    title = None
    mode = 0
    for tok in _split_args(rest):
        if "=" not in tok:
            raise CwfError(f"参数 {tok!r} 不是 key=value 形式"
                           f"（是不是节点类型写错了？已按 {type_!r} 解析）")
        k, _, v = tok.partition("=")
        k = k.strip()
        val = parse_value(v)
        if k in ("标题", "title", "名称"):
            title = str(val)
        elif k in ("mode", "模式"):
            mode = int(val)
        elif k in ("bypass", "旁路"):
            mode = 4 if val else 0
        elif k in ("mute", "静音"):
            mode = 2 if val else 0
        elif k in ("out", "输出槽"):
            out_hint = int(val)
        elif isinstance(val, str) and b.looks_like_source(k, type_, val):
            # 端口显式赋值：`positive=正向` —— 这是接线，不是控件
            port_in.append((k, val))
        else:
            kwargs[k] = val

    node = b.make_node(type_, alias=alias, title=title, widgets=kwargs, mode=mode)

    # 内联流。规则（看箭头方向定谁是主链，`<-` 的节点只当「供料方」）：
    #   `a -> b`        a 的输出接 b（端口名可省，按目标类型反推）
    #   `a <- b`        b 的输出接 a
    #   `a -> b -> c`   顺下去：a→b→c
    #   `a <- c -> d`   c 只给 a 供料，d 接的还是 **a**（这条最容易搞错）
    me = Endpoint(node=alias, index=out_hint)
    prev: Endpoint = me
    pending_in: Optional[Endpoint] = None      # 刚接进来的「供料方」
    first_arrow = flow[0][0] if flow else None
    if first_arrow == "<-":
        # 从 `<-` 起头：把本行节点接到第一个供料方后面，之后继续往下走
        first = parse_endpoint(flow[0][1])
        b.add_link(first, me, ln, src_is_real=_has_port(first))
        prev = me
        cursor = 1
    else:
        cursor = 0
    while cursor < len(flow):
        arrow, ref = flow[cursor]
        ep = parse_endpoint(ref)
        if arrow == "->":
            if pending_in is not None:
                # `-> x` 紧跟在一个 `<- y` 后面：x 接的是上一个主链节点，不是 y
                b.add_link(prev, ep, ln, src_is_real=_has_port(prev))
                pending_in = None
                cursor += 1
                continue
            b.add_link(prev, _with_prev_port(ep, prev), ln, src_is_real=_has_port(prev))
            prev = ep
        else:
            b.add_link(ep, _with_prev_port(prev, ep), ln, src_is_real=_has_port(ep))
            pending_in = ep
        cursor += 1

    for port, src_ref in port_in:
        b.add_link(parse_endpoint(src_ref), Endpoint(node=alias, slot=port), ln)


def _has_port(ep: Endpoint) -> bool:
    """这个端点有没有写明端口（写了就用它，没写就按对端类型推断）。"""
    return ep.index is not None or ep.slot is not None


def _resolve_ways(s: Node, d: Node, se: Endpoint, de: Endpoint
                  ) -> List[Tuple[int, int, Optional[str]]]:
    """穷举「s 的输出 → d 的输入」的所有合法槽位组合。

    返回 [(oi, di, 推断方式), ...]，按可信度排序：
      1. 两端都写明 → 最可信
      2. 一端写明 → 用它的类型推另一端
      3. 两端都没写 → 只有在「只有一种类型组合能对上」时才给结果
    """
    ways: List[Tuple[int, int, Optional[str]]] = []
    if not d.inputs:
        return ways

    # 收集候选输出槽
    outs: List[int] = []
    if se.index is not None:
        outs = [se.index]
    elif se.slot is not None:
        for i, x in enumerate(s.outputs):
            if x.name == se.slot or x.display == se.slot or x.name.lower() == se.slot.lower():
                outs = [i]
                break
    if not outs:
        outs = list(range(len(s.outputs)))

    # 收集候选输入槽
    ins: List[int] = []
    if de.index is not None:
        ins = [de.index]
    elif de.slot is not None:
        for i, x in enumerate(d.inputs):
            if x.name == de.slot or x.display == de.slot or x.name.lower() == de.slot.lower():
                ins = [i]
                break
    if not ins:
        ins = list(range(len(d.inputs)))

    for oi in outs:
        if oi < 0 or oi >= len(s.outputs):
            continue
        otype = (s.outputs[oi].type or "*").upper()
        for di in ins:
            if di < 0 or di >= len(d.inputs):
                continue
            itype = (d.inputs[di].type or "*").upper()
            if otype == itype:
                ways.append((oi, di, "exact"))
            elif otype == "*" or itype == "*" or itype == "COMBO":
                ways.append((oi, di, "wild"))
    # 精确匹配优先
    ways.sort(key=lambda w: 0 if w[2] == "exact" else 1)
    return ways


def _with_prev_port(target: Endpoint, source: Endpoint) -> Endpoint:
    """`a -> b.model` 这种目标写了端口、来源没写：把来源的口留空让工具去推断；
    反过来 `a.MODEL -> b` 时，目标的口也留空。真正干活的是 _connect_one。"""
    return target


def _link_line(b: Builder, line: str, ln: int) -> None:
    r"""纯连线行：`a -> b`、`a -> b -> c`、`a <- c`、`a -> b <- c -> d`。

    `<-` 有**括号语义**：它把「当前主链节点」挂起来，转头去接供料方；
    之后的 `->` 接的还是原来的主链节点。所以
        `a -> b <- c -> d`  等价于  a→b、c→b、b→d
    这是最容易被写错的一处，单独拉出来实现并测。
    """
    toks, arrows = _scan_arrows(line)
    if len(toks) < 2 or not arrows:
        raise CwfError(
            f"这行既不像节点定义、也不像连线：{line!r}；"
            f"节点写成「短名 类型 控件=值」，连线写成「a -> b」"
            f"（引号里的箭头不算连线）")
    if len(arrows) != len(toks) - 1:
        raise CwfError(f"这行连线写得不对（箭头和端点数量对不上）：{line!r}")

    cursor = 0
    first_arrow = arrows[0] if arrows else None
    prev = parse_endpoint(toks[0])
    if first_arrow is None:
        return
    if first_arrow == "<-":
        b.add_link(parse_endpoint(toks[1]), prev, ln,
                   src_is_real=_has_port(parse_endpoint(toks[1])))
        cursor = 1
    while cursor < len(arrows):
        arrow = arrows[cursor]
        ep = parse_endpoint(toks[cursor + 1])
        if arrow == "->":
            b.add_link(prev, ep, ln, src_is_real=_has_port(prev))
            prev = ep
        else:
            b.add_link(ep, prev, ln, src_is_real=_has_port(ep))
            # 括号语义：主链节点不变，下一个 `->` 还是接它
        cursor += 1
