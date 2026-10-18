# opencode-delegate

把有边界的任务从主智能体派发给本机 [OpenCode](https://opencode.ai) 子智能体执行，让贵的前沿模型额度只
花在「拆题、审阅、集成」上，而不是花在执行上。

这是一个标准 stdio MCP 服务（底下是一层很薄的 Python CLI），任何 MCP 客户端都能直接调用：Codex、
Claude Code、Claude Desktop、Cursor、Cline、Continue、Zed、Windsurf。主智能体负责写派单、跟踪进度、
审证据并承担方案设计与最终判断；子智能体用自己的额度执行明确任务。

[English](README.md)

## 为什么

开源模型方案通常提供较充裕的 token 额度，适合承担读文件、编写代码、整理材料和运行测试等执行工作。
主代理负责明确需求、审阅证据和集成结果，把较贵的额度留给关键判断。实际额度、价格和能力取决于
你选择的服务，不由本工具保证。

本工具不绑定任何模型品牌。默认跟随 OpenCode 的配置，也允许父代理为每个任务指定模型。

## 委派职责边界

主智能体负责方案设计和最终判断。默认只把事实查询、按已确定方案编码、执行测试交给子智能体，
子智能体可以自行处理常规编码细节。架构、研究假设和算法选择由主智能体确定；只有用户明确要求
子智能体提供方案建议时才委派这类咨询。遇到影响算法或模型的未定义决策，先报告缺口，暂停依赖它的实现。

派单前写清交付物、已确定规则、输入、允许改动、验收证据和预算／停止条件；超出执行时限的工作拆开。
不能为完成局部验证而删去设计约束，也不能把测试通过当作设计成功。具体的派单前检查和每次任务必须
携带的子智能体边界说明，见 [SKILL 的委派契约](SKILL.md#delegation-contract-parent-designs-worker-executes)。

## 前置要求

| 项目 | 说明 |
|---|---|
| Python | 3.9 或更高（在 3.12 上开发与测试）。**无任何第三方依赖**，只用标准库。 |
| OpenCode CLI | 在 2.0.26 上测试过。需已安装并完成配对。 |
| 模型路由 | 用你 OpenCode 里已经配好的那个。不限定品牌、订阅或 provider。 |
| 操作系统 | 完整功能需要 Windows 10/11，见下方支持矩阵。 |

## 安装

需要 Node.js 16+、Python 3.9+，以及已能正常调用模型的 OpenCode。启动器会自动查找 Python；
它不会替你安装 Python、购买模型订阅或修改 OpenCode 配置。

### 推荐：一条 npm 命令安装

```bash
npm install -g --allow-git=all github:Upstream17/opencode-delegate
```

这会直接从公开 GitHub 仓库安装，无需手动 clone。当前尚未发布到 npm registry，所以暂时不要使用
`npm install -g opencode-delegate`；正式发布后才可改用这个短命令。GitHub 来源安装可能需要 Git。

npm 12 默认禁止 Git 来源，`--allow-git=all` 为本次安装显式放行，不修改全局 npm 设置。
不识别该选项的旧版 npm 可以省略它。

### 将服务添加到 MCP 客户端

安装与注册是两件事：npm 安装工具，客户端配置决定哪个智能体可以调用它。
Claude Desktop、Claude Code 等使用 `mcpServers` 的客户端添加：

```json
{
  "mcpServers": {
    "opencode_delegate": { "command": "opencode-delegate", "args": [] }
  }
}
```

Codex 在 `~/.codex/config.toml` 中添加：

```toml
[mcp_servers.opencode_delegate]
command = 'opencode-delegate'
args = []
```

保存后重启客户端。模型默认沿用 OpenCode 当前配置，无需在 MCP 中重复选择。

<details>
<summary>不想全局安装：让客户端通过 npx 自动下载并启动</summary>

把上面的命令换成：

```json
{ "command": "npx", "args": ["--allow-git=all", "-y", "github:Upstream17/opencode-delegate"] }
```

这是 stdio 服务，启动后等待 MCP 请求是正常行为；通常应由客户端启动，而非手工守着终端。
</details>

<details>
<summary>其他客户端与 Windows 启动问题</summary>

Zed 在设置中使用 `context_servers`，里面的 `command` 和 `args` 同上。
各客户端的设置位置不同，请使用其官方 MCP 配置格式。

若 Windows 客户端找不到全局命令，先重启客户端，让它读取更新后的 PATH。
若客户端不能直接执行 npm 的 `.cmd` 启动脚本，可执行 `npm root -g` 找到安装目录，
改用 `node.exe` 启动 `<npm全局目录>/opencode-delegate/bin/opencode-delegate.js`。
这只是部分 Windows 客户端需要的兼容方式，并非默认安装步骤。
</details>

<details>
<summary>源码安装与可选 skill（开发者）</summary>

```bash
git clone https://github.com/Upstream17/opencode-delegate.git
```

把客户端 `command` 设为 Python 解释器，`args` 设为检出目录中 `mcp_server.py` 的绝对路径。
Windows TOML 路径使用单引号字面量字符串，避免反斜杠转义错误。

支持 skill 目录的智能体还可以把仓库放入自己的 skill 根目录。MCP 与 skill 可分别使用。
状态、日志保存在用户目录中，安装目录无需写入；环境变量见后文。
</details>

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

新任务的优先级是：

1. 父代理在 `oc_run` 中指定的 `model`。
2. MCP 环境变量 `OC_DELEGATE_MODEL`。
3. 当前任务目录下 OpenCode 的默认模型配置（每个新任务重新读取）。
4. 都没有时，不传模型参数，由 OpenCode 自行选择，并记录实际路由。

读取 OpenCode 配置不修改任何文件。读取失败或 CLI 不支持该查询时，会交回 OpenCode 选择。
解析到的默认模型会作为本任务的固定路由保存；以后修改 OpenCode 默认值只影响新任务。
`oc_continue` / `oc_recover` 沿用该任务记录，不会重新读取默认值或静默换模型。
历史上未固定模型的任务仍保持未固定，由 OpenCode 处理该会话。

`oc_run` 返回的 `model_source` 说明来源。固定路由的校验结果为 `verified`；完全未指定路由时，
会采纳导出证据中的有效路由，结果为 `adopted`。缺失、混用或不匹配的路由会标记为待审阅。

如出现 provider/auth 错误，应检查实际路由、登录与使用权限；403 不一定表示缺少默认模型。
若共享服务在任务刚结束时返回结构不完整的导出，helper 会短暂退避并最多重试三次；仍必须取得有效
路由证据才会报告完成。超时和语义上的路由不匹配不会重试或放宽校验。
本工具不会自动切换服务商或修改你的 OpenCode 设置。本文中的 `provider/model-name` 仅为格式占位，
需要显式选择时请使用 OpenCode 中真实可用的模型 ID。

## 环境变量

| 变量 | 默认值 | 含义 |
|---|---|---|
| `OC_DELEGATE_STATE` | `$CODEX_HOME/opencode-delegate/state`，否则 `~/.opencode-delegate/state` | 任务记录、轮次日志、viewer 记录的存放位置。 |
| `OC_DELEGATE_MODEL` | 未设置 | 钉定一个默认子智能体模型。未设置时会读取 OpenCode 自身配置里的默认模型（只读，不修改）；若那里也没有，才交给 OpenCode 自行决定。 |
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
