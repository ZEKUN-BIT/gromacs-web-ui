# 依赖与 CI

应用的直接 Python 依赖只维护在根目录 `requirements.txt`；`pyproject.toml` 的包元数据读取同一文件。测试和开发附加依赖定义在 `pyproject.toml`。Linux 安装和 CI 使用完整版本与 SHA-256 哈希锁：

- `requirements/runtime.lock`：应用、传递依赖及安装本项目所需的构建工具。
- `requirements/dev.lock`：运行依赖加测试、代码检查及 Playwright；运行依赖版本受 runtime 锁约束。
- `packaging/windows/windows-python-requirements.lock`：Windows 安装器专用的 Ubuntu 24.04 / WSL2 / CPython 3.12 / x64 锁，另含 ACPYPE 和 Open Babel 等科学工具。保留既有生成和校验流程。

新锁使用 Python 与平台条件标记，保持项目 `Python >=3.11` 的兼容声明。CI 实际验证 Ubuntu x64 的 Python 3.11 和 3.12；其他版本和平台是否可安装还取决于上游是否提供兼容 wheel，不代表已经经过本项目验证。Python 3.11 会选用兼容的 NumPy、SciPy 和 contourpy 版本。安装时只接受 wheel，不在用户机器上临时编译科学依赖。

## 安装

```bash
./setup.sh          # 运行应用
./setup.sh --dev    # 开发和测试
```

脚本首先检查锁与声明是否同步，再用 `pip --require-hashes --only-binary=:all:` 安装，最后用已锁定的构建工具执行 `--no-deps --no-build-isolation -e .` 并运行 `pip check`。重复执行可以更新已有环境；切换回普通安装不会卸载已有开发工具。`pip install -e .` 仍是标准源码安装入口，但不提供锁定环境的可复现保证。

## 更新依赖

修改 `requirements.txt` 或 `pyproject.toml` 后，在独立工具环境中安装固定版本的 uv，然后生成两份锁；不需要更新应用的 `.venv`：

```bash
python3 -m venv /tmp/gromacs-lock-tools
/tmp/gromacs-lock-tools/bin/python -m pip install uv==0.9.10
PATH="/tmp/gromacs-lock-tools/bin:$PATH" python3 scripts/lock_dependencies.py --generate
python3 scripts/lock_dependencies.py --check
```

生成器只解析并写文件，不安装应用依赖。它优先保留现有锁中的版本，仅在新的约束需要时重新选择，并在两份锁都解析成功后写回。主动升级某个传递依赖时，使用 `--generate --upgrade-package 包名`，可重复指定包名。提交前审查版本变更、在独立虚拟环境安装两份锁，并运行测试。

修改应用直接依赖时也要用 Windows 的既有生成器更新 WSL 科学工具锁：在 Linux x64 / Python 3.12 上运行 `python packaging/windows/lock-python-dependencies.py --resolve`，并执行 `tests/test_windows_dependency_lock.py`。普通用户无需安装 uv。

## 真实 GROMACS 验证

普通 PR 修改后端 Python、工作流、分析脚本、测试/fixture、力场、教学模拟输入、依赖或 CI 配置时，会自动运行已有的小型真实模拟、checkpoint 恢复和轨迹分析。文档和纯前端改动无需运行真实模拟。触发范围由 `scripts/check_gromacs_changes.py` 定义；新增重要输入目录时也应更新这里。

每周定时、手动触发和标签触发继续运行真实模拟。真实模拟的 runner 固定为 Ubuntu 24.04，与 Windows 的 WSL 环境一致，避免 `ubuntu-latest` 升级期间的软件源变化。测试固定使用单线程 CPU，设置 15 分钟上限；失败时保留诊断文件 3 天。Python 测试矩阵每次检查锁的源摘要，验证运行锁的完整解析，并从开发锁实际安装后执行 `pip check`。
