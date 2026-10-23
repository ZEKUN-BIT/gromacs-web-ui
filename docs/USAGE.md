# 使用与配置说明

这里保留 API、环境变量、配体输入和 MDP 配置的详细说明。首次运行请看 [README](../README.md)；Windows 安装问题请看 [安装指南](WINDOWS.md)。

## Linux 启动配置

`setup.sh` 创建 Python 虚拟环境、安装项目依赖并注册 Linux 应用菜单和桌面快捷方式。日常双击 **GROMACS Console** 或在应用菜单搜索 **GROMACS 控制台**，服务在后台启动并自动打开浏览器；重复点击沿用同一项目已运行的实例。没有启用桌面目录时只安装应用菜单入口。也可继续在终端使用 `run.sh`，不会重复安装依赖。GROMACS 和所需的科学工具由用户在 Linux 环境中准备，配体参数化需要 Open Babel 与 ACPYPE，DSSP 分析需要 DSSP。

已有环境可执行一次 `python3 scripts/linux_desktop.py install` 补装快捷方式。安装只写当前用户目录，不需要 sudo，也不会设置开机自启；项目移动后需重新运行此命令。桌面环境若仍要求信任快捷方式，可右键选择“允许启动”。

关闭浏览器不会停止后台服务或模拟任务。启动失败会显示提示，可在应用图标的右键菜单选择“查看启动日志”；日志位于 `${XDG_STATE_HOME:-~/.local/state}/gromacs-console/<项目标识>/launch.log`。启动器不关闭或重启已运行的服务，不会通过快捷方式终止模拟。

如果 `gmx` 不在 PATH 中，可指定位置：

```bash
GMX_BIN=/path/to/gromacs/bin/gmx ./run.sh
```

桌面快捷方式通过 `start.sh` 启动，读取项目的 `.env`。需要激活已有 Conda/Amber 环境、设置代理或固定端口时，复制 `.env.example` 为 `.env` 后编辑；不使用 Conda 时将 `CONDA_ENV` 留空。启动器会查找 PATH 和用户目录下常见的 Miniforge、Miniconda、Anaconda、Mambaforge 安装位置。示例中的路径需要换成自己的安装位置。桌面启动只使用本机地址，默认端口为 8000。

## 默认模拟流程

蛋白及蛋白–配体水溶液体系默认使用 `EM → 带位置限制的 C-rescale NPT → 无位置限制 C-rescale NPT → Parrinello-Rahman NPT production`。进入生产前检查 EM 收敛及 NPT 的温度、密度和体积稳定性；默认运行 3 个重新分配速度的生产副本，并分别保存轨迹与分析结果。

NVT 可在出现异常盒子振荡、极端初始压力或结构仍不稳定时勾选“先做 NVT（异常时补救）”。模拟时长、温度和输出间隔可在 MDP 覆盖中修改；模板只作为起点，需要按体系检查。

## 任务提交 API

`POST /api/jobs` 使用 multipart：`params` 字段是与 `/api/preview` 共用的 JSON 参数模型，`files` 字段可重复附加上传文件。未知参数会返回 422，避免预览与实际任务的参数定义漂移。

上传时界面显示传输进度，传输完成后显示输入校验与准备状态。上传文件会边写入边计算 SHA-256；复制文件、准备模板与实验清单在线程池中执行，实验清单成功保存后任务才会入队。`experiment-manifest.json`、`metadata.json`、`run.log` 是任务保留文件名，不能作为上传文件名。

任务读取接口采用分层数据：`GET /api/jobs?limit=50&offset=0` 只返回分页摘要，`GET /api/jobs/{id}` 才返回完整参数和命令。浏览器通过 `GET /api/events` 的 SSE 流接收任务状态和增量日志；`GET /api/jobs/{id}/log?offset=N&limit=N` 也可供脚本按字节偏移增量读取。文件列表每个任务步骤只重新索引一次，最多返回 1000 项，并通过 `truncated` 标记是否截断。

