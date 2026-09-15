# 节点库：本机装了什么节点、怎么按功能找

## 一句话

`cwf nodes tree` = 「**能干这件事的节点有哪些**」。
分类是**按功能**走的，不是按插件名 —— 找的是「谁能把图放大」，不是「哪个插件里有放大」。

---

## 数据从哪来

| 来源 | 提供什么 |
|---|---|
| `/object_info` | 节点类型、原生分类、端口、控件、默认值、可选值 —— **权威定义** |
| `object_info` 的 `python_module` 字段 | 形如 `custom_nodes.comfyui-minimax-h3-audio-T8` —— **唯一能反查插件包的权威线索** |
| 你的工作流 JSON | `properties.cnr_id`（补充线索，老工作流常缺）+ **使用频次** |

结果缓存在 `~/.cwf/node_catalog.json`（4.6 MB），带三把"钥匙"：
目录版本号 + 工作流库指纹（文件数:最新修改时间）+ ComfyUI 地址。
**ComfyUI 升级、工作流库变了、或分类法改版才重建**，否则读缓存。

---

## 实测规模（2026-09-15，ComfyUI 在线重建）

```
节点类型总数       7746
实际用过的类型     1014
插件包数量          189
扫描工作流          435 个（13533 个节点实例）
```

贡献最多的包：`comfy-core` 729、`ComfyUI-Apt_Preset` 396、`ComfyUI_RH_OpenAPI` 377、
`comfyui-minimax-h3-audio-T8` 331（用过 1319 次）、`RES4LYF` 293。

> 「用过次数」= 在你 435 张工作流里出现过的实例总数。**0 次不代表没用**。

---

## 按功能分类找节点（重点）

### 为什么不能直接用 ComfyUI 自带的分类

`/object_info` 里每个节点确实有个 `category` 字段，但对插件节点来说它**基本等于插件名**：

```
Apt_Preset 395   RunningHub 380   T8 356   RES4LYF 290   😺dzNodes 246 ...
```

按这个分类找节点 = 按「谁做的」找。只有 comfy-core 的原生分类是真·功能路径
（`model/sampling/samplers`、`image/upscaling`）。179 个分类里绝大多数是厂商名。

### 所以 cwf 自己判：16 个功能大类

| id | 中文 | 种数 | 用过 | 这一类的节点能干什么 |
|---|---|---|---|---|
| `load` | 加载输入 | 157 | 18 | 把磁盘/网络上的文件读进来 |
| `model` | 模型与适配 | 658 | 123 | 模型本体的加载、合并、补丁、量化 |
| `cond` | 条件与提示词 | 456 | 77 | 提示词编码、条件合并、区域与引导 |
| `sample` | 采样与生成 | 395 | 62 | 采样器、调度器、噪声、引导器 |
| `latent` | 潜空间 | 194 | 33 | 潜空间的产生与变换 |
| `image` | 图像处理 | 1625 | 182 | 变换、合成、滤镜、放大、修复 |
| `mask` | 遮罩 | 510 | 45 | 遮罩的产生与布尔运算 |
| `video` | 视频 | 900 | 186 | 视频帧、合成、插帧、视频模型 |
| `audio` | 音频 | 288 | 53 | 音频的加载、处理、合成、识别 |
| `threed` | 三维 | 134 | 22 | 3D 网格、相机、贴图、渲染 |
| `text` | 文本与数据 | 600 | 50 | 字符串、JSON、列表、正则、模板 |
| `num` | 数值与逻辑 | 516 | 62 | 数学、比较、布尔、开关、计数 |
| `output` | 输出与预览 | 317 | 44 | 保存 / 预览 图像、视频、音频、模型 |
| `flow` | 流程与组织 | 79 | 25 | 便签、跳线、路由、静音旁路、参数控件 |
| `api` | 网络与云 | 703 | 23 | 云服务、大模型、下载上传 |
| `misc` | 未归类 | 214 | 9 | 规则没判出来，等你归位 |

大类之下还有小类（如 `image/放大 / 缩放`、`mask/分割 / 检测`、`video/插帧`），
共 100+ 个，`cwf nodes tree --cat 图像处理` 展开看。

### 判定依据（三档证据，可靠性递减）

1. **端口类型**（最硬）—— 输出 `VIDEO` 的就是视频节点，跟谁写的无关。
   这是唯一跨插件通用的语义信号。
   *例：`MiniMaxH3DualClockSamplerT8` 的原生分类写着 `T8/MiniMax H3/Audio`，
   但它输出 `sampler:SAMPLER, sigmas:SIGMAS` —— 端口证据压过分类路径，判成采样。*
2. **类型名**（很强）—— 按词切分后逐 token 匹配，不是拿 `in` 硬套子串。
   `WanPhantomSubjectToVideo` → `{wan, phantom, subject, to, video}`。
3. **原生分类路径**（兜底）—— 核心节点的英文功能路径，或插件分类的末段。

拿不准就进 `misc`，**不猜**。实测自动判定覆盖率 **97.2%**，
置信度分布 `high 3459 / med 3920 / low 158 / none 208`。

### 命令

```bash
cwf nodes tree                      # 16 类总览，每类带小类数和代表节点
cwf nodes tree --cat 视频            # 展开某一类
cwf nodes tree --cat image/放大      # 展开某个小类
cwf nodes list --cat 遮罩 --used-only   # 平铺列出你用过的遮罩节点
cwf nodes classify VAEDecode         # 它是怎么被判成这个类的（依据全摊开）
cwf nodes setcat 某节点 图像处理/放大   # 改一行，永久生效
cwf nodes atlas                      # 导出全量图鉴 → ~/.cwf/atlas/
```

