# GROMACS Console

在浏览器里准备体系、运行 GROMACS、查看模拟结果。支持 Linux，也可以通过 Windows 安装器在 WSL2 中运行。

上传文件、设置参数后，可以先看完整命令，再提交任务。模拟在自己的电脑上运行，日志和结果按任务保存。

## 可以做什么

- 运行蛋白、蛋白–配体及多配体模拟，也支持能量最小化和已有 TPR。
- 管理任务队列，查看实时日志，取消任务或从 checkpoint 续跑，搜索历史结果。
- 处理周期边界、居中和拟合，计算 RMSD、RMSF、Rg、氢键、PCA 等指标，导出图表与原始数据。
- 对已有结果继续分析，查看构象变化、配体接触，比较不同体系或突变体。

## 安装与启动

### Windows

运行 `GromacsConsole-0.2.6-windows-x64-setup.exe`，安装后双击桌面的 **GROMACS Console**。

首次启动会配置或复用 Ubuntu 24.04 WSL2，并安装 GROMACS、Open Babel、ACPYPE 和 DSSP。兼容的 NVIDIA 显卡可使用 CUDA 加速；其他机器使用 CPU。支持 x64 Windows 10/11，PowerShell 7 可选。

首次配置需要联网，建议预留至少 8 GB 空间，模拟轨迹还会额外占用磁盘。安装、更新和常见报错见 [Windows 安装指南](docs/WINDOWS.md)。

### Linux

需要 Python 3.11+ 和可用的 GROMACS。进入项目目录后运行：

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

打开 <http://127.0.0.1:8000>。如果 `gmx` 不在 PATH 中，用 `GMX_BIN=/path/to/gmx ./run.sh` 指定位置。以后启动只需运行 `./run.sh`；配体参数化和 DSSP 需要另行准备对应工具，见 [环境配置](docs/USAGE.md#linux-启动配置)。

## 第一次使用

1. 在“新建任务”中选择流程，上传对应的结构、拓扑或轨迹文件。
2. 检查参数和命令预览。拿不准时，可以先用 **Dry run** 检查命令链。
3. 提交后在“任务详情”查看日志、质量检查和图表，下载需要的结果。

已有模拟结果可用“继续分析”创建新任务。默认会清理体积较大的中间轨迹；需要用于可视化时，请勾选“保留拟合轨迹”。

配体净电荷、质子化状态、力场和原子分组需要自己确认。质量报告能帮助发现问题，不能代替对体系和采样的判断。服务默认用于本机，请勿直接暴露到公网。

## 详细文档

- [使用与配置](docs/USAGE.md)：配体输入、MDP、环境变量和 API。
- [轨迹分析](docs/TRAJECTORY-ANALYSIS.md)：PBC、拟合、参考结构和时间窗口。
- [研究分析](docs/RESEARCH-ANALYSIS.md)：构象、配体接触和跨任务比较。
- [图表导出](docs/PLOTTING.md)：XVG 绘图与导出选项。
- [Windows 安装](docs/WINDOWS.md)：GPU 支持、空间占用、更新和排错。

## 开发

```bash
./setup.sh
.venv/bin/pytest
.venv/bin/ruff check app tests
```

Windows 安装包的构建方法见 [安装指南](docs/WINDOWS.md#开发者构建)。

## 许可

Copyright (c) 2026 yzk · [MIT License](LICENSE)。允许使用、修改和再分发，须保留版权及许可声明。第三方组件遵循各自许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。