## 任务执行架构

已有文件分析使用 `GET /api/jobs/{id}/analysis-inputs` 获取候选文件，`POST /api/jobs/{id}/analysis` 提交选择与指标。后者只接受受限的分析参数，不接受自定义命令或程序路径；输入复制和实验清单准备完成后才入队。来源必须是已完成的正式任务，输入必须是该任务目录内非空的普通文件，拒绝符号链接和目录越界。

“检查轨迹规模与空间”调用 `POST /api/jobs/{id}/analysis-preflight`。研究分析会在复制前检查轨迹原子数、时间范围、取样帧数和 C-alpha 数量；超限时建议取帧间隔，由用户确认后修改。预检按递增轨迹时间估算，worker 仍会检查实际帧数、时间和坐标。Dry run 不解码科学输入。新版本 TPR 若暂不受 MDAnalysis 支持，会用已配置的 GROMACS 在临时目录转换参考结构。

页面通过 `POST /api/jobs/{id}/analysis-stream` 接收 NDJSON 准备进度；原同步接口仍可供脚本使用。输入副本边复制边计算 SHA-256，不再为清单额外完整读取一次。复制开始后可点击“停止准备”；来源和目标锁保留到副本清理结束。关闭页面不主动取消准备，重新打开可在任务列表查看已创建任务；连接中断时先确认任务状态再提交。

`run.sh` 同时启动一个 Web 进程和一个独立执行 worker。Web 进程只负责准备任务、查询 SQLite 状态库和写入取消/接管/续跑指令；worker 通过事务领取任务并启动 GROMACS。`--workers` 和 `WEB_CONCURRENCY` 只能为 `1`，执行 worker 也通过独占锁避免重复启动。

worker 会对临时数据库/文件系统错误退避重试；连续失败或执行线程异常使进程非零退出。Linux 启动器在任一服务退出时停止另一服务，便于服务管理器重启。健康状态同时检查独占锁、同一 worker 的 10 秒内心跳及线程容量，页面通过现有 SSE 定期更新执行服务状态。重启后的计算恢复继续使用已保存的进程身份，不会仅凭 PID 重启或终止模拟。

运行设置中的“每任务分析并发数”默认 2，范围 1–16；也可用 `GROMACS_WEB_ANALYSIS_PARALLEL` 设置默认值。分析组按此上限分批执行，各并行分析进程的数值库限定为 1 线程。任务领取时冻结 CPU 和分析并发预算，设置变化作用于之后领取的任务。CPU 预算取 `mdrun` 所需线程数与分析并发峰值的较大值；并发限制降低内存和磁盘读取压力，但不等于按 GiB 强制限制内存。

任务状态以 `runtime/jobs.sqlite3` 为准，旧的 `runtime/jobs/*/metadata.json` 仅在首次迁移时导入一次。之后不会持续重写 JSON；开发脚本需要人工可读快照时，可调用 `JobStore(runtime_root).export_metadata(job_id)`，导出文件不能替代数据库备份，也不会在重启时覆盖数据库中的状态。运行进程会记录 PID、PGID、启动时间、`/proc` 启动时钟和命令 SHA-256 指纹；重启后的接管和取消只有在这些身份信息全部匹配时才会操作进程组。

每个任务还会记录各步骤的开始/结束时间、耗时、CPU 时间和峰值常驻内存。`mdrun` 输出可解析时，页面会结合模拟 step 与 MDP `nsteps` 展示进度。任务目录中的 `experiment-manifest.json` 包含 GROMACS 版本、命令、参数、输入与 MDP 的 SHA-256、力场来源和主机/GPU 信息，可用于复现实验。

任务详情支持复制任务和结构化参数差异比较。复制只带原始上传、MDP 和任务内力场，不复制 XTC、EDR 等大输出。

实验记录中的项目、标签、备注和置顶状态按任务保存为浏览器本机草稿，切换任务或刷新后可继续编辑，点击保存才写入任务记录。服务器记录变化时会提示核对；浏览器存储不可用时草稿仅保留在当前页面。文件列表读取失败会显示原因和重试按钮。

