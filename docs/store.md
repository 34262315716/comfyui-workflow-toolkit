# 节点知识库（cwf store）

## 它解决什么问题

`/object_info` 里有 **7746 种节点**。每次要用某个节点都重查一遍端口是什么、
控件叫什么 —— 这是最烦的摩擦，也是"每次从零开始看"的根源。

`cwf store` 把「查过 / 摸清 / 常用」的节点沉淀下来，三样东西：

| 沉淀物 | 作用 |
|---|---|
| **别名 alias** | `采样器 = KSampler` —— 之后所有命令和 DSL 里都能直接用「采样器」 |
| **笔记 note** | 这个节点怎么接、有什么用、踩过什么坑 |
| **收藏 pin** | 常用节点的短名单，`cwf store list` 一眼看全 |

---

## 存哪、长什么样

全在 `~/.cwf/store/`，**纯文本**：

```
~/.cwf/store/
├── aliases.txt          一行一条：别名 = 节点类型
├── pins.txt             一行一个：节点类型
├── categories.txt       一行一条：节点类型 = 功能分类（覆盖自动判定）
├── notes/
│   ├── KSampler.md      一个节点一份笔记
│   └── VAEDecode.md
└── README.md            自动生成的总览（= store export 的内容）
```

`aliases.txt` 长这样，**你可以直接用记事本改，改完立刻生效**：

```
# 节点别名 —— 一行一条：别名 = 节点类型
采样器 = KSampler
主模型 = CheckpointLoaderSimple
出片 = SaveVideo
```

`categories.txt` 是你对**功能分类**的手动覆盖（详见 `references/nodes.md`）：

```
KSampler = 采样与生成/采样器
MiniMaxH3* = 视频/视频工程套件      # 支持尾部通配，一条管一族
```

同一个别名/类型重复记只会**就地覆盖**，不会堆出多行同名条目。

> 设计取向：**出问题你自己就能改，不用找工具作者。**
> 所以没有用二进制/数据库，就是能看懂、能手改的文本。

---

## 命令

### 沉淀

```bash
cwf store mark KSampler -a 采样器 -p -c 采样与生成/采样器 -n "model 接模型；latent_image 接潜空间"
```
| 参数 | 作用 |
|---|---|
| `-a, --alias` | 起个别名（以后所有命令和 DSL 里都能用） |
| `-c, --cat` | 顺手把**功能分类**改成你指定的（写进 `categories.txt`，立刻生效） |
| `-n, --note` | 记一笔（可多次调用追加） |
| `-p, --pin` | 加进常用列表（开关式：再执行一次就取消） |

`mark` 一次把这几件事都做了，比分开调省事。分类写错大类会当场报错，不会留垃圾。

**写错类型名会被拦下**，不会在知识库里留垃圾：
```
✖ 节点库里没有 'KSamplerr'，没法记。相近的有：KSampler、KSamplerAdvanced
```

### 检索

```bash
cwf store find 解码          # 中文词也行
cwf store find ksampler
cwf store find               # 不给词：把沉淀过的全列出来
```

**检索顺序是刻意的**：别名精确命中 → 类型精确命中 → 收藏/笔记 → 别名模糊 →
中文关键词展开（`解码` → `decode`）→ 全量节点字典。

所以**你沉淀过的永远排最前面**，`★` 标出来。这就是"不用每次从零看"的实现。

> 中文片段匹配做了长度加权：`VAEDecode`(9 字符) 会排在
> `MiniMaxH3AVDecodeSafetyT8Advanced`(31 字符) 前面 —— 越短越可能是主角。

### 速查卡

```bash
cwf store show 采样器        # 端口 + 控件 + 来源包 + 你用过的次数 + 你的笔记
cwf store show VAEDecode
```

输出长这样，一屏看完：
```
KSampler  「KSampler」
    必接输入: model:MODEL、positive:CONDITIONING、negative:CONDITIONING、latent_image:LATENT
    输出:     LATENT:LATENT
    控件:     seed、steps、cfg、sampler_name、scheduler、denoise
    来源包:   comfy-core　你用过的次数 255

  ── 你的笔记 ──
  采样主节点。model 接模型，positive/negative 接提示词，latent_image 接潜空间。
```

### 别名 / 收藏 / 总览

```bash
cwf store alias                          # 列出所有自定义别名
cwf store alias 出片 SaveVideo            # 起别名
cwf store pin SaveVideo                  # 收藏/取消收藏（开关）
cwf store list                           # 沉淀总览
cwf store export --out my-notes.md       # 导出（不给 --out 就写 README.md）
```

---

## 在 DSL 里直接用别名

沉淀过的节点，DSL 里可以直接当类型名写：

```
采样   采样器  positive=正向 negative=正向 seed=1 steps=28
```

工具解析类型名的顺序：**精确类型名 → store 自定义别名 → 内置中文别名**
（内置的那批：解码/主模型/放大/潜空间…，共 60+ 条，见 `cwf nodes alias`）。

store 排在前面，因为那是**你自己起的名字**，优先级更高。

---

## 和「模块 pack」「节点库 nodes」的区别

容易混，说清楚：

| | 是什么 | 什么时候用 |
|---|---|---|
| **`cwf store`** | 节点**知识**的沉淀（别名、笔记、常用） | 摸清一个节点后记下来，下次直接叫名字 |
| **`cwf nodes`** | 本机**装了哪些节点**的统计（7746 种、189 个包） | 想找"有没有能 X 的节点"、看某包装了什么 |
| **`cwf pack`** | 工作流**片段**的复用（可拼装的模块） | 一段采样链想在别的图里复用 |

一句话：`nodes` 是"有什么"，`store` 是"我记住的"，`pack` 是"我攒下的零件"。

---

## 导出与迁移

```bash
cwf store export                     # 写成 ~/.cwf/store/README.md
cwf store export --out D:/备份.md     # 或指定路径
```

导出的是完整 markdown（别名表 + 常用节点的端口 + 全部笔记），人也能读。
换机器时把 `~/.cwf/store/` 整个目录拷过去就行，或者用导出的 md 存档。

---

## 环境变量

| 变量 | 作用 |
|---|---|
| `CWF_STORE` | 覆盖知识库目录（默认 `~/.cwf/store`） |
