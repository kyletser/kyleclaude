# KyleClaude

**KyleClaude：从零实现一个本地 Claude Code Agent 系统（mini 版）。**

也可以理解为：我们自己动手实现一个 **minClaude**，不仅实现Agent内核，还是先一套 TUI。

大家可以看一下效果：（接入的是deepseek-v4-flash，当然大家也可以接入其他模型）。

![](docs/images/2026-06-09_19-36-12.jpg)

可以接入命令，可以使用skill，可控制上下文，可压缩：

![](docs/images/2026-06-10_09-38-25.jpg)

可以下达一个稍稍负责的任务，KyleClaude自动完成规划 并执行：

（KyleClaude会先申请一下本地编辑权限）

![](docs/images/2026-06-10_09-41-25.jpg)

然后规划并执行：

![](docs/images/2026-06-10_09-42-43.jpg)

当然，它不是要一比一复刻 Claude Code 的所有产品能力，而是把 Claude Code 这类 AI 编程 Agent 最核心的运行机制拆出来：

* 用户输入一个目标，Agent 能自己规划下一步
* 模型不是只回答文本，而是能主动发起工具调用
* 工具调用不是直接裸跑，而是有参数校验和权限审批
* 代码发现使用结构化 Glob/Grep，遵守工作区边界和 ignore 规则，并限制结果规模
* 代码修改使用 read hash 和精确 Edit，检测并发变化后通过原子替换落盘
* 多文件修改使用 unified diff 全量预检和事务提交，失败时回滚已提交文件
* 变更自检使用只读 Git Diff，结构化展示状态、增删统计和有界补丁
* 每次文件修改自动生成 checkpoint，rewind 前检测用户后续修改并事务恢复
* 运行可通过 CLI/TUI 主动取消，并清理 Bash 进程树、权限等待和派生 Subagent
* assistant/tool_result 按 block 即时持久化，崩溃后归档不完整尾部并恢复合法会话边界
* Trace 默认只记录元数据，统一脱敏后按大小轮转并限制历史文件数量
* Core 强制监听 loopback，客户端必须用本机随机 token 完成首帧认证
* 执行过程不是黑盒，而是通过事件流实时展示到 TUI
* 每一次 run 都能留下 events、trace、session 记录，方便复盘和排查
* 多轮会话不是简单拼接历史，而是有 thread、notes、context 分层记忆
* 会话支持 list/resume/rename/fork/export/delete，TUI 可用 `/sessions` 切换历史
* 上下文快爆了，不是粗暴截断，而是有水位检测和 compact 压缩
* 复杂任务可以交给子 Agent，外部工具可以通过 MCP 接进来

我们要做的是一个真正能跑任务、能调工具、能看过程、能管权限、能续上下文、能扩展生态的本地 Agent 运行时。

### 快速运行

```powershell
uv sync
uv run kyle-core
```

Core 首次启动会生成 `~/.kyle/ipc-token`。CLI/TUI 自动读取该文件并在发送任何业务命令前
完成认证；`KYLE_HOST` 只能配置为 `127.0.0.1`、`::1` 或 `localhost`。

另开一个终端启动交互界面：

```powershell
uv run kyle-tui
```

无人值守任务默认使用 fail-fast 权限模式，遇到需要审批的工具会立即以退出码 `3` 结束：

```powershell
uv run kyle run --goal "分析当前项目并运行测试"
uv run kyle run --goal "修复问题" --permission-mode deny
uv run kyle run --goal "修改并验证" --permission-mode allow-list `
  --allow-tool edit_file --allow-tool apply_patch --allow-tool bash
