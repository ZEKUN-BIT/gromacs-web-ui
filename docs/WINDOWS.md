# Windows 安装与空间控制

面向 Intel/AMD x64 的 Windows 11，或支持 WSL2 的 Windows 10（至少 build 19041，建议 22H2）。需要启用硬件虚拟化。首次安装需要联网；启用 WSL2 时 Windows 可能请求管理员权限和重启。ARM64 暂不支持。

## 给普通用户

1. 运行 `GromacsConsole-0.2.6-windows-x64-setup.exe`，按向导安装。
2. 双击桌面的 **GROMACS Console**。启动器首先检查发行版的 WSL1/2 类型、Ubuntu 版本与 CPU 架构。已有 x64 Ubuntu 24.04 WSL2 时，显示复用提示并跳过 WSL 和 Ubuntu 镜像下载；缺少兼容环境时才下载、校验官方 Ubuntu 镜像并直接导入 WSL2。需要重启时，重启后再次双击。
3. 首次运行自动安装 Python 依赖、Open Babel、ACPYPE、配体参数化所需的 AmberTools 和 DSSP，并检测 GPU。兼容 NVIDIA GPU 使用预编译 CUDA 版 GROMACS 2026.3；其他机器复用或下载同版本预编译 CPU 版，无需源码编译。窗口实时显示系统依赖、计算环境、科学工具和程序更新四个阶段；期间不要关闭窗口。以后日常启动不重复安装。
4. 页面打开后即可使用。关闭网页和启动窗口不会停止正在进行的模拟。完成或取消所有任务后，通过开始菜单的 **Stop Service** 停止后台服务。

也可以解压 `GromacsConsole-0.2.6-windows-x64.zip` 到固定目录，双击 `Start.cmd`。不要只拿出其中一个脚本运行。安装包与 ZIP 使用相同的安装逻辑。

桌面、开始菜单与 `Start.cmd` 共用 PowerShell 入口：优先使用电脑上已经安装的 **x64 PowerShell 7**，找不到时使用 Windows 自带的 **Windows PowerShell 5.1**。WSL 基础组件的管理员权限步骤也使用同一选择规则。安装器不下载或捆绑 PowerShell，不要求用户先安装 PowerShell 7；诊断日志记录实际使用的版本与位数。

0.2.4 的安装向导支持 Windows 高 DPI 缩放，使用 Segoe UI / 微软雅黑 UI 字体，并换成与工作台一致的深绿、浅绿分子图形。欢迎页和页头使用四倍尺寸的插图，文字由原生控件绘制。桌面、开始菜单、安装器、卸载器和 Windows 软件列表采用专用图标，内含 16–256 像素的十种尺寸；网页标签图标也使用相同图形。安装到原目录即可更新已有快捷方式。便携 ZIP 内附 `GromacsConsole.ico`，可在自行创建的快捷方式属性中选择它。

安装器无需事先知道显卡型号：从 WSL 内查询实际 CUDA 驱动、设备和计算能力。自动 GPU 路线支持 **NVIDIA Pascal（计算能力 6.0）及更新 GPU，且驱动 CUDA API 至少 12.9**。先在 Windows 安装兼容的 NVIDIA 驱动；不要在 WSL 内安装普通 Linux NVIDIA 显卡驱动。GPU 运行库下载后须通过真实的短 GPU 模拟，才完成替换；检测到兼容 GPU 但安装或验证失败会报错并保留旧计算环境。

**AMD、Intel GPU，以及驱动不兼容或 WSL 内不可见的 NVIDIA GPU，会明确采用 CPU 环境。**这不是所有显卡共用的 GPU 二进制；SYCL/ROCm 等其他后端仍需单独配置。安装日志记录检测结果和原因，Linux 应用目录的 `gpu-status.json` 保存本次安装结果。页面“应用推荐”只有在当前 GROMACS CUDA 构建和驱动设备均可用时才勾选 GPU；安装完成后使用此按钮启用新任务的 GPU 非键计算，已有任务参数不修改。

科学工具使用固定版本的官方 wheel：ACPYPE 2026.9.4 自带配体所需的 AmberTools 二进制和 GAFF/GAFF2 数据，Open Babel 由 openbabel-wheel 提供，不额外安装完整 Conda 或整套 Amber。Ubuntu 安装 `liblapack3`、`libblas3` 和 `libgfortran5`，满足 AmberTools 未随 wheel 打包的本地运行库依赖。安装结束先检查 SQM 能否启动，再实际生成乙醇的 BCC/GAFF2 参数并检查拓扑、电荷和键；DSSP 通过 Ubuntu 软件源安装。Web 与计算 worker 共享工具路径，不需要手工激活环境。

