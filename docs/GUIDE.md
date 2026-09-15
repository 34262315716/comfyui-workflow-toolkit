# cwf · ComfyUI 工作流工具 — AI Agent 使用指南

> **这个工具已经打包成 skill：`comfyui-workflow`**（装在 `~/.dsh/skills/comfyui-workflow/`）。
> 日常使用直接说需求即可，DSH 会自动加载该技能；本文件是同一套内容的**落地版手册**，
> 供直接翻查或维护工具时参考。两边的命令与参数一致。

> 一句话：**别直接读写 ComfyUI 的 JSON，用 `cwf`。**
> 它把 142 KB 的节点图压缩成 2 KB 的语义摘要，把「猜数组下标」变成「按名字改参数」，
> 还能一键把挤成一团的节点排成 Graphviz 级别的清晰版式。

---

## 0 · 30 秒上手

```bash
cwf ws list --dir 人像                   # 工作流库里有什么
cwf ws info 我的工作流                   # 一眼看规模
cwf outline 我的工作流                   # 数据流分几阶段（给 AI 读最省 token）
cwf validate 某工作流                    # 缺节点/缺模型/断线，全查出来
cwf beautify 某工作流 --in-place         # 一键重排（自动备份）
cwf nodes list 解码                      # 本机有哪些节点（支持中文别名）
cwf build my.dsl --name 我的新流          # 用几行 DSL 拼一张新工作流
```

Windows 下直接用 `tools\cwf.cmd`，参数完全一致。JSON 输出加 `--json`。

---

## 1 · 命令速查

### 找 & 读

| 命令 | 干什么 | 典型用法 |
|---|---|---|
| `ws list` | 列工作流 | `--dir 人像` `--match 高清` `--limit 20` |
| `ws info` | 规模概要 | 节点/连线/旁路/终结点/孤儿 |
| `ws resolve` | 体检：节点类型和模型文件是否都在本机 | 换机/重装后必跑 |
| `find` | 按名字模糊找 | `cwf find 高清` |
| `grep` | 在**内容**里搜（类型/标题/参数值） | `cwf grep "silver hair"` |
| `cat` | 看内容 | `-d` 出每个节点的端口与参数 |
| `outline` | **按数据流阶段**列结构 | 让 AI 快速理解一张陌生的图 |
| `deps` | 某节点的上下游链 | `cwf deps 某流 解码 --depth 4` |
| `diff` | 两版工作流差在哪 | 参数级差异 |
| `stats` | 结构体检 + 排版指标 | `--with-layout` |
| `validate` | 全量校验 | `--strict` 有错就退出码 1 |

### 节点库（本机装了什么）

| 命令 | 干什么 |
|---|---|
| `nodes stats` | 总览：**按功能分类 / 按原生分类 / 按插件包** 三种分布 + 覆盖率 |
| `nodes tree [--cat X]` | **按功能浏览**：16 大类 → 小类 → 具体节点（中英文都收） |
| `nodes list [词]` | 列节点，支持中文别名与 `--cat`（功能分类）/ `--category`（原生分类） |
| `nodes show 类型 -v` | 一个节点的说明书：功能分类、必接输入、输出、控件、默认值、来源包、用了几次 |
| `nodes classify 类型` | 它为什么被判成这个类（命中哪条规则、依据是什么） |
| `nodes setcat 类型 分类` | 手动改功能分类（写进 `store/categories.txt`，立刻生效） |
| `nodes atlas` | 导出全量功能分类图鉴 → `~/.cwf/atlas/` |
| `nodes pkg [包名]` | 按插件包看，或列出贡献最多的包 |
| `nodes unused` | 装了但从没用过的类型（清理插件参考） |
| `nodes alias` | 中文别名对照表 |
| `nodes build` | 重建缓存（装了新插件后跑） |
| `schema search/show/produce/accept/models` | 更细的节点字典查询 |

### 硬件预检（提交前的红线）

| 命令 | 干什么 |
|---|---|
| `precheck 某流` | 只预检不提交：ControlNet / 视频 / 大画布 |
| `run 某流` | 提交执行；**命中红线直接拒绝**（退出码 2） |

