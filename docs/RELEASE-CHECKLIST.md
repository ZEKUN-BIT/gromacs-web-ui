# 发布与验收记录

本清单用于记录实际结果，未执行的项目保持“未验证”。CI、模拟安装测试不能替代 Windows 实机验收。

## 发布准备

- 记录源码提交、版本号、依赖锁和修改说明。
- 运行 pytest、Ruff、前端状态检查、三种尺寸浏览器检查与新产品流程检查。
- 显式运行真实 GROMACS 测试，包括短模拟、直接 TPR、checkpoint 恢复和教学示例。
- 执行 python scripts/sync_frontend_assets.py，确认 --check 通过，避免旧浏览器混用前后版本模块。
- 构建 Windows EXE 和 ZIP，验证新安装包含帮助文档与离线示例。
- 标签构建自动准备 GitHub Draft Release；审核产物和验收记录后再发布。README 链接到 Releases，不指向临时 CI artifact。

## Windows 验收矩阵

| 系统 | 环境 | 硬件 | 操作 | 状态与证据 |
| --- | --- | --- | --- | --- |
| Windows 10 x64 | 全新 WSL2 | CPU | 安装、重启恢复、启动、教学示例 | 未验证 |
| Windows 11 x64 | 全新 WSL2 | NVIDIA | 安装、GPU 检测、短模拟 | 未验证 |
| Windows 11 x64 | 已有 Ubuntu 24.04 WSL2 | CPU | 复用环境、启动、示例 | 未验证 |
| Windows 11 x64 | 已有兼容 WSL2 | NVIDIA | 升级、旧任务读取、checkpoint 恢复 | 未验证 |
| Windows 10/11 x64 | 安装失败后恢复 | CPU | 网络中断、空间不足、重试、诊断 | 未验证 |

每条记录填写测试人、日期、操作系统版本、驱动、GROMACS 版本、安装包 SHA-256、预期/实际结果与日志位置。不能把“配置了测试流程”填写为通过。

## 可选发行者签名

仓库支持通过 Windows runner 的 SignTool 签名 EXE。需要维护者提供代码签名证书和密码，在 GitHub Secrets 配置 WINDOWS_SIGNING_PFX_BASE64、WINDOWS_SIGNING_PASSWORD。没有证书时构建保留未签名状态，不能宣称已验证发布者身份。

签名脚本只处理本次构建的 EXE，使用 SHA-256 与 RFC3161 时间戳，验证签名后重新生成 EXE 校验值。证书通过临时 PFX 直接传给 SignTool，不导入持久证书存储；临时文件在 finally 中移除。时间戳失败会令该签名步骤失败。证书和密码只应提供给受信任的 Windows 构建 runner，拉取请求构建不会取得签名秘密。

构建产物包含 windows-signing.json，记录 signed/unsigned 与最终 EXE 哈希。仅标签和 pyproject.toml 版本一致时创建草稿；重复构建可更新已有草稿，工作流拒绝覆盖已发布的 Release。每次正式发行使用新版本和对应标签，不复用既有版本的下载文件。

下载后可在 PowerShell 中执行 `Get-FileHash .\GromacsConsole-<版本>-windows-x64-setup.exe -Algorithm SHA256`，核对同一 Release 的 `.sha256` 文件。校验和用于发现下载损坏；发行者身份以实际 Authenticode 签名为准。

证书未配置、签名未执行、Windows 实机未验收时，发行说明需分别标注。此清单不授权上传证书、发布 Release 或上传研究数据。