复制、已有结果分析、清理和删除通过跨进程文件锁协调；忙碌的任务会提示稍后重试。删除失败会保留待删除状态，允许重试，避免残留文件被误当成可运行任务。文件扫描与实际删除在 SQLite 写事务外执行，避免阻塞其他任务更新。

如果 `gmx` 已经在 `PATH` 中：

```bash
./run.sh
```

`setup.sh` 按哈希锁安装运行依赖；开发使用 `./setup.sh --dev`。`run.sh` 只启动服务。修改依赖声明后先按[依赖维护说明](DEPENDENCIES.md)更新锁，再重新执行安装。

## AI 调度

首页内嵌 AI 对话区，直接输入并点击“发送”即可请求模型。首次配置 API 后，页面自动恢复服务商、模型和已保存密钥的可用状态；刷新或重启服务无需重复填写。顶部“AI 对话”定位对话区，“新建任务”定位手动表单。OpenAI / DeepSeek 的配置独立于普通运行设置；密钥也可从服务端 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` 读取。支持任务查询、模拟草案和经确认的复制、取消、续跑、重试、分析任务，详见 [AI 调度指南](AI-AGENT.md)。

## 目录结构

```text
app/                 FastAPI 后端、页面模板和静态资源
docs/                产品与界面设计文档
forcefields/         项目随附或自行添加的 GROMACS *.ff 力场
mdp/protein/         蛋白体系 MDP 模板
mdp/complex/         蛋白-配体体系 MDP 模板
tests/               自动化测试
runtime/jobs/        模拟任务及输出（可能非常大，不属于源码）
setup.sh             创建环境和安装依赖
run.sh               启动服务
```

`runtime/`、`.venv/` 和 Python 缓存均不应提交到版本控制。删除任务输出请优先使用页面中的“删除”操作，避免误删仍在运行或需要保留的模拟结果。

新任务目录采用“时间—任务名—短 ID”的格式，便于直接在文件系统中定位，例如 `20260812-103000-pet13-低盐-a1b2c3d4/`；旧任务目录继续兼容。页面左侧也可以按任务名、状态或工作流搜索。

如果模拟输出盘较慢或项目盘空间不足，可以把 `GROMACS_WEB_ROOT` 指向独立的高速 SSD/NVMe 目录。已完成任务的文件清单会在内存中缓存，切换任务时不再重复递归扫描大型结果目录。

## 环境变量

```bash
GMX_BIN=/path/to/gmx
GROMACS_WEB_MAX_PARALLEL=1
GROMACS_WEB_ANALYSIS_PARALLEL=2
GROMACS_WEB_MAX_UPLOAD_FILES=20
GROMACS_WEB_MAX_UPLOAD_FILE_BYTES=5368709120
GROMACS_WEB_MAX_UPLOAD_TOTAL_BYTES=21474836480
GMX_NTMPI=1
GMX_NTOMP=8
GROMACS_WEB_MIN_FREE_DISK_BYTES=2147483648
HOST=127.0.0.1
PORT=8000
```

上传限制分别控制单次请求的文件数、单文件字节数和总字节数。默认值为 20 个文件、单文件 5 GiB、总计 20 GiB；请结合运行盘容量调整。

服务重启后，worker 会检查保存的进程身份并处理任务恢复。运行中的任务可以在身份匹配时重新接管；没有匹配进程的任务会保留中断状态，供检查后续跑或重试。

worker 在启动计算前和运行期间检查输出盘剩余空间，默认每 5 秒采样。低于停止阈值的两倍时显示预警，并记录空间消耗速度和预计触达阈值的时间；低于阈值时向身份匹配的计算进程发送 SIGTERM，等待 GROMACS 保存 checkpoint。任务随后记为中断，详情中显示停止原因和找到的 checkpoint；超过 120 秒仍未退出才会强制终止。默认阈值为 2 GiB，可通过上面的环境变量调整。该检查不能保证在突发大量写入或文件系统故障时一定生成 checkpoint。

运行产生的任务数据默认保存在项目内的 `runtime/jobs/`。页面里的 `gmx binary` 设置会写入 `settings.json`。

## 完整蛋白-配体流程

页面里的 `Complex MD` 支持两种配体入口：

1. `prepared itp/gro`：上传蛋白 `.pdb/.gro`、`lig_GMX.itp` 和 `lig_GMX.gro`。
2. `obabel/acpype`：上传 `lig.sdf` 或 `lig.mol2`，后端调用 `obabel` 和 `acpype` 生成 GAFF/GAFF2 的 GROMACS 文件。

### 多配体复合物

同一个复合物里有多个**不同**配体（例如辅因子 + 底物、两个不同小分子）时，在「蛋白-配体体系」分区里点「＋ 添加配体」逐行配置。提交参数使用 `ligands` 列表：

```json
{
  "workflow": "protein_ligand_md",
  "protein_file": "complex.pdb",
  "ligands": [
    { "key": "ligA", "residue": "BTA", "mode": "acpype", "charge": -1, "charge_confirmed": true, "smiles": "CC(=O)[O-]" },
    { "key": "ligB", "residue": "ATP", "mode": "prepared", "gro_file": "atp.gro", "itp_file": "atp.itp", "count": 2 }
  ]
}
```

每个配体可配置：`key`（决定工作文件名、index 组名和 POSRES 宏）、`name`（moleculetype 提示）、`residue`（复合物 PDB 中的残基名，自动拆分时按它提取）、`mode`（prepared/acpype）、`count`、`gro_file`/`itp_file`、`structure_file`/`chemistry_file`/`smiles`、`charge`/`charge_confirmed`、`protonation_mode`/`ph` 和 `posres`。旧的单配体字段（`ligand_name`、`ligand_gro_file` 等）仍然有效，后端会把它归一化成单个 `lig` 配体，命令链与旧版本一致。

多配体任务在单配体流程之外自动多做两件事：

- **`[ atomtypes ]` 合并**：每个 ACPYPE `*_GMX.itp` 都自带 `[ atomtypes ]` 段；直接 `#include` 两个 itp 会让 `c3`、`ca` 等同名类型被定义两次，`grompp` 会以 `Atomtype multiply defined` 报错。后端会解析各配体 itp，把 `[ atomtypes ]`（及 `[ nonbond_params ]`/`[ pairtypes ]`）按类型名去重合并成一份，只保留在第一个含该段的 itp 里，其余 itp 删除该段；同一类型名定义不一致会直接报冲突而不是静默覆盖。报告写在 `ligand-atomtypes-merge.txt`。
- **独立分组与限制**：每个配体生成独立的 `make_ndx/genrestr`（`posre_<key>.itp`、`POSRES_<KEY>` 宏）和 index 组（多配体时 `Ligand_<key>`，单配体保持 `Ligand`；`Protein_Lig` 始终是蛋白 + 全部配体）。平衡 MDP 的任务内副本会把 `define` 改写为 `-DPOSRES -DPOSRES_LIGA -DPOSRES_LIGB …`（不改仓库模板；若在 MDP 覆盖里显式填了 `define` 则以你的为准）。配体 RMSD、mindist、氢键按每个配体分别输出 `*-ligand_<key>-*.xvg`。

