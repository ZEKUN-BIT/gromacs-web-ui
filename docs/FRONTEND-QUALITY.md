# 前端质量检查

常规浏览器检查使用 Playwright Chromium 和拦截的合成 API，不会提交真实模拟、访问用户运行目录或连接外部 AI 服务；另有下文明确区分的隔离真实引擎检查。CI 保留现有六个浏览器流程，并增加提交校验、任务连接恢复、图表绘制和统一质量检查。

在安装项目锁定的开发依赖和 Chromium 后，从仓库根目录运行：

```bash
python -m playwright install --with-deps chromium
python scripts/check_submission_browser.py --artifact-dir runtime/frontend-quality/submission
python scripts/check_connection_browser.py
python scripts/check_plot_rendering_browser.py --output runtime/frontend-quality/plot-rendering.json
python scripts/check_frontend_quality.py --artifact-dir runtime/frontend-quality
```

CI 在 Python 3.12 的测试任务中执行浏览器检查；失败时上传 `runtime/frontend-quality/` 下已经生成的诊断产物，保留 3 天。失败前尚未执行的检查不会产生报告，具体错误也应结合对应步骤的日志查看。

## 覆盖范围与稳定条件

`check_submission_browser.py` 检查字段错误、提交状态及重复提交等行为。`check_connection_browser.py` 使用虚拟时钟和合成 EventSource，检查更新事件、旧连接隔离、手动重连、45 秒心跳超时、重试退避、离线恢复及隐藏页面生命周期。

`check_plot_rendering_browser.py` 使用 24 张图、每图 1,200 个合成数据点，检查缩放、屏外尺寸变化、滚动进入视口、主题、DPR、隐藏恢复及替换/重置。正常与禁用 IntersectionObserver 两条路径均须通过：屏外和已移除画布不得绘制，同一画布每帧最多绘制一次。输出包含绘制次数和画布 setup/paint 耗时；耗时不包括数据准备、完整页面渲染或浏览器合成。

统一质量检查覆盖 12 个视图：

| 工作区 | 尺寸 | 主题 |
| --- | --- | --- |
| 配置 `compose`、AI `agent`、任务 `detail` | 桌面 1440 × 1000、手机 390 × 844 | 浅色、深色 |

浏览器上下文固定为 `zh-CN`、`UTC`、DPR 1，日期固定为 2026-10-26 UTC。截图等待字体与有限动画完成，隐藏插入光标，并遮罩单独通过连接测试验证的最近同步时间。截图使用仓库内的 Inter 和测试专用 CJK 子集，减少操作系统中文字体差异。

测试中文字体的 family 为 `Gromacs Snapshot CJK`，仅由质量脚本通过 `/__qa__/` 注入。它们不放入 `app/static` 或应用发布包；应用继续使用系统中文字体。固定测试字体只覆盖当前源码与测试中的字符，新文案出现缺字时应先更新字形子集。来源、许可、重建方式和 SHA-256 见 [测试资产说明](../tests/browser_assets/README.md)。

## 截图评审与更新

基线位于 `tests/browser_baselines/`。默认检查要求所有基线存在、图片尺寸一致，并比较 RGB 像素；单通道差异超过 24 的像素计入变化比例，当前允许比例为 0.3%。超过限制时输出高亮差异图。实际阈值以 `tests/browser_budgets.json` 为准。

CI 只比较基线，绝不运行 `--update-snapshots`。缺失基线应使检查失败，不能通过自动生成来消除失败。

有意更改界面时，按以下流程更新：

1. 先运行默认检查，查看实际截图、差异图和对应代码变更；确认布局、文本、焦点、明暗主题和手机视图均符合预期。
2. 对需要接受的视觉变化作出显式评审决定，再运行更新命令。该命令会替换本轮全部 12 张基线，提交前必须逐张检查差异。
3. 将经过评审的基线随代码提交，再运行默认检查，确认比较通过；不要在 CI 中添加更新参数。

```bash
python scripts/check_frontend_quality.py --artifact-dir runtime/frontend-quality --update-snapshots
python scripts/check_frontend_quality.py --artifact-dir runtime/frontend-quality
```

