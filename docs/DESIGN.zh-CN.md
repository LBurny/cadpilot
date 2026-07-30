# CADPilot — MCP 设计文档

[English](DESIGN.md) | **中文**

本文档描述 CADPilot 的架构与关键设计决策。CADPilot 是一个 MCP（Model Context Protocol）服务器，让 AI 客户端能够驱动 FreeCAD。

## 1. 目标

* 让 AI 客户端**完整**掌控 FreeCAD：文档、参数化建模、约束草图、装配、定量验证。
* 压低 AI 客户端的**上下文开销**：精简的工具列表、简短的 docstring、文本优先的响应、按需获取的文档。
* 让每个变更**事务化、可回滚**，使 AI 代理能像人类设计师一样试错与回溯。
* 提供**数据驱动的空间感知**（锚点、拓扑、测量），让定位不依赖截图或猜测。

## 2. 两进程架构

```
AI 客户端（Claude Code、Cherry Studio 等）
   │  MCP over stdio
   ▼
cadpilot MCP 服务器（src/cadpilot，Python ≥ 3.12，经 uvx 运行）
   │  XML-RPC over TCP，localhost:9875
   ▼
FreeCAD 插件（addon/CADPilot，宿主在 FreeCAD 进程内）
   │
   ▼
FreeCAD 文档 / GUI
```

为什么是两个进程？

* **进程隔离**：MCP 服务器从不 import FreeCAD。它可以运行在任何地方（包括通过 `--host` 连接另一台机器），并且能在 FreeCAD 重启后存活 —— XML-RPC 客户端检测到断连后会重建代理并重试一次。
* **MCP 生命周期**：AI 客户端会随意启动和终止 stdio 服务器，而 FreeCAD 是长寿命的 GUI 应用；插件持有 CAD 状态，MCP 服务器可以来来去去。
* **版本兼容**：MCP 服务器优先从 `mcp.server.fastmcp` 导入 `FastMCP`（MCP 1.x），失败时回退到 `MCPServer`（MCP 2.x）。

默认传输超时 150 秒。socket 超时**不**重试 —— 操作可能仍在服务端执行中。

## 3. GUI 线程派发

FreeCAD 的文档树与 GUI API **不是线程安全的**，所有文档/GUI 操作必须运行在 FreeCAD 主（GUI）线程。XML-RPC 服务器运行在独立线程，因此插件负责跨线程摆渡：

* `dispatch_to_gui(task)` 把包装后的可调用对象放入共享队列，并阻塞 RPC 线程（默认 60 秒超时），直到 GUI 线程通过**逐调用响应队列**返回结果 —— 某次调用的超时绝不会污染后续调用的响应。
* GUI 线程通过 Qt 信号（`QueuedConnection`）被**立即**唤醒，另有 500ms 心跳定时器兜底。
* 排空循环在鼠标按住、弹窗或模态对话框打开时跳过当前节拍，MCP 任务不会打断 3D 导航或对话框。重入守卫防止任务内部的 `processEvents` 触发嵌套排空。
* 任务内的异常被捕获、记录到 FreeCAD 报告视图，并以错误字符串返回 —— 绝不会杀死派发循环。
* 停止时通过哨兵值抑制定时器重新调度，循环真正终止。

`execute_code` 的用户脚本运行在插件模块命名空间的**副本**中，用户代码里的赋值不会泄漏并污染 RPC 服务器自身的全局变量。

## 4. 工具面设计

### 4.1 统一 `cad()` 派发器

所有变更收敛到单个 `cad(operation=...)` 工具（仿 nsforge 的 `math()` 风格），而不是每个操作一个 MCP 工具：

* `create_object` / `edit_object` / `delete_object` / `batch`
* Part 特征：`boolean`（支持多工具体列表）、`fillet`、`chamfer`、`loft`、`sweep`、`mirror`、`pattern`、`move`
* Sketcher/PartDesign：`variables`、`sketch`、`pad`、`pocket`、`revolution`、`groove`、`thickness`、`draft`、`datum_plane`、`hull`

这样常驻工具列表保持精简 —— 这很重要，因为每个工具定义都会在 `tools/list` 时注入 AI 客户端的上下文。

### 4.2 Docstring 预算

每个 `@mcp.tool()` 的 docstring 在**每次**对话中都消耗上下文 token。因此 docstring 限制为 1–3 行摘要加简要参数；详尽的参数/语义参考放在 `tool_docs.py`，通过 `operation_help` 工具按需获取。有测试强制总 docstring 预算（< 14,000 字符）。

### 4.3 知识层级