```

`allow-list` 不能绕过命令 deny pattern 或工作区外路径检查；chat/TUI 继续使用交互审批。

管理历史会话：

```powershell
uv run kyle sessions --all
uv run kyle session rename SESSION_ID "新的标题"
uv run kyle session fork SESSION_ID --title "实验分支"
uv run kyle session export SESSION_ID --format markdown -o session.md
uv run kyle session delete SESSION_ID --yes
```

TUI 中输入 `/sessions` 打开会话选择器，输入 `/new` 创建并切换到新会话。

提交前可运行与 CI 相同的完整门禁（包含 wheel 构建和鉴权 Core smoke）：

```powershell
make verify
```

当前能力的逐项证据、验证边界和剩余 Phase 1 工作见
[`docs/LIGHTWEIGHT_AGENT_COMPLETION_AUDIT.md`](docs/LIGHTWEIGHT_AGENT_COMPLETION_AUDIT.md)。

你学完之后，再看 Claude Code、Codex、Cursor 这些 AI 编程工具，就不会只停留在“它好像很智能”。

你能看懂它背后那条工程主线：

**用户目标 → Agent Loop → 模型思考 → 工具调用 → 结果回填 → 事件展示 → 会话续航。**

### KyleClaude 长什么样？

KyleClaude 的最终形态是这样的：

![](docs/images/2026-06-10_14-30-58.jpg)

用户不是直接和一个脚本对话，而是通过 `kyle` CLI 或 `kyle-tui` 连接到常驻的 `kyle-core` 守护进程。

真正执行任务的是 Core daemon。

CLI 和 TUI 只是客户端。

这意味着：

* TUI 崩了，Agent 任务不一定要跟着死
* 后续可以同时接 CLI、TUI、Web 前端
* 所有任务过程都能通过事件流订阅
* 所有命令、响应、事件都要通过类型化协议通信
* Agent 的工具调用、会话记忆、权限审批、上下文压缩，都在同一条运行链路里完成

这就是它和普通 AI Demo 最大的区别：

**普通 Demo 是“调用模型”。KyleClaude 是“搭一个本地 Agent 运行时”。**

### 项目专栏目录

![](docs/images/2026-06-10_11-55-26.jpg)

从项目演示，运行到 项目实战：架构如何设计、环境怎么搭，Agent loop，上下文、可压缩、MCP、skill支持这些如恶化设计。

最后再到求职相关：项目的简历写法、项目亮点、本项目常见面试题，都给大家准备好了。

从**项目源码到答疑，一条龙服务，不用担心学不会，有什么问题都可以在专属微信群提问**：（[知识星球](https://programmercarl.com/other/kstar.html)里每个项目都有专属答疑群）

![](docs/images/2026-06-10_14-34-29.jpg)

### 项目特色

这个项目，我采用全新的讲解方式，不是一下子直接给大家全部项目代码。

而且分成了 8个阶段，一步一步，带大家实现完整的kyleClaude。

每个阶段都不是堆功能，而是解决一个真实的 Agent 工程问题。

![](docs/images/2026-06-10_11-01-32.jpg)

| 阶段 | 主题 | 这一阶段真正解决的问题 |
| --- | --- | --- |
| S0 | 骨架与协议契约 | CLI 和 daemon 通过真实 IPC 完成一次 ping/pong |
| S1 | Agent 最小闭环 | 一次 `kyle run` 从 goal 到 LLM、工具、事件文件完整跑通 |
| S2 | 事件流外化 | AgentRunner 搬进 daemon，CLI/TUI 通过 IPC 订阅同一份事件流 |
| S3 | 自主规划与 TUI | Agent 能用任务工具拆解复杂目标，TUI 展示完整执行过程 |
| Trace | 系统级时间线 | IPC / EventBus / LLM 三层数据流可追踪、可回放 |
| S4 | 会话与记忆 | 多轮 run 进入同一个 session，thread 和 notes 接住上下文 |
| S5 | 工具安全 | 工具调用前有参数校验、权限审批、失败分类和重试 |
| S6 | 上下文治理 | 长会话下有 context 水位、tool_result 截断和 compact |
| S7 | 扩展边界 | Skills、Subagents、MCP 让 Agent 可组织、可派生、可接外部工具 |

从第一章开始，项目就不是“先写一个脚本，后面再慢慢重构”。

KyleClaude 在 S0 就先把 `kyle` CLI 和 `kyle-core` daemon 拆开，通过 TCP NDJSON + JSON-RPC 2.0 通信。

这一步看起来比普通脚手架更重，但它换来的是后面所有能力都不用推倒重来：

* TUI 可以复用同一套 IPC
* 事件订阅可以复用同一套通道
* 权限审批可以通过事件推到前端
* trace 可以关联请求和响应，同时默认移除业务 payload，且永不记录 IPC 认证帧
* 后续 Web 前端也可以接入同一个 Core

这就是工程项目里真正值钱的地方。

不是“能不能跑”，而是系统边界一开始就立住。

### 项目架构图

![](docs/images/20260610114820_KyleClaude架构图-分层版.png)

KyleClaude 的核心不是一个 prompt，而是一套完整的本地 Agent 运行链路：

```latex
用户目标
  → CLI / TUI
  → JSON-RPC over NDJSON
  → kyle-core daemon
  → AgentRunner
  → AgentLoop
  → LLM Provider
  → ToolRegistry
  → PermissionManager
  → EventBus
  → Session Store
  → TUI 实时渲染 / events.jsonl 持久化 / trace 回放
