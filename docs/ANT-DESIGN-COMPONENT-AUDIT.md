# Ant Design 组件选型：GROMACS 桌面工作台

2026-11-20 · 这是选型记录，不是功能发布清单。核对了[官网组件总览](https://ant.design/components/overview/)、下列组件的示例及 API，并用发布包做了两个隔离的 Chromium 实验。

**结论：原样取用官方 SVG；借用数据展示和编辑组件的交互；暂不把 `antd` 加入产品依赖。** 本项目的 HTML 模板由原生 ES 模块操作，没有 React 挂载点。`antd` 的组件不是可粘贴的 HTML 标签：本次核对的 [`antd@6.7.1` 包清单](https://registry.npmjs.org/antd/6.7.1) 要求 React 和 React DOM 均 ≥18。能单独挂载，不等于适合为一个图标或一个按钮引入它们。

## 一、无需 React，哪些素材真的可以直接用？

[官网图标目录](https://ant.design/components/icon/)展示图形；实际取文件用官方 [Ant Design Icons](https://github.com/ant-design/ant-design-icons) 的 [`@ant-design/icons-svg@4.6.0`](https://registry.npmjs.org/@ant-design/icons-svg/4.6.0)。不要复制官网演示里的 React 标签，也不用运行时安装图标包。

| 已验证的独立 SVG | 工作台对应位置 | 采用边界 |
| --- | --- | --- |
| `diff.svg`（DiffOutlined） | 任务详情 → 更多操作 → 比较参数 | 第一选择。图形补充「比较参数」文字，不把它变成靠猜的纯图标按钮。 |
| `branches.svg`（BranchesOutlined） | 已有轨迹分析 → 来源任务 | 标识派生任务和来源的关系；不用于普通的上一页/下一页。 |
| `file-done.svg`（FileDoneOutlined） | 结果摘要 → 已生成的报告入口 | 表示有报告，不表示科学结果有效或所有检查通过。没有报告不显示这个入口。 |

验证的是 `inline-namespaced-svg/outlined/` 下带 `xmlns` 的**原始文件**，在同源外部 CSS 中作为 `mask`，用 `currentColor` 着色。三个 SVG 共 2,344 字节。实际引入时只收录选中的文件，随包保留[上游 MIT 版权与许可](https://github.com/ant-design/ant-design-icons/blob/master/LICENSE)，在第三方清单登记。已有 Lucide 的关闭、保存、上传、搜索等图标不换；不要在同一排用两套图形表达同一动作。

## 二、哪些组件值得带到工作台？

下表的“采用”指采用该组件的**交互**，不是已经安装同名组件。

| 优先级 / 官网组件 | 项目里的实际位置 | 怎么用、有什么限制 |
| --- | --- | --- |
| **先做** · [Table](https://ant.design/components/table/) | 比较两个任务的参数 | 接口已经只返回差异以及两侧任务名；现有确认框把值用箭头串在一行，最多显示 30 项。先用语义化三列表格（参数路径、任务 A、任务 B）显示全部差异，可在结果内查找；保留 `0`、`false` 和长数组的原意。不要加接口无法支持的“显示相同项”开关。若以后需要多列排序、固定列等，再评估官方 Table。 |
| **先做** · [Tag](https://ant.design/components/tag/) / [Select](https://ant.design/components/select/) | 实验记录 → 标签 | 目前是逗号分隔的文本。可复用现有中英文逗号解析、去重和保存逻辑，渲染为可删除标签，删除按钮用“移除标签 WT”这类可读名称；中文输入法组字时不能误提交。只有确实要从大量已有标签中搜索、补全时才考虑 `Select` 的 tags 模式。 |
| **值得做** · [Timeline](https://ant.design/components/timeline/) | 任务详情 → 执行记录 | `step_runs` 有实际步骤、状态及可用的起止时间。按发生顺序排，失败、重跑各留一条；没有结束时间就不填。与新建任务的 [Steps](https://ant.design/components/steps/) 分工：一个讲实际发生了什么，一个讲配置走到哪一步。 |
| **按需** · [Splitter](https://ant.design/components/splitter/) | 结构预览与参数的桌面双栏 | 有长参数、要放大分子时才有收益。先试这一对面板，不改全站侧栏；规定最小宽度，提供键盘调宽、重置，验证预览画布同步调整。官方 `lazy` 是松手后才改变面板大小，可用于避免拖动时持续重绘。 |
| **轻量即可** · [Avatar](https://ant.design/components/avatar/) / [Badge](https://ant.design/components/badge/) | AI 对话身份 / 任务状态 | 头像回答“谁说的”，徽标回答“状态如何”。AI 已有身份图标和角色文本；如补头像，按一组消息显示一次即可。任务状态已有徽标，维持“文字＋状态色”，不加无数据来源的红点或头像在线提示。 |
| **已有对应实现** · [Card](https://ant.design/components/card/) / [Descriptions](https://ant.design/components/descriptions/) / [Divider](https://ant.design/components/divider/) / [Button](https://ant.design/components/button/) | 协议模板卡片 | 卡片、键值说明、细分割线、主次操作都已具备。继续用现有 `article`、`dl` 和 `button`；不值得为了改组件名再造一版。 |
| **暂不替换** · [Tree](https://ant.design/components/tree/) / [Empty](https://ant.design/components/empty/) | 输出文件 | 目录树需要真实的节点和子节点加载语义。当前接口按路径搜索、分页；把单页拼成树会使用户误以为已看到了整个目录。保留当前的搜索、分组、分页和分情形空状态。 |
| **暂不引入** · [Tooltip](https://ant.design/components/tooltip/) / [Popover](https://ant.design/components/popover/) / [Tour](https://ant.design/components/tour/) | 参数说明和首次使用 | 短提示才适合轻提示；净电荷、执行条件等影响科研决策的说明保留可由键盘打开、可长期阅读的帮助，不藏进悬停气泡。不叠加全站强制导览。 |

## 三、实测：“局部用官方 React 组件”到底差在哪？

产品的安全响应头要求 `script-src 'self'; style-src 'self'`。这里的“本地”指同源静态文件，不是首次打开时去 CDN 下载。下面均为临时目录中的生产模式小样，页面和资源请求由 Playwright 同源拦截，使用与应用相同的 CSP；未启动模拟、未改产品依赖。

| 实验 | 结果 |
| --- | --- |
| 仅加载外部 CSS 和上面三个 SVG，不加载页面 JS | Chromium 在 20×20 图标区域渲染出三种图形；零 CSP 违规，零应用 JS 请求。 |
| React 与 React DOM 19.2.0、`antd` 6.7.1；单个挂载点，两个差异行和一个可删除标签 | `Table` 点击表头可改变行序，`Tag` 可关闭。默认模式出现 `style-src-elem` 拦截；按[官方 Zero Runtime 示例](https://ant.design/docs/react/customize-theme/)使用 `theme={{ zeroRuntime: true }}` 加同源 `antd/dist/antd.css` 后，这个组合仍有内联样式被拦截。**交互可用不是样式和 CSP 已验收**；不据此断言所有组件在所有配置下都有问题。 |
| 以上小样的构建产物 | esbuild 0.28.2、`--bundle --minify`、生产模式；导入 `ConfigProvider, Table, Tag`，JS 共 **811,618 B（793 KiB，含 React）**，官方完整 CSS **1,019,422 B（996 KiB）**。这是本小样的未压缩传输编码前文件大小，不是单个 Table 的成本，也不是按需提取 CSS 后的下限。 |

仓库按静态文件原始字节数做预算，不按 gzip 大小。核对时 JS 已用 353,158 / 393,216 B（余约 39 KiB），CSS 已用 151,521 / 163,840 B（余约 12 KiB）。仅改为懒加载并不能消掉发布体积。若将来确有重型表格需求，可另做一个只在打开时挂载的试验：按组件预提取样式、对齐灰绿明暗主题、核验键盘与关闭后的焦点、确认无 CSP 违规，再讨论是否给独立模块设预算；不为此开放 `unsafe-inline`。

**取舍顺序：**先选配 `diff.svg` 和原生参数差异表，再处理可移除标签，最后根据执行记录的使用情况决定是否加时间线。头像、徽标、分割线不作为单独引库的理由。

**实施进展：**第一项已落地：`DiffOutlined` 和原生参数差异表已接入「运行概览 → 更多操作 → 比较参数」。可删除标签和时间线仍为候选项。
