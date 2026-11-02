# 2026-10-28 优化与验收记录

本轮覆盖任务恢复、真实引擎回归、研究比较、历史调度与空间统计、上传恢复、AI 诊断，以及 Windows 验收记录工具。代码基于 `343c8af` 后的工作区，版本仍为 `0.2.10`。验证环境为 Linux、Python 3.12.13、GROMACS 2025.4；计算测试使用隔离临时目录、单线程 CPU，没有重启或操作已有服务及模拟任务。

## 实现与验证范围

| 优化项 | 实现与证据 |
| --- | --- |
| worker 中断恢复 | 命令输出持久写入独占创建的普通文件，worker 增量补收并保存游标。恢复证据绑定步骤，状态持久化后才清理输出。七种真实终止 worker 的回归覆盖运行中接管、接管后取消、停机期间完成、失败时拒绝旧完成标志、接管前取消、输出消费与状态提交之间退出、相邻两个 mdrun 的切换窗口。 |
| 真实引擎覆盖 | 六项真实 GROMACS 测试通过，覆盖短模拟及 RMSD API、checkpoint 续跑、可离线查看的教程报告、小肽准备、预参数化配体准备，以及默认配体索引排除氢原子。准备测试发现并修复无氢配体默认索引命名问题；自定义索引组保持兼容。`mdp/` 和 `sample_mdp/` 已纳入引擎 CI 触发范围。 |
| 研究比较 | 比较结果保留来源任务、质量警告、帧数和实际时间范围，并在界面及导出 JSON 中展示。缺失采样信息保持未知；恶意名称和说明通过浏览器文本转义回归。 |
| 历史调度 | 活动进程注册表保留仍需核验的记录，已确认退出的历史任务不再逐轮检查；旧记录迁移、索引查询计划及只恢复运行任务均有回归。 |
| 空间统计 | 任务摘要持久缓存，终态最多复用 300 秒、活动任务 5 秒；任务版本或根目录变化提前失效。分页、全历史搜索、跨页选择与强制刷新已完成。清理预览和执行仍重新扫描、加锁并核验指纹，不依赖展示缓存。 |
| 上传恢复 | 上传进度、取消和两分钟无传输进度检测；准备阶段没有短总超时。持久提交编号绑定参数和文件摘要，创建与关联同事务完成；不确定结果先核对原提交，取消封存可阻止迟到请求，删除任务后旧请求不会重建。 |
| AI 诊断 | 向模型提供白名单质量说明、有界数值、失败步骤和错误类别；不新增原始日志、任意错误文本或命令传输。未知原因明确留待本机复核。 |
| Windows 验收 | 新工具采集只读环境信息并生成未验证记录，检查版本、提交、安装器及证据哈希，拒绝缺少实际结果、路径越界、符号链接和 junction。工具测试通过；实机验收尚未执行。 |

## 最终回归

首轮全量执行结果：**1020 passed，2 skipped，13 warnings，76.34 秒**。其中包含六项真实引擎测试。语句与分支综合覆盖率为 **84.63%**，终端四舍五入显示 85%。当时两项跳过均来自 `tests/test_windows_packaging.py`，原因是尚未提供真实 NSIS 编译器；已在下方补充验证中执行并通过。警告为 Starlette 测试客户端依赖弃用提示及合成 PDB 缺少可选属性的 MDAnalysis 提示。

执行命令：

```bash
GROMACS_INTEGRATION=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
GMX_DISABLE_GPU_DETECTION=1 CUDA_VISIBLE_DEVICES='' \
PYTHONPATH=/tmp/gromacs-msgpack-verification-d5_mdjdl/lib:/home/zek/gromacs-web-ui \
.venv/bin/python -m pytest --cov=app --cov-report=term-missing \
  --cov-report=json:/tmp/gromacs-optimization-final-coverage.json \
  --junitxml=/tmp/gromacs-optimization-final-junit.xml
```

