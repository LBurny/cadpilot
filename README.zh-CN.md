# CADPilot

[English](README.md) | **简体中文**

**AI 驾驶 FreeCAD。** CADPilot 是一个 MCP（Model Context Protocol）服务器，让 AI 客户端（Cherry Studio、Claude Code、ZCode、Kimi Code、Claude Desktop 等）完全掌控 [FreeCAD](https://www.freecad.org/)：创建文档、构建全约束草图、运行参数化特征、以持久化关节装配零件、验证几何 —— 全部通过 XML-RPC 上的工具调用完成。

## 安装

### 第一步：安装 FreeCAD 插件

FreeCAD 插件目录：

* Windows：`%APPDATA%\FreeCAD\Mod\`
* macOS：
  * FreeCAD 1.1：`~/Library/Application Support/FreeCAD/v1-1/Mod/`
  * FreeCAD 1.0：`~/Library/Application Support/FreeCAD/v1-0/Mod/`
* Linux：
  * Ubuntu：`~/.FreeCAD/Mod/` 或 `~/snap/freecad/common/Mod/`（snap 安装）
  * Debian：`~/.local/share/FreeCAD/Mod`
  * Arch / CachyOS（`extra/freecad` 的 FreeCAD 1.1）：`~/.local/share/FreeCAD/v1-1/Mod/`

把 `addon/CADPilot` 目录复制到插件目录：

```bash
git clone https://github.com/LBurny/cadpilot.git
cd cadpilot

# Linux（Ubuntu/Debian）
mkdir -p ~/.FreeCAD/Mod/
cp -r addon/CADPilot ~/.FreeCAD/Mod/

# macOS（FreeCAD 1.1）
mkdir -p ~/Library/Application\ Support/FreeCAD/v1-1/Mod/
cp -r addon/CADPilot ~/Library/Application\ Support/FreeCAD/v1-1/Mod/

# Windows（PowerShell）
Copy-Item -Recurse addon/CADPilot "$env:APPDATA\FreeCAD\Mod\"
```

重启 FreeCAD，从工作台列表选择 **CADPilot**，点击 **CADPilot** 工具栏中的 **Start RPC Server** 启动 RPC 服务器。如需每次启动 FreeCAD 时自动运行，在 **CADPilot** 菜单中勾选 **Auto-Start Server**。

### 第二步：安装 MCP 服务器

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后无需显式安装 —— `uvx` 首次使用时自动从 PyPI 拉取：

```bash
uvx cadpilot
```

或用 pip：`pip install cadpilot`

服务器通过 stdio 讲 MCP 协议，并连接 FreeCAD 插件在 `localhost:9875` 上的 XML-RPC 服务 —— 通常不需要手动运行，AI 客户端会通过下面的配置自动启动它。

## 客户端配置

所有支持 stdio 的 MCP 客户端使用同一份配置 —— **command** 为 `uvx`，**args** 为 `["cadpilot"]`：

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uvx",
      "args": ["cadpilot"]
    }
  }
}
```

* **Claude Code**：`claude mcp add cadpilot -- uvx cadpilot`
* **Kimi Code**：`kimi mcp add cadpilot -- uvx cadpilot`
* **Cherry Studio**：设置 → MCP 服务器 → 添加服务器，类型 `stdio`，命令 `uvx`，参数 `cadpilot`
* **ZCode**：把上面的 JSON 片段加入 `~/.zcode/cli/config.json` 的 `mcp.servers` 下
* **Claude Desktop / Cursor 等**：把 JSON 片段粘贴到对应客户端的 MCP 配置文件

### 启动选项

工具响应默认纯文本 —— 截图按需开启（单次调用传 `with_screenshot=true` 或用 `get_view` 工具），token 占用低。启动参数：

* `--with-screenshots`：每个变更/读取类工具响应都附带截图（适合多模态模型）
* `--only-text-feedback`：永不返回截图，即使调用方请求（纯文本模型的硬保证）
* `--host <ip>`：连接另一台机器上的 FreeCAD 实例

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uvx",
      "args": ["cadpilot", "--with-screenshots"]
    }
  }
}
```

开发模式（从本地克隆运行而非 PyPI）：`"command": "uv"`，`"args": ["--directory", "/path/to/cadpilot", "run", "cadpilot"]`。

## 远程连接

RPC 服务器默认只监听 `localhost`。要从局域网内另一台机器控制 FreeCAD：

1. 在 **CADPilot** 工具栏勾选 **Remote Connections**（下次重启后绑定 `0.0.0.0`），并点击 **Configure Allowed IPs** 输入允许连接的 IP 或 CIDR 网段（逗号分隔），例如 `192.168.1.100, 10.0.0.0/24`。只有列出的地址可以连接；修改设置后需重启 RPC 服务器。
2. 让 MCP 服务器指向该机器：`"args": ["cadpilot", "--host", "192.168.1.100"]`。

## 工具

* **`cad`** —— 统一 CAD 变更工具：`create_object` / `edit_object` / `delete_object` / `batch`，参数化特征（`boolean` / `fillet` / `chamfer` / `loft` / `sweep` / `mirror` / `pattern` / `move`），Sketcher/PartDesign 操作（`variables` / `sketch` / `pad` / `pocket` / `revolution` / `groove` / `thickness` / `draft` / `datum_plane` / `hull`）。边/面选择器由 `get_topology` 提供；每个变更都在事务内执行、可回滚。
* **`execute_code` / `execute_code_async` / `get_task_result`** —— 在 FreeCAD 中执行任意 Python（GUI 线程安全），或对耗时 OCCT 计算使用后台执行 + 轮询。
* **建模会话** —— `session_start` / `session_status` / `session_get_steps` / `session_rollback` / `session_redo` / `session_add_note` / `session_pause` / `session_resume` / `session_list` / `session_complete`：步骤记录 + 基于 FreeCAD 原生事务撤销的回滚。
* **知识层级** —— `save_pattern` / `recall_patterns`（可复用工作流记忆）、`inspect_freecad`（运行时 API 内省）、`operation_help`（按需获取操作参考文档）。
* **几何感知** —— `measure_geometry` / `get_topology` / `check_interference` / `get_positioning_info`：每步建模后的定量反馈。
* **装配** —— `get_anchors` / `set_anchors` / `assemble` / `align_shapes` / `verify_assembly` 提供数据驱动的空间定位；`assembly_session` 提供基于配合的装配状态机（FreeCAD 1.1 Assembly 工作台持久化关节）与声明式优先级裁剪。
* **文档与视图** —— `create_document` / `list_documents` / `get_objects` / `get_object` / `get_view`（截图默认长边 768px 封顶，节省 token）。

这些工具背后的架构见[设计文档](docs/DESIGN.zh-CN.md)；可在 FreeCAD 中打开演示模型 [`examples/ModernBicycle.FCStd`](examples/ModernBicycle.FCStd) 试用。

## 文档

* MCP 设计文档：[English](docs/DESIGN.md) | [中文](docs/DESIGN.zh-CN.md)
* [更新日志](CHANGELOG.md)

## 开发

```bash
git clone https://github.com/LBurny/cadpilot.git
cd cadpilot
uv sync
uv run pytest          # 运行测试套件
uv run ruff check .    # 代码检查
uv run cadpilot        # 从源码运行 MCP 服务器
```

## 致谢

本项目最初基于 [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp)（作者 Shirokuma (k tanaka)）开发 —— 非常感谢原作者的工作，本项目部分内容参考并衍生自该项目（MIT License）。

## 许可证

MIT —— 见 [LICENSE](./LICENSE)。
