---
name: comfyui-workflow
description: |
  ComfyUI 工作流的读写、编辑、自动排版与模块化拼装工具包（`cwf` 命令）。用命令行而不是直接读写那几十 KB 的 JSON——把 142KB 节点图压成 2KB 语义摘要、把"猜数组下标"变成"按名字改参数"、把挤成一团的节点一键排成分层清晰的版式，并能把片段切出来复用到别的工作流。
  能力：(1) 读/查——列工作流库、看结构、按数据流阶段梳理、查节点上下游、两版 diff；(2) 校验——节点类型是否都装了、引用的模型文件是否存在、接线与类型是否对、必要输入有没有漏；(3) 自动排版——分层图算法（拆环→分层→交叉最小化→长边拉直→语义分区→消重叠），只改坐标不改语义；(4) 改图——改参数（支持按类型批量）、加删节点、接线断线、把节点插进已有连线、摘掉节点并焊接上下游、静音/旁路；(5) 造图——用几行 DSL 拼一张新工作流，自动接线自动排版自动校验；(6) 模块化——把工作流切成可复用模块存起来，再拼进别的图；(7) 节点库——查本机装了多少种节点、各是什么性质、来自哪个插件包、你用过几次；(8) 提交执行与格式转换。
  触发词：ComfyUI 工作流、工作流 JSON、节点图、工作流排版、节点太乱、重新排列节点、美化工作流、工作流美化、改工作流参数、换模型、加节点、删节点、接线、连线、断线、模块复用、工作流片段、拼装工作流、新建工作流、写个工作流、工作流校验、工作流报错、节点缺失、模型路径不存在、工作流备份、节点库、装了哪些节点、有多少节点、插件包、cwf、ComfyUI workflow、node graph layout、rearrange nodes。
  用户说「这张工作流太乱了帮我排一下」「改一下这张工作流的参数/提示词」「这几个节点帮我接起来」「把这段切出来以后复用」「检查这张图能不能跑」「我电脑上装了多少节点」「用我说的节点拼一张新工作流」时使用本技能。
  不适用：AI 绘画提示词创作（见 ai-painting 技能）、MiniMax H3 视频提示词写作（见 minimax-h3 技能）、ComfyUI 本身的安装配置与自定义节点开发、纯图片编辑。
---

# ComfyUI 工作流工具包（cwf）

**核心原则：不要直接读写工作流的 JSON，用 `cwf`。** 理由不是"方便"，而是下面这些**实测**数字：

| | 直接读写 JSON | 用 cwf |
|---|---|---|
| 理解一张 144 节点主力流 | 142.2 KB / **145,664 字符**全进上下文，其中 **39% 是 pos/size/order 排版噪声** | `outline` + `ws info` ≈ **2 KB 语义摘要**（约 1/70） |
| 改一个参数 | 得先猜 `widgets_values` 数组哪个下标对应哪个控件（这库里**146 处是字典形态**，位置数组与字典混着来） | `--set "5. 一采:steps=40"`，按名字改 |
| 接线 | 手算 `links` 数组格式 + `origin_slot` 下标 + 输出端反向引用，三处必须一致 | `源.输出 -> 目标.输入`，类型不对当场报错 |
| 改坏了怎么知道 | 打开 ComfyUI 才知道 | `cwf validate` 一次查清 |
| 节点挤成一团 | 手动拖一百多个 | `beautify` 一键，自动备份 |

**三个真实世界的坑**（直接读写一定会踩，cwf 已替你趟过）：

1. **顶层 `links` 数组经常是陈旧的**。实测某工作流记录 `origin_slot=612`，而那个节点只有 2 个输出——id 与端口对不上。ComfyUI 前端真正读的是节点上的 `link` 字段。cwf 以节点侧为权威，对不上就修、缺了就反推。
2. **`pos`/`size` 有 3731 处是字典**（`{"0":..,"1":..}` 而非数组），硬按数组读会 `KeyError`。
3. **`SetNode`/`GetNode` 不在 `/object_info` 里**——它们是前端 JS 注册的，不是"节点没装"。

---

## 0 · 调用方式

通过启动器调用（它自动找 ComfyUI 的 venv Python）：

```bash
# Windows 直接调
<仓库根>\tools\cwf.cmd <子命令> [参数]

# 或跨平台包装脚本（路径已配好，推荐 agent 用这个）
bash ~/.dsh/skills/comfyui-workflow/scripts/cwf.sh <子命令> [参数]
```

