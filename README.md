# cwf — ComfyUI 工作流命令行工具箱

> **Read, edit, compose and auto-layout ComfyUI workflows from the command line —
> without ever touching the raw JSON by hand.**
> Zero dependencies (Python standard library only). 41 subcommands. 100% offline.

一句话：**别直接读写 ComfyUI 的 JSON，用 `cwf`。**

它把一张 142 KB 的节点图压成 2 KB 的语义摘要；把"猜数组下标"变成"按名字改参数"；
把挤成一团的节点一键排成分层清晰的版式，并且**保证交出来的图没有任何两个节点框重叠**。

---

## 它解决什么

ComfyUI 的工作流文件是给前端 JavaScript 看的，对人和 AI 都不友好：

| 麻烦 | cwf 的做法 |
|---|---|
| 几十上百 KB 的 JSON，看不懂结构 | `cwf outline` 按数据流阶段列出来，几 KB 读完 |
| 改个参数要数 `widgets_values` 的下标 | `cwf set 流 --set "节点:控件=值"`，按名字改 |
| 节点挤成一团，或者拉成 10000px 的长条 | `cwf beautify` 一键重排，**保证 0 重叠** |
| 换台机器就一堆红节点、缺模型 | `cwf validate` 一次查完 |
| 想复用某个片段，只能手抄 | `cwf pack` 切成模块，再拼进别的图 |
| 想让 AI 帮忙改，但 JSON 塞不进上下文 | `cwf outline` + DSL，几十行搞定 |
| 手滑提交了跑不动的活，跑到一半 OOM | `cwf run` 默认护栏，显式放行才提交 |
| 不知道这张图自己机器跑不跑得动 | `cwf fit` 量化到 GB：余量、瓶颈、判定 |

设计前提是：**主要使用者是 AI agent，其次才是人。** 所以输出默认紧凑、
支持 `--json`、错误信息直接给出下一步该敲什么命令。

---

## 安装

不需要 pip，不需要虚拟环境，不装任何第三方包：

```bash
git clone https://github.com/34262315716/comfyui-workflow-toolkit.git
cd cwf
python cwf_run.py --help
```

要求 Python 3.8+（开发环境实测于 3.13）。可选：装了 Pillow 才能用
`cwf render --out x.png` 出 PNG，没装就出 SVG，核心功能不受影响。

告诉它你的工作流目录在哪（三种方式，任选）：

```bash
# 方式一：环境变量（推荐）
export CWF_WORKFLOWS=~/ComfyUI/user/default/workflows     # Linux / macOS
set CWF_WORKFLOWS=D:\ComfyUI\user\default\workflows       # Windows

# 方式二：不设也行 —— cwf 会自己找
cwf ws list

# 方式三：每次显式指定
cwf ws list --root ~/ComfyUI/user/default/workflows
```

自动探测会从常见位置（家目录、`Documents`、各盘符根、`/opt`、`/mnt` 等）
有界下探，直接找形如 `.../user/default/workflows` 的目录，不会扫全盘。
「ComfyUI Desktop / 便携版 / 整合包」各种装法都能认出来。

Windows 用户可以直接用 `cwf.cmd`（会自己找 `.venv` 里的 python，找不到就用 PATH 上的 `python`）。

---

## 30 秒上手

```bash
cwf ws list                     # 工作流库里有什么
cwf ws info 某个流               # 一眼看规模：节点/连线/旁路/孤儿
cwf outline 某个流               # 按数据流分几阶段（给 AI 读最省 token）
cwf validate 某个流              # 缺节点 / 缺模型 / 断线，一次查完
cwf beautify 某个流 --in-place   # 重排版式（自动备份原文件）
cwf nodes list 解码              # 本机有哪些节点（支持中文别名）
cwf build my.dsl --name 新流      # 用几行 DSL 拼一张新工作流
cwf render 某个流 --out 图.png   # 离线画成图，不用开 ComfyUI
```