红线：本机 8 GB 显存 + 34 GB 内存。**含 ControlNet 的图不跑**（容易爆）、
**视频不擅自跑**（只能跑你准备好并验证过的流程）、图片模型可以跑。
放行要显式加 `--allow-controlnet` / `--allow-video`。

### 手动摆位置（agent 自己说了算）

| 命令 | 干什么 |
|---|---|
| `place 某流 --col "A B" --col "C"` | **一行一列**指定版式；列内从上往下堆 |
| `place 某流 "#12=100,200"` | 绝对坐标；`+=` 是相对微调 |
| `place 某流 --pack --fold 2` | 把空隙压紧、每 2 列并 1 列（别让图拉太长） |

只改 `pos`：接线、控件值、`to_api()` 前后完全一致。不给 `--out`/`--in-place` 就不落盘。

### 出图（离线渲染）

| 命令 | 干什么 |
|---|---|
| `render 某流 --out x.svg` | **不开浏览器把工作流画成图**（纯标准库，矢量） |
| `render 某流 --out x.png` | 出 PNG（本机有 Pillow 时）；`--scale` / `--max-px` 控制大小 |
| `render 某流 --out x.png --theme light` | 亮色主题；默认按功能分类给标题栏上色，`--color comfy` 是素色 |

### 排版（本项目的主打）

| 命令 | 干什么 |
|---|---|
| `layout 某流 --out x.json` | 只排版，不动内容 |
| `beautify 某流 --in-place` | 排版 + 补标题 + 重排序号，**自动备份** |
| `--compact` / `--loose` | 紧凑 / 宽松 |
| `--no-groups` | 不重画分区框 |
| `--keep-notes` | 注释节点留在原地 |
| `--strays bottom\|right\|keep` | 孤立节点放哪 |

### 改

| 命令 | 干什么 |
|---|---|
| `set 某流 --set "节点:控件=值"` | 改参数（**按名字，不靠下标**） |
| `rename 某流 --rename "节点=新标题"` | 改标题 |
| `mode 某流 节点 --mode bypass` | 正常/静音/旁路 |
| `add 某流 --type X --arg k=v` | 加节点 |
| `insert 某流 --type X --on A B` | **插进已有连线中间**（A→X→B） |
| `splice 某流 节点` | 摘掉节点并**焊接上下游** |
| `remove 某流 节点 --keep-wiring` | 删节点（可选保留数据流） |
| `connect / disconnect` | 连线 / 断线 |

**节点引用（所有改/删/连命令通用）**

| 写法 | 含义 |
|---|---|
| `#12` / `12` | 按节点 id |
| `type:KSampler` | 按类型，**可批量**（`--set`/`mode`/`rename` 会全部改） |
| `title:采样` | 按标题子串，可批量 |
| `~^05\.` | 按标题正则 |
| `05. 我的主采样器` | 标题全等 / 去序号后比 / 子串 |
| `解码` `主模型` `放大` | **中文别名**（60+ 条映射，见 `cwf nodes alias`） |

批量示例：
```bash
cwf set 某流 --set "type:LoadImage:image=新图.png"      # 所有 LoadImage 一起改
cwf mode 某流 "type:CLIPTextEncode" --mode bypass       # 所有文本编码一起旁路
```
> 安全设计：批量改如果**有任何一个节点没有那个控件，整批不动**（先预检再写），
> 不会出现"改了一半崩了"。

### 沉淀（节点知识库）

| 命令 | 干什么 |
|---|---|
| `store mark 节点 -a 别名 -p -n 笔记` | 一次把别名/收藏/笔记都记下 |
| `store find [词]` | 检索：沉淀过的排最前面（★ 标记） |
| `store show 节点` | 速查卡：端口+控件+你的笔记 |
| `store list` / `store export` | 总览 / 导出 markdown |
| `store alias` / `store pin` | 单独管别名 / 收藏 |

沉淀物在 `~/.cwf/store/`，纯文本，可手改。

### 造（DSL）

