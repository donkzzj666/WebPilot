# 测试说明

## 组件与边界测试：unit/

`test_foundation.py` 检查配置、SQLite 能力、存储归属、API 和 Worker；`test_network_audit.py` 检查网络审计分类不会漏掉真实外联。部分测试会启动独立 Worker，因此这里的 `unit/` 也包含轻量组件边界检查。

从根目录运行：

```sh
.venv/bin/python -m pytest
```

`pyproject.toml` 将默认收集范围限定为 `tests/unit/` 和 `tests/storage/`，避免混入历史评测夹具。

## 浏览器与多进程集成

入口位于 `scripts/verification/`，统一通过 `./scripts/check.sh` 运行。测试会使用真实 Chromium 和独立测试数据库，结果写入 `artifacts/verification/M1-02/`。完整命令见[脚本说明](../scripts/README.md)。

## M0 合成评测夹具：fixtures/m0/

`arithmetic.py` 是独立的合成计分例子，`acceptance_test.py` 是原有 GitHub Actions 验收入口，与产品后端无关。CI 已更新为 `python3 tests/fixtures/m0/acceptance_test.py`；夹具实现及断言保持原样，既有 M0 分支不受目录整理影响。

## 持久化测试：storage/

迁移空库与历史数据升级、失败回滚、结构漂移、外键、不可覆盖历史、同一业务键竞争、WAL 读写并发和进程中断。并发用独立进程与独立连接验证，不用内存锁模拟生产一致性。单独运行 `.venv/bin/python -m pytest tests/storage`。
