# GROMACS Console 0.2.12

## 更新内容

- 工作台改为桌面布局（最小宽度 1200 像素），保留明暗主题；任务列表、运行状态、空状态和参数比较入口使用统一的组件样式，不再提供单独的手机布局。
- 任务详情可逐帧播放 XTC / TRR 轨迹、定位帧号、查看模拟时间；PDB / GRO 结构预览支持导出 PNG。运行日志新增搜索、匹配跳转和高亮。
- 改进任务复制与恢复、批量文件归档和断线重连。轨迹读取库升级为 MDAnalysis 2.11.0，支持读取 GROMACS 2026.4 的 TPR；Linux 与 Windows / WSL 的 Python 依赖锁同步更新。

## Windows 下载与升级

安装 `GromacsConsole-0.2.12-windows-x64-setup.exe`，或完整解压 `GromacsConsole-0.2.12-windows-x64.zip` 后运行 `Start.cmd`。两个文件均附 SHA-256。升级前等正在运行的模拟结束，再在原安装目录执行 **Configure or Update**；无需删除 WSL 发行版、设置或模拟结果。

**引擎版本与控制台版本不同。** Linux 可配置 GROMACS 2026.4。Windows 安装器首次配置时仍下载已固定校验值的 conda-forge GROMACS **2026.3**（CPU / CUDA），并不包含或自动安装 2026.4。首次配置需要联网。

`windows-signing.json` 记录安装器签名状态。完整 Windows / WSL 首装、重启、旧环境升级和 GPU 硬件验收与 CI 分开记录，见[发布清单](https://github.com/ZEKUN-BIT/gromacs-web-ui/blob/v0.2.12/docs/RELEASE-CHECKLIST.md)。
