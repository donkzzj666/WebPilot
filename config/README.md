# 构建与版本记录

这里记录“验证时使用了什么”，不是用户填写业务设置的位置。

| 文件 | 含义 |
| --- | --- |
| `build-manifest.json` | 当前依赖、运行时、锁文件路径与哈希 |
| `browser-lock.json` | Playwright、Chromium 版本与发行物摘要 |
| `m1-01-validated-versions.txt` | M1-01 初次验证的版本记录 |
| `m1-01-source-manifest.json` | M1-01 初次交付的历史源文件快照，使用整理前的路径 |
| `m1-02-source-manifest.json` | M1-02 核心持久化验收时的源文件快照 |

检查当前构建：`.venv/bin/python scripts/dependencies/build_manifest.py --check`。

历史源文件快照不代表当前工作区，也不随重构改写；对应代码见 [710ba08](https://github.com/donkzzj666/WebPilot/tree/710ba08080f75371796d9230c9c871b7fff47f01)。目录变化见[导读](../docs/project-structure.md)。本地运行配置示例在根目录 `.env.example`。