所有命令都支持 `--json`；`cwf <命令> --help` 看详细用法。

---

## 核心能力

### 1 · 读 —— 把 JSON 变成人话

```bash
cwf outline 某个流        # 按数据流阶段梳理，自动分组
cwf cat 某个流 -d         # 每个节点的端口与参数
cwf deps 某个流 采样器 --depth 4     # 某节点的上下游链
cwf diff 旧版 新版         # 参数级差异
cwf grep "silver hair"    # 在内容里搜（类型/标题/参数值）
```

### 2 · 校验 —— 换机器之前先体检

```bash
cwf validate 某个流 --strict
```

一次报出：节点类型本机没装、引用的模型文件不存在、连线类型对不上、
必要输入漏了、悬空的输出。

### 3 · 改 —— 按名字改，不数下标

```bash
cwf set 流 --set "KSampler:steps=30" --set "CLIP:clip_name=xxx.safetensors"
cwf rename 流 --rename "KSampler=主采样"
cwf mode 流 --mute "某节点"              # 静音 / 旁路 / 恢复
cwf connect 流 --pair "加载器.IMAGE" "采样器.image"
cwf insert 流 --node 中间件 --between A B  # 插进已有连线中间
cwf splice 流 --node 某节点                # 摘掉它并把上下游焊上
```

### 4 · 造 —— 用 DSL 手搓一张新工作流

不用写 JSON，用几行文本描述数据流，剩下交给工具：

```
@title 文生图

模型   CheckpointLoaderSimple  ckpt_name=你的模型.safetensors   标题="1. 主模型"
正向   CLIPTextEncode          text="a cat on a windowsill"     标题="2. 正向"
负向   CLIPTextEncode          text="blurry, lowres"            标题="3. 负向"
潜图   EmptyLatentImage        width=1024 height=1024 batch_size=1
采样   KSampler                seed=42 steps=24 cfg=7.0 sampler_name=euler
解码   VAEDecode
保存   SaveImage               filename_prefix=cwf_demo

模型.MODEL        -> 采样.model
模型.CLIP         -> 正向.clip
模型.CLIP         -> 负向.clip
模型.VAE          -> 解码.vae
正向.CONDITIONING -> 采样.positive
负向.CONDITIONING -> 采样.negative
潜图.LATENT       -> 采样.latent
采样.LATENT       -> 解码.samples
解码.IMAGE        -> 保存.images
```

```bash
cwf dsl-check my.dsl                  # 只验语法
cwf build my.dsl --name 我的新流       # 自动接线 + 自动排版 + 自动校验
cwf scaffold                          # 打印模板
```

完整可跑的例子在 [`examples/`](examples/)：

```bash
cwf build examples/txt2img.dsl --name demo --out examples/     # 建图
cwf render examples/demo.json --out examples/demo.png          # 出图
```

DSL 的几个要点：

- **一行一个节点**：`名字  节点类型  参数=值 ...`，用空格分隔，不是 `=`
- **连线单独一行**：`模型.MODEL -> 采样.model`
- **端口可以省**：写 `模型 -> 采样` 时由**类型能力**推断谁喂给谁
  （所以 `模型 -> 采样` 和 `采样 <- 模型` 是一回事）
- **省略端口会造成歧义时报错**，不会瞎猜 —— 比如两个 `CONDITIONING`
  都指向同一个 `KSampler` 时，必须写清 `.positive` / `.negative`
- `$name` 是虚拟跳线，跨区域连线时自动建 `Set` / `Get` 节点
- `*端口` 广播到多个下游
- `标题="..."` 设标题；短名字存在 `properties.cwf_short`，可用 `short:` 前缀寻址

### 5 · 排版 —— 一键从"挤成一团"到分层清晰

```bash
cwf layout 某个流 --in-place      # 自动排版，只改坐标，不动语义
cwf beautify 某个流 --in-place    # 排版 + 补标题 + 重排序号
cwf place 某个流 --col "A B" --col "C"   # 手动指挥：按列摆、绝对坐标、相对微调
```