这里的临时 `PYTHONPATH` 只用于验证按哈希安装的 msgpack 1.2.4，没有原地修改共享 `.venv`。复现时可使用按最终开发依赖锁安装的独立环境，省略此临时路径。

浏览器与静态检查：

- 最终真实浏览器流程通过：上传、预览、确认、worker 执行、GROMACS、SSE、日志及结果 ZIP 下载。使用独立 localhost 服务和临时 runtime；冷/热 DOM 就绪时间观测值为 168/35 ms，属于单次本机测量。
- 上传、任务导航、研究分析、AI 会话及内容、产品流程、连接恢复、异常恢复和图表按需渲染脚本通过。空间管理在最后一次界面调整后重跑，桌面与移动视口均通过，覆盖 61 条记录的分页、搜索、跨页选择和刷新。
- 12 个页面/主题/视口组合通过自动 WCAG 规则、已有截图基线和资源预算检查；自动规则未发现违规，不等同于完整人工无障碍认证。未更新截图基线。
- Linux Chromium 与 Firefox 在 1440、768、390 像素视口通过原生字体、减弱动画及相关布局检查；这些结果不代表 Windows 原生界面或物理移动设备验收。
- Node 前端状态与分析流检查、Ruff 检查及格式检查、依赖锁源文件校验、前端资源指纹校验和 `git diff --check` 均通过。资源指纹为 `a3c62023f9227a95`。

本机诊断文件位于 `/tmp/gromacs-optimization-final-junit.xml`、`/tmp/gromacs-optimization-final-coverage.json` 和 `runtime/frontend-quality/` 下的 `optimization`、`storage-optimization`、`platform-optimization`、`live-optimization-final`。这些是本地运行产物，不作为已提交的发布证据。

## NSIS 补充验证

此前缺编译器而跳过的两项回归已补跑。测试移至 `tests/test_windows_nsis.py`，仅依赖 Python 标准库、pytest 和打包模块，避免原测试文件在模块导入时加载 Linux `fcntl`，导致原生 Windows 无法收集用例。

| 执行环境 | 编译器与输入编码 | 实际结果 |
| --- | --- | --- |
| Linux | Ubuntu NSIS `v3.10-2`；UTF8 | 原始源码编译成功，安装和卸载中文文本校验通过。 |
| Linux | Ubuntu NSIS `v3.10-2`；CP437 | 原始源码编译成功，实际乱码产物被校验器拒绝；两项 pytest 合计 2 passed、0 skipped。 |
| Linux 上的隔离 Wine 10.0 | Windows NSIS `v3.13`；UTF8 | 使用 Windows 编译器实际生成 EXE，中文校验通过。 |
| Linux 上的隔离 Wine 10.0 | Windows NSIS `v3.13`；CP1252 | 使用 Windows 编译器实际生成 EXE，安装/卸载中文乱码按预期被拒绝。 |

随后以真实 Linux 编译器和严格模式执行 `pytest tests/test_windows*.py -q`，结果为 **287 passed、0 skipped，29.71 秒**。修改后的测试通过 Ruff 和格式检查，工作流 YAML 解析及 `git diff --check` 通过。

Linux NSIS 来自 Ubuntu 软件源的 `nsis`/`nsis-common` 包，仅解压到临时目录。Windows NSIS 安装包采用工作流固定的 3.13 版本，SHA-256 核对为 `db9a1038bbc0e53fef24f423fd609979bd578e942f1be1198db186d1c42652a3`，安装到独立 Wine 前缀。没有安装项目应用或改变现有 WSL/模拟环境。

Linux 原生负例使用 CP437，因为 CP1252 的未定义字节会被 iconv 在编译前拒绝；Windows 保留原始 CP1252 场景。验收只要求错误产物被拦截，不将 Linux CP437 结果表述为原生 Windows CP1252 验证。校验器按整个字符串区查找文本，短标题 `FinishTitle` 与标准语言表有重复，乱码负例针对其余五条具有区分度的文本断言，其中包括卸载说明。