所有命令都支持 `--json`（结构化输出）；不加则输出人读文本。

**环境常量**（工具已内置，一般不用传）：

| 项 | 值 |
|---|---|
| 工具内核 | `<仓库根>\tools\cwf\` |
| 工作流库根 | `<ComfyUI>/user/default/workflows`（即你的工作流库） |
| ComfyUI 服务 | `http://127.0.0.1:8188` |
| 缓存 | `~\.cwf\`（节点字典 `object_info.*.json` + 节点库 `node_catalog.json`） |

工具不在上述位置时：`scripts/cwf.sh` 支持用环境变量 `CWF_HOME` / `CWF_PYTHON` 覆盖。

---

## 1 · 安全边界（先看这条）

> ### ⛔ 硬件红线（本机 8 GB 显存，命令级拦截）
>
> * **不得提交含 ControlNet 的工作流** —— 吃显存和内存都很凶，容易爆。
> * **不得擅自提交视频工作流** —— 8 GB 顶不住（16 GB 才行，但现实没有）。
>   视频**只能跑用户自己准备好、验证过的流程**。
> * **图片模型的工作流可以直接跑。**
>
> 这不是提醒，是 `cwf run` / `queue` 里写死的拦截：命中就拒绝提交（退出码 2），
> 必须显式 `--allow-controlnet` / `--allow-video` 才放行。
> 提交前想先看看：`cwf precheck 某工作流`（只预检，不提交）。


| 命令类别 | 是否动文件 | 风险 |
|---|---|---|
| **读/查/校验**：`ws list/info/resolve` `find` `grep` `cat` `outline` `deps` `diff` `stats` `validate` `nodes *` `schema *` | 不动 | **零风险** |
| **排版**：`layout` `beautify` | 默认只打印，`--out` 才写 | 只改 `pos`/`size`/`groups`，**语义不可能变**（全库 17173 条连线实测：接线与控件值一字节未变） |
| **改/拼/造**：`set` `rename` `mode` `add` `remove` `insert` `splice` `pack` `build` | 默认 `--out` 写新文件 | 批量改**先预检再写**：有节点没那个控件就整批不动 |
| **提交执行**：`run` | 往 ComfyUI 队列真提交 | ⚠ 会占用显卡 |
| **原地覆盖**：`--in-place` | 覆盖原文件 | 先自动备份成 `.bak-<时间戳>` |

**改图标准流程**（永远别一上来就 `--in-place`）：

```bash
cwf validate 原图.json                          # 1. 看现状
cwf set 原图.json --set "..." --out 新图.json    # 2. 改到新文件
cwf validate 新图.json                          # 3. 复验
cwf beautify 新图.json --in-place               # 4. 满意了再原地排版（自动备份）
```

---

## 2 · 按任务走（最常见的十件事）

### 2.1 「这张工作流是干嘛的？」——理解陌生的图

> 想直接**看一眼**而不是读文字？`cwf render 某流 --out preview.svg` —— 见 §2.9。

```bash
cwf outline 某流              # 按数据流阶段列结构（最省 token，先看这个）
cwf ws info 某流              # 规模、高频节点类型、终结点、孤儿节点
cwf cat 某流 -d --limit 30    # 需要细节时：每个节点的参数与端口
cwf deps 某流 解码 --depth 4   # 追某个节点的上下游链
```

### 2.2 「这张图能跑吗？」——体检

```bash
cwf validate 某流              # 全量校验
cwf ws resolve 某流            # 专查：节点类型装没装、模型文件在不在
```

`validate` 会查出：节点类型不存在 / 引用的模型文件不存在 / 连线指向不存在的槽位 / 输入类型不匹配 / 必要输入没接线 / 孤儿节点 / 没有终结点。**报错会附带下一步建议。**

### 2.3 「太乱了，帮我排一下」——排版美化

```bash
cwf beautify 某流 --in-place       # 排版 + 补标题 + 重排序号，自动备份
cwf layout 某流 --out 新图.json     # 只排版，不改别的
```

可调：`--compact`（紧凑）/ `--loose`（宽松）/ `--no-groups`（不重画分区框）/ `--keep-notes`（注释留原地）/ `--h-gap --v-gap`（间距）。

**引擎做的事**（不是"重新摆一遍"）：拆环 → 最长路径分层+收紧 → 中位数启发式+逐对交换精修 → 长边插虚节点+两趟对齐拉直 → 横竖版式择优（避免排出面条）→ 语义分区聚类 → 全局消重叠。

### 2.4 「帮我改一下」——改参数/节点/接线

```bash
# 改参数（可按类型批量）
cwf set 某流 --set "5. 一采:steps=40" --out 新图.json
cwf set 某流 --set "type:LoadImage:image=新图.png" --out 新图.json