## 安装速度

0.2.5 实现了进一步优化：

- 普通启动把系统、架构、安装标记和磁盘探测合并，正常已有环境的路径由约七次 WSL 调用减为三次。具体耗时仍需 Windows 实机测量。
- 应用和计算环境分别计算更新指纹。仅程序变化时，本地验证现有 Python、科学工具和 GROMACS 能否启动，跳过包配置；程序文件暂存完成后才停止服务并提交更新。需要环境修复时自动转完整配置，手动 **Configure or Update** 始终执行完整检查。
- Ubuntu 24.04 / Python 3.12 / x64 使用统一的 53 包版本及 SHA-256 锁；只进行一轮完整依赖解析和安装。依赖标记丢失也会检查真实导入；确认包文件损坏时进行一次受控修复，超时及原生库错误不会触发全面重装。
- GPU 某个包下载失败时停止本次安装的其他下载，等待进程退出再清理。整次 GPU 下载共享默认 3600 秒预算；重试沿用剩余预算，进度显示剩余时间。GROMACS `solvate`、`grompp` 各限时 60 秒，计算验证仍保留真实 GPU PME/FFT。
- pip 安装阶段默认限时 1800 秒；Linux 安装脚本默认总时限 7200 秒，短 WSL 探测默认 60 秒，Windows 本地 HTTP 连通性探测总时限 60 秒并显式绕过代理。超时显示具体阶段，不关闭整个 WSL。

较慢网络可以在启动前设置 Windows 环境变量 `GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS`（1–86400 秒），启动器校验后会明确传入 Linux；也可在 Linux 侧设置该变量，或使用 `gpu_setup.py --download-timeout 秒数` 调整计算包下载预算。Windows 使用 `GROMACS_WSL_SETUP_TIMEOUT` 调整 Linux 脚本总时限、`GROMACS_WSL_PROBE_TIMEOUT` 调整探测时限（均为 1–86400 秒）。完整安装仍受各阶段及脚本总时限约束。

0.2.1 减少首次安装和后续更新的等待：

- CPU 路线改用经过真实短模拟验证的预编译包，下载约 **35.2 MB**，省去源码下载与编译。
- GPU 的四个独立运行库最多同时下载四个；全部校验通过后才解压安装。首次 GPU 下载总量仍约 375 MiB，并发效果取决于网络。
- Ubuntu 系统包逐个检查，仅有缺包时运行一次 `apt update` 并安装缺失项；已有完整环境可离线跳过这一阶段。
- 成功验证后保存依赖记录。requirements、验证脚本、Python 与已安装包版本均未变时，执行离线依赖、导入和工具检查，跳过三轮 pip 安装与乙醇试算。环境损坏或依赖变更时重新安装验证。旧版尚无依赖记录，首次升级仍需完成一次验证。
- Linux 安装输出实时显示并写入诊断日志，四个阶段分别标明进度，完成时显示总耗时。

首次安装仍可能需要下载 Ubuntu 镜像、系统包和 Python 科学计算依赖；网络较慢时，这些下载仍会占用较多时间。没有预设完整安装耗时，尚未在用户的 Windows 电脑测量。

0.2.2 补上每个计算运行库的下载量、速度与耗时；已知大小的包显示百分比。cuFFT 单个压缩包约 **191.6 MiB**，其他三个包显示 `Verified` 后仍可能需要等待它下载完成。新版对持续低速或无数据的连接进行超时重试，并明确显示重试原因；同次安装中的临时下载可续传，最终完整 SHA-256 校验通过才会解压。安装结束仍清除临时包，不保存一份永久的大文件缓存。

0.2.4 在验证脚本变化或旧版没有可用依赖记录时，先通过离线 pip 检查现有包是否满足当前要求；满足的安装阶段跳过联网，只安装缺失或不符合版本要求的阶段。验证逻辑变化仍执行完整乙醇试算。系统库、DSSP 或 AmberTools 原生工具错误不会触发全量 Python 包重新下载，并保留具体诊断；真正的 Python 环境损坏仍走修复流程。

## 空间占用