Windows 构建工作流已补齐两端测试入口：Linux 安装 NSIS，Windows 将编译器加入 PATH 并安装 pytest；均启用 `GROMACS_REQUIRE_NSIS=1`，缺编译器直接失败，并始终保留 JUnit 报告。本轮未推送或执行远程 CI，原生 Windows job 的结果仍以之后的实际运行记录为准。

本机证据保存在 `/tmp/gromacs-nsis-verification-9o141n__/`：`linux-nsis-fixed.xml`、`windows-suite.xml` 和 `windows-compiler-check/windows-compiler.json`，对应编译日志及测试 EXE 也保留在该临时目录。Wine 编译检查不等同于 Windows 桌面、安装、WSL 或 GPU 实机验收。

## 历史调度测量

每组在独立临时 SQLite 库中批量插入已完成任务，进程身份检查被替换为“已退出”，以排除实际 `/proc` I/O。旧实现每组测一次空闲 `claim_next`；新实现先完成一次历史验证及索引修剪，再取连续十次空闲调用的均值。

| 历史任务数 | 原实现单次 | 新实现稳态均值 | 新实现十轮身份检查总数 |
| --- | ---: | ---: | ---: |
| 0 | 0.37 ms | 0.32 ms | 0 |
| 1000 | 46.37 ms | 0.34 ms | 0 |
| 5000 | 226.59 ms | 0.36 ms | 0 |

该合成测量说明已经确认退出的历史任务不再拖慢每轮空闲调度。首次迁移/验证仍有随历史规模增长的成本；两列采样方法不同，也没有包含真实进程查询，不能将比值解释为生产吞吐提升。

## 依赖与安全复核

运行/开发锁中的 msgpack 从 1.2.1 定向升级到 1.2.4，以修复 CVE-2026-114613；Windows 科学工具锁原有 1.2.3 保持不变。最终 `pip-audit --disable-pip --no-deps -r <lock>` 对运行、开发和 Windows 三份锁分别检查 45、58、53 项，均为 **0 条已知漏洞、0 个跳过项**。运行锁完成全依赖、哈希约束及 wheel 可用性的安装解析；升级包另在隔离目录验证编解码和 MDAnalysis 导入。

共享开发环境仍保留旧 msgpack，另有不在项目分发锁内的 dnspython 2.8.0 公告 CVE-2026-114605。以上审计结论针对最终分发锁与审计时公告，不表示当前共享环境已全部升级。

独立交叉复核闭环了两个崩溃恢复窗口，以及 Windows 证据记录的空实际结果和 junction 校验问题。OWASP 十类复核见 [安全复核](SECURITY-REVIEW.md) 和 [AI 与研究诊断复核](AI-RESEARCH-DIAGNOSTICS-REVIEW.md)。

## 保留的运行边界

持久输出在当前命令执行期间保留完整 stdout，额外占用磁盘；`run.log` 的轮转上限不限制它。worker 停机期间输出仍可能增长，磁盘监测要等 worker 恢复才继续。游标约每秒保存，崩溃后可能重放最后一小段日志。清理必须等进程退出、输出消费和步骤/终态提交完成。详见 [使用说明](USAGE.md)。

小肽/配体准备只运行短最小化、20 步 NVT，并验证 NPT 编译，未验证充分平衡、科学结论或 ACPYPE 自动参数化。所有真实计算均使用 CPU，本次没有验证 GPU 模拟性能。

**Windows 实机验收仍待完成。** 当前主机没有 Windows 桌面及 WSL/GPU 实机条件，不能验证五类安装、升级、WSL/GPU 和故障恢复场景。采集和核验命令已写入 [发布验收清单](RELEASE-CHECKLIST.md)；采集器始终生成 `unverified`，只有补充实际操作和证据后才可能通过完整检查。检查器验证记录的一致性，不能独立证明实机操作发生过。本轮未发布版本，也未运行远程 CI。
