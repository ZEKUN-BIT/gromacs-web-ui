# GROMACS Console

一个面向本地 GROMACS 的轻量 Web 前端，支持 Linux 和 Windows + WSL2。浏览器负责配置、上传输入、查看命令、跟踪日志和下载输出；后端在本机用参数数组调用 `gmx`，不通过 shell 拼接命令。

Copyright (c) 2026 yzk。本项目自有代码采用 [MIT 许可](LICENSE)，允许使用、修改、再分发和商用，须保留版权与许可声明。第三方软件和字体遵循各自许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。署名与许可的具体说明见 [版权说明](docs/LICENSING.md)。

## 功能

- 研究分析：任务详情新增构象变化、配体结合和突变体比较。选择时间范围、取帧间隔和单配体索引组后，计算蛋白拟合后的 C-alpha RMSD/Rg/RMSF、前后段残基位移、PCA、配体位置/内部构象 RMSD、逐残基重原子接触占有率及初始口袋保持率。跨任务按蛋白链和序列对齐残基，按轨迹等权比较组均值、SD 和残基差值，并核对来源模拟条件。方法与限制见 [研究分析说明](docs/RESEARCH-ANALYSIS.md)。

- 工作区分为新建任务和任务详情，详情提供运行概览、质量检查、分析图谱和输出文件；窄屏通过任务列表按钮展开侧栏。
- 历史任务分页浏览，按名称、编号、状态或流程搜索全部任务；切页和搜索保留当前打开任务的详情。
- 图谱支持按指标和副本筛选、同指标副本叠加、悬停读数与时间区间（ns）筛选。区间均值基于原始 XVG 数据计算后再抽稀显示；RMSF 等非时间轴图不参与时间筛选，下载始终保留完整 XVG。叠加只合并相同指标和坐标单位，不插值或拼接不同副本。
- 模拟协议摘要按项目模板、上传 MDP 和本次覆盖显示各阶段时长、目标温度、压控方式及参数来源；常用时长、温度和步长可以直接修改。参数草稿保存在当前浏览器，恢复后需重新选择输入文件并确认配体电荷。
- 热力学质量门读取本次实际使用的 NPT MDP 中的 `ref_t`；不同温控组目标温度不一致时停止并要求按组核查，不用平均温度代替逐组检查。
- Protein MD 模板：`结构预检 -> pdb2gmx -> editconf -> solvate -> grompp/genion -> EM 质量门 -> 位置限制 NPT -> 无限制 NPT -> 多副本 production`
- Protein-Ligand MD 模板：配体参数化/导入、复合物坐标合并、`topol.top` 注入配体拓扑、配体位置限制、`index.ndx`、NPT 平衡/生产、轨迹居中和拟合
- 多配体复合物：`Complex MD` 支持多个不同配体（`ligands` 列表），自动合并各配体坐标、把各配体 itp 的 `[ atomtypes ]` 去重合并、生成独立的 index 组与 POSRES 宏，并按配体输出 RMSD/mindist/氢键分析；旧的单配体字段继续兼容