- Windows 安装包只包含程序、网页和 MDP 模板；不包含 Linux 系统镜像、Electron、Conda、现有模拟结果或开发依赖。
- 已有兼容的 Ubuntu 24.04 WSL2 会复用；应用使用独立的 `gromacs-console` 用户，不修改默认用户。
- 没有兼容发行版时，从 Canonical 官方下载约 370 MiB 的 Ubuntu 24.04.5 WSL rootfs；固定 SHA-256 校验后导入，临时下载随即删除。不会捆绑到 Windows 安装包内，也不会保留第二份 rootfs 备份。已验证的压缩镜像解压内容约 1.28 GB，实际 VHD 和后续依赖占用另计。
- Python 使用系统解释器与独立虚拟环境；升级为固定安全版本的 pip，安装应用及科学工具依赖，禁用 pip 下载缓存并要求预编译 wheel。新增科学工具依赖与 pip 自身使用固定 SHA-256。
- 配体工具新增约 190 MB 的解压文件，DSSP 及其系统依赖另计。该数字来自 Linux wheel 实测，实际 WSL 磁盘占用会有差异。
- GPU 路线只安装预编译 GROMACS 的 SSE2 版本和必要 CUDA 运行库，避免重复 AVX 构建，不安装完整 CUDA SDK、Linux NVIDIA 驱动或外部 MPI。GROMACS 与 GPU 运行库实测合计约 **701 MiB**，下载约 **375 MiB**；首次下载需要临时空间，下载归档安装后清除。已有匹配的 GPU 环境重新验证后跳过下载。
- 0.2.4 在所有 GPU 下载校验成功后，每完成一个包的解压就立即释放该临时归档；按当前固定包大小，计算运行库安装阶段的峰值文件占用减少约 **289 MiB**。最终 GPU 环境大小和首次下载总量仍如上；这是文件大小推算，不等于 Windows VHD 立即缩小。
- CPU 路线验证并复用已有环境，或安装 SSE2 预编译包；新 CPU 运行目录实测约 **38.5 MiB**，临时归档在安装结束或失败时清除。
- 新版不安装编译工具，不执行 `apt autoremove`；早期版本安装的编译工具可能仍留在现有 Ubuntu 内。
- 0.2.5 直接流式解压 conda 包，省去额外的 `.tar.zst` 临时文件，减少约 33.6 MiB（CPU）或 59.9 MiB（GPU）的临时写入；此数值不代表整体峰值或 Windows VHD 会同比缩小。
- 下载前检查 Windows TEMP、注册发行版的实际 VHD 存储卷与 Linux 文件系统。程序更新预留 256 MiB；已有环境完整修复预留 CPU 2 GiB / GPU 候选 3 GiB，首次配置预留 CPU 3 GiB / GPU 候选 4 GiB。估算包含新文件和临时峰值，不重复复制模拟轨迹；最终 GPU 路线仍由 CUDA 探测决定。
- Web 与 worker 使用独立服务日志，运行中按约 10 MiB 轮转，各保留一份备份；普通 HTTP 访问不再逐条记录。任务科学日志采用原有规则。

**安装包大小不等于安装后占用。**完整 WSL2 Ubuntu 加 Python 科学计算环境通常需要数 GB；建议首次安装预留至少 **8 GB**，包括下载、解压及更新时保留旧环境的临时空间。实际占用取决于已有环境和系统版本，尚未在 Windows 实机测量。轨迹文件可能远大于程序本身；界面中的输出间隔和中间轨迹保留设置决定后续增长。

结果目录默认是：

```text
\\wsl.localhost\GromacsConsole-Ubuntu-24.04\home\gromacs-console\.local\share\gromacs-console\runtime
```

开始菜单 **Open Results** 会打开实际选中的发行版目录。模拟在 WSL 的 Linux 文件系统内运行，不直接在 Windows 挂载盘上运行，减少大量小文件读写开销。

删除任务请使用页面的“删除”。WSL 虚拟磁盘可能不会立即把删除文件释放的空间返还给 Windows；可按微软 WSL 官方说明对所用发行版的虚拟磁盘进行空间回收。不要为了回收空间执行 `wsl --unregister`，它会删除该发行版的全部数据，也可能影响其他软件。

## 更新与卸载

将 0.2.6 安装器安装到原 Windows 目录（或把新版 ZIP 全部文件覆盖到原目录），再运行 **Configure or Update**。启动器复用已经登记的兼容 WSL2 Ubuntu；匹配且验证通过的 GROMACS/GPU 运行库跳过下载，设置和模拟结果保留。日常启动也会比较应用内容校验值并自动更新。