| 命令 | 干什么 |
|---|---|
| `scaffold txt2img` | 打印可直接改的模板（还有 `img2img` / `h3_ref2va` / `blank`） |
| `dsl-check my.dsl` | 只验语法，不建图 |
| `build my.dsl --name 名 --validate` | 建图 + 自动排版 + 校验 |
| `export-dsl 某流` | **把已有工作流转成 DSL**，AI 读起来比 JSON 省 90% token |

### 模块化（排列组合）

| 命令 | 干什么 |
|---|---|
| `pack split 某流 --nodes "#10,#11" --name 采样链 --dir mods` | 切一块存成模块 |
| `pack list --dir mods` | 模块库里有啥 |
| `pack show 采样链 --dir mods` | 模块的对外接口（入口/出口 + 类型） |
| `pack use 采样链 --dir mods --workflow 目标流 --out out.json --layout` | 塞进另一张图 |

`--nodes` 选择器：`#1,#2` / `10-30` / `标题子串` / `type:KSampler` / `~正则`。

### 跑 & 转

| 命令 | 干什么 |
|---|---|
| `run 某流 --wait` | 提交到 ComfyUI 并等结果，列出产物 |
| `queue` | 看队列 |
| `export-api 某流 --out api.json` | 导出 `/prompt` 用的 API 格式 |
| `import-api api.json --out x.json` | API 格式转回可编辑的 UI 工作流（并自动排版） |

---

## 2 · DSL：几行字造一张工作流

```
@title 文生图 + 潜空间二采放大

模型   CheckpointLoaderSimple  ckpt_name=anything-v5.safetensors  标题="1. 主模型"
正向   CLIPTextEncode          text="1girl, cinematic lighting"   标题="2. 正向"
负向   CLIPTextEncode          text="worst quality"               标题="3. 负向"
潜图   EmptyLatentImage        width=832 height=1216 batch_size=1
采样   KSampler                seed=12345 steps=28 cfg=6.5 sampler_name=euler scheduler=normal
放大   LatentUpscaleBy         upscale_method=nearest-exact scale_by=1.5
复采   KSampler                seed=12345 steps=14 cfg=6.0 denoise=0.45
解码   VAEDecode               # 无参数节点，直接写类型就行
保存   SaveImage               filename_prefix=CWF/test

模型.MODEL -> 采样.model
模型.MODEL -> 复采.model
模型.CLIP  -> 正向.clip
模型.VAE   -> 解码.vae
正向.CONDITIONING -> 采样.positive
潜图.LATENT -> 采样.latent
采样.LATENT -> 放大.samples
放大.LATENT -> 复采.latent
复采.LATENT -> 解码.samples
解码.IMAGE  -> 保存.images
```

**要点**

- 一行一个节点：`短名  节点类型  控件=值 ...`（短名唯一，后面连线用它）
- **内联连线（推荐）**：`模型 -> 采样 <- 潜图` —— 端口名可省，工具按类型推断；
  三种写法：两头都省 / 只写一头 / 定义行里直接写 `端口=源`
- 方向由**端口能力**决定而非箭头：`a -> b` 与 `b <- a` 等价
- `<-` 有括号语义：`a -> b <- c -> d` 等价于 `a→b`、`c→b`、`b→d`
- **有歧义会报错不瞎猜**（`正向 -> 采样` 会问你是 positive 还是 negative）
- 老写法仍然兼容：`a.X -> b.Y -> c.Z` 单独成行
- 端口可以省略：`a.CONDITIONING -> b`（只有一个能收这类型的输入时自动接）
- 广播：`a.MODEL -> *model`（所有叫 model 的输入都接上）
- 虚拟跳线（复用值）：`unet.MODEL -> $model` 存，隔几行 `$model -> 采样.model` 取。
  工具会自动生成真正需要的 `SetNode`/`GetNode` 并配对
- 注释：`#`，但引号里的 `#` 是内容
- 中文端口名可用：`a.模型 -> b.模型`
- 建完自动排版 + 可以 `--validate` 立刻查错。**接错类型它会当场告诉你**

---

## 3 · 排版引擎做了什么

`cwf beautify` 不是「重新摆一遍」，而是一套分层图算法（Sugiyama 风格）：

