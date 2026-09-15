# cwf 命令全表

调用方式：`<仓库根>\tools\cwf.cmd <子命令> [参数]`
或 `bash ~/.dsh/skills/comfyui-workflow/scripts/cwf.sh <子命令> [参数]`

**通用参数**（几乎所有子命令都支持）：

| 参数 | 作用 |
|---|---|
| `--json` | 输出结构化 JSON 而不是人读文本 |
| `--server URL` | ComfyUI 地址（默认 `http://127.0.0.1:8188`） |
| `--offline` | 只用缓存的节点字典，不连服务 |
| `--refresh` | 强制重新拉取节点字典 |

---

## 一、读 / 查

### `cwf ws list` — 列工作流
```
--root DIR      工作流库根（默认 <ComfyUI>/user/default/workflows）
--dir SUB       只看某子目录，如 人像、放大、实验
--match S       名字包含 S
--limit N       最多 N 条
```
输出按修改时间倒序（最近动过的在前），带大小与相对路径。

### `cwf ws info <工作流>` — 概要
节点/连线/分区框/旁路数量、高频节点类型、终结点、孤儿节点（没接线的）。

### `cwf ws resolve <工作流>` — 环境体检
专门回答"换台机器还能不能跑"：节点类型在本机装没装、引用的模型文件在不在模型库里。

### `cwf find <关键词>` — 按名字模糊找
打分排序（全等 100 / 文件名命中 50 / 路径命中 10，越短越靠前，最近改过的加权）。

### `cwf grep <正则>` — 在**内容**里搜
搜节点类型、节点标题、控件值。例：`cwf grep "silver hair"` 找哪些工作流用了这个提示词。

### `cwf cat <工作流>` — 看内容
```
-d, --detail    输出每个节点的参数、端口、去向
--filter S      只看标题或类型包含 S 的节点
--limit N       最多显示 N 个（默认 60）
```

### `cwf outline <工作流>` — 按数据流阶段列结构
**给 agent 读最省 token 的命令。** 用最长路径把节点分成若干阶段，每阶段列出节点与关键控件。
比 `cat -d` 少一个数量级的输出量。

### `cwf deps <工作流> <节点>` — 上下游链
```
--depth N       递归深度（默认 6）
```
节点参数同样支持 `#id` / 类型名 / 标题 / **中文别名**（`解码`、`主模型`）。

### `cwf diff <A> <B>` — 比较两个工作流
列出：只在 A 的节点、只在 B 的节点、参数差异（逐控件）、连线差异。

### `cwf stats <工作流>` — 结构体检
```
--with-layout   顺便跑一次排版并给出质量指标
```

### `cwf validate <工作流>` — 全量校验
```
--strict        有错误时退出码 1（给脚本用）
```
查：节点类型不存在 / 模型文件不存在 / 连线指向不存在的槽位 / 类型不匹配 /
必要输入没接线 / 孤儿节点 / 没有终结点。

---

## 二、排版

### `cwf layout <工作流>` — 只排版
### `cwf beautify <工作流>` — 排版 + 补标题 + 重排序号

共用参数：
```
--out FILE          输出到新文件
--in-place          覆盖原文件（自动备份 .bak-<时间戳>）
--compact           紧凑一点
--loose             宽松一点
--no-groups         不重画分区框
--keep-notes        注释节点留在原地
--h-gap N           列间距（默认 90）
--v-gap N           同列节点间距（默认 45）
--group-pad N       分区框内边距（默认 34）
--margin N          画布外边距（默认 80）
--strays MODE       孤立节点放哪：auto | bottom | right | keep
```
`beautify` 额外：
```
--renumber          标题加 01. 02. 序号
--no-title          不给缺标题的节点补标题
```

**只改 `pos` / `size` / `groups`，绝不动执行语义**（全库 17173 条连线实测）。

---

## 三、改

所有改图命令共用：
```
--out FILE      输出新文件（默认不写文件，只打印）
--in-place      覆盖原文件（自动备份）
--layout        改完顺便重排一次
--compact / --loose / --no-groups / --keep-notes / --h-gap / --v-gap ...
```

### `cwf set <工作流> --set "节点:控件=值"`
可重复 `--set`。节点部分支持全部选择器，**匹配多个就全部改**：
```bash
cwf set 某流 --set "5. 一采:steps=40" --set "type:LoadImage:image=新图.png"
```
> 安全：**先预检再写**。只要有一个匹配到的节点没有那个控件，整批不动、不写文件。

### `cwf rename <工作流> --rename "节点=新标题"`
### `cwf mode <工作流> <节点> --mode bypass|mute|normal`
支持批量（`type:CLIPTextEncode`）。
### `cwf add <工作流> --type 类型 [--title T] [--arg k=v ...]`
### `cwf remove <工作流> <节点...> [--keep-wiring]`
`--keep-wiring` 把上下游接直（等于 splice）。
### `cwf insert <工作流> --type 类型 --on 上游 下游 [--title T] [--arg k=v]`
把新节点插进 A→B 这条已有连线上，自动按类型选输入/输出槽。
### `cwf splice <工作流> <节点>`
摘掉节点并把它的上下游接直。
### `cwf connect <工作流> --pair 源.输出 目标.输入`
### `cwf disconnect <工作流> --link 编号` 或 `--to 节点.输入`
### `cwf mute-group <工作流> <节点...> --mode bypass|mute`