提示词指引模型按序查询：① 自身知识 → ② `recall_patterns`（持久化的成功工作流模式库）→ ③ `inspect_freecad`（对象属性/方法或 API 文档的运行时内省）。成功的方法通过 `save_pattern` / `session_complete` 存回，系统由此积累项目专属知识。

## 5. 事务、会话与回滚

每个提交的变更都运行在 **FreeCAD 事务**（`doc.openTransaction`）内。在此之上，建模会话（`session_start` … `session_complete`）把每个变更记录为步骤：

* 操作、描述、参数、结果摘要
* `objects` 指纹（排序后的对象名），用于漂移检测
* 模型或用户附加的笔记（观察/假设）

于是 `session_rollback(to_step)` 就是 `doc.undo()` × N 加日志截断 —— 无需删除重建。被移除的步骤进入重做缓冲区，直到新步骤将其清空，与 FreeCAD 的重做语义一致。`execute_code` 步骤是非原子的，会阻塞回滚（除非强制执行）。

变更响应会自动审计：每次提交的 `cad()` 调用后重跑只读的连通性检查（`verify_assembly`），对断连孤岛追加警告；`session_status` 暴露风险（回滚后状态漂移、非原子步骤、文档已关闭）并给出下一步建议。审计绝不阻塞变更本身。

会话与模式以 JSON 持久化在 `$CADPILOT_HOME`（默认 `~/.cadpilot/`），原子写入（临时文件 + 重命名）。

## 6. Sketcher 与 PartDesign 操作

草图在单个事务中**原子**构建：几何（列表顺序即 GeoId）加约束，立即求解。结果报告 `dof` / `fully_constrained` / 求解器警告；冲突或畸形的规格回滚并返回求解器诊断（`ConflictingConstraints`、`RedundantConstraints`）。

值得注意的细节：

* `external: [[obj, "EdgeN"|"VertexN"], …]` 添加外部几何。只允许引用起点/终点（`mid`/`center` 会在求解时失败，因此提前拒绝）。体外目标通过幂等的 `PartDesign::SubShapeBinder` 自动桥接，因为 PartDesign 拒绝草图体外的外部几何。
* `datum_plane` 依附到原点平面或现有面；草图通过 `plane={"datum": name}` 依附。
* **依附融合**：对依附在实体面上的草图做 pad/pocket 时，操作直接作用于*该*实体 —— pocket 切割它，pad 融合进它。先 pad 再布尔减是错误做法。
* `hull` 构建视觉外壳：把 2–3 个视图轮廓草图沿各自法向拉伸后求交。结果是静态 `Part::Feature`（重载文档后存活）；同名重跑就地替换 Shape，便于迭代。
* 任何属性中以 `=` 开头的字符串都通过 ExpressionEngine 绑定，使全链路支持电子表格驱动的参数化（`variables` 操作）。

## 7. 几何感知与反馈

`measure_geometry` / `get_topology` / `check_interference` / `get_positioning_info` 是只读的 Shape 查询，派发到 GUI 线程但不开启事务：

* 所有坐标为**全局坐标** —— FreeCAD 的 Shape 内部位置已携带对象的 Placement，因此不做手工变换（v0.2 修复过一个双重变换 bug）。
* 拓扑列表按尺寸排序并分页（`limit`/`offset`）；面和边携带语义信息（类型、中心、法向、半径、轴向、起终点），供模型为后续特征操作挑选选择器。
* 舍入（4 位有效数字）只发生在报告边界；内部计算使用原始向量。

## 8. 空间定位与装配

AI 驱动 CAD 最难的问题是零件间的相对定位。CADPilot 用数据而非截图来解决：

* **锚点**（`get_anchors` / `set_anchors`）：每个对象的命名点 + 方向。自动推导（`bbox_center/min/max`、`com`、主圆柱面的 `axis_*`、最大平面的 `face*_center`），加上显式命名锚点 —— 以 JSON 存进 `App::PropertyString`，使用**局部**坐标，随 Placement 移动。
* **`assemble`**：配合列表 `{obj, anchor, target, target_anchor, mode: center|touch|axis, offset}` 在单事务内应用；每个锚点在移动后重新解析，报告逐配合残差（毫米 + 角度），超差即中止。
* **`align_shapes`**：单步面/边/顶点对齐（`touch` / `center` / `axis`）。
* **`verify_assembly`**：全文档审计 —— 悬空对象（最近邻距离）、干涉（公共体积）、显式锚点对检查，以及基于并查集的**接触图**，报告相对主装配体的孤岛。