1. **规整化** —— 旁路/静音节点穿透，`SetNode/GetNode` 虚拟跳线接回真实数据流，
   `Reroute` 收缩成点
2. **拆环** —— 深度优先找反馈边，反向后再分层
3. **分层** —— 最长路径分层 + 收紧，节点尽量往左靠
4. **层内排序** —— 中位数/重心启发式多轮扫描 + **逐对相邻交换精修**
5. **坐标分配** —— 长边插虚节点，两趟中位数对齐再取平均，**长链自动拉直**
6. **落位** —— 吸附 8px 网格，横竖版式择优（避免排出 6 万像素高的面条），
   层太多时自动折行
7. **分区框** —— 按语义（加载/提示词/采样/解码/输出/放大/辅助）+ 连通性聚类，
   同区被拆散时按空间邻近合并，**大面积糊在一起的框直接合成一个**（硬推开会把画布撑爆）
8. **消重叠** —— 全局兜底，任何两个节点（含注释）都不许重叠

**实测（435 张真实工作流全库跑，2026-09-15）**

| 指标 | 结果 | 性质 |
|---|---|---|
| 排版成功率 | 435/435 | 硬指标 |
| 节点重叠 | **0** | 硬指标 |
| 分区框套住成员 | **100%** | 硬指标 |
| **接线被改错** | **0**（全库 17173 条连线、控件值逐项比对） | 硬指标 |
| `to_api()` 排版前后 | **逐字节相同** | 硬指标 |
| 排版幂等（连跑两次） | 位置不剧变 | 稳定性 |
| 长宽比 | 中位 2.8 · 最大 16.7 | 形状 |
| 每节点占位 | 中位 359k px² | 形状 |
| 全库排版耗时 | 约 15 秒 | 性能 |
| 分区框压边的图 | 43/435（9.9%） | ⚠ 已知限制 |
| 连线路径压到节点框 | 中位 33% 的边 | ⚠ 已知限制 |

> ⚠ **「连线压到节点框」是纯外观问题，不是接线错误。** 它指的是我按正交路由
> 推算出的走线会从某个节点矩形上经过（在画布上就是线从节点下面钻过去）。
> 实测全库 17173 条连线的「源槽→目标槽」在排版前后**没有一条**发生变化，
> 提交给 ComfyUI 的 `prompt` 数据逐字节相同。另外 ComfyUI 实际画的是贝塞尔
> 曲线，我这个直角路由只是量化观感的近似尺子。

---

## 4 · 节点库：你的电脑上有什么

```bash
$ cwf nodes stats
  节点类型总数       7746
  实际用过的类型     1014
  插件包数量          189
  扫描工作流          435 个（13528 个节点实例）
```

数据从两处来：
- `/object_info` —— 节点类型、分类、端口、控件、默认值（权威定义）
- **你的工作流 JSON** —— 每个节点带 `properties.cnr_id`；更权威的是 `/object_info`
  里的 `python_module` 字段（`custom_nodes.<包名>`），这是唯一能反查「这节点属于哪个
  插件包」的线索

结果缓存在 `~/.cwf/node_catalog.json`，ComfyUI 升级或工作流库变了才重建。

### 4.1 按功能分类找节点（而不是按插件名）

`/object_info` 自带的 `category` 字段对插件节点来说**基本等于插件名**
（`Apt_Preset` 395 种、`RunningHub` 380 种、`T8` 356 种…），按它找等于按「谁做的」找。
只有 comfy-core 的原生分类是真·功能路径（`model/sampling/samplers`、`image/upscaling`）。

所以 cwf 自己判：**16 个功能大类**，判定依据按可靠性排序 ——
**端口类型**（最硬：输出 `VIDEO` 的就是视频节点，跟谁写的无关）→ **类型名**
（按词切分逐 token 匹配，不是 `in` 硬套子串）→ **原生分类路径**（兜底）。
拿不准就进 `未归类`，**不猜**。

```bash
$ cwf nodes tree
  模型与适配      658 种  用过 123   10 小类   模型补丁 187、模型加载 104、LoRA 89
  图像处理      1625 种  用过 182   12 小类   变换 896、合成 / 批处理 130、色彩 119
  视频         900 种  用过 186    7 小类   通用 702、视频工程套件 63、视频模型 39
  ...
  未归类        214 种  用过   9    2 小类
```