`--cat` 中英文都收：`视频` / `video` / `图像处理/放大` / `image/放大`。

### 判错了怎么办

7746 个插件节点不可能条条都判对。改一行就永久生效（**不用重建缓存**）：

```bash
cwf nodes setcat MiniMaxH3PromptRelayPlanT8Advanced 条件与提示词/条件处理
cwf store mark KSampler -c 采样与生成/采样器        # 沉淀时顺手归类
```

写入 `~/.cwf/store/categories.txt`，纯文本，支持尾部通配：

```
MiniMaxH3* = 视频/视频工程套件      # 一条管一族
```

用户覆盖**永远优先于任何自动规则**。

---

## 全量图鉴导出

```bash
cwf nodes atlas                    # → ~/.cwf/atlas/  （17 个文件，约 830 KB）
cwf nodes atlas --used-only        # 只导你用过的 1014 种
```

产出结构：

```
index.md              总览 + 16 类目录 + 怎么看 + 怎么改
01_加载输入.md         每类一个文件，按小类分节
...
16_未归类.md
node_index.json       机读紧凑索引 {大类:{小类:{类型:[包,用量]}}}
```

每行一个节点：`- **VAEDecode** `` `comfy-core` `` ×324 samples:LATENT, vae:VAE → IMAGE`

这份是**全量快照**（7746/7746，一个不少），适合全文检索和离线翻阅；
要按需查询还是用 `cwf nodes tree`。

---

## 常用查询

```bash
cwf nodes stats                     # 总览：按功能 + 按原生分类 + 按插件包
cwf nodes show VAEDecode -v         # 说明书：功能分类、端口、控件、默认值、来源
cwf nodes list 解码                  # 搜节点（支持中文别名）
cwf nodes list 放大 --category 图像处理   # 按**原生**分类过滤（多数是插件名）
cwf nodes pkg                            # 列出贡献最多的包
cwf nodes pkg minimax-h3                 # 这个包装了哪些节点
cwf nodes unused                    # 装了但从没用过（清理插件的参考）
cwf nodes alias                     # 中文别名对照表
cwf nodes build                     # 装了新插件之后重建缓存（会重新拉 /object_info）
```

---

## 接线相关的查询（拿不准就别猜）

```bash
cwf schema show KSampler            # 这个节点要接什么、有哪些控件
cwf schema produce LATENT           # 谁能产出 LATENT
cwf schema accept MODEL             # 谁能接收 MODEL
cwf schema models checkpoints       # 本机有哪些 checkpoint 文件
cwf schema models loras             # 有哪些 lora
cwf schema categories               # 原生分类树
```

`models` 支持的类别：`checkpoints` `loras` `vae` `unet` `text_encoders` `clip`
`clip_vision` `controlnet` `style_models` `images`。

---

## 机器可读导出

不需要把节点库读进上下文，直接执行脚本拿 JSON：

```bash
python ~/.dsh/skills/comfyui-workflow/scripts/node_report.py            # 全量总览
python .../node_report.py --top 30                                      # 前 30 个包
python .../node_report.py --package kjnodes                             # 某包装了什么
python .../node_report.py --text                                        # 人读文本
```

分类结果也在节点库 JSON 里的每个节点上：`tax` / `tax_sub` / `tax_path_zh` /
`tax_rule`（命中哪条规则）/ `tax_conf`（置信度）。

---

## 中文别名表（节选）

| 你说 | 解析成 |
|---|---|
| 主模型 / 大模型 / 底模 | `CheckpointLoaderSimple` → `UNETLoader` |
| lora / 罗拉 | `LoraLoader` → `LoraLoaderModelOnly` |
| vae | `VAELoader` |
| 编码器 / 文本编码器 | `CLIPLoader` → `DualCLIPLoader` |
| 读图 / 载入图 | `LoadImage` |
| 读视频 / 载入视频 | `LoadVideo` → `VHS_LoadVideo` |
| 正向 / 负向 | `CLIPTextEncode` |
| 采样 / 采样器 | `KSampler` → `SamplerCustomAdvanced` |
| 潜空间 / 空潜空间 | `EmptyLatentImage` |
| 噪声 | `RandomNoise` |
| 调度器 | `BasicScheduler` |
| 引导 | `CFGGuider` → `BasicGuider` |
| 解码 / VAE解码 | `VAEDecode` → `VAEDecodeTiled` |
| 保存 / 存图 / 出图 | `SaveImage` |
| 保存视频 / 视频合成 | `SaveVideo` → `VHS_VideoCombine` |
| 放大 / 高清放大 | `ImageScaleBy` → `LatentUpscaleBy` → `ImageUpscaleWithModel` |
| 修脸 / 换脸 | `FaceDetailer` / `ReActorFaceSwap` |
| 跳线 | `SetNode` / `GetNode` |
| 开关 | `Any Switch (rgthree)` |

一个词对应多个候选时，**取本机真实存在的第一个**；都不存在才退回第一个候选。

**端口别名**（连线时可用）：`模型`→MODEL、`条件`→CONDITIONING、`潜空间`→LATENT、
`图像`→IMAGE、`遮罩`→MASK、`提示词`→text、`种子`→seed、`步数`→steps、
`宽`/`高`→width/height、`批次`→batch_size、`去噪`→denoise、`采样方法`→sampler_name。

完整表：`cwf nodes alias`