---

## 四、造（DSL）

### `cwf scaffold [模板]` — 打印可直接改的 DSL 模板
模板：`txt2img`（默认）/ `img2img` / `h3_ref2va` / `blank`
### `cwf dsl-check [文件]` — 只验语法不建图（文件为 `-` 或省略时读 stdin）
### `cwf build <文件> --name 名字` — 建图 + 自动排版
```
--base 工作流      在已有工作流上继续拼
--validate        建完顺手校验
--print-only      只打印 JSON 不写文件
--no-layout       不排版
--out FILE        输出路径（默认 工作流库/cwf_new/<name>.json）
```
### `cwf export-dsl <工作流>` — 把已有工作流转成 DSL
**AI 读这个比读 JSON 省 90% token。** `--out FILE` 写文件。

---

## 五、模块化

### `cwf pack split <工作流> --nodes 选择器 --name 名字`
```
--dir DIR          存到模块库目录
--description S    说明
```
### `cwf pack list --dir DIR`
### `cwf pack show <名字> --dir DIR` — 看模块的入口/出口接口
### `cwf pack use <名字> --dir DIR [--workflow 目标流] --out 拼好的.json`
```
--as 别名前缀       默认用模块名
--prefix 标题前缀
--rename 入口=新名   可重复
--layout           拼完顺便排版
```

---

## 五之后 · 手动摆位置

### `cwf place <工作流> [规格…]` — 手动指定节点位置
自动排版交给工具，这个是**你说了算**。只改 `pos`，接线与控件值一律不动。
```
--col "T1 T2"      一列节点（列内从上到下），可重复，从左到右排开
--plan FILE        列计划文件，一行一列（# 注释，col 前缀可省）
--gap-h N          列间距，默认 90
--gap-v N          行间距，默认 45
--pack             把现有空隙全压到 --gap-h/--gap-v（只挪位置，不改顺序）
--fold N           每 N 列并成一列（压短总长度）；配合 --pack 用
--no-regroup       不重算分区框（默认会重算，且重算不挪节点）
--out / --in-place 落盘（不给就只打印，不写文件）
```
位置规格：
```
"#12=100,200"          绝对坐标
"标题子串=100,200"      选择器同其它命令
"type:KSampler+=0,-300" 相对位移（在当前坐标上挪）
```
选择器：`#id` / `type:` / `title:` / `short:`（DSL 短名）/ `~正则` / 中文别名。

---

## 六之前 · 离线渲染

### `cwf precheck <工作流>` — 硬件预检（不提交）
只过一遍"这台机器跑不跑得动"，不动队列：
```
⚠ 含 ControlNet 相关节点 2 个 —— 8 GB 显存 + 34 GB 内存容易被它爆掉
⚠ 含视频生成相关节点 72 个 —— 这台机器跑视频顶不住
⚠ #12 的潜空间是 1920×1080（2.1 MP），8 GB 显存下很可能放不下
```
`cwf run` / 提交前会自动跑这套检查，**命中红线直接拒绝**（退出码 2）：
```
⛔ ControlNet 很吃显存和内存，8 GB + 34 GB 这台容易爆
   涉及节点 1 个：[57]
触到本机硬件红线，拒绝提交。确认要跑就显式放行：
  cwf run 某流 --allow-controlnet
```
识别依据：节点类型名（`controlnet` / `wanvideo` / `savevideo` / `vhs_` …）
**加上端口类型**（`CONTROL_NET`、`VIDEO`、`WANVID*`）——只靠名字会漏掉
名字里看不出来的预处理节点。

---

### `cwf render <工作流> --out <文件>` — 把工作流画成图
不开浏览器、不开 ComfyUI 前端、不联网。扩展名决定格式（`.svg` / `.png`）。
```
--out PATH          必填；这工具默认不落盘
--format svg|png    显式指定格式（--out 没有扩展名时用）
--scale F           整体缩放，默认 1.0
--max-px N          长边上限；PNG 默认 4200，SVG 默认不限；0 = 不限
--pad F             四周留白，默认 48
--theme dark|light  配色主题
--color function|comfy|none   标题栏配色；function 按功能分类（默认）
--title S           图例标题，默认用文件名
--font F            字号缩放
--no-legend --no-grid --no-widgets --no-widget-names --no-notes
--open              渲染完用系统默认程序打开
```
节点自带的 `color`/`bgcolor` 永远优先于 `--color`。
PNG 需要 Pillow；缺了会提示改用 SVG 或安装方法，不会甩 traceback。
详见 `references/render.md`。