16 类（`--cat` 中英文都收）：

`load` 加载输入 · `model` 模型与适配 · `cond` 条件与提示词 · `sample` 采样与生成 ·
`latent` 潜空间 · `image` 图像处理 · `mask` 遮罩 · `video` 视频 · `audio` 音频 ·
`threed` 三维 · `text` 文本与数据 · `num` 数值与逻辑 · `output` 输出与预览 ·
`flow` 流程与组织 · `api` 网络与云 · `misc` 未归类

实测自动判定覆盖率 **97.2%**，置信度 `high 3459 / med 3920 / low 158 / none 208`。
判错了改一行就永久生效，**不用重建缓存**：

```bash
cwf nodes setcat 某节点 图像处理/放大
cwf store mark KSampler -c 采样与生成/采样器     # 沉淀时顺手归类
```

写入 `~/.cwf/store/categories.txt`（纯文本，支持尾部通配 `MiniMaxH3* = 视频/视频工程套件`），
**用户覆盖永远优先于自动规则**。

要全量离线翻阅：`cwf nodes atlas` → `~/.cwf/atlas/`（17 个文件，7746 种一个不少）。

---

## 5 · 给 AI agent 的建议用法

**理解一张陌生的图**（省 token，比读 JSON 高效得多）：
```bash
cwf outline 某流                  # 阶段化结构
cwf cat 某流 -d --limit 30        # 需要细节时
cwf deps 某流 8 --depth 5         # 追某条链
```

**改一张图的标准姿势**：
```bash
cwf validate 某流                       # 1. 先确认现状
cwf set 某流 --set "5. 一采:steps=40" --out 新.json   # 2. 改（不覆盖原件）
cwf validate 新.json                    # 3. 复验
cwf beautify 新.json --in-place         # 4. 排版（自动备份）
```

**造新图**：
```bash
cwf scaffold txt2img > my.dsl     # 起模板
# 编辑 my.dsl
cwf build my.dsl --name 名字 --validate
```

**复用已有能力**：先把好用的片段切出来，以后拼装
```bash
cwf pack split 某流 --nodes "type:SamplerCustomAdvanced,type:RandomNoise" \
    --name H3采样核心 --dir <仓库根>\modules
cwf pack use H3采样核心 --dir ... --workflow 新图.json --out 拼好的.json --layout
```

**红线**
- 默认**不覆盖**原文件；`--in-place` 会先备份成 `.bak-<时间戳>`
- `validate` 有错就别急着 `run`
- ComfyUI 没启动时：`--offline` 用缓存字典，`ws resolve` 也能查缺什么

---

## 6 · 环境

| 项 | 值 |
|---|---|
| 工具位置 | `<仓库根>\tools\` |
| 启动器 | `tools\cwf.cmd`（自动找 ComfyUI 的 venv Python） |
| 直接跑 | `python -X utf8 tools\cwf_run.py <命令>` |
| 工作流库 | `<ComfyUI>/user/default/workflows`（可用 `CWF_WORKFLOWS` 覆盖） |
| 节点库缓存 | `~\.cwf\node_catalog.json` |
| 节点字典缓存 | `~\.cwf\object_info.*.json` |
| ComfyUI 地址 | `http://127.0.0.1:8188`（可用 `--server` 或 `CWF_SERVER` 覆盖） |
| 依赖 | 只用 Python 标准库，无第三方包 |

---

## 7 · 已知边界

- **前端注册的节点**（`SetNode`/`GetNode`/`Reroute`/rgthree 的开关）不在 `/object_info` 里，
  这是正常的；节点库会把它们标成「⚠未注册」而不是报错
- `validate` 的「必要输入没接线」有误报可能：某些节点的 `required` 其实是控件。
  看提示里的类型，`COMBO` 类的基本可以忽略
- 模块 `pack` 按 **类型** 对接，不校验语义匹配——接完记得 `validate`
- 排版不改变任何执行语义（只动 `pos`/`size`/`groups`），可放心对成品跑
