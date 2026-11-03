# 发布与验收记录

本清单用于记录实际结果，未执行的项目保持“未验证”。CI、模拟安装测试不能替代 Windows 实机验收。

## 发布准备

- 记录源码提交、版本号、依赖锁和修改说明。
- 运行 pytest、Ruff、前端状态检查、三种尺寸浏览器检查与新产品流程检查。
- 显式运行真实 GROMACS 测试，包括短模拟、直接 TPR、checkpoint 恢复和教学示例。
- 执行 python scripts/sync_frontend_assets.py，确认 --check 通过，避免旧浏览器混用前后版本模块。
- 构建 Windows EXE 和 ZIP，验证新安装包含帮助文档与离线示例。
- 标签构建自动准备 GitHub Draft Release；审核产物和验收记录后再发布。README 链接到 Releases，不指向临时 CI artifact。

### 必须执行的 NSIS 编译回归

`tests/test_windows_nsis.py` 使用真实编译器生成安装器，验证 UTF-8 中文文本，并确认错误编码的产物被拒绝。Windows 负例使用 CP1252；Linux 使用可完整解码原始字节的 CP437，因为 Linux iconv 会提前拒绝源码中的未定义 CP1252 字节。

Windows 构建工作流会在 Linux 和 Windows 上安装 NSIS、执行测试并保存 JUnit。两端均设置 `GROMACS_REQUIRE_NSIS=1`，编译器不可用时失败，不能静默跳过。Windows 本地补跑时先将 NSIS 安装目录加入 PATH：

```powershell
$env:Path = "${env:ProgramFiles(x86)}\NSIS;" + $env:Path
$env:GROMACS_REQUIRE_NSIS = '1'
python -m pytest tests/test_windows_nsis.py -v --junitxml=reports/windows-nsis-tests.xml
```

Linux 安装发行版的 `nsis` 软件包后可运行 `GROMACS_REQUIRE_NSIS=1 python -m pytest tests/test_windows_nsis.py -v`。测试只编译并读取安装器，不运行安装或修改 WSL 环境。这两项编译回归与下面的实机验收矩阵分别记录。

## Windows 验收矩阵

| 系统 | 环境 | 硬件 | 操作 | 状态与证据 |
| --- | --- | --- | --- | --- |
| Windows 10 x64 | 全新 WSL2 | CPU | 安装、重启恢复、启动、教学示例 | 未验证 |
| Windows 11 x64 | 全新 WSL2 | NVIDIA | 安装、GPU 检测、短模拟 | 未验证 |
| Windows 11 x64 | 已有 Ubuntu 24.04 WSL2 | CPU | 复用环境、启动、示例 | 未验证 |
| Windows 11 x64 | 已有兼容 WSL2 | NVIDIA | 升级、旧任务读取、checkpoint 恢复 | 未验证 |
| Windows 10/11 x64 | 安装失败后恢复 | CPU | 网络中断、空间不足、重试、诊断 | 未验证 |

每条记录填写测试人、日期、操作系统版本、驱动、GROMACS 版本、安装包 SHA-256、预期/实际结果与日志位置。不能把“配置了测试流程”填写为通过。

### 采集与核验实机证据

在每台实际验收的 Windows 机器上，用 Python 3.11+ 从本版本源码运行以下命令。工具只读取 WSL 列表、GROMACS 版本、NVIDIA 名称/驱动及安装器哈希，不会安装、升级或启动模拟。`--source-commit` 填写构建安装包的完整提交 SHA；`--gmx` 可指定该发行版中的可执行文件路径。

```powershell
python scripts/windows_acceptance.py collect --installer C:\releases\setup.exe --version 0.2.11 --source-commit FULL_40_CHARACTER_COMMIT --case win10-fresh-cpu --tester TESTER --distro Ubuntu-24.04 --output evidence\win10-fresh-cpu.json
```

其余场景分别使用 `win11-fresh-nvidia`、`win11-existing-cpu`、`win11-upgrade-nvidia` 和 `win-recovery-cpu`。采集结果和所有步骤一律为 `unverified`；只读探测成功不能证明首次安装、重启、GPU 计算或恢复通过。当前 Linux 开发环境无法完成这五项 Windows 实机验收，矩阵继续保持“未验证”。

在专门的验收环境逐项执行报告 `steps` 中列出的操作，填写每步实际结果 `actual` 和状态，记录硬件、驱动、GROMACS 版本及安装/恢复耗时。将本次操作日志或截图放在报告目录中，把相对路径与 SHA-256 加入 `evidence`，并在步骤中引用这些路径；原始探测文件只用于环境信息，不能代替操作证据。低磁盘和断网测试使用可丢弃的测试环境，旧任务恢复使用专门的短测试任务。

五种场景实际通过后，在发行审核时运行：

```powershell
python scripts/windows_acceptance.py check --installer C:\releases\setup.exe --version 0.2.11 --source-commit FULL_40_CHARACTER_COMMIT --require-complete evidence\win10-fresh-cpu.json evidence\win11-fresh-nvidia.json evidence\win11-existing-cpu.json evidence\win11-upgrade-nvidia.json evidence\win-recovery-cpu.json
```

核验拒绝未完成的步骤、缺失/变动的证据、越界路径、重复场景，以及不匹配的版本、源码提交或安装器哈希。它验证记录完整性和维护者填写的结果，不能独立证明实机操作发生过。省略 `--require-complete` 可检查尚未完成的单条记录，不表示发布验收通过。报告及证据由维护者审核后随验收记录保存。

## 可选发行者签名

仓库支持通过 Windows runner 的 SignTool 签名 EXE。需要维护者提供代码签名证书和密码，在 GitHub Secrets 配置 WINDOWS_SIGNING_PFX_BASE64、WINDOWS_SIGNING_PASSWORD。没有证书时构建保留未签名状态，不能宣称已验证发布者身份。

签名脚本只处理本次构建的 EXE，使用 SHA-256 与 RFC3161 时间戳，验证签名后重新生成 EXE 校验值。证书通过临时 PFX 直接传给 SignTool，不导入持久证书存储；临时文件在 finally 中移除。时间戳失败会令该签名步骤失败。证书和密码只应提供给受信任的 Windows 构建 runner，拉取请求构建不会取得签名秘密。

构建产物包含 windows-signing.json，记录 signed/unsigned 与最终 EXE 哈希。仅标签和 pyproject.toml 版本一致时创建草稿；重复构建可更新已有草稿，工作流拒绝覆盖已发布的 Release。每次正式发行使用新版本和对应标签，不复用既有版本的下载文件。

下载后可在 PowerShell 中执行 `Get-FileHash .\GromacsConsole-<版本>-windows-x64-setup.exe -Algorithm SHA256`，核对同一 Release 的 `.sha256` 文件。校验和用于发现下载损坏；发行者身份以实际 Authenticode 签名为准。

证书未配置、签名未执行、Windows 实机未验收时，发行说明需分别标注。此清单不授权上传证书、发布 Release 或上传研究数据。
