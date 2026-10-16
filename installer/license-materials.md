# NearMate 免费发行构建输入

NearMate 0.2.0 与内置 StreamVox 2.0.0 免费使用。安装构建不需要产品证书、发行令牌、公钥、CA 或授权中心地址，也不向 Backend 或 StreamVox 注入信任材料。

构建只消费公开固定版本的 StreamVox wheel，不引用上游源码或内部发布物。公开 URL、文件大小和 SHA256 见 [Windows 安装构建](README.md)。`build_all.ps1` 自动下载缺失的 wheel 并验证；`uv.lock` 保留同一公开来源与摘要，源码 checkout 可直接取得依赖。

公开资产的固定版本、下载地址、摘要和本地缓存复用方法见 [公开运行资产](runtime-assets.md)。构建自动准备 PortableGit、固定语音模型清单和实时模型资产，保持 CUDA/Universal 隔离、自包含 Python、单一 ONNX Runtime、无 Torch、完整 runtime 清单和真实音频/RTC smoke。这些运行资产与校验独立于产品授权。