# 节点与接线
cwf rename 某流 --rename "5. 一采=主采样器" --out 新图.json
cwf mode 某流 "type:CLIPTextEncode" --mode bypass --out 新图.json
cwf insert 某流 --type ImageScale --on "解码" "保存" --title 缩放 --out 新图.json
cwf splice 某流 "缩放" --out 新图.json          # 摘掉它并把上下游接直
cwf connect 某流 --pair 模型.MODEL 采样.model --out 新图.json
cwf disconnect 某流 --to 采样.model --out 新图.json
```

**节点引用写法**（所有改/删/连命令通用）：

| 写法 | 含义 |
|---|---|
| `#12` / `12` | 按节点 id |
| `type:KSampler` | 按类型，**可批量** |
| `title:采样` | 按标题子串，可批量 |
| `~^05\.` | 按标题正则 |
| `05. 我的主采样器` | 标题全等 / 去序号后比 / 子串 |
| `解码` `主模型` `放大` | **中文别名**（60+ 条，见 `cwf nodes alias`） |

### 2.5 「能干这件事的节点有哪些？」——按功能找节点

**别按插件名找节点。** ComfyUI 自带的 `category` 字段对插件节点来说基本等于**插件名**
（`Apt_Preset` 395 种、`RunningHub` 380 种、`T8` 356 种…），按它找等于按「谁做的」找。

cwf 自己按**功能**把 7746 种节点分成 16 大类：

```bash
cwf nodes tree                      # 16 类总览：每类种数、用过多少、有哪些小类
cwf nodes tree --cat 视频            # 展开：视频/插帧、视频/合成、视频/视频模型…
cwf nodes list --cat 遮罩 --used-only   # 平铺列出你用过的遮罩节点
cwf nodes classify VAEDecode         # 它是怎么被判成这个类的（依据全摊开）
```

16 类（中英文都能当 `--cat` 用）：

| `load` 加载输入 | `model` 模型与适配 | `cond` 条件与提示词 | `sample` 采样与生成 |
|---|---|---|---|
| `latent` 潜空间 | `image` 图像处理 | `mask` 遮罩 | `video` 视频 |
| `audio` 音频 | `threed` 三维 | `text` 文本与数据 | `num` 数值与逻辑 |
| `output` 输出与预览 | `flow` 流程与组织 | `api` 网络与云 | `misc` 未归类 |

判定依据是**端口类型**（最硬，输出 VIDEO 的就是视频节点，跟谁写的无关）
→ 类型名（按词切分逐 token 匹配）→ 原生分类路径。实测自动判定覆盖率 **97.2%**，
拿不准宁可进 `misc` 也不猜。

判错了改一行就永久生效，**不用重建缓存**：

```bash
cwf nodes setcat 某节点 图像处理/放大          # 直接改
cwf store mark KSampler -c 采样与生成/采样器    # 沉淀的时候顺手归类
```

写入 `~/.cwf/store/categories.txt`（纯文本，支持 `MiniMaxH3* = 视频/视频工程套件` 尾部通配），
**用户覆盖永远优先于任何自动规则**。

要全量离线翻阅就导图鉴：`cwf nodes atlas` → `~/.cwf/atlas/`（17 个文件，7746 种一个不少）。

### 2.6 「这个节点我上次摸清了」——沉淀进节点知识库

**这是省时间的核心。** 每次要用某个节点都重查一遍端口，是最烦的摩擦。
查明白之后花一条命令记下来，以后直接叫名字：

```bash
cwf store mark KSampler -a 采样器 -p -c 采样与生成/采样器 -n "model 接模型；latent_image 接潜空间"
#   -a 起别名（以后所有命令和 DSL 里都能用这个名字）
#   -p 加进常用列表（cwf store list 一眼看全）
#   -c 顺手归类（免得自动分类判错）
#   -n 记一笔（怎么接、踩过什么坑）

cwf store find 解码        # 检索：★ 标记的是你沉淀过的，排最前面
cwf store show 采样器      # 速查卡：功能分类 + 端口 + 控件 + 你的笔记，一屏看完
cwf store list             # 沉淀总览：别名 / 常用 / 笔记
cwf store export           # 导出 markdown，换机器直接拷
```