更新、停止与移除均先检查活动任务：准备中、排队、运行中，以及中断后等待执行、续跑、接管或取消的任务都会阻止操作。即使任务已标记失败、取消或完成，只要登记的任务进程仍存活，也会继续保护计算环境。检查兼容旧版单进程记录与并行进程列表，核对 PID、启动时钟、进程组、命令指纹和任务工作目录，只识别本应用目录内身份匹配的进程。请先在页面完成或取消任务，并等待进程实际停止。

0.2.5 的应用文件替换具有事务记录。任一文件替换失败时恢复旧应用；进程突然退出时保留备份，下次启动发现未完成记录后执行恢复。只有新程序和版本标记全部提交成功才删除旧应用备份；不回滚或复制用户结果。环境包修复与应用文件事务分别处理，Python/GROMACS 升级不承诺整个环境快照回滚。活动任务与存活进程检查使用安装载荷中的辅助程序和系统 Python 标准库；即使已安装的应用源码或虚拟环境缺失，更新前的保护仍能执行。WSL 设置也按变更原子保存，损坏时尝试恢复上次配置。

本版本还补齐两份网页字体及各自 OFL 许可。自有代码署名 `Copyright (c) 2026 yzk`，采用 MIT；安装目录的 `LICENSE` 和 `THIRD_PARTY_NOTICES.md` 分别说明项目许可及第三方组件。

从 0.1.x 升级时，直接覆盖安装到原 Windows 目录即可。新版会补齐科学工具，并将兼容 NVIDIA 机器上的 CPU GROMACS 自动升级为 CUDA 版；使用相同的 `gmx` 路径，已有设置和模拟结果保留。更换显卡或 Windows 驱动后，可以再次运行 **Configure or Update** 重新检测。

开始菜单 **Remove Computation Environment** 在输入 `REMOVE` 后删除本应用的 Python 虚拟环境、GROMACS 和程序代码，保留 `runtime/` 与 `settings.json`。如果只卸载 Windows 程序，会保留整个 WSL 计算环境与结果。不会卸载共享 Ubuntu、删除其他 Linux 用户或注销 WSL 发行版。

如果安装失败，重新运行快捷方式即可重试。网络中断可能需要切换可访问官方 GROMACS、Ubuntu 软件源和 PyPI 的网络。检查安装窗口输出；启动错误日志在 Linux 应用目录的 `service.log`。

### WSL 安装失败的诊断

若旧安装器在磁盘空间检查阶段报 `Unable to find type [ulong]` 或“找不到类型 [ulong]”，原因是 Windows PowerShell 5.1 没有该类型别名。它发生在 Windows 启动脚本内，已有 WSL 和 GPU 安装仍可复用。0.2.6 将三个磁盘 API 输出变量改为完整类型 `[System.UInt64]`，同时兼容 5.1 和 7。安装到原目录后运行 **Configure or Update** 即可重试，无需重新下载 Ubuntu 或删除计算环境。

0.1.0 把提权 WSL 安装命令的所有非零返回值都显示成“需要重启”，而且没有保留安装输出；这个提示本身不能证明电脑需要重启。0.1.1 将管理员权限限定在 WSL 基础组件安装，Ubuntu 在原 Windows 用户下注册，保留原始错误、十六进制 HRESULT 和日志。只有明确的重启返回码或 Windows 可选功能处于待重启状态时才要求重启。支持 `--web-download` 时，使用官方直接下载，绕过 Microsoft Store 下载路径。

安装窗口最后会显示诊断日志路径，默认：

```text
%LOCALAPPDATA%\GromacsConsole\Logs\wsl-setup.log
```

