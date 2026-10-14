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
| 模型路由 | 用你 OpenCode 里已经配好的那个。推荐 `opencode-go/deepseek-v4.1-flash`，需要 OpenCode Go 订阅。 |
| 操作系统 | 完整功能需要 Windows 10/11，见下方支持矩阵。 |

## 安装

### 1. 获取代码（仅源码安装需要；npm 安装跳过这一步）

```powershell
git clone https://github.com/Upstream17/opencode-delegate.git "$env:USERPROFILE\.codex\skills\opencode-delegate"
```

放在哪个目录都可以。运行时路径里没有任何硬编码的用户名或机器路径：脚本用 `__file__` 解析自身位置，
状态根目录默认是 `$CODEX_HOME/opencode-delegate/state`（没有则退回 `~/.opencode-delegate/state`），
OpenCode 可执行文件在运行时动态探测。

### 2. 注册 MCP 服务

两种方式。**A** 完全不写路径；**B** 直接跑这份检出。

**A. 走 npm 启动器（无路径、无需转义）**

自带的 `bin/opencode-delegate.js` 会探测 Python 3.9+ 并把 `mcp_server.py` 交给它。使用 `mcpServers`
配置结构的客户端可以这样注册；其他客户端按自身格式配置：

```json
{
  "mcpServers": {
    "opencode_delegate": { "command": "npx", "args": ["-y", "opencode-delegate@0.4.1"] }
  }
}
```

机器上仍然需要 Python 3.9+；启动器会依次探测 `py -3`、`python`、`python3`，并认 `OC_DELEGATE_PYTHON`。
在发布到 npm 之前，可以直接指向仓库：`"args": ["-y", "github:Upstream17/opencode-delegate"]`。

**B. 直接从检出运行（绝对路径）**

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

Claude Desktop、Claude Code 等客户端使用下面的 `mcpServers` 结构。配置文件的位置和格式因客户端而异，不能一概而论：

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

Zed 应在设置里使用 `context_servers`：

```json
{
  "context_servers": {
    "opencode_delegate": { "command": "npx", "args": ["-y", "opencode-delegate@0.4.1"] }
  }
}
```

Codex 走 npm 时使用 TOML：

```toml
[mcp_servers.opencode_delegate]
command = 'npx'
args = ['-y', 'opencode-delegate@0.4.1']
```

Windows 上若客户端不能直接执行 `npx.cmd`，可以将 `command` 设为 `node.exe` 的绝对路径，
并在 `args` 里填写已安装包的 `bin/opencode-delegate.js` 路径。按客户端自身规则注册，不默认添加 shell。
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
oc_status  {task: "fix-flaky-test"}  # read the returned revision string
oc_wait    {task: "fix-flaky-test", after_revision: "<revision from oc_status>"}            # 真正有进展或完成时才唤醒
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

## 选择模型

默认不钉任何模型。不传 `model` 时，子智能体跑在你 OpenCode 自己配置的模型上，任务记录会写下实际使用的路由：

```jsonc
"verification": { "status": "adopted", "providerID": "opencode-go", "id": "deepseek-v4.1-flash" }
```

在 `oc_run` 上传 `model` 可以为单个任务钉定模型；设 `OC_DELEGATE_MODEL` 可以改所有任务的默认值。已钉定的任务
会保持自己的路由：`oc_continue` 与 `oc_recover` 自动继承，传一个不一样的模型会被拒绝，而不是被静默换掉。
`verification.status` 为 `verified` 表示请求的路由与导出结果一致，`adopted` 表示没有请求路由、直接采纳实际
结果，而 `mismatch` / `inconsistent` / `unverifiable` 表示证据不支持这次运行——这几种会把该轮标记为待审阅。

**不钉模型时，需要 OpenCode 自己能选到可用模型。** 如果遇到 provider/auth 错误，请检查
实际选择的模型、登录凭据、订阅权限与端点。403 并不一定意味着缺少默认模型。可以将自己有权限
使用的模型合并进现有 OpenCode 配置，保留其他字段，例如：

```jsonc
"model": "opencode-go/deepseek-v4.1-flash"
```

也可设 `OC_DELEGATE_MODEL`，仅给本 MCP 钉定默认模型。安装器和启动器不会修改 OpenCode 配置，
也不会在失败后静默切换 provider。

## 环境变量

| 变量 | 默认值 | 含义 |
|---|---|---|
| `OC_DELEGATE_STATE` | `$CODEX_HOME/opencode-delegate/state`，否则 `~/.opencode-delegate/state` | 任务记录、轮次日志、viewer 记录的存放位置。 |
| `OC_DELEGATE_MODEL` | 未设置 | 钉定一个默认子智能体模型。未设置时交给 OpenCode 自己的配置决定。 |
| `OC_DELEGATE_LABEL` | `codex-delegate:` | OpenCode 会话标题前缀，标题剩余部分即任务名。 |
| `OC_DELEGATE_WATCH` | Windows 为 `1`，其他平台为 `0` | 设为 `0` 禁用 viewer；非 Windows 的 watch 请求在创建进程前返回不支持。 |
| `OC_DELEGATE_SESSION_WAIT` | `20` | 等待 viewer 就绪的秒数（上限 60）。 |
| `OC_DELEGATE_OPENCODE` | 自动探测 | 覆盖 OpenCode 可执行文件路径。 |
| `OC_DELEGATE_PYTHON` | 自动探测 | npm 启动器使用的 Python 3.9+ 解释器；探针也会使用。 |
| `OC_DELEGATE_SERVER` | 本目录 | 仅供 `mcp_probe.py` 覆盖服务脚本路径。 |

## 支持矩阵

| 能力 | Windows 10/11 | macOS / Linux |
|---|---|---|
| 派发、状态、结果、返工、恢复 | 已验证预览版；OpenCode 2.0.26 | 实验性支持，作者未验证真实模型集成 |
| TUI viewer、同窗口切 tab | 支持 | 不支持；默认关闭，显式请求也在启动进程前拒绝 |

POSIX 子进程使用独立进程组，超时会清理后代进程。仓库 CI 覆盖这些分支，但并不等于已验证所有
系统上的真实 OpenCode/provider 组合。首次发布以 Windows 为主要支持平台。

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
tests/                 基于 tests/fake_opencode.py 的离线测试（标准库 unittest 测试）
```

运行测试：

```powershell
python -B -m unittest discover -s tests -v
npm test
```

测试使用假的 OpenCode，不联网、不产生付费模型调用。Python 测试只用标准库；`npm test` 使用
Node 内置断言。Python 3.9+ 和 Node 16+ 是独立前置，安装 OpenCode 并不保证两者齐全。

发布前检查实际包：

```powershell
npm pack --ignore-scripts
python -B tests/verify_package.py opencode-delegate-0.4.1.tgz
```

该检查验证精确文件白名单，并从解压后的包完成 Node -> Python 握手。CI 会预先放置缓存文件，
验证它不会被打包。仓库包含 Windows/Linux/macOS、Python 3.9/3.12 的工作流；推送后才能获得
这些环境的实际运行结果。本机验证不能替代完整跨平台验证。

## 许可证

MIT，见 [LICENSE](LICENSE)。