沉淀物是**纯文本**，放在 `~/.cwf/store/`：
`aliases.txt`（一行一条）、`pins.txt`、`notes/<类型>.md`、`categories.txt`。
**出问题你自己就能改，不用找工具作者。** 内置的中文别名（解码/主模型/放大…）
在代码里，不用重复写。

> 沉淀过的节点在 DSL 里可以直接当类型名用：
> `采样 采样器 seed=1 steps=8` —— 不必记 `KSampler` 这个英文名。

### 2.7 「拼一张新的」——DSL 造图

**新写法：一行写完节点和接线，端口名可以省。**

```
@title 文生图

模型   CheckpointLoaderSimple  ckpt_name=xxx.safetensors
正向   CLIPTextEncode          text="1girl, cinematic"
潜图   EmptyLatentImage        width=832 height=1216 batch_size=1
采样   KSampler                positive=正向 negative=正向 seed=1 steps=28
解码   VAEDecode
保存   SaveImage               filename_prefix=CWF/out

模型 -> 采样 <- 潜图            # 工具按类型自动接对：model←MODEL、latent_image←LATENT
模型.CLIP -> 正向 -> 采样.positive
采样 -> 解码 -> 保存
```

三种接法，按需要挑：

| 写法 | 含义 |
|---|---|
| `a -> b` | a 和 b 之间连一根线，**端口由工具按类型推断** |
| `a.MODEL -> b` 或 `a -> b.model` | 只写一头，另一头按类型推断 |
| `采样 KSampler positive=正向` | 节点定义行里直接接线（最紧凑） |

推断规则：`->` 只表示「a 与 b 之间有连线」，**真正谁喂给谁由端口能力决定**
（能产出 → 能接收）。所以 `模型 -> 采样 <- 潜图` 里那个反向的 `<-` 也能正确处理。

**有歧义时报错而不是瞎猜**：`正向 -> 采样` 会告诉你 CONDITIONING 既能接
`positive` 也能接 `negative`，让你写明——因为挑错就是整张图出错。

```bash
cwf scaffold blank > x.dsl               # 起模板（另有 txt2img / img2img / h3_ref2va）
cwf dsl-check x.dsl                      # 先验语法
cwf build x.dsl --name 名字 --validate    # 建图 + 排版 + 校验
cwf export-dsl 某工作流                   # 反推：把已有工作流转成 DSL 再改
```

DSL 完整语法（虚拟跳线 `$名字`、广播 `*端口`、中文端口名）见 `references/dsl.md`。

### 2.8 「这段以后还要用」——模块化

```bash
cwf pack split 某流 --nodes "type:SamplerCustomAdvanced,type:RandomNoise" \
    --name H3采样核心 --dir <模块库目录> --description "H3 采样四件套"
cwf pack list --dir <模块库目录>
cwf pack show H3采样核心 --dir <模块库目录>      # 看对外接口
cwf pack use H3采样核心 --dir <模块库目录> --workflow 目标流.json --out 拼好的.json --layout
```

`--nodes` 选择器：`#1,#2` / `10-30` / `标题子串` / `type:KSampler` / `~正则`。

### 2.9 「我想看一眼它长什么样」——离线渲染成图

**不用开 ComfyUI，也不用开浏览器。** 直接把工作流画成图：

```bash
cwf render 某流 --out preview.svg          # 默认 SVG：纯标准库，矢量，放多大都清晰
cwf render 某流 --out preview.png          # PNG：本机有 Pillow 时可用
cwf render 某流 --out p.png --scale 2      # 放大出图
cwf render 某流 --out p.png --theme light  # 亮色主题
cwf render 某流 --out p.svg --color comfy  # 标题栏素色（跟前端一致）
cwf render 某流 --out p.png --open         # 渲染完直接打开
```

画出来的东西：节点框（标题栏、端口圆点、控件条、静音/旁路标记）、
连线（按数据类型上色）、分区框、便签、左上角图例（按功能分类统计）、右下角尺寸。

两处**有意不照抄前端**，都是为了让静态图更可读：

| 行为 | 为什么 |
|---|---|
| 标题栏按**功能分类**上色 | 一眼看出"哪段在加载、哪段在采样、哪段在出图"。要前端素色用 `--color comfy` |
| 控件条显示 `名字: 值` | 静态图没有悬停提示，只写个 `28` 谁都看不出是步数。要纯前端观感用 `--no-widget-names` |

**节点自己带的 `color`/`bgcolor` 永远优先**（工作流里 38% 的节点有你标的颜色）。