开始菜单 **Diagnose WSL** 会显示 WSL 版本、状态与发行版列表，保存到同一日志，不安装、转换或删除发行版。便携版可从命令提示符运行 `Start.cmd Diagnose`。提权阶段的日志另保存在 `%ProgramData%\GromacsConsole\SetupLogs\`，同时合并到当前用户的日志；日志可能包含 Windows 用户名等系统信息。

| 错误 | 对应处理 |
| --- | --- |
| `0x80072EE7` | 检查 DNS 和网络，下载服务器名称无法解析。 |
| `0x80072EFD` / `0x80072EFE` | 检查网络、代理或下载连接；重启不一定能解决。 |
| `0x80370102` / `0x80370114` | 检查 BIOS/UEFI 硬件虚拟化及 Windows Virtual Machine Platform。 |
| `0x800701BC` | 执行 `wsl --update` 更新内核。 |
| `0x80070005` | 检查管理员权限、企业设备策略或安全软件限制。 |
| `0x800704C7` | 管理员权限提示被取消，重新运行并完成权限提示。 |
| `3010` / `1641` | Windows 明确要求重启，重启后再次启动。 |

0.1.1 的 `--web-download` 仍需要访问 GitHub 的 `DistributionInfo.json`。若出现 `Wsl/InstallDistro/WININET_E_TIMEOUT` 和该 URL，就是发行版清单下载超时。0.1.2 在确认缺少兼容发行版后直接下载并校验官方 rootfs，再用 `wsl --import --version 2` 注册为 `GromacsConsole-Ubuntu-24.04`，跳过 GitHub 清单。日志从原始输出字节区分 UTF-16LE 和 UTF-8，避免中文 WSL 输出乱码导致识别错误。

WSL 程序版本与 Linux 发行版版本不同：WSL2 已安装，并不代表里面已经存在本安装器支持的 Ubuntu 24.04。已有兼容发行版时绝不调用 Ubuntu 下载/导入；已有其他 Linux 时保留它们，补装兼容发行版。新导入的 VHD 放在 `%LOCALAPPDATA%\GromacsConsole\WSL\Ubuntu-24.04`，不会覆盖已注册发行版或非空存储目录。

如果新版仍失败，请根据原始 WSL 错误文字与退出码诊断。升级安装器不会删除已有 Ubuntu 或结果；不要使用 `wsl --unregister` 排错。

若 0.1.2 出现 `/bin/bash: --install: command not found`、退出码 `127`，这是启动器给 WSL 选项添加引号导致的参数解析错误，也会让已有环境的检测失败。0.1.3 让普通选项直接传递，仅对含空格等内容的参数进行转义，并在日志中记录实际执行路径与参数。将当前 0.2.6 安装器安装到原目录（例如 `E:\GromacsConsole`），或将新版 ZIP 的全部文件覆盖到原目录，再启动即可；无需因此重启或重装 WSL。已有发行版、设置与模拟结果保留。

0.1.4 改用原始 UTF-8/LF 字节向 Bash 传入安装脚本，避免 Windows PowerShell 管道附加 CRLF 被解析为额外的 Linux 命令。Linux 安装阶段的 stdout、stderr 和退出码现在也写入同一诊断日志，失败提示会标明发行版和执行用户。旧版本仅记录 `WSL operation failed (127)` 时，不能从日志确定具体缺失的命令。已有成功导入的 Ubuntu 24.04 会在重试时复用，无需再次下载或删除它。

若 0.2.1 在系统包安装成功后显示 `湁瑯敨⁲湩瑳污慬楴湯椠⁳畲湮湩⹧�` 并退出 1，原文是 `Another installation is running.`，说明 Linux 安装锁已被其他进程持有。WSL 的 UTF-16 中文警告与 Linux 的 UTF-8 错误可能写入同一 stderr；旧版为整个输出流固定一种编码，导致后续错误乱码。0.2.2 按输出行处理编码切换，控制台和日志均保留可读的原文。

新版还在 Windows 系统依赖阶段之前取得安装互斥锁，重复打开安装窗口时先等待已有流程完成。Linux 锁出现短暂冲突时最多等待 60 秒；仍被占用则显示明确的忙碌提示并返回 75。安装锁不会传给普通安装子命令，持有进程退出后自动释放。不要手工删除 `install.lock` 来绕过正在运行的安装。先查看原安装窗口，等它完成后将新版装到原目录并启动；已成功安装的系统包、Ubuntu、设置与结果会复用。

`wsl: 检测到 localhost 代理配置，但未镜像到 WSL` 表示 Windows 的本机代理未转发到 WSL NAT 网络。这是独立的网络警告；若日志已显示软件源访问和包安装成功，它不能解释随后出现的安装锁退出。安装器不会自动修改共享的 WSL 网络模式或 Windows 代理设置。

若科学工具验证报 `sqm: error while loading shared libraries: liblapack.so.3`，是 0.2.2 及更早版本漏列 AmberTools 系统运行库导致的。0.2.3 自动检查并补装 LAPACK、BLAS 和 GNU Fortran 运行库，并在配体转换前检查 SQM 的实际启动状态，失败时保留原始库名和修复提示。对固定 ACPYPE wheel 的全部 53 个 ELF 已检查依赖；SQM 的同一个乙醇计算在隔离 Ubuntu 24.04 库中先复现缺库错误，再补库运行成功。开发主机已有这些库的验证不能替代该检查。

安装当前 0.2.6 到原目录，再运行 **Configure or Update** 即可；已验证的相同 GPU 运行库会复用。也可先在 Windows PowerShell 手动补库，再用原快捷方式重新配置：

```powershell
wsl -d GromacsConsole-Ubuntu-24.04 -u root --exec apt-get update
wsl -d GromacsConsole-Ubuntu-24.04 -u root --exec apt-get install -y --no-install-recommends liblapack3 libblas3 libgfortran5
```

若使用了其他兼容发行版名称，请相应替换 `-d` 后的名称。无需因此重新导入 Ubuntu 或删除计算环境；这些包仅补充本地运行库，设置和模拟结果保留。

可以手动指定兼容的 Ubuntu 24.04 WSL2：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Console.ps1 -Action Install -Distro Ubuntu-24.04
```

