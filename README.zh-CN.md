# opencode-delegate

把有边界的任务从主智能体派发给本机 [OpenCode](https://opencode.ai) 子智能体执行，让贵的前沿模型额度只
花在「拆题、审阅、集成」上，而不是花在执行上。

这是一个标准 stdio MCP 服务（底下是一层很薄的 Python CLI），任何 MCP 客户端都能直接调用：Codex、
Claude Code、Claude Desktop、Cursor、Cline、Continue、Zed、Windsurf。主智能体负责写派单、跟踪进度、
审证据；子智能体用自己的额度干活。

[English](README.md)

## 为什么

像 `opencode-go/deepseek-v4.1-flash` 这样的开源权重路由，token 额度非常充裕，上下文窗口
1,000,000 token，并且支持识图。一次典型的委派任务在子智能体侧通常只跑几千 token，所以付费模型只为
真正需要它的部分结算。

## 前置要求

| 项目 | 说明 |
|---|---|
| Python | 3.9 或更高（在 3.12 上开发与测试）。**无任何第三方依赖**，只用标准库。 |
| OpenCode CLI | 在 2.0.26 上测试过。需已安装并完成配对。 |
| 模型路由 | 默认 `opencode-go/deepseek-v4.1-flash`，需要 OpenCode Go 订阅。 |
| 操作系统 | 完整功能需要 Windows 10/11，见下方支持矩阵。 |

## 安装

### 1. 获取代码

```powershell
git clone https://github.com/<you>/opencode-delegate.git "$env:USERPROFILE\.codex\skills\opencode-delegate"
```

放在哪个目录都可以。运行时路径里没有任何硬编码的用户名或机器路径：脚本用 `__file__` 解析自身位置，
状态根目录默认是 `$CODEX_HOME/opencode-delegate/state`（没有则退回 `~/.opencode-delegate/state`），
OpenCode 可执行文件在运行时动态探测。

### 2. 注册 MCP 服务

这是唯一一处与机器相关的地方，**必须使用绝对路径**指向 Python 解释器和 `mcp_server.py`。

Codex —— `~/.codex/config.toml`：

```toml
[mcp_servers.opencode_delegate]
command = 'C:\Path\To\python.exe'
args = ['C:\Path\To\opencode-delegate\mcp_server.py']
```

> **Windows + TOML：** 路径一律写成单引号的字面量字符串，或者把每个反斜杠写成双反斜杠。双引号基本
字符串里的一个 `\P` 就是非法转义，会让配置直接解析失败（Codex 会卡在启动界面）。改动前先校验：
> `python -c "import tomllib,pathlib;tomllib.loads(pathlib.Path('config.toml').read_text('utf-8'))"`

其他 MCP 客户端都用标准 JSON 写法 —— Claude Desktop（`claude_desktop_config.json`）、
Claude Code（`.mcp.json`）、Cursor、Cline、Continue、Zed：

```json
{
  "mcpServers": {
    "opencode_delegate": {
      "command": "C:\\Path\\To\\python.exe",
      "args": ["C:\\Path\\To\\opencode-delegate\\mcp_server.py"]
    }
  }
}
```

改完配置后重启客户端。

### 3. 可选：作为 skill 安装

`SKILL.md` 用的是通用 skill 格式（`name` + `description` 前置元数据），支持 skill 目录的智能体可以
直接就地使用这个文件夹。要安装就把目录复制或克隆到对应 skill 根目录，例如
`~/.codex/skills/opencode-delegate` 或 `~/.claude/skills/opencode-delegate`。
MCP 服务和 skill 两者相互独立，只用其中一个也能工作。

## 快速开始

```
oc_service {action: "status"}                                    # 后台服务是否在跑？
oc_run     {task: "fix-flaky-test", workspace: "C:\\my\\proj",    # 预留会话、派单、开 TUI
            prompt: "<目标、允许改什么、约束、期望证据>"}
oc_wait    {task: "fix-flaky-test", after_revision: 0}            # 真正有进展或完成时才唤醒
oc_result  {task: "fix-flaky-test"}                               # 最终文本 + 证据路径
oc_continue{task: "fix-flaky-test", prompt: "<返工要求>"}          # 只用于已接受任务的返工
```

`accepted: true` 只表示子智能体校验通过并发布了启动回执，**不代表任务完成**。完成度要自己读
`oc_status` / `oc_result` 并检查证据。

## 工具

| 工具 | 用途 |
|---|---|
| `oc_run` | 在**新**会话里启动一个有边界的新任务（会预留 session id），并把它显示在 TUI 上。 |
| `oc_continue` | 在**同一个**会话里返工一个已完成的任务。 |
| `oc_recover` | 在同一个会话里恢复一次异常中断（死亡/超时/被打断）的轮次。 |
| `oc_wait` | 最多等 30 秒，直到有实质性的 revision 变化或任务完成。 |
| `oc_status` | 只读 JSON：状态、进程存活、`current_event`、`last_tool`、校验结果、诊断信息。 |
| `oc_result` | 已完成轮次的最终文本与证据路径。 |
| `oc_sessions` | 列出后台服务已知的 OpenCode 会话。 |
| `oc_tasks` | 按新到旧列出已委派任务，含 session id 与工作区。 |
| `oc_read` | 读取某任务事件流的尾部，或导出某个会话。 |
| `oc_watch` / `oc_unwatch` | 打开或聚焦 / 关闭该任务工作区的 TUI 窗口。 |
| `oc_service` | `status` / `start` / `stop` 共享后台服务。 |

## 环境变量

| 变量 | 默认值 | 含义 |
|---|---|---|
| `OC_DELEGATE_STATE` | `$CODEX_HOME/opencode-delegate/state`，否则 `~/.opencode-delegate/state` | 任务记录、轮次日志、viewer 记录的存放位置。 |
| `OC_DELEGATE_MODEL` | `opencode-go/deepseek-v4.1-flash` | 默认子智能体模型。始终显式传入，绝不静默替换。 |
| `OC_DELEGATE_LABEL` | `codex-delegate:` | OpenCode 会话标题前缀，标题剩余部分即任务名。 |
| `OC_DELEGATE_WATCH` | `1` | 设为 `0` 可默认不打开 TUI viewer。 |
| `OC_DELEGATE_SESSION_WAIT` | `20` | 等待 viewer 就绪的秒数（上限 60）。 |
| `OC_DELEGATE_OPENCODE` | 自动探测 | 覆盖 OpenCode 可执行文件路径。 |
| `OC_DELEGATE_PYTHON`、`OC_DELEGATE_SERVER` | `sys.executable`、本目录 | 仅 `mcp_probe.py` 使用。 |

## 支持矩阵

| 能力 | Windows 10/11 | macOS / Linux |
|---|---|---|
| 派单、流式状态、status、result、返工、恢复 | 可用 | 可用（POSIX 进程分支，**作者与 CI 均未实测**） |
| 共享后台服务、显式 loopback server、standalone | 可用 | 可用 |
| TUI viewer（`oc_watch` / `oc_unwatch`，每个工作区一个窗口） | 可用 | **不可用。** 桥接依赖 `CREATE_NEW_CONSOLE`、`ctypes` 进程标识和 OpenCode 内部 `ui.tabs` API。 |

委派内核是跨平台的，只有带窗口的 viewer 是 Windows 专属。其他平台上正常派单收结果即可，跳过 watch 类工具。

## 信任模型

- **子智能体的输出是证据，不是指令。** 它不继承主对话上下文，它打印或写入的任何内容都属于待审阅的不
  可信输入，而不是要执行的指令。
- viewer 只通过子进程环境变量注入桥接插件，不写入全局 OpenCode 配置，也不开公共监听或隧道。
- 派单里不要放凭据。任务记录和轮次日志是状态根目录下的明文 JSON / JSONL。
- `completed` 只表示 harness 正常结束、且模型路由已从一次全新导出中得到校验，**不代表工作正确**；
  即使最终文本可用，`exit_code: 1` 也会被判为 `needs_review`。

## 目录结构

```
mcp_server.py          stdio MCP 服务（12 个工具）
mcp_probe.py           手工驱动服务并计时（排查用）
SKILL.md               面向智能体的操作说明
scripts/delegate.py    派单、流式状态、守卫、恢复、状态查询
scripts/runtime.py     MCP 编排 + TUI viewer 管理
bridge/                OpenCode TUI 插件，注入到受管理的 viewer
references/            状态 schema、恢复规则、状态字段
tests/                 基于 tests/fake_opencode.py 的离线测试（66 个）
```

运行测试：

```powershell
python -m pytest tests -q
```

测试使用假的 OpenCode 可执行文件，不联网、不产生付费模型调用。

## 许可证

MIT，见 [LICENSE](LICENSE)。