`--skip-visual` 可在准备基线时单独检查可访问性和预算；它不能代替最终截图回归，也不用于 CI。

## 可访问性、预算与报告

axe-core 4.10.3 检查 WCAG 2 A/AA、2.1 A/AA 和 2.2 AA 对应的自动规则。发现的违规写入 `accessibility.json`，检查要求零违规。自动规则不能覆盖全部可访问性要求，键盘操作、焦点顺序、屏幕阅读器和内容可理解性仍需人工验证。

`tests/browser_budgets.json` 管理以下上限：

| 指标 | 当前上限 | 测量含义 |
| --- | --- | --- |
| JavaScript | 384 KiB | `app/static` 中 JS 源文件字节总量 |
| CSS | 160 KiB | CSS 源文件字节总量 |
| 应用字体 | 512 KiB | 应用 WOFF2 总量，不含测试 CJK 字体 |
| 合成页面准备时间 | 5,000 ms | 导航后至合成任务列表和编辑字段准备就绪 |
| 启动期最长主线程任务 | 1,000 ms | 准备阶段收集的 Long Tasks |

资源总量与逐场景启动数据写入 `performance.json`。这些是受控 Chromium 合成环境的实验室回归预算，既不是压缩后的实际网络传输量，也不是现场 Core Web Vitals。机器负载、浏览器版本和运行环境会影响计时；比较性能时使用相同环境、多次测量，并结合用户操作场景评估。调整预算需要说明原因和测量证据，不能仅为消除失败而放宽。

图表计数及局部耗时另存为 `plot-rendering.json`。需要对比修改前代码时，可传入保存的旧 `plots.js`：

```bash
python scripts/check_plot_rendering_browser.py --baseline-module /path/to/previous-plots.js --output runtime/frontend-quality/plot-rendering.json
```

该选项仅收集旧实现的数据，行为断言仍针对当前实现执行。完整质量检查不需要启动真实服务。


## 恢复、原生平台与真实引擎检查

```bash
python scripts/check_resilience_browser.py
python -m playwright install --with-deps chromium firefox
python scripts/check_platform_browser.py --artifact-dir runtime/frontend-quality/platform
python scripts/check_live_browser.py --gmx /path/to/gmx --artifact-dir runtime/frontend-quality/live
```

恢复测试使用拦截 API，覆盖产品初始化 503 后重试且保留 File/参数、两个真实标签页的共享草稿冲突选择、2505 项输出的全量搜索和分页、文件分组展开，以及 URL 后退与刷新恢复。草稿检测依赖 localStorage 比较和 storage 事件，不提供跨进程事务锁。

平台测试在 Linux 与 Windows CI 上分别运行 Chromium、Firefox，使用系统中文字体，检查 1440、768、390 像素布局、减少动画、高对比度、触控上下文和输入焦点。半宽视口模拟 200% 缩放后的布局压力；聚焦后缩小视口高度模拟键盘可视区域。两者均不是实际浏览器缩放、手机软键盘或物理设备验收。原生截图与 `platform-browser.json` 保存为 CI 附件；固定 CJK 视觉基线只在另一组质量检查使用。

真实链路脚本启动临时运行目录中的 API 和 worker，在导入应用前替换设置加载器，避免仓库的 settings.json 覆盖测试隔离。它生成 64 原子氩体系 TPR，通过真实浏览器选择文件、获取预览、确认提交、运行 CPU GROMACS，并验证原生 SSE 完成事件、日志、结果包和最终结构下载。退出时停止测试子进程并移除临时任务；不接触已有服务、设置或任务。该检查在标签、手动、定时和相关 PR 的 GROMACS 集成任务中运行。

`live-browser.json` 记录本地冷/热导航、资源传输量和浏览器支持的 LCP、CLS、Long Tasks，仅用于实验室观察，不提供现场 INP。本次 Linux 本地一次测量 DOMContentLoaded 为冷启动 145 ms、热加载 43 ms；不同机器或负载不能直接比较。启动时不请求 AI API；打开 AI 后才读取配置，会话 ETag 命中时跳过响应体和前端重绘，服务器仍需构造公开会话内容。