如果当前电脑无法下载 Ubuntu 镜像，可在另一台网络可用的电脑下载 [固定版本官方镜像](https://releases.ubuntu.com/24.04/ubuntu-24.04.5-wsl-amd64.wsl)，复制过来后，在安装目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Console.ps1 -Action Start -UbuntuImage "D:\Downloads\ubuntu-24.04.5-wsl-amd64.wsl"
```

本地镜像同样需要匹配安装器内置的 SHA-256：`bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e`。用户提供的镜像文件会保留，安装器不删除它。这个参数只解决 Ubuntu 镜像导入；首次安装 Python/GROMACS 依赖仍需访问 Ubuntu 软件源、PyPI 和 conda-forge 下载源。WSL 基础组件本身尚未安装的电脑仍可能需要微软下载源。

WSL 的 localhost 转发需要启用；监听仅使用 `127.0.0.1`，不会为此修改防火墙或打开局域网端口。默认优先使用 8000，忙时选择 8001–8099，并打开实际端口。

## 开发者构建

仅需要 Python 3.11+。生成轻量 ZIP：

```bash
python3 packaging/windows/build.py
```

生成 Windows EXE 安装器还需要 NSIS 3：

```bash
python3 packaging/windows/build.py --makensis /path/to/makensis
```

更改应用或科学工具 requirements 后，在 Linux x64 / Python 3.12 上重新生成 Windows 目标锁，再测试和构建：

```bash
python3.12 packaging/windows/lock-python-dependencies.py --resolve
```

该命令只解析依赖并生成完整版本/哈希锁，不安装软件包；它检查固定科学包哈希、间接依赖、extras、目标 Python/架构和 Ubuntu 24.04 的 glibc 兼容性。其他平台开发依赖仍由通用 requirements 管理。

图标和向导插图已随源码提供，普通构建无需图形依赖。需要修改图形时，用装有 Pillow 的 Python 执行 `packaging/windows/generate_artwork.py`，会重新生成 Windows ICO/BMP 和网页 SVG 图标。

Windows 示例：

```powershell
python packaging/windows/build.py --makensis "C:\Program Files (x86)\NSIS\makensis.exe"
```

产物位于 `dist/`，每个发布文件都有 `.sha256`。CPU 与 GPU GROMACS 均使用固定 conda-forge 预编译包，CUDA 运行库使用 NVIDIA 发布的 PyPI wheel；只解压所需文件，不安装 Conda。安装前均校验固定 SHA-256。应用载荷在 Windows 和 WSL 两侧核对 SHA-256。这些校验用于检测损坏，不替代发行者身份验证：公开分发前建议给 EXE 做代码签名；当前生成的安装器未签名，Windows 可能提示未知发布者。

GitHub Actions 的 **Windows installer** 可手动执行，或在 `v*` 标签推送时生成 EXE/ZIP 并保存为 workflow artifact，不会自动发布。Linux CI 验证安装辅助模块、科学工具、服务和脚本语法；Windows CI 已配置 **Windows PowerShell 5.1 和 PowerShell 7 两个宿主**，两者均执行安装、控制台与宿主选择测试，包括生产脚本的 `[System.UInt64]` 变量声明。Windows 测试还要求真实 `wsl.exe --help` 正确识别 Windows 选项，并在该平台调用真实磁盘 API；发行版安装、复用、编码与提权宿主选择使用隔离模拟，不安装或启动发行版。

开发主机上的 PowerShell 7、Linux 辅助程序及隔离测试已验证；**Windows PowerShell 5.1、实际 UAC 提权、完整 WSL2 安装/重启、Windows 浏览器转发和 GPU 硬件运行尚未在本机实测**。上述双宿主 CI 是已配置的验收流程，不能当作已经取得的 Windows 实机测试结果。完整安装与硬件加速仍需在相应 Windows 机器上验收。
