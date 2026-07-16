# KyleClaude 技术架构文档

## 1. 项目概述

KyleClaude 是一个本地 AI Agent 运行时系统，实现了 Claude Code 的核心工作机制。项目采用分阶段开发模式（S0-S7），从零构建一个完整的 AI 编程助手。

### 核心特性
- **ReAct Agent Loop**：规划-执行-观察循环
- **事件驱动架构**：EventBus 实现执行过程外化
- **双进程设计**：守护进程 + 多客户端架构
- **类型化协议**：JSON-RPC 2.0 + NDJSON + Pydantic v2
- **三层记忆系统**：Session → Thread → Notes
- **上下文治理**：水位检测 + 自动/手动压缩
- **扩展生态**：Skills + Subagents + MCP

---

## 2. 系统架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                       用户界面层                                │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐       │
│  │  kyle CLI   │  │ kyle-tui    │  │  Web UI     │       │
│  │  (调试工具)  │  │  (主界面)    │  │  (规划中)   │       │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘       │
│         │                  │                  │               │
│         └──────────────────┼──────────────────┘               │
│                            │ JSON-RPC 2.0 over NDJSON        │
└────────────────────────────┼────────────────────────────────────┘
                             │ TCP 127.0.0.1:7437
┌────────────────────────────┼────────────────────────────────────┐
│  kyle-core (守护进程)     │                                  │
│  ┌─────────────────────────▼──────────────────────────────┐   │
│  │              Transport Layer (传输层)                    │   │
│  │  • SocketServer: TCP 服务器，处理 NDJSON 消息        │   │
│  │  • SocketClient: 客户端连接管理                       │   │
│  │  • IpcEventBroadcaster: 事件广播到订阅客户端         │   │
│  └─────────────────────────────────────────────────────────┘   │
│                            │                                  │
│  ┌─────────────────────────▼──────────────────────────────┐   │
│  │              Protocol Layer (协议层)                    │   │
│  │  • commands.py: 命令类型定义 (Ping, Run, Subscribe) │   │
│  │  • events.py: 事件类型定义 (Run, Step, Tool, LLM)  │   │
│  │  • envelope.py: JSON-RPC 信封封装                   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                            │                                  │
│  ┌─────────────────────────▼──────────────────────────────┐   │
│  │              Core Runtime (核心运行时)                  │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────┐  │   │
│  │  │ AgentRunner  │  │ AgentLoop    │  │EventBus │  │   │
│  │  │ (运行编排)    │  │ (ReAct循环) │  │(事件总线)│  │   │
│  │  └─────────────┘  └─────────────┘  └─────────┘  │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────┐  │   │
│  │  │  ToolRegistry│  │Permission   │  │Compactor │  │   │
│  │  │ (工具注册)    │  │Manager      │  │(上下文压缩│  │   │
│  │  └─────────────┘  └─────────────┘  └─────────┘  │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────┐  │   │
│  │  │Session      │  │LLM Provider │  │MCP      │  │   │
│  │  │Manager      │  │(Anthropic) │  │Manager  │  │   │
│  │  └─────────────┘  └─────────────┘  └─────────┘  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                            │                                  │
│  ┌─────────────────────────▼──────────────────────────────┐   │
│  │              Persistence Layer (持久化层)               │   │
│  │  • SessionStore: 会话数据持久化                      │   │
│  │  • EventWriter: 事件写入 events.jsonl                │   │
│  │  • TraceWriter: 追踪数据写入 trace.jsonl             │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 进程模型

```
┌─────────────────┐         ┌─────────────────┐
│   kyle-core     │         │   kyle /       │
│   (daemon)     │◄───────►│   kyle-tui     │
│                 │  TCP    │   (client)     │
│  • 事件总线     │  127.0. │                 │
│  • Agent 运行   │  0.1:   │  • 订阅事件     │
│  • 工具执行     │  7437   │  • 发送命令     │
│  • 会话管理     │         │  • 实时渲染     │
└─────────────────┘         └─────────────────┘
```

**设计优势**：
- TUI 崩溃不影响 Agent 执行
- 支持多客户端同时连接
- 所有事件可回溯和重放

---

## 3. 核心模块详解

### 3.1 协议层 (`core/bus/`)

**设计理念**：所有 IPC 消息都是类型化的 Pydantic v2 模型，使用 `discriminated union` 实现类型安全的命令和事件路由。

#### 命令 (Commands)
客户端 → 服务端：
- `CoreAuthenticateCommand`：连接首帧携带本机随机 token，成功后才允许业务命令
- `PingCommand`：心跳检测
- `AgentRunCommand`：启动一次性 Agent 运行；携带 headless permission mode 和显式工具 allow-list
- `RunCancelCommand`：取消 active run 并等待 Session 状态稳定落盘
- `SessionCreateCommand`：创建会话
- `SessionSendMessageCommand`：发送消息
- `SessionList/Resume/Rename/Fork/Export/DeleteCommand`：完整会话生命周期；fork 只复制当前
  transcript 与 notes，不复制旧 run 产物
- `EventSubscribeCommand`：订阅事件流
- `PermissionRespondCommand`：响应权限审批

#### 事件 (Events)
服务端 → 客户端：
- `RunStartedEvent` / `RunFinishedEvent`：运行生命周期
- `SessionInterruptedEvent`：运行取消后的可恢复 Session 状态
- `StepStartedEvent` / `StepFinishedEvent`：步骤生命周期
- `ToolCallStartedEvent` / `ToolCallFinishedEvent`：工具调用
- `LlmTokenEvent` / `LlmUsageEvent`：LLM 流式输出和使用统计
- `PermissionRequestedEvent`：权限审批请求
- `ContextCompactedEvent`：上下文压缩完成

**类型安全示例**：
```python
Command = Annotated[
    PingCommand | AgentRunCommand | ...,
    Discriminator("type")
]
```

### 3.2 传输层 (`core/transport/`)

#### SocketServer
- 基于 `asyncio.start_server` 的 TCP 服务器
- 只接受 loopback 监听地址和 loopback peer
- 首帧同步完成 `core.authenticate`，使用常量时间 token 比较，失败后立即断连
- 读取 NDJSON 行，解析为 JSON-RPC 2.0 请求
- 注册命令处理器 (`server.register("method.name", handler)`)
- 启动前探测端口，防止多实例冲突

#### IpcEventBroadcaster
- 实现 `EventHandler` 接口，订阅 EventBus
- 根据 `topics` 和 `scope` 过滤事件
- 支持历史事件回放 (`replay_from_run`)

#### Trace 安全边界
- 认证请求与响应不进入 Trace，token 不进入 `KyleConfig` 或日志
- IPC/Event 默认只记录 method/type、ID、状态、耗时和 token 统计，不记录 prompt/params/output
- LLM 正文默认关闭；显式开启 payload 后仍经过递归 secret redaction
- API key、Authorization/Bearer、token、password、cookie、private key 及常见 key pattern 统一脱敏
- `TraceWriter` 使用单写队列保持顺序，按 `max_bytes` 轮转并只保留 `backup_count` 份历史
- Writer 重启会处理已超限文件；写盘或轮转失败会传播错误，不会卡死 shutdown

### 3.3 Agent 运行时

#### AgentLoop (`core/loop.py`)
ReAct 循环实现：

```python
async def run(self, context: ExecutionContext) -> None:
    while not context.is_done():
        # [plan] 调用 LLM
        response = await self._provider.chat(...)

        # [observe] 追加 assistant 消息
            context.add_assistant_message(blocks)
            transcript.append_assistant(step, blocks)  # flush + fsync

        # [act] 执行工具调用
        for tc in response.tool_calls:
            result = await invoke_tool(...)
            context.add_tool_result(...)
            transcript.append_tool_result(...)  # 每个结果立即落盘

        # 检查终止条件
        if response.stop_reason == "end_turn":
            context.mark_success()

        # 检查上下文压缩
        if needs_compaction:
            await self._compactor.compact(context, ...)
```

**关键特性**：
- 支持 `max_steps` 限制
- 处理 `max_tokens` 中断
- 保留 `thinking_blocks` 用于扩展思考模式

#### AgentRunner (`core/runner.py`)
运行编排器：
- 组装所有运行时依赖
- 构建工具注册表（内置工具 + MCP 工具）
- 创建 `ExecutionContext`
- 管理事件写入和会话持久化

### 3.4 工具系统 (`core/tools/`)

#### ToolRegistry
简单的工具注册表：
```python
registry = ToolRegistry()
registry.register(ReadFileTool())
registry.register(BashTool())
```

#### 内置工具 (`core/tools/builtin/`)
- `ReadFileTool`：读取文件，并返回完整文件 SHA-256 与截断元数据
- `GlobTool`：按 glob 结构化查找工作区文件，支持 ignore 规则和结果上限
- `GrepTool`：按正则结构化检索文本，返回路径、行列号和截断元数据
- `EditFileTool`：old/new 精确替换、hash 冲突检测、原子写入和 unified diff
- `ApplyPatchTool`：多文件 unified diff 预检、事务提交、逐 hunk 诊断和失败回滚
- `GitDiffTool`：只读返回 changed files、staged/unstaged 状态、numstat 和有界 diff
- `CheckpointListTool` / `CheckpointRewindTool`：列出自动快照并在冲突预检后事务恢复
- `WriteFileTool`：以临时文件、fsync 和原子替换写入文件
- `BashTool`：执行命令；超时或 run 取消时终止 Windows/POSIX 子进程树
- `ListDirTool`：列出目录
- `NoteSaveTool`：保存笔记到会话
- `TaskCreateTool` / `TaskUpdateTool` / `TaskListTool`：任务管理

#### 工具调用流程
```
LLM 返回 tool_use block
    ↓
invoke_tool() 查找工具
    ↓
PermissionManager.check_and_wait()
    ↓
工具执行 (invoke)
    ↓
返回 tool_result 给 LLM
```

### 3.5 权限管理 (`core/permissions/`)

#### 六层评估策略
1. **Tier 1**：`deny_patterns`（bash 命令黑名单）
2. **Tier 2**：`OUTSIDE_CWD_HEURISTICS`（跨目录操作强制审批）
3. **Tier 3**：Session 级 `always` 缓存
4. **Tier 4**：持久化 `always` 缓存 (`policy.toml`)
5. **Tier 5**：`allow_patterns`（bash 命令白名单）
6. **Tier 6**：工具默认策略 (`ALLOW` / `DENY` / `ASK`)

#### Headless 模式
- `fail_fast`：ASK 立即结束 run，`reason=permission_required`，CLI 退出码为 3
- `deny`：ASK 立即生成 permission_denied tool_result，模型可改用只读或低风险方案
- `allow_list`：只放行命令中显式列出的 ASK 工具
- Headless 不读取交互式 always-allow 缓存，且不能绕过 deny pattern 或 outside-cwd 检查
- 模式按 one-shot Session 隔离，run 完成后清理；chat/TUI 始终保持交互审批

#### 审批流程
```
工具调用请求
    ↓
PermissionManager.check_and_wait()
    ↓
创建 Future 挂起当前执行
    ↓
发布 PermissionRequestedEvent 到 TUI
    ↓
用户决策 (allow/deny)
    ↓
客户端发送 PermissionRespondCommand
    ↓
respond() 解析 Future
    ↓
工具执行 / 拒绝
```

### 3.6 会话管理 (`core/session/`)

#### 三层记忆架构
```
Session (会话)
  ├── Thread (线程，消息列表)
  │     ├── user message
  │     ├── assistant message (with tool calls)
  │     ├── tool results
  │     └── ...
  ├── Notes (笔记，持久化知识)
  └── Context (上下文文件)
```

#### SessionManager
- 创建/关闭会话
- 发送消息并等待完成
- 维护 active run task，处理 `run.cancel`、daemon shutdown 和中断状态持久化
- 冷启动时识别 active 会话，归档未配平/未写完整的 transcript 尾部并回退到合法消息边界
- 获取历史消息
- 手动压缩会话

#### Transcript v2
- `thread.jsonl` 的 assistant 与 tool_result 使用稳定 `message_id` / `block_id`
- 每个 block 记录 `block_index` / `block_count`，可识别半条消息与孤立 tool call
- 追加后执行 flush + fsync；同一 block 重放时按 ID 去重
- 取消或 daemon 冷启动时保留 `thread_interrupted_*.jsonl` 归档和恢复审计日志
- 自动 compact 使用原子替换持久摘要，避免下一轮重新加载压缩前历史

### 3.7 上下文压缩 (`core/compact/`)

#### 触发条件
- 上下文使用率 ≥ `compact_threshold` (默认 0.80)
- 用户在 TUI 中输入 `/compact`

#### 压缩流程
1. 将消息列表序列化为文本
2. 使用压缩提示词调用 LLM
3. 生成六段式摘要：
   - 原始目标
   - 已完成步骤
   - 关键约束和发现
   - 当前文件状态
   - 剩余 TODO
   - 关键数据
4. 替换消息列表为 `[摘要, 确认回复]`
5. 写入 `summary_<ts>.md`

### 3.8 LLM 提供商 (`core/llm/`)

#### AnthropicProvider
- 流式调用 Anthropic API
- 支持 Prompt Caching (`cache_control: {type: "ephemeral"}`)
- 网络中断自动重试 (最多 3 次)
- 发布 `LlmTokenEvent` (流式) 和 `LlmUsageEvent` (统计)

#### 模型支持
```python
MODEL_CONTEXT_WINDOWS = {
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-7": 200_000,
}
```

### 3.9 MCP 支持 (`core/mcp/`)

#### McpServerManager
- 启动/停止 MCP 服务器
- 获取 MCP 工具并注册到 ToolRegistry
- 支持 Stdio 传输

### 3.10 技能系统 (`core/skills/`)

#### SkillLoader
- 从 `~/.kyle/skills/` 加载技能
- 技能是 Markdown 文件，包含工具调用模板
- TUI 中通过 `/skill_name` 触发

### 3.11 子代理系统 (`core/subagent/`)

#### SpawnAgentTool
- 创建子 Agent 处理子任务
- 共享 TaskManager 跟踪后台任务
- 子 Agent 运行在独立的 ExecutionContext

### 3.12 追踪系统 (`core/trace/`)

#### TraceWriter
- 记录所有 IPC 消息和事件
- 格式：`TraceRecord(ts, direction, layer, kind, data)`
- 用于调试和性能分析

---

## 4. 数据流

### 4.1 命令执行流程

```
用户输入 "总结 README.md"
    ↓
TUI: ChatTextArea.on_submit()
    ↓
IPC: session.send_message command
    ↓
Core: SessionManager.send_message()
    ↓
AgentRunner.run_and_capture()
    ↓
AgentLoop.run()
    ├── LLM chat call
    ├── Tool call: read_file("README.md")
    ├── Tool call: note_save(summary)
    └── Final answer
    ↓
SessionStore.append_messages()
    ↓
TUI: session.waiting_for_input event
```

### 4.2 事件流

```
AgentLoop.run()
    ↓
bus.publish(RunStartedEvent)
    ↓
EventBus
    ├── EventWriter (写入 events.jsonl)
    ├── IpcEventBroadcaster (推送到订阅客户端)
    └── TraceWriter (记录到 trace.jsonl)
    ↓
TUI: _handle_event()
    ├── LLMTokenEvent → LLMStreamBlock.append_token()
    ├── ToolCallStartedEvent → ToolCallBlock
    └── RunFinishedEvent → 显示完成横幅
```

---

## 5. 技术栈

| 组件 | 技术 | 用途 |
|------|------|------|
| 编程语言 | Python 3.12 | 主语言 |
| 数据验证 | Pydantic v2 | 类型化协议契约 |
| 异步框架 | asyncio | 异步 I/O |
| LLM SDK | anthropic >= 0.25 | Anthropic API 调用 |
| TUI 框架 | textual >= 0.75 | 终端 UI |
| HTTP 客户端 | httpx | 代理支持 |
| 配置管理 | python-dotenv | .env 文件解析 |
| 构建系统 | Hatchling | 打包和分发 |
| 包管理器 | uv | 依赖管理 |
| Linter | Ruff | 代码风格检查 |
| 类型检查 | MyPy | 静态类型检查 |
| 测试框架 | Pytest + pytest-asyncio | 单元测试和集成测试 |

---

## 6. 配置系统

### 四级优先级
1. 内建默认值
2. `~/.kyle/config.toml`
3. `.env` 文件
4. 系统环境变量

### 关键配置项
```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level = "INFO"
file = "~/.kyle/logs/core.log"
format = "text"

[llm]
default_model = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"

[agent]
max_steps = 20

[compaction]
auto_threshold = 0.80

[permission]
timeout_s = 60.0

[trace]
enabled = false
file = "~/.kyle/trace.jsonl"
include_llm_payload = false

[mcp]
servers = []  # MCP 服务器配置
```

---

## 7. 测试策略

### 测试分层
- **单元测试** (`tests/unit/`)：快速，无需守护进程
- **集成测试** (`tests/integration/`)：自动启动守护进程
- **端到端测试** (`tests/e2e/`)：完整用户场景

### 测试覆盖
- 协议层：命令/事件的序列化和反序列化
- 传输层：TCP 服务器和客户端
- 工具系统：工具注册和调用
- 权限管理：审批流程
- 会话管理：消息持久化

---

## 8. 部署和运行

### 开发环境
```bash
# 安装依赖
uv sync

# 启动守护进程
uv run kyle-core

# 启动 TUI
uv run kyle-tui

# 运行测试
uv run pytest tests/ -v

# Lint 和类型检查
uv run ruff check src tests scripts
uv run mypy src
```

### 生产环境
```bash
# 安装为系统服务
pip install .

# 启动守护进程 (systemd)
systemctl start kyle-core

# 用户连接 TUI
kyle-tui
```

---

## 9. 优化方向

### 9.1 性能优化

#### 1. 异步 I/O 优化
- **当前状态**：使用 `asyncio` 但部分操作可能阻塞
- **优化建议**：
  - 工具执行使用 `asyncio.to_thread()` 避免阻塞事件循环
  - 文件 I/O 使用 `aiofiles`
  - 考虑使用 `uvloop` 作为事件循环策略

#### 2. LLM 调用优化
- **当前状态**：每次调用都发送完整上下文
- **优化建议**：
  - 实现更智能的上下文窗口管理
  - 支持多种压缩策略（提取式 vs 生成式）
  - 实现请求批处理（多个工具调用合并）

#### 3. 内存优化
- **当前状态**：会话历史全部加载到内存
- **优化建议**：
  - 实现懒加载（按需加载历史消息）
  - 使用磁盘缓存减少内存占用
  - 实现消息过期机制

### 9.2 功能增强

#### 1. 多模型支持
- **当前状态**：仅支持 Anthropic API
- **优化建议**：
  - 抽象 `LLMProvider` 接口，支持 OpenAI、Gemini、本地模型
  - 实现模型路由（根据任务类型选择模型）
  - 支持模型 fallback（主模型失败时切换）

#### 2. 协作功能
- **当前状态**：单用户本地使用
- **优化建议**：
  - 实现多用户会话隔离
  - 支持会话共享和导出
  - 实现实时协作（WebSocket + CRDT）

#### 3. 工具市场
- **当前状态**：内置工具 + MCP 工具
- **优化建议**：
  - 实现工具市场（类似 VS Code 扩展）
  - 支持工具版本管理
  - 实现工具依赖解析

#### 4. 高级规划
- **当前状态**：ReAct 单步规划
- **优化建议**：
  - 实现树形规划（MCTS 或 Beam Search）
  - 支持多路径探索
  - 实现反思机制（自我纠错）

### 9.3 可靠性提升

#### 1. 错误处理
- **当前状态**：部分错误直接抛出
- **优化建议**：
  - 实现错误分类（可恢复 vs 不可恢复）
  - 实现指数退避重试
  - 实现断路器模式（防止级联故障）

#### 2. 状态恢复
- **当前状态**：崩溃后状态丢失
- **优化建议**：
  - 实现检查点机制（定期保存运行状态）
  - 支持从检查点恢复
  - 实现事务性操作（原子性保证）

#### 3. 监控和告警
- **当前状态**：仅文件日志
- **优化建议**：
  - 实现指标收集（Prometheus）
  - 实现分布式追踪（OpenTelemetry）
  - 实现健康检查端点

### 9.4 安全性增强

#### 1. 工具沙箱
- **当前状态**：工具直接执行
- **优化建议**：
  - 实现容器化工具执行（Docker）
  - 实现资源限制（CPU、内存、时间）
  - 实现文件系统隔离

#### 2. 认证和授权
- **当前状态**：本地 Core 强制 loopback；首次启动生成 `~/.kyle/ipc-token`，客户端首帧认证，
  错误凭据统一失败并断连；桌面 sidecar 可通过 `KYLE_IPC_TOKEN` 注入进程级临时凭据
- **优化建议**：
  - Desktop 由 Rust bridge 持有临时 token，不向 WebView 暴露
  - 若未来支持远程 Core，再增加 TLS、凭据轮换和基于角色的访问控制
  - 将认证失败计数纳入不含敏感信息的安全指标

#### 3. 输入验证
- **当前状态**：依赖 Pydantic 验证
- **优化建议**：
  - 实现命令注入检测
  - 实现路径遍历防护
  - 实现敏感信息过滤

### 9.5 用户体验

#### 1. TUI 增强
- **当前状态**：基础终端 UI
- **优化建议**：
  - 实现语法高亮（代码块）
  - 支持鼠标操作
  - 实现主题定制

#### 2. 文档和教程
- **当前状态**：外部文档
- **优化建议**：
  - 实现内置帮助系统
  - 支持交互式教程
  - 实现命令自动补全

#### 3. 多语言支持
- **当前状态**：中文 + 英文混合
- **优化建议**：
  - 实现国际化 (i18n)
  - 支持用户界面语言切换
  - 实现多语言文档

### 9.6 架构演进

#### 1. 微服务化
- **当前状态**：单体守护进程
- **优化建议**：
  - 拆分为独立服务（Agent 服务、工具服务、会话服务）
  - 使用消息队列解耦
  - 实现服务发现

#### 2. 云原生支持
- **当前状态**：本地运行
- **优化建议**：
  - 实现容器化部署 (Docker)
  - 支持 Kubernetes 编排
  - 实现无状态设计（状态外置）

#### 3. 插件系统
- **当前状态**：代码级扩展
- **优化建议**：
  - 实现动态加载机制
  - 定义插件 API 契约
  - 实现插件隔离（沙箱执行）

---

## 10. 代码质量

### 优点
1. **类型安全**：广泛使用 Pydantic 和 MyPy 严格模式
2. **测试覆盖**：单元测试 + 集成测试
3. **代码风格**：Ruff 强制统一
4. **文档完善**：每个函数都有中文注释

### 改进建议
1. **减少重复代码**：
   - `CLAUDE.md` 和 `AGENT.md` 完全重复，建议合并
   - 部分工具实现有相似逻辑，建议提取基类

2. **增强错误处理**：
   - 部分 `except Exception` 过于宽泛
   - 建议定义业务异常层次结构

3. **优化模块耦合**：
   - `AgentRunner` 依赖过多，建议使用依赖注入
   - 部分循环导入问题（如 `compactor.py` 中的延迟导入）

4. **性能监控**：
   - 建议添加性能基准测试
   - 实现慢查询日志（工具执行超过阈值时记录）

---

## 11. 总结

KyleClaude 是一个设计良好的本地 AI Agent 运行时系统，具有以下优势：

1. **架构清晰**：分层设计，职责明确
2. **类型安全**：Pydantic v2 + MyPy 严格模式
3. **可扩展性**：工具系统、技能系统、MCP 支持
4. **可恢复性**：增量 transcript、Session resume/fork/export 与取消恢复
5. **可观测性**：事件溯源 + 脱敏轮转追踪
6. **用户体验**：TUI 实时渲染、权限审批与 Session picker

**后续优化重点**：
1. 性能优化（异步 I/O、内存管理）
2. 功能增强（多模型、协作、工具市场）
3. 可靠性提升（错误处理、状态恢复、监控）
4. 安全性增强（工具沙箱、资源限制、输入验证）
5. 架构演进（微服务、云原生、插件系统）

---

## 附录：关键文件索引

| 文件路径 | 功能描述 |
|----------|------------|
| `src/kyle_claude/core/app.py` | 守护进程入口 |
| `src/kyle_claude/core/loop.py` | Agent Loop 实现 |
| `src/kyle_claude/core/runner.py` | Agent Runner |
| `src/kyle_claude/core/bus/commands.py` | 命令类型定义 |
| `src/kyle_claude/core/bus/events.py` | 事件类型定义 |
| `src/kyle_claude/core/tools/registry.py` | 工具注册表 |
| `src/kyle_claude/core/permissions/manager.py` | 权限管理器 |
| `src/kyle_claude/core/session/manager.py` | 会话管理器 |
| `src/kyle_claude/core/compact/compactor.py` | 上下文压缩器 |
| `src/kyle_claude/core/llm/provider.py` | LLM 提供商 |
| `src/kyle_claude/tui/app.py` | TUI 应用 |
| `WIRE_PROTOCOL.md` | 线协议文档 |
| `RUNBOOK.md` | 运维手册 |
