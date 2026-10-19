# GROMACS Console 0.2.8

0.2.8 修复 Windows 安装向导的中文乱码，并在打开浏览器前核对实际运行的应用版本。0.2.7 在 Windows 上编译时，NSIS 按系统默认代码页读取了 UTF-8 脚本，使“欢迎安装”等自定义文案在写入 EXE 时已经损坏。

## 修复内容

- 显式指定 NSIS 的 UTF-8 输入编码，使安装向导的中文标题、说明和完成页正确保存。
- 在构建后检查 EXE 内的中文文案，并在 Windows CI 中实际打开安装器、选择简体中文、读取欢迎页文字。
- 启动器同时核对版本、安装标识和运行实例，避免仅凭 HTTP 200 将旧服务或其他 WSL 实例当作新版本。发现地址对应错误服务时提示原因，不打开浏览器；闲置旧进程会安全重启，已有排队或运行任务时继续阻止重启。
- 包含引导/专业视图、协议模板、教学示例、项目标签、空间管理及结果摘要包等工作台功能；完整功能说明见 [0.2.7 发布说明](https://github.com/ZEKUN-BIT/gromacs-web-ui/releases/tag/v0.2.7)和[快速上手](QUICKSTART.md)。

## 下载与更新

下载 `GromacsConsole-0.2.8-windows-x64-setup.exe` 安装到原目录，或完整解压 `GromacsConsole-0.2.8-windows-x64.zip` 后运行 `Start.cmd`。安装器和 ZIP 均附 SHA-256 校验文件。

已有计算任务时，请等待任务结束，再执行 **Configure or Update**。已有 WSL 环境、设置与模拟结果保留。更新完成后从 **GROMACS Console** 快捷方式重新启动，启动窗口会显示实际运行的 `GROMACS Console 0.2.8`，再打开工作台。

0.2.7 的发布包已包含上述工作台功能。0.2.8 增加了真实临时服务的升级回归，确认更新后的页面、功能 API 和离线资源均可访问；仅检查安装目录中的文件不足以确认浏览器连接的是新服务。

## 验证边界

构建、自动化测试和向导中文验证结果以本 Release 的构建记录为准。发行者证书是否配置由 `windows-signing.json` 标明；未配置时安装器为 unsigned。

完整 Windows/WSL 首次配置、UAC/重启恢复和 GPU 硬件验收仍按[发布清单](RELEASE-CHECKLIST.md)分别记录，向导文字检查不能替代这些验收。