常规蛋白水溶液和蛋白-配体体系默认采用 `EM → 带位置限制的 C-rescale NPT → 无位置限制 C-rescale NPT → Parrinello-Rahman NPT production`。进入生产前会检查 EM 收敛以及 NPT 的温度、密度和体积稳定性。默认生成 3 个重新分配速度的独立生产副本，并分别保存轨迹、热力学量、骨架 RMSD、块平均误差和副本间差异。NVT 不是默认必经步骤；只有直接 NPT 出现异常盒子振荡、极端初始压力或结构仍不稳定时，才在前端勾选“先做 NVT（异常时补救）”。
- 单独能量最小化：上传结构、拓扑和 `minim.mdp`
- 直接运行 `.tpr`
- RMSD 分析：上传 `.tpr` 和 `.xtc/.trr`，明确执行 `nojump → cluster+center → rms -fit rot+trans`；预处理、指标拟合与参考定义见 [轨迹分析说明](docs/TRAJECTORY-ANALYSIS.md)。
- 已有结果追加分析：已完成的正式任务详情提供「继续分析」，选择已有轨迹、TPR、可选 NDX/EDR，按需计算 RMSD、Rg、能量、氢键、DSSP 或 PCA。优先配对同名文件；手动选取时需确认拓扑与轨迹的原子顺序一致。输入在服务器本地独立复制并计算 SHA-256，无需重复上传；会占用额外磁盘空间。先修复 PBC 并保留完整体系，分析完成后清理中间轨迹。新任务及实验清单保留来源任务和文件映射，删除原任务不影响已复制的输入。
- 轨迹后处理：`trjconv -center -pbc cluster`（默认，适合二聚体/多聚体；`mol/res/atom` 可用 `-ur compact`）、`-fit rot+trans`、提取第 0 帧
- 存储控制：NVT/NPT 默认只写压缩轨迹；后处理完成后自动删除体积较大的中间产物（居中轨迹、拟合轨迹、RMSD 用的整化轨迹、classic 分析用的整化轨迹），拟合轨迹如需可视化可在后处理区勾选「保留拟合轨迹」；生产轨迹的落盘频率可用「MDP 覆盖 → 输出间隔」调大（默认 10 ps，改成 50/100 ps 可让 `.xtc` 缩小 5–10 倍）
- 并发任务：执行 worker 支持多个任务并行（`max_parallel`，可在设置面板或 `GROMACS_WEB_MAX_PARALLEL` 环境变量配置）。GROMACS 任务都是子进程，worker 用线程并行调度，内存开销小；注意每个任务还会用 `ntomp` 个 OpenMP 线程，建议「并发任务数 × ntomp ≤ CPU 核数」
- 性能辅助：运行环境会按物理核心数和 GPU 数量推荐 `ntmpi` / `ntomp` / `-pin`，提交区提示 CPU 超配、单 GPU 争用和长轨迹帧数；运行详情显示实时 `ns/day` 与预计剩余时间，完成后采用 GROMACS 最终 `Performance` 数据。
- TPR 基准：任务详情中的「性能基准」会复制已有 TPR，默认依次短跑物理核心数、中间线程数和全部逻辑 CPU，生成 `benchmark-report.txt` / `benchmark-results.json` 并在速度卡片标出最佳组合；不会修改来源任务的轨迹。
- 资源感知调度：GPU `mdrun` 在单卡机器上按任务独占 GPU 槽，CPU-only 工作仍可在 CPU 容量允许时越过等待中的 GPU 任务执行，避免仅按任务数并发造成 GPU 与 OpenMP 线程争用。
- 任务内并行：classic 分析阶段的 RMSF / Rg / SASA / 配体 RMSD / mindist / 氢键彼此独立，会在整化轨迹就绪后并发执行
- 失败步骤恢复：支持重试与 checkpoint 续跑；仅允许跳过没有后续文件依赖的可选分析、清理或绘图。体系准备与质量门禁止跳过，并行分析按整组重试。跳过记录保存在任务元数据并显示在详情中。
- 运行保护：`run.log` 超上限自动轮转保留末尾；执行前检查剩余磁盘空间（默认 <2 GB 中止，可用 `GROMACS_WEB_MIN_FREE_DISK_BYTES` 调整）
- 图表容量：`xvg_plots` 达到文件/曲线数上限时会明确提示「部分省略」，并可直接下载完整 `.xvg`
- 分析套件：势能、RMSD、回转半径、DSSP、氢键、PCA、SHAM 自由能图
- 自定义 `gmx` 子命令，限制在常用 GROMACS 子命令范围内
- 实时轮询任务状态、日志和输出文件
- `Dry run` 模式用于只检查命令链

## Windows 安装版（WSL2）

支持 Intel/AMD x64 Windows 10/11，通过轻量安装器配置或复用 Ubuntu 24.04 WSL2，双击桌面快捷方式打开页面。首次运行自动安装 Open Babel、ACPYPE/配体 AmberTools 和 DSSP；检测到兼容 NVIDIA GPU 时安装并验证 CUDA 版 GROMACS，否则使用 CPU。两种路线均使用预编译运行库，GPU 文件并行下载，更新时复用验证通过的依赖。安装包不包含 Linux 镜像、Conda、完整 CUDA SDK 或模拟结果，临时下载随后清除。GPU 支持范围、空间控制及构建方式见 [Windows 安装说明](docs/WINDOWS.md)。

0.2.6 修复 Windows PowerShell 5.1 无法识别 `[ulong]` 的安装错误，优先使用已安装的 x64 PowerShell 7，未安装时回退到系统 5.1。升级到原安装目录后运行 **Configure or Update**，复用已有 WSL 发行版和验证通过的计算环境。此版本还修复任务取消/恢复、分析时间窗口、异常数值检查、分块统计、历史任务浏览和确认弹窗，并正确识别 CUDA 同一主版本内的小版本兼容。

```bash
python3 packaging/windows/build.py
# 如已安装 NSIS，可同时生成 Windows setup.exe：
python3 packaging/windows/build.py --makensis /path/to/makensis
```

## Linux 运行

在项目目录使用启动脚本运行：

```bash
./start.sh
```

`start.sh` 会读取项目根目录的 `.env`。先复制 `.env.example`，按本机情况设置 Conda 环境、Amber 初始化脚本、GROMACS 路径和监听地址；不使用 Conda 时将 `CONDA_ENV` 留空即可。需要临时修改端口时仍可执行 `PORT=8010 ./start.sh`。