算法是分层的（Sugiyama 那一套）：

```
拆环 → 分层 → 插虚拟节点 → 中位数扫描降交叉 → 交换优化
     → 坐标分配 → 语义分区 → 消重叠
```

**硬保证：交出来的图，任何两个节点框都不重叠。** 这不是"尽量"——
`layout` 和 `place` 收尾都强跑一次消重叠，实测 435 张真实工作流，0 处重叠。

版式目标是**最小化最长边**，而不是最小化面积或追求黄金比例。因为长条
（10000×800）在 ComfyUI 里最难用，得横向滚半天。实测调整前后：

| 指标 | 调整前 | 调整后 |
|---|---|---|
| 宽度中位 | 4158 px | **2088 px** |
| 长边比 p90（最长:最短） | 12.6 : 1 | **1.9 : 1** |
| 每节点占位中位 | 359k px² | **210k px²** |
| 排版耗时 | 67 ms/张 | **34 ms/张** |

另外还有折叠列模式（`--fold N`）和紧凑打包（`--pack`），
让宽高比更接近屏幕。

### 6 · 节点库 —— 你知道本机装了多少种节点吗

```bash
cwf nodes stats              # 总量 + 按功能/原生/插件包三种分布
cwf nodes tree               # 16 大类 → 小类 → 具体节点
cwf nodes show 某类型 -v      # 一个节点的说明书：端口、控件、默认值、来源包、用过几次
cwf nodes classify 某类型     # 它为什么被判成这个类（命中哪条规则）
cwf nodes setcat 某类型 分类   # 手动改分类，立刻生效
cwf nodes unused             # 装了但从没用过的类型（清理插件参考）
cwf nodes atlas              # 导出全量分类图鉴
```

分类引擎是 16 大类、上千条规则的功能分类法（不是 ComfyUI 自己的
`category` 字段，那个太乱）。判定靠中文语义规则表，自动覆盖率 97%+，
剩下的可以手动覆盖。

### 7 · 模块化 —— 把工作流切成可复用的零件

```bash
cwf pack extract 某个流 --name 我的采样核心 --nodes "1-20" --dir ./modules
cwf pack insert 另一个流 --module 我的采样核心
cwf pack list
```

摘出来的模块带着自己的接口（暴露哪些输入、哪些输出），
拼进别的图时自动接线。

### 8 · 渲染 —— 不开 ComfyUI 也能看图

```bash
cwf render 某个流 --out out.svg
cwf render 某个流 --out out.png --scale 0.6 --theme light
```

纯 Python 画的：节点框、控件值、连线、分区框、注释、图例全都有。
有 Pillow 就出 PNG，没有就出 SVG（浏览器直接看，还能无损缩放）。

### 9 · 执行 —— 带护栏的提交

```bash
cwf precheck 某个流     # 干跑：这台机器跑不跑得动
cwf run 某个流          # 提交到 ComfyUI
cwf queue               # 看队列
```

`run` 默认会拦下两类工作流，要跑必须显式放行：

- **ControlNet 类** —— 显存和内存占用最凶，最容易 OOM
- **视频生成类** —— 长任务，崩一次损失大

理由是这两类"一次记不住就白跑"，所以做成命令级默认拒绝，
而不是写在文档里靠人记得住。阈值和开关都能调：

```bash
export CWF_VRAM_GB=24          # 声明显存，警告语会带上它，画布阈值跟着放宽
export CWF_MAX_MP=8            # 自定义"大画布"阈值（百万像素）
export CWF_GUARD=off           # 整体关掉，只报告不拦
export CWF_GUARD_ALLOW=video   # 把视频类预先放行
```

还有 `cwf export-api` / `cwf import-api` 在 UI 格式和 API 格式之间转换。

### 10 · 负载量化 —— 这张图我这台机器跑得动吗

```bash
cwf rig                    # 设备能力：显存 / 内存 / 架构 / 实测速度
cwf load 某个流            # 工作流负载：要多少显存和内存
cwf fit 某个流             # 两者对照：余量、瓶颈、判定
```