出图之前先排版，图才好看：`cwf beautify 某流 --out 排好的.json` 再 render。

> 大图（几千节点）PNG 会缩到看不清字，这时**用 SVG** —— 矢量放大不糊。
> 命令会自动提示。



### 2.10 「这几个节点我要自己摆」——手动指定位置

自动排版是「工具说了算」。想让**你（agent）说了算**，用 `place`：

```bash
# 1. 列计划：一行一列，列内从上往下堆（最好用，结构一眼看清）
cwf place 某流 --col "主模型 正向 负向" --col "一采" --col "放大 二采" \
               --col "解码 出图" --gap-h 70 --gap-v 30 --out 摆好的.json

# 2. 列计划也可以写成文件（# 开头是注释，col 前缀可省）
cwf place 某流 --plan plan.txt --out 摆好的.json

# 3. 绝对坐标 / 相对微调（选择器与其它命令完全一致）
cwf place 某流 "#12=1000,200" "type:KSampler+=0,-300" --out 摆好的.json

# 4. 只想压紧、不改结构
cwf place 某流 --pack --gap-h 48 --gap-v 22 --out 紧凑的.json
cwf place 某流 --pack --fold 2 --out 折成两栏的.json     # 每 2 列并成 1 列
```

**选择器**：`#id` / `type:类型` / `title:子串` / `short:短名` / `~正则` / 中文别名。
`short:` 是 **DSL 建的图**最靠得住的抓手 —— 一张图里常有四个 `EmptyLatentImage`，
只有 DSL 短名（`竖图`/`横图`/`方图`/`宽图`）能区分谁是谁；不带前缀直接写短名也能匹配。

四条保证：

* **交出来一定 0 重叠**。这是工具的责任 —— 手写的列计划、绝对坐标、或者原来
  就乱的版式，`place` 都会在收尾时把压在一起的节点推开，并在输出里报
  「节点重叠 0 处」。不用你回 ComfyUI 手拖。（`--no-fix-overlap` 可关。）
* **只改 `pos`**。接线、控件值、`to_api()` 前后完全一致（有测试盯着）。
* **没说就不落盘**：`--out` 写指定文件、`--in-place` 先备份再改，两个都不给就不写。
* **坐标是你说了算的**：明确指定的绝对坐标不会被"顺手归一化"改掉；
  只有整张图落在很远的负半轴（多半是被拖飞了）才会整体拉回来。
* 挪完会**重算分区框**（`--no-regroup` 可关）。重算框**不会顺带挪节点**。

> 什么时候该手摆：横向拉太长的链（用 `--col` 折成几栏）、分支多的图
> （每个分支竖成一列，扇出线就平行了）、跳线（Set/Get）节点该贴在使用者旁边
> 而不是飘在中间。

### 版式质量的判断口径（重要，别搞反）

用户 2026-09-15 明确过：

* ✅ **节点框不能重叠** —— 这是硬指标，也是工具的责任，
  不能让用户回 ComfyUI 手拖。
* ❌ **连线交叉不算问题** —— ComfyUI 里线交叉本来就是常态。
  测试里交叉数只**报告**，不当门槛。
* ✅ **别把图拉成长条** —— 横条和竖条一样难用，所以盯"最长的那条边"。

`layout` / `beautify` / `place` 交出来的东西**保证 0 节点重叠**
（收尾会强跑一次消重叠，全库 435 张实测 0 处）。
版式评分的主目标就是**最小化最长边**，面积只做次要项。

---

## 3 · 给 agent 的使用建议

**理解陌生图按这个顺序，别一上来全量 `cat -d`**：
```
ws info  →  outline  →（需要细节才）cat -d --limit N / deps
```

**改图分两步**：先 `--out` 出新文件 → `validate` → 满意再 `--in-place`。

**接线拿不准就别猜**：
```bash
cwf nodes tree --cat 视频     # 按**功能**找节点（16 大类，中英文都能用）
cwf nodes show 节点类型 -v    # 功能分类、必接输入、输出、控件、默认值、来源包、你用过几次
cwf schema produce LATENT     # 谁能产出 LATENT
cwf schema accept MODEL       # 谁能接收 MODEL
cwf nodes list 放大           # 中文别名也能搜
```

**ComfyUI 没启动时**：读/查/校验照常可用（子命令后加 `--offline`，如 `cwf ws info 某流 --offline`）；`schema refresh`、`nodes build`、`run` 需要服务在线。

---

## 4 · 命令全表

完整清单与参数含义见 **`references/commands.md`**。速查：