---

## 六、节点库 / 节点字典

### `cwf nodes stats` — 总览
节点类型总数、用过的类型数、插件包数量、扫描了多少工作流；**按功能分类（16 类）**、
按原生分类、按插件包三种分布，以及自动判定覆盖率。
```
--limit N      每节列多少条
--root DIR     工作流库根（用于统计使用频次）
--rebuild      强制重建缓存
```
### `cwf nodes tree [--cat X]` — 按功能浏览节点库
不给 `--cat` 时列 16 大类总览（种数 / 用过 / 小类数 / 代表小类）；
给了就展开这一类的小类与具体节点。
```
--cat S        如 视频 / video / 图像处理/放大（中英文都收）
--limit N      每个小类列几个节点，默认 8
```
### `cwf nodes list [关键词]` — 列节点
```
--cat S         按**功能分类**过滤（16 大类，可带小类）
--category S    按**原生分类**过滤（多数插件这里填的是插件名）
--limit N       默认 40
--used-only     只看用过的
```
支持中文别名（`解码` → VAEDecode）。
### `cwf nodes show <类型> [-v]` — 一个节点的说明书
功能分类（含判定规则）、原生分类、来源包、你用过的次数、必接输入、可选输入、输出、控件；
`-v` 连默认值与可选值一起列。
### `cwf nodes classify <类型>` — 这个节点为什么被判成这个类
把判定依据全摊开：名称词元、输入输出端口、原生分类、来源包，以及命中了第几条规则。
用来复核自动判定，或搞清楚"它为什么不在这儿"。
### `cwf nodes setcat <类型…> <分类>` — 手动改功能分类
```
cwf nodes setcat KSampler 图像处理/放大
cwf nodes setcat Anything Everywhere 流程与组织/通配广播   # 名字带空格不用加引号
cwf nodes setcat Fast Groups Bypasser (rgthree) --to 流程与组织/静音
```
节点类型名里带空格很常见（`Mask Fill Holes`、`LayerUtility: … V2`），
所以最后一个参数算分类、前面全算类型名；分类名自己带空格时用 `--to` 明确指定。
写入 `~/.cwf/store/categories.txt`（纯文本，支持尾部通配 `MiniMaxH3* = 视频/...`），
**立刻生效，不用重建缓存**；用户覆盖永远优先于自动规则。写错大类会报错。
### `cwf nodes atlas [--out DIR]` — 导出全量功能分类图鉴
```
--out DIR      默认 ~/.cwf/atlas
--used-only    只导你用过的
```
产出 `index.md` + 每类一个 markdown（按小类分节，每行一个节点含端口与用量）
+ `node_index.json` 机读紧凑索引。全量 7746 种一个不少。
### `cwf nodes pkg [包名]` — 按插件包看
不给包名则列出贡献最多的包（包名, 类型数, 使用次数）。
### `cwf nodes unused` — 装了但从没用过的类型（清理插件参考）
### `cwf nodes alias` — 中文别名对照表
### `cwf nodes build` — 重建节点库缓存
会重新拉 `/object_info`（ComfyUI 需在线），并重跑功能分类。

### `cwf schema search <词>` / `show <类型>` / `produce <类型>` / `accept <类型>` / `models <类别>` / `categories` / `refresh` / `stats`
更底层的节点字典查询。
`models` 的类别：`checkpoints` `loras` `vae` `unet` `text_encoders` `controlnet` `clip_vision` `style_models` `images`。

---

## 七、运行 / 转换

### `cwf run <工作流> [--wait] [--force] [--timeout N]`
提交到 ComfyUI 执行。`--wait` 等它跑完并列出产物。提交前会先校验，不过就拒绝（`--force` 跳过）。
> ⚠ 会占用显卡。这条链路尚未在本机实测。

### `cwf queue` — 看队列
### `cwf export-api <工作流> --out api.json` — 导出 `/prompt` 用的 API 格式
### `cwf import-api <api.json> --out x.json` — API 格式转回可编辑的 UI 工作流（并自动排版）

---

## 八、节点引用写法（改/删/连命令通用）

| 写法 | 含义 | 能否批量 |
|---|---|---|
| `#12` 或 `12` | 按节点 id | 否 |
| `type:KSampler` | 按类型（精确） | **是** |
| `title:采样` | 按标题子串 | **是** |
| `~^05\.` | 按标题正则 | **是** |
| `05. 我的主采样器` | 标题全等 → 去序号后比 → 子串 | 歧义则报错 |
| `解码` / `主模型` / `放大` | **中文别名** | 视情况 |

`--set` 的节点选择器吃最左边那段，所以带冒号的选择器也不会切错：
`--set "type:CLIPTextEncode:text=xxx"` 解析为「所有 CLIPTextEncode 的 text 改成 xxx」。

---

## 九、退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | `--strict` 校验发现错误 |
| 2 | 用户级错误（找不到文件/节点、参数写错、服务不可用） |
| 130 | 被中断 |
