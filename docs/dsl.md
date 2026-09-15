# DSL：用几行文字造一张 ComfyUI 工作流

## 目录

- [最小可用例子](#最小可用例子)
- [节点行](#节点行)
- [连线行](#连线行)
- [虚拟跳线 `$名字`](#虚拟跳线名字)
- [广播 `*端口`](#广播端口)
- [指令 `@`](#指令-)
- [值的写法](#值的写法)
- [常见错误](#常见错误)
- [模板](#模板)

---

## 最小可用例子

```
@title 文生图

模型   CheckpointLoaderSimple  ckpt_name=anything-v5.safetensors  标题="1. 主模型"
正向   CLIPTextEncode          text="1girl, silver hair, cinematic"  标题="2. 正向"
潜图   EmptyLatentImage        width=832 height=1216 batch_size=1
采样   KSampler                positive=正向 negative=正向 seed=12345 steps=28
解码   VAEDecode
保存   SaveImage               filename_prefix=CWF/test

模型 -> 采样 <- 潜图            # 端口自动推断
模型.CLIP -> 正向 -> 采样.positive
采样 -> 解码 -> 保存
```

**内联连线是推荐写法**：节点和接线写在一起，端口名能省就省。
下面几节先讲这套新语法，再讲老的长写法（两者可以混用）。

```bash
cwf dsl-check demo.dsl                        # 先验语法
cwf build demo.dsl --name 我的文生图 --validate  # 建图 + 排版 + 校验
```

**建完会自动排版**，不需要手动摆位置。

---

## 内联连线（推荐）

### 三种写法

| 写法 | 例子 | 说明 |
|---|---|---|
| 两头都省 | `模型 -> 采样` | 端口由**类型能力**推断 |
| 只写一头 | `模型.MODEL -> 采样` 或 `模型 -> 采样.model` | 另一头按类型推断 |
| 定义行里接 | `采样 KSampler positive=正向` | 最紧凑，定义与接线一行 |

### 方向由端口能力决定，不由箭头决定

`->` 只表示「a 与 b 之间有一根线」。**真正谁喂给谁看端口能产出/能接收什么。**
所以这两种写法效果一样：

```
模型 -> 采样          # 同一件事
采样 <- 模型          # 同一件事
```

这也让反向箭头有意义：`模型 -> 采样 <- 潜图` 里，`<-` 表示「潜图 喂给 采样」，
即使箭头指向左边，工具也能接对（`模型` 根本没有输入，不可能反向喂）。

### `<-` 有括号语义（最容易写错的一处）

```
a -> b <- c -> d
```
等价于：`a→b`、`c→b`、`b→d`。
**`<-` 之后的 `->` 接的还是 b（主链节点），不是 c。** 已有专门的回归测试。

链式则一路顺下去：`a -> b -> c` 就是 `a→b`、`b→c`。

### 端口推断的规则与边界

推断只在**唯一确定**时才发生，否则报错：

```
✖ 第 10 行：`正向 -> 采样` 有多种接法，定不下来：
    CLIPTextEncode.CONDITIONING(CONDITIONING) → KSampler.positive(CONDITIONING)、
    CLIPTextEncode.CONDITIONING(CONDITIONING) → KSampler.negative(CONDITIONING)；
  请写明端口，例如 `正向.CONDITIONING -> 采样.positive`
```

**这是刻意的**：正向提示词既能接 positive 也能接 negative，随便挑一个就是整张图出错。
工具宁可停下来说清楚，也不瞎猜。

### 指定多输出节点用哪个口

多输出的节点（如 `CheckpointLoaderSimple` 有 MODEL/CLIP/VAE 三个），
内联连线时可以：

```
模型.MODEL -> 采样       # 写明输出名
模型 -> 采样.model       # 或者写明目标端口，反推输出
采样out=1 ...           # 定义行里用 out=下标 指定默认输出
```

---

## 节点行

```
短名   节点类型   控件=值 控件=值 ...   标题="显示名"
```

- **短名**：每行第一个词，必须唯一，后面连线用它。可以用中文。
- **节点类型**：ComfyUI 里的真实类型名。工具会用节点字典做最长匹配，
  所以 `Any Switch (rgthree)` 这种带空格括号的类型也没问题。
- **控件**：`key=value`，key 是节点字典里的控件名（如 `ckpt_name`、`steps`、`text`）。
  写错会立刻报错并列出可用控件名。
- **`标题="..."`**：可选，写到节点的 title 字段（画布上显示的名字）。
- **`bypass=true` / `mute=true` / `mode=4`**：可选，设置节点模式。

类型写错时不会静默失败：
```
✖ DSL 第 3 行出错：ComfyUI 里没有节点类型 'CLIPTextEnco'。
  （拼写？节点未安装？或者用 `cwf schema search CLIPTextEnco` 找找）
```

---

## 连线行

任何包含 `->`（或 `→`）的行都是连线。

| 写法 | 含义 |
|---|---|
| `a.MODEL -> b.model` | 按输出名 / 输入名 |
| `a[0] -> b[1]` | 按下标 |
| `a.CONDITIONING -> b` | 省略目标端口（目标只有一个能收这类型的输入时自动接） |
| `a -> b` | 两端都省略（唯一连接时） |
| `a.X -> b.Y -> c.Z` | 链式，一条行写多段 |
| `a.MODEL -> *model` | 广播（见下） |

**端口名可以用中文**：`a.模型 -> b.模型`（60+ 条映射，见 `cwf nodes alias`）。

**类型不匹配会警告但不拦**（因为有些节点用通配类型）：
```
⚠ 第 24 行：类型可能不匹配 —— VAEDecode#6.IMAGE(IMAGE) → LatentUpscaleBy#7.samples(LATENT)
```
这种基本就是你接错了，值得看一眼。

---

## 虚拟跳线 `$名字`

ComfyUI 里有一对「虚拟跳线」节点（KJNodes 的 `SetNode` / `GetNode`），
用来把一个值传给很远的地方，免得拉一条横穿全图的长线。

DSL 里写作 `$名字`，工具会**自动生成真正需要的 SetNode / GetNode 并配对**：

```
unet   UNETLoader   unet_name=xxx.safetensors
vae    VAELoader    vae_name=yyy.vae
clip   CLIPLoader   clip_name=zzz.safetensors

采样一  # 这里要用 model / clip / vae，但它们在图上很远
采样二  # 也要用同一份

unet.MODEL -> $model          # 存进名为 model 的跳线
clip.CLIP  -> $clip
vae.VAE    -> $video_vae

# …中间可以隔很多行…
$model -> 采样一.model         # 从跳线取出来
$clip  -> 采样一.clip
$video_vae -> 采样一.vae
$model -> 采样二.model         # 取多少次都行
```

不写 `$xxx -> ...` 的话，工具**不会**生成多余的 GetNode——按需生成。

---

## 广播 `*端口`

```
模型.MODEL -> *model      # 所有叫 model 的输入都接上
```

适合"一个模型要喂给好几个采样器"的场景。注意会跳过源节点自己。

---

## 指令 `@`

| 指令 | 作用 |
|---|---|
| `@title 我的工作流` | 写进 `extra.workflow_title`（画布标签页显示的名字） |
| `@meta 随便写点` | 写进 `extra.cwf_meta` 数组，纯记录 |

---

## 值的写法

| 写法 | 解析成 |
|---|---|
| `steps=28` | 整数 28 |
| `cfg=6.5` | 浮点 6.5 |
| `denoise=1.0` | 浮点 1.0 |
| `bypass=true` / `yes` / `on` | 布尔 True |
| `image=none` / `null` | None |
| `text="带 空格 和 # 号"` | 字符串（引号内的 `#` 是内容不是注释） |
| `text="第一行\n第二行"` | 字符串，`\n` 转义成真实换行 |
| `sampler_name=euler` | 字符串 euler |

**注释**：`#` 前面是空白（或行首）才算注释起点，所以 `模型#1` 这种引用不受影响。

---

## 常见错误

| 报错 | 原因 |
|---|---|
| `参数 'xxx' 不是 key=value 形式` | 节点类型写错了，后面的东西被当成了参数。报错里会告诉你按什么类型解析的 |
| `节点 X 没有输入 'y'` | 控件名拼错。报错会列出可用控件名 |
| `连接线至少要有头和尾` | 一行里只有一个端点 |
| `跳线 $x 没有对应的 -> $x 写入` | 只取了没存 |
| `节点短名 'x' 重复了` | 每行的第一个词必须唯一 |
| `引号没闭合` | 字符串引号配对错了 |

---

## 模板

```bash
cwf scaffold txt2img      # 文生图
cwf scaffold img2img      # 图生图
cwf scaffold h3_ref2va    # MiniMax H3 参考生视频骨架
cwf scaffold blank        # 空模板 + 语法注释
```

## 从已有工作流反推 DSL

想改造一张现有的图，先把它导成 DSL 看结构：

```bash
cwf export-dsl 某工作流 --out 反推.dsl
```

导出的 DSL 可以改完再 `cwf build` 回去——比手改 JSON 靠谱得多。
给 AI 读的话，DSL 比 JSON 省约 90% token。