```
读：ws list|info|resolve   find   grep   cat   outline   deps   diff   stats   validate
排：layout   beautify   place
改：set   rename   mode   add   remove   insert   splice   connect   disconnect   mute-group
造：build   scaffold   dsl-check   export-dsl
看：render
拼：pack split|list|show|use
库：nodes stats|tree|list|show|classify|setcat|atlas|pkg|unused|alias|build
沉淀：store find|show|mark|note|alias|pin|list|export
字典：schema search|show|produce|accept|models|categories|refresh|stats
跑：run   queue   precheck（硬件预检，不提交）        转：export-api   import-api
```

---

## 5 · 已知边界（别当万能的）

1. **节点重叠 0、接线被改 0**——硬保证，全库 435 张实测。
2. **连线偶尔从某个节点框上经过**（中位约 33% 的边）——纯外观。试过精细的逐列通道路由，结果交叉数翻倍反而更乱，故保留朴素路由。**不影响接线正确性，不影响能否运行。**
3. **分区框偶尔互相压边**（实测 43/435 = 9.9%）——同为纯外观问题。
4. **`validate` 的「必要输入没接线」可能误报**：某些节点的 `required` 实际渲染成控件。看类型，`COMBO` 类基本可忽略。
5. **`pack` 按类型对接，不校验语义匹配**——拼完记得 `validate`。
6. **`run` 提交执行这条链路未在本机实测**（没敢在主人不知情时占用显卡）。首次使用留意。
7. **前端注册的节点**（`SetNode`/`GetNode`/`Reroute`/rgthree 开关）不在 `/object_info`，节点库会标「⚠未注册」而不报错。
8. **`place` 只保证「坐标听你的」，不保证好看**。它不会替你判断"这样摆连线会不会更乱"——摆完自己看一眼（`cwf render`），或先看 `strata.count_geometric_crossings` 之类的指标。分区框**压边**也不会被自动推开（推开是靠平移节点实现的，那会违反"坐标听你的"）。
9. **功能分类是启发式判定，不是官方定义**。7746 种插件节点不可能条条判对：实测自动覆盖率 **97.2%**，剩下 2.8%（214 种）落在 `未归类`，其中**你用过的只有 9 种**。判错不用忍——`cwf nodes setcat` 改一行永久生效。另：有 115 种节点压根不在 `/object_info` 里（插件卸载残留、前端虚拟节点），它们端口信息为空，判定只能靠名字，置信度标 `low`。

---

## 6 · 排障

| 症状 | 处理 |
|---|---|
| `找不到工作流 'x'` | 路径相对工作流库根，也可传绝对路径；模糊匹配到多个会列候选 |
| `ComfyUI 里没有节点类型 'x'` | 拼写错或节点没装。`cwf nodes list x` 搜相近的 |
| 读不到 `/object_info` | ComfyUI 没启动。用 子命令后加 `--offline` 走缓存，或启动后 `schema refresh` |
| 报「节点类型不存在」但明明装了 | 那是前端注册的节点，属正常。真缺插件用 `cwf ws resolve` 确认 |
| `nodes` 统计不对 | 装了新插件要 `cwf nodes build` 重建节点库 |
| 工具找不到 | 检查 `<仓库根>\tools\cwf.cmd`；或用包装脚本 |
| 工作流里有一堆 `GetNode/SetNode` | 那是虚拟跳线（传值用的），正常。见 `references/architecture.md` |

---

## 7 · 打包资源

| 文件 | 何时读 |
|---|---|
| `references/commands.md` | 需要某个子命令的完整参数说明时 |
| `references/dsl.md` | 要用 DSL 造图 / 写 DSL 语法时 |
| `references/architecture.md` | 需要理解数据模型（两种 widgets 形态、虚拟跳线、陈旧 links）、或要改这个工具本身时 |
| `references/nodes.md` | 要查节点库、**按功能找节点**、看分类依据与覆盖率、改错判的分类时 |
| `references/store.md` | 要用节点知识库沉淀/检索、或想知道沉淀物存在哪、怎么手工改时 |
| `references/place.md` | 要手动指定节点位置（列计划 / 绝对坐标 / 压紧折列）时 |
| `references/render.md` | 要用 `cwf render` 出图、调主题/配色/缩放，或想知道渲染器不画什么时 |
| `scripts/cwf.sh` | 调用入口（bash 包装，内含路径配置） |
| `scripts/node_report.py` | 一次性导出机器可读的节点库总览（不必读进上下文即可执行） |