复合物的轨迹后处理与完整结构/配体相互作用分析覆盖每个生产副本。多副本的居中轨迹、拟合轨迹和初始结构文件加入 `_r01`、`_r02` 等后缀；单副本保留原有文件名。已有任务保存的命令链不会自动改写，新任务使用更新后的流程。

多配体自动拆分按**残基名**提取每个配体，因此多配体时每个配体都必须填写 `residue`；PDB 中出现了没有对应配体行的 HETATM 残基时任务会停止并列出残基名（防止辅因子被静默吞掉）。不确定残基名时，页面上点「从复合物 PDB 识别配体」会自动扫描 HETATM 并填好各行。单个配体仍支持留空残基名、把全部非水非离子 HETATM 当作该配体的旧行为。

共折叠复合物推荐同时上传原始配体 SDF/MOL2，或填写 SMILES。系统以共折叠 PDB 决定配体重原子坐标，以 SDF/SMILES 决定连接关系、键级、芳香性和形式电荷；两者映射后才加氢并交给 ACPYPE。重原子数量、原子映射或形式电荷与已确认净电荷不一致时会停止，并在成功时生成 `<key>-chemistry-map.txt`。默认保留 SDF/SMILES 表达的质子化状态，也可显式选择按目标 pH 重新估计。

Linux 环境需要提前准备：