三个命令给的都是**数值**，不是"大概能跑"这种感觉：

```
负载画像：MiniMax+H3+导演台全能工作流.json
────────────────────────────────────────────────────────────
  权重合计  37.78 GB   ← 磁盘实测，误差 <1%
    unet           19.53 GB  …ngularity_ref2va_Pruned_v1.3_int8.safetensors
    text_encoders  14.61 GB  …n3vl_32b_heretic_minimax_h3_nvfp4.safetensors
    vae             2.95 GB  minimax_h3_video_vae_int8_convrot.safetensors
    vae             0.56 GB  minimax_h3_audio_vae_fp32.safetensors

  补丁合计  2.98 GB   （2 个 LoRA，合并进基座）

  ── 与这台机器的关系 ──────────────────────────────────
  显存需求  10.15 GB   （流式模式：只要求装得下一块）
            ██████████████████████  7.50 GB 可用 · 余量 -2.66 GB
            ⚠ 权重 37.8 GB 超过显存，必然走动态流式加载
  内存需求  38.28 GB
            ██████████████████████  31.78 GB 实有 · 余量 -6.50 GB

  ✖ 判定：不够 · 瓶颈在内存
      · 内存差 6.5 GB。这台机器已经在靠交换文件硬撑，
        速度会掉得很厉害。降负载最有效的一条是换更小的量化版本
```

**数字是怎么来的（这决定了它能不能信）**

| 量 | 来源 | 精度 |
|---|---|---|
| 权重占用 | 模型文件在磁盘上的字节数 | **实测误差 <0.2%** |
| 设备能力 | `nvidia-smi` + ComfyUI `/system_stats` | 精确 |
| 真实速度 | ComfyUI 日志里的 `34.94s/it` | 实测 |
| 激活项 | 分辨率 × 通道 × 分块帧数 | **估的，±50%** |

权重项之所以敢说 "误差 <0.2%"，是因为跟 ComfyUI 自己的日志对过账 ——
日志里 `prepared for dynamic VRAM loading. NNNNMB Staged` 与磁盘字节：

```
19995 MB  ←→  19996 MB   差 +0.0%
14955 MB  ←→  14957 MB   差 +0.0%
  576 MB  ←→    577 MB   差 +0.2%
```

**自动校准**：`cwf fit` 会去翻 ComfyUI 日志，把日志里的实测占用按**文件大小**
配对到本次工作流用到的模型，当场给出偏差。配对不上就什么都不说 ——
拿一次无关运行的记录来"校准"是胡说八道。

两条口径（这是最容易搞混的地方）：

- **内存** = 全部权重必须装得下。动态加载要把权重整个驻留内存。
- **显存** = 要么全装下，要么按块流式（那就只要求"装得下一块"）。

所以显存小不等于跑不动，但内存不够就是真的跑不动。

还有一条容易被忽略的：**LoRA 不算独立常驻权重**。它合并进基座模型
（日志里写着 `208 patches attached`），磁盘上虽然好几个 GB，
但不额外增加峰值。这一点第一版算错过，估高了 8.9%。

---

## 三步走的设计原则

1. **只改该改的。** 排版只动 `pos` / `size` / `groups`，
   `to_api()` 出来的东西前后**逐字节相同**——这条有测试盯着。
2. **不懂就问，别猜。** 端口对不上、节点名有歧义，直接报错并列出候选，
   绝不默默挑一个。
3. **零依赖。** 核心只用标准库。要装包才能跑的工具，在别人机器上
   第一步就卡住了。

---

## 踩过的三个坑（ComfyUI JSON 的隐藏地雷）

这大概是这个工具最实在的部分，写给同样要解析这些文件的人：

1. **顶层 `links` 是脏的。** 删节点后它不会清理，于是
   `nodes[].inputs[].link` 和顶层 `links` 经常对不上。
   **以节点侧的 `link` 为准**，把它当权威，顶层 `links` 当成缓存，
   加载时自愈重建。
