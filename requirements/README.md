# Python 依赖

| 文件 | 用途 |
| --- | --- |
| `requirements.in` | 产品运行需要的直接依赖 |
| `requirements.lock` | 运行依赖及传递依赖的精确版本和 SHA-256 |
| `requirements-dev.in` | 包含运行依赖，再加入测试和锁定工具 |
| `requirements-dev.lock` | 本地开发、测试、验证使用的完整哈希锁 |

初学者直接在项目根目录执行 `./scripts/bootstrap.sh`；它会安装 `requirements/requirements-dev.lock`。不要手工改锁文件中的版本或哈希。

本次仅移动这四个文件，文件内容、依赖版本和哈希未变。锁头注释保留生成时的原始命令与旧路径，作为历史记录。未来维护依赖时，从根目录使用 `requirements/` 下的输入和输出路径，完成验证后更新 `config/` 中的构建记录；不要直接照抄历史注释里的临时约束路径。

前端依赖仍由 `frontend/package.json` 和 `frontend/package-lock.json` 管理。