### 装配会话（持久化关节）

`assembly_session` 是基于 FreeCAD 1.1 原生 Assembly 工作台的独立状态机：`start`（固定件）→ `add_component` → `mate` → `solve` → `verify` → `complete`，另有 `unmate` / `rollback(to_step)` / `status`。组件是持有 Placement 的 `App::Link`；关节持久化在文档中，移动父件后重新 solve 即可 reposition 子件。

关键实现规则（在 FreeCAD 1.1.3 上实地验证）：

* 必须先跑 `preSolve`（matchJCS），再做最终的单次 `solve(True)`；重复 solve 会损坏求解器状态。
* `link.Shape` 是全局坐标，但关节引用和 `findPlacement` 是零件局部坐标 —— 残差用几何方式测量（`distToShape` + 法向夹角），绝不用关节坐标系数学。
* 配合引用接受 `face` / `anchor` / `point`；面上最近的顶点决定配合落点（GUI 点击语义）。
* `trim={"winner": "inserted"|"base"}` 执行声明式优先级裁剪：在败方 link 的局部坐标系内烘焙非破坏性切割，并重指向 link。每个步骤预计算自己的 undo 负载，回滚聚合为单个 RPC 规格原子执行。
* MCP 侧在记录任何状态前先校验插件结果（`{"success": false}`），失败的 RPC 绝不会留下半截的会话状态。

## 9. 截图与 token 经济

截图是**可选项，默认关闭**：

* 单次调用用 `with_screenshot` 开启；`--with-screenshots` 设为默认开；`--only-text-feedback` 是硬性关闭，覆盖一切（适合纯文本模型）。
* 变更类工具在同一次 RPC 派发中内联截图（单往返）；对旧版插件回退为第二次调用。
* 默认截图长边 768px 封顶以省 token；`get_view` 接受显式尺寸。
* 部分视图类型（TechDraw、Spreadsheet）无法截图 —— 插件返回 `None`。

## 10. 后台任务

`execute_code_async` 在守护线程中运行后台安全代码（耗时 OCCT 计算）并返回 `task_id`；状态、`task_print()` 输出和异常栈保存在有界的内存注册表（FIFO，上限 50）中，经 `get_task_result` 轮询。后台任务绝不重定向 `sys.stdout`（进程级竞态）；后台代码通过 `task_print()` 或线程安全的 `FreeCAD.Console` 汇报。后台代码不得触碰文档/GUI —— 那是 `execute_code` 的职责。

## 11. 安全

* RPC 服务器默认绑定 `localhost`；远程访问是显式开关（**Remote Connections** 切换后绑定 `0.0.0.0`）。
* 开启远程后，带过滤的 XML-RPC 服务器强制执行 IP/CIDR **白名单**（默认 `127.0.0.1`）；非法条目在配置时即被拒绝。
* `execute_code` **设计上**就是任意代码执行 —— 它是让系统完备的逃生舱。因此威胁模型把任何可达的 MCP 客户端视为完全可信，这正是远程访问默认关闭且有白名单守护的原因。
* `--host` 的值在启动时校验（IPv4/IPv6/主机名）。

## 12. 可靠性与兼容性

* **重连**：客户端在死连接错误时重建 XML-RPC 代理并重试一次（FreeCAD/插件重启场景）。
* **优雅降级**：新客户端对旧插件自动回退（单发截图 RPC、可选参数）；旧客户端对新插件继续可用。
* **名称净化**：FreeCAD 会净化文档/对象名（空格转下划线、去重）。RPC 处理器始终返回*实际*名称而非请求名称。
* **版本容忍**：thickness/draft 同时支持 FreeCAD ≥ 1.1 的 LinkSub `Base` 布局和 ≤ 1.0 的 `Faces` 属性（运行时探测）；`Part::Mirroring` 与 `Part::Mirror` 通过探测择优。
* **热重载**：插件可在不重启 FreeCAD 的情况下重载（停 RPC 服务器 → 延迟 `importlib.reload` 全部子模块 → 启动），并备有重启与停服排空发生竞态时的修复路径。

## 13. 测试策略

pytest 套件（约 200 个测试）基于一个记录每次调用的假 XML-RPC 连接，覆盖 MCP 服务器侧：响应整形、截图策略优先级、重连行为、会话/模式状态机、`cad()` 派发、装配状态机（含 RPC 失败路径）、引导启发式，以及 docstring 预算。插件侧需要真实 FreeCAD，由 `tests/live_sketch_verify.py` 做端到端验证：构建参数化支架，检查表达式传播、失败诊断和撤销。