2. **`pos` / `size` 有时是数组，有时是 `{"0": x, "1": y}` 这种字典。**
   两种都得认，否则一读就崩。
3. **`SetNode` / `GetNode` 不在 `/object_info` 里。** 它们是前端 JavaScript
   自己实现的虚拟节点，走 API 提交时会被就地展开。
   拿 `/object_info` 校验工作流会误报"节点不存在"，要单独放行。

---

## 目录结构

```
cwf/
  cli.py            命令行入口，41 个顶层子命令
  lib/
    graph.py        工作流数据模型（节点/连线/槽位/分区），读写与自愈
    schema.py       /object_info 节点字典、尺寸估算、缓存
    strata.py       自动排版引擎（分层 + 消重叠 + 分区）
    dsl.py          DSL 解析器
    render.py       离线渲染（SVG / PNG）
    taxonomy.py     16 类功能分类引擎
    catalog.py      全库节点索引
    store.py        节点知识库（别名 / 笔记 / 收藏）
    terms.py        中文术语表
    paths.py        可移植的路径探测
    meta.py         生成图元数据读取与统计（cwf meta）
    sigma.py        sigma 表计算与安全校验（cwf sigma）
    pack.py         模块拆装
tests/
  run_tests.py      一次跑完全部自测
  harness.py        零依赖测试骨架
  test_*.py         7 组、150+ 条断言
```

---

## 测试

```bash
python tests/run_tests.py          # 全部
python tests/run_tests.py -v       # 失败时打完整堆栈
python tests/run_tests.py layout   # 只跑文件名含 layout 的
```

不用 pytest —— 本工具承诺只用标准库，测试也照这个规矩来。

自测分两类：

- **不依赖真实工作流的**：图模型、DSL、分类、渲染、护栏、消重叠。随时可跑。
- **依赖真实工作流的**：加载全库、排版指标、几何质量。没设
  `CWF_WORKFLOWS` 时**自动跳过**并说明原因，不会给你一片红灯。

排版相关的判据刻意写成**自校准**的：

- 硬指标 —— 节点框重叠必须为 0，分区框必须不压边
- 相对指标 —— 穿节点数「不比原始手摆的图差」

不写死绝对阈值，是因为那是拿某台机器上某几张图调出来的数字，
换个人立刻失效，只会逼后来的人去调松阈值。

---

## 已知边界

说清楚不做什么，比吹能做什么有用：

- **不做精细连线路由。** 现在只用朴素的 L 形走线，跨多列的长边会从
  中间列的节点上压过去（穿节点率中位约 37%）。试过"逐列通道分配"的
  精细路由，结果是交叉数翻倍、观感更差，所以先保留朴素版。
  另外按使用者的口径：**线交叉本身不是问题，节点框重叠才是**，
  所以交叉数只测量、不当门槛。
- **超大图排版效果有限。** 300+ 节点的视频图，密度仍然只有 6% 左右——
  这是图本身的形状决定的，`cwf place` 手动指挥更合适。
- **`run` 的提交路径未做端到端实测。** 会做校验和护栏，但没有在真实
  服务上跑过完整出图流程，第一次用请自己盯一下。
- **分类引擎有 3% 左右落在兜底类。** 那部分靠 `cwf nodes setcat` 手动覆盖。
- **负载量化的激活项是线性估算，捕捉不到注意力的超线性增长。**
  权重项（占 95%+）是磁盘实测、误差 <0.2%，可信；
  但「分辨率多高就跑不动」这件事由注意力工作集决定，随 token 数超线性增长，
  本工具只能给经验提醒（`CWF_ATTN_MP` 可调），给不出准数。
- **只处理 UI 工作流格式。** API 格式可以 `import-api` 转进来，但
  从 API 格式转出来的图没有原始坐标信息。

---

## License

MIT —— 见 [LICENSE](LICENSE)。
