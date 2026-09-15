# 最小文生图流程 —— cwf 的 DSL 长这样：一行一个节点，端口靠类型自动推断
#
#   python cwf_run.py build examples/txt2img.dsl --name demo_txt2img --out examples/
#
# 端口名来自本机 /object_info。如果你装的节点版本不同，
# 用 `cwf nodes show 节点类型 -v` 看真实端口名。

@title cwf 示例 · 文生图

模型   CheckpointLoaderSimple  ckpt_name=改成你的模型.safetensors   标题="1. 主模型"
正向   CLIPTextEncode          text="a cat on a windowsill, soft morning light"   标题="2. 正向"
负向   CLIPTextEncode          text="blurry, lowres, watermark"                 标题="3. 负向"
潜图   EmptyLatentImage        width=1024 height=1024 batch_size=1             标题="4. 空潜空间"
采样   KSampler                seed=42 steps=24 cfg=7.0 sampler_name=euler scheduler=normal denoise=1.0   标题="5. 采样"
解码   VAEDecode                                                                标题="6. 解码"
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