```bash
conda activate MD
source "$CONDA_PREFIX/amber.sh"
which gmx
which acpype
which obabel
```

`amber19sb`、OPC 和 OPC3 优先使用 GROMACS 2026.3 官方自带版本；不要在项目里放一份重复副本。`forcefields/` 只用于 GROMACS 没有提供的自定义力场。`opc/tip4p` 默认使用四点水坐标模板，三点水使用 `spc216.gro`。

Protein MD 默认保留原始输入；只有显式勾选“清理水/离子/HETATM”才生成清理后的 `protein_pdb2gmx_input.pdb`。Protein-Ligand MD 可把上传的复合物 PDB 拆成 `protein_pdb2gmx_input.pdb` 和每个配体的 `<key>_from_complex.pdb`（单配体兼容旧的 `ligand_from_complex.pdb`）；如果没有单独上传配体 `.gro/.itp` 或 `.sdf/.mol2`，拆出的配体 PDB 会进入 ACPYPE 参数化流程。辅因子、金属和关键结晶水不应依赖自动清理决定去留。

每个任务会生成 `structure-preflight.txt`。其中的 altloc、断号、HETATM 和 occupancy 只能提示风险，不能自动决定质子化状态、互变异构体、末端、二硫键、缺失残基、金属配位或生物学装配体；这些内容必须由使用者结合实验条件确认。`sampling-summary.json` 和 `structural-convergence.json` 给出相对收敛证据，但不构成绝对收敛证明。

拖入或选择新文件后，界面会按当前工作流自动同步右侧的文件字段，例如 `Protein-Ligand MD` 会优先把新上传的 `.pdb/.gro` 写入 `protein_file`。如果某个文件字段仍指向不在本次上传列表中的旧文件，提交前会弹窗阻止，避免任务运行时找不到文件。

ACPYPE/Antechamber 参数化前必须确认配体净电荷。界面中的电荷 `0` 只是默认值，不代表所有配体都应为中性；如果净电荷设置错误，常见报错是电子数奇偶不匹配，随后 `sqm` 或 `antechamber` 失败。真实运行 ACPYPE 前需要在对应配体行勾选“已确认总电荷”；Dry run 可不勾选，用于先检查命令链。

`Complex MD` 会自动执行这些内部文件处理：

- 复制或提取每个配体的 `<key>_GMX.itp/gro`
- 多配体时合并各配体 itp 的 `[ atomtypes ]`（去重，冲突报错）
- 合并 `protein_processed.gro` 和所有 `<key>_GMX.gro` 为 `complex.gro`
- 将每个配体 `.itp` 和 `posre_<key>.itp` 插入 `topol.top`
- 在 `[ molecules ]` 中添加每个配体的分子数
- 使用 `mdp/complex/` 中的 MDP 文件；仓库模板不自动改写 `define`/`tc-grps`，但任务目录里的平衡 MDP 副本会把 `define` 改写成与拓扑一致的多配体 `POSRES_*` 宏（显式覆盖了 `define` 时以覆盖值为准）

默认 index 脚本为：

```text
1 | 13
name 20 Protein_Lig
! 20
name 21 Water_and_Ions
q
```