首次安装或依赖变化时才需要执行下面的环境安装步骤：

```bash
cd gromacs-web-ui
chmod +x setup.sh run.sh
./setup.sh
GMX_BIN=/path/to/gromacs/bin/gmx ./run.sh
```

默认的 `amber19sb`、OPC 和 OPC3 直接使用 GROMACS 2026.3 官方自带文件；`forcefields/` 目录仅用于放置 GROMACS 未提供的额外力场。

打开：

```text
http://127.0.0.1:8000
```

## 任务提交 API

`POST /api/jobs` 使用 multipart：`params` 字段是与 `/api/preview` 共用的 JSON 参数模型，`files` 字段可重复附加上传文件。未知参数会返回 422，避免预览与实际任务的参数定义漂移。

任务读取接口采用分层数据：`GET /api/jobs?limit=50&offset=0` 只返回分页摘要，`GET /api/jobs/{id}` 才返回完整参数和命令。浏览器通过 `GET /api/events` 的 SSE 流接收任务状态和增量日志；`GET /api/jobs/{id}/log?offset=N&limit=N` 也可供脚本按字节偏移增量读取。文件列表每个任务步骤只重新索引一次，最多返回 1000 项，并通过 `truncated` 标记是否截断。

## 任务执行架构

已有文件分析使用 `GET /api/jobs/{id}/analysis-inputs` 获取候选文件，`POST /api/jobs/{id}/analysis` 提交选择与指标。后者只接受受限的分析参数，不接受自定义命令或程序路径；输入复制和实验清单准备完成后才入队。来源必须是已完成的正式任务，输入必须是该任务目录内非空的普通文件，拒绝符号链接和目录越界。

`run.sh` 同时启动一个 Web 进程和一个独立执行 worker。Web 进程只负责准备任务、查询 SQLite 状态库和写入取消/接管/续跑指令；worker 通过事务领取任务并启动 GROMACS。`--workers` 和 `WEB_CONCURRENCY` 只能为 `1`，执行 worker 也通过独占锁避免重复启动。

任务状态保存在 `runtime/jobs.sqlite3`，旧的 `runtime/jobs/*/metadata.json` 会在首次启动时自动导入，并继续作为便于人工查看的镜像。运行进程会记录 PID、PGID、启动时间、`/proc` 启动时钟和命令 SHA-256 指纹；重启后的接管和取消只有在这些身份信息全部匹配时才会操作进程组。

每个任务还会记录各步骤的开始/结束时间、耗时、CPU 时间和峰值常驻内存。`mdrun` 输出可解析时，页面会结合模拟 step 与 MDP `nsteps` 展示进度。任务目录中的 `experiment-manifest.json` 包含 GROMACS 版本、命令、参数、输入与 MDP 的 SHA-256、力场来源和主机/GPU 信息，可用于复现实验。

任务详情支持复制任务和结构化参数差异比较。复制只带原始上传、MDP 和任务内力场，不复制 XTC、EDR 等大输出。

如果 `gmx` 已经在 `PATH` 中：

```bash
./run.sh
```

`setup.sh` 负责创建虚拟环境和安装依赖；`run.sh` 只启动服务，因此日常重启不会重复升级 pip 或访问软件源。修改 `requirements.txt` 后重新执行一次 `./setup.sh`。

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
GROMACS_WEB_MAX_UPLOAD_FILES=20
GROMACS_WEB_MAX_UPLOAD_FILE_BYTES=5368709120
GROMACS_WEB_MAX_UPLOAD_TOTAL_BYTES=21474836480
GMX_NTMPI=1
GMX_NTOMP=8
HOST=127.0.0.1
PORT=8000
```

上传限制分别控制单次请求的文件数、单文件字节数和总字节数。默认值为 20 个文件、单文件 5 GiB、总计 20 GiB；请结合运行盘容量调整。

服务重启时，先前处于排队或运行状态的任务会标记为“已中断”，不会被误认为仍在执行。

运行产生的任务数据默认保存在项目内的 `runtime/jobs/`。页面里的 `gmx binary` 设置会写入 `settings.json`。

## 完整蛋白-配体流程

页面里的 `Complex MD` 对应 Notion 中的完整流程，支持两种配体入口：

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

## 使用建议

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

## 安全边界

- 后端使用 `subprocess.Popen([...], shell=False)`。
- 上传文件名会清理路径片段。
- 下载接口会校验目标路径必须在对应任务目录下。
- 自定义命令必须以配置的 `gmx` 二进制开头，且子命令在允许列表内。
- 内部文件处理只读写当前任务目录内的文件。

不要把这个服务直接暴露到公网。需要多人使用时，请放在 VPN、SSH 隧道或反向代理认证之后。