```

你学完以后，面试官再问 AI Agent 项目，你就不是说：

“我调用了大模型 API。”

而是能说：

* 我实现了 ReAct AgentLoop 和工具调用闭环
* 我用 EventBus 把 Agent 执行过程外化成事件流
* 我实现了 TUI 实时渲染、工具折叠块、权限审批卡片
* 我实现了 Session、thread、notes 三层记忆体系
* 我实现了上下文水位检测、tool_result 截断、自动 compact 和手动 compact
* 我实现了 Skills、Subagents、MCP 外部工具接入
* 我用 pytest、mypy strict、ruff 保证项目质量
* 我实现了守护进程 + 多客户端架构
* 我设计了 JSON-RPC 2.0 + NDJSON 的类型化 IPC 协议

这就不是“AI 套壳项目”了。

这是一个能拿去讲系统设计、异步并发、协议建模、工具安全、上下文工程、多 Agent 编排的高质量项目。

### 项目亮点

![](docs/images/2026-06-10_11-48-11.jpg)

KyleClaude 最大的亮点，是把 Claude Code 这类 AI 编程 Agent 背后的核心机制，用一个 mini 版工程完整跑通：它不是单进程脚本，而是 `kyle-core` daemon + CLI/TUI 多客户端架构；

不是一次性调大模型，而是 ReAct AgentLoop，支持模型思考、工具调用、结果回填和多步执行；

不是让模型说执行就执行，而是把工具调用放进 `ToolRegistry` 和 `PermissionManager`，先做参数校验、权限审批、失败分类，再把 tool result 返回给模型；

不是只展示最终答案，而是通过 `EventBus`、events、trace 和 TUI，把 token 流、工具调用、审批、上下文水位都实时展示并可回放；

不是简单拼接聊天历史，而是用 session、thread、notes、context 和 compact 做上下文治理；

最后还支持 Skills、Subagents、MCP，把工作流、子 Agent 和外部工具统一接进同一套运行链路。

也就是说，这个项目真正能讲的不是“我接了一个大模型接口”，而是“我实现了一个本地 Agent 运行时”。


### 这个项目适合谁？

如果你正在准备秋招、春招、实习、社招，想做一个 AI 项目，想了解Agent工作原理，这个项目很适合你。

如果你已经做过 RAG、聊天机器人、AI 助手，想把项目深度往 Agent 工程方向拔高，这个项目也很适合。

如果你想理解 Claude Code、Codex、Cursor 这类 AI 编程工具背后的运行时设计，这个项目同样值得系统学一遍。

它不是教你背概念。

**它是带你从 S0 到 S7，八个阶段，把一个本地 Agent 工具从零搭出来**。

每一章都有明确的执行路径，每一阶段都能运行、能验证、能留下文件证据。

你不是最后拿到一个黑盒项目。

你会知道它每一层为什么存在。

### QA

**1、这个项目有视频吗**？

项目如何配置环境，启动，部署，运行，已经功能介绍，是有视频的。

主要项目讲解为文字专栏的方式。

项目有专属答疑微信群，不懂得的地方可以在群里提问，我们都会答疑。

**2、这个项目用什么语言开发**？

Python

**3、KyleClaude项目用Python实现，有其他语言版本吗**？

实现一个Agent 关键在于Agent的原理，面试官不会问你 你用什么语言实现的Agent。

就像大家目前看到 Claude原理的文章，没有人会重点强调这是用什么语言实现的，而是强调Claude 这个agent的原理。

所以 在开发 KyleClaude，我们考虑使用python，就是因为python最容易上手。

**4、我是C++、Java、Go或者其他语言选手，能做这个项目吗**？

如果是 C++、Java、Go或者其他语言选手，做个项目没问题，这个项目写简历上，面试官也不会问你语言问题，而是聚焦Agent的设计与实现。

我们项目专栏上，简历写法，项目亮点，都不强调编程语言，都聚焦Agent原理。