如果你的体系中配体组编号不是 `13`，需要在表单里改 `ligand group`，或直接填写 `custom index script`。多配体任务不使用该默认脚本，而是自动生成 `System / Protein / Ligand_<key>… / Protein_Lig / Water_and_Ions / Backbone / C-alpha / MainChain / MainChain+H / SideChain / Water`；自定义脚本时请自行保证组名与 MDP 引用一致。

## MDP 模板与交互选择

MDP 模板会按任务类型自动复制到任务目录。推荐准备两套真实文件：

```text
mdp/
  protein/
    ions.mdp
    minim.mdp
    nvt.mdp
    npt.mdp
    md.mdp
  complex/
    ions.mdp
    minim.mdp
    nvt.mdp
    npt.mdp
    md.mdp
```

选择规则为：`Protein MD` 和 `EM` 使用 `mdp/protein/`；`Complex MD` 使用 `mdp/complex/`。同名文件优先级为：用户上传的 `.mdp` > 当前任务类型目录 > 项目根目录 `mdp/*.mdp` > `sample_mdp/*.mdp` > 代码内置兜底模板。任务日志会记录每个自动写入的 MDP 来源。

蛋白-only 的 MDP 不应引用 `Protein_Lig` 或 `POSRES_LIG`。复合物 MDP 可以引用 `Protein_Lig` 和 `Water_and_Ions`，但这些名称必须和自动生成或自定义的 `index.ndx` 一致；Web UI 会在复合物的 `grompp` 步骤中自动带上 `-n index.ndx`。

前端提供可选的“MDP 覆盖”面板，默认折叠且不会修改 MDP；需要时展开并启用后，只会修改本次任务工作目录中的 MDP 副本，不会改动 `mdp/protein/` 或 `mdp/complex/` 模板。当前白名单覆盖项包括：`dt`、NVT/NPT/MD 的 `nsteps`、`ref_t`、`gen_temp`、`ref_p`、`tc-grps`、`tau_t`、NVT/NPT 的 `define`、NVT 的 `gen_vel`、NPT/MD 的 `pcoupl`、`constraints`、`rcoulomb`、`rvdw`、`nstenergy`、`nstlog` 和生产阶段的 `nstxout-compressed`。命令预览和任务日志会列出每个被覆盖的文件、键和值。

所有需要交互选择的 GROMACS 命令都会由 Web UI 预先写入 stdin，并在命令预览和任务日志中显示。例如 `genion` 默认发送 `solvent_group`，`make_ndx` 默认发送 index 脚本，`trjconv -center` 在 `-pbc cluster`（默认）下发送「聚拢组 / 居中组 / 输出组」，在 `-pbc mol/res/atom/whole/nojump` 下发送「居中组 / 输出组」。如果某一步要手动指定输入，打开“自定义交互 stdin”，点击“从预览生成模板”，再按步骤标题修改对应块：

```text
[Create ligand heavy-atom index]
0 & ! a H*
q

[Center trajectory]
Protein_Lig
Protein_Lig
System
```

没有写在覆盖模板里的交互步骤会继续使用自动默认值。

`pdb2gmx` 的 force field 和 water model 已在表单中暴露；如果你的体系需要配体、小分子参数、膜体系或特殊 index group，优先使用 `Complex MD`，并在提交前先用 `Dry run` 检查命令链。

后处理建议在 MD 完成后运行：

1. `Suite`：从原始轨迹自动修复 PBC 并居中；RMSD/PCA 在各自分析程序内拟合，Rg、氢键使用未旋转轨迹。复合物须设置正确的聚拢组与 NDX。
2. `Post`：需要可视化时生成居中轨迹、拟合轨迹和 `start.pdb`，并按需勾选保留。二聚体/多聚体请保持 `pbc mode = cluster`。接触或氢键分析应使用未旋转轨迹，保持周期盒子与坐标方向一致。
3. 开启 PCA/SHAM 前，先确认 `gmx covar` 和 `gmx anaeig` 的 group 选择符合体系。

生产阶段自带的骨架 RMSD 在 nojump 后用 `trjconv -pbc cluster -center` 聚拢并居中，再明确使用 `gmx rms -fit rot+trans`。新流程只应用于新建任务，旧 XVG 需要重新分析。

## 项目空间管理

侧栏的“空间管理”显示磁盘剩余空间、运行目录总占用，以及输入、轨迹、checkpoint、拓扑参数、日志和报告等分类占用。可以按名称或 ID 筛选任务，选择任务后先点击“预览清理”，检查文件明细和预计回收量，再确认执行。预览有效期为 10 分钟；任务状态或待清理文件发生变化后，需要重新预览。

常规清理仅面向已完成或已取消、没有待处理操作和活跃计算进程的任务，清理已标记为可丢弃的轨迹以及蛋白/复合物 MD 的 EM、NVT、NPT 中间轨迹及对应 GROMACS 备份。输入、拓扑、MDP、checkpoint、生产轨迹和分析报告默认保留；“同时清理日志”需要单独勾选。失败、中断、运行、排队及准备中的任务受保护，方便诊断和恢复。

需要彻底移除旧结果时，显式选择“删除所选任务及全部结果”。这会一并删除任务记录、输入、checkpoint、生产轨迹和报告，确认前请先备份需要的数据。清理范围限定在本项目配置的运行目录，包含符号链接或特殊文件的任务会被跳过。源码、力场模板、Python 环境和项目缓存不属于此功能的清理范围。界面中的回收量按文件大小估算，实际磁盘可用空间还受文件系统、硬链接和其他程序写入影响。

对应 API 为 `GET /api/storage`、`POST /api/storage/preview` 和 `POST /api/storage/cleanup`。执行请求需要携带预览返回的 `preview_token`，令牌仅可用一次，并绑定所选任务、清理选项及文件快照；状态或文件发生变化时会拒绝旧预览。浏览器的操作请求必须来自当前本机控制台。

## 大文件与研究历史

XVG 图表采用流式读取，单文件默认上限 512 MiB。降采样保留分箱中的极值，统计量基于筛选窗口内全部有效数据；返回的 `sampling` 说明采样规模，`skipped_files` 说明被跳过的文件及原因。缓存有数量和内存上限，并在文件变化后失效；图表只用于浏览，原始 XVG 仍可下载。

研究比较页面通过 `GET /api/research/jobs?limit=50&offset=0&search=...` 分页搜索全部已索引报告，不再受普通任务列表最近 200 条的限制。翻页或搜索会保留已选择的参考和比较任务。

## 回归验证

```bash
.venv/bin/pytest
.venv/bin/ruff check app tests
.venv/bin/ruff format --check app tests
node scripts/check_frontend_state.mjs
.venv/bin/python scripts/check_frontend_browser.py
.venv/bin/python scripts/check_storage_browser.py
GROMACS_INTEGRATION=1 .venv/bin/pytest -m gromacs_integration -v
```

浏览器检查需要安装 Playwright 和 Chromium，所有接口使用合成数据，不会提交真实模拟或清理真实任务。真实 GROMACS 检查默认跳过；启用后在临时目录中生成 64 原子的氩体系，验证 HTTP 提交、实际计算、RMSD/XVG API 与 checkpoint 续跑，缺少 GROMACS 时会失败。CI 覆盖 Python 3.11/3.12，并在手动、每周和标签发布时运行真实引擎检查。

## 安全边界

- 后端使用 `subprocess.Popen([...], shell=False)`。
- 上传文件名会清理路径片段。
- 下载接口会校验目标路径必须在对应任务目录下。
- 自定义命令必须以配置的 `gmx` 二进制开头，且子命令在允许列表内。
- 内部文件处理只读写当前任务目录内的文件。

不要把这个服务直接暴露到公网。需要多人使用时，请放在 VPN、SSH 隧道或反向代理认证之后。
