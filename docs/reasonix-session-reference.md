# Reasonix 会话与协议参考文档

> 来源: Reasonix v0.52.0 源码分析 + CLI 实测 + ACP 协议逆向
> 用途: 设计微信 ↔ Reasonix 网关时的会话持久化、协议对接参考

---

## 1. 会话存储体系

### 1.1 存储位置和格式

```
~/.reasonix/
├── config.json                    ← 全局配置
├── sessions/                      ← 会话存储目录
│   ├── <session-name>.jsonl       ← 对话记录（每行一条 JSON 消息）
│   ├── <session-name>.meta.json   ← 会话元数据
│   └── <session-name>.events.jsonl ← 事件日志（工具调用、模型选择等）
├── usage.jsonl                    ← 全局用量统计
├── slash-usage.json               ← 斜杠命令使用统计
└── version-cache.json             ← 版本缓存
```

### 1.2 会话文件格式

**对话记录 (.jsonl)** — 每行一个 JSON 对象:
```jsonl
{"role":"user","content":"你好"}
{"role":"assistant","content":"你好！...","reasoning_content":"The user is greeting me..."}
{"role":"user","content":"帮我写个函数"}
{"role":"assistant","content":"好的，...","tool_calls":[...]}
```

**元数据 (.meta.json):**
```json
{
  "summary": "你好",                    ← 最近对话摘要
  "workspace": "/root",                 ← 工作目录
  "totalCostUsd": 0.0036,              ← 累计花费
  "cacheHitTokens": 0,                 ← 缓存命中 token
  "cacheMissTokens": 25700,            ← 缓存未命中 token
  "totalCompletionTokens": 40,         ← 总输出 token
  "lastPromptTokens": 12850,           ← 最近一轮输入 token
  "turnCount": 1,                      ← 对话轮数
  "balanceCurrency": "CNY"
}
```

**事件日志 (.events.jsonl):**
```jsonl
{"id":1,"ts":"2026-05-27T03:22:54.344Z","turn":0,"type":"session.opened","name":"code-root","resumedFromTurn":0}
{"id":2,"ts":"2026-05-27T03:23:24.441Z","turn":1,"type":"model.turn.started","model":"deepseek-v4-flash","reasoningEffort":"high","prefixHash":"e59043e6906bb5af"}
{"id":42,"ts":"2026-05-27T03:23:24.982Z","turn":1,"type":"model.final","content":"你好！...","usage":{...},"costUsd":0.0018}
```

### 1.3 会话命名规则

| 模式 | 命名 | 示例 |
|------|------|------|
| `code <dir>` | `code-<目录名>` | `code-root`, `code-my-project` |
| `code` (默认) | `code-<当前目录名>` | `code-root` |
| `chat` | `default` 或 `--session <name>` | `default`, `my-session` |
| `acp` | `acp-<timestamp>` | `acp-20260527-032254` |

### 1.4 会话恢复机制

```bash
# 恢复最近的会话
reasonix code --continue    # 或 -c
reasonix chat --continue

# 恢复指定会话（即使已空闲）
reasonix code --session code-root --resume    # 或 -r
reasonix chat --session my-session --resume

# 强制新建（忽略已有会话）
reasonix code --new    # 或 -n
reasonix chat --new

# 禁用会话持久化
reasonix code --no-session
```

**关键行为:**
- 会话恢复时，Reasonix 从 `.jsonl` 文件加载完整对话历史到内存
- 对话历史参与 DeepSeek 前缀缓存——恢复的会话能享受缓存命中（降低成本）
- `--resume` 可以强制恢复已空闲的会话，不加则可能提示选择

---

## 2. ACP 协议详解

### 2.1 协议基础

- **传输**: stdio（stdin/stdout）
- **格式**: NDJSON（每行一个 JSON 对象）
- **协议**: JSON-RPC 2.0（request/response + notification）
- **方向**: Client（网关）→ Agent（Reasonix），Agent 也会主动发 notification

### 2.2 完整协议流程

```
Client                              Agent (Reasonix acp)
  │                                      │
  │──── initialize ─────────────────────→│  握手
  │←─── {protocolVersion, capabilities} ─│
  │                                      │
  │──── session/new ────────────────────→│  创建会话
  │←─── {sessionId} ────────────────────│
  │                                      │
  │──── session/prompt ─────────────────→│  发送用户消息
  │     {sessionId, prompt: "消息内容"}   │
  │                                      │
  │←─── session/update ─────────────────│  流式响应（多条）
  │     {sessionId, update: {            │
  │       sessionUpdate: "agent_message_ │
  │         chunk",                      │
  │       content: {type:"text",text:...}│
  │     }}                               │
  │                                      │
  │←─── session/update ─────────────────│  工具调用通知
  │     {sessionUpdate: "tool_call",     │
  │      toolCallId, title, status}      │
  │                                      │
  │←─── session/update ─────────────────│  工具结果通知
  │     {sessionUpdate: "tool_call_      │
  │      update", status: "completed"}   │
  │                                      │
  │←─── {stopReason: "end_turn"} ───────│  结束
  │                                      │
  │──── session/cancel ─────────────────→│  取消（可选）
  │     {sessionId}                      │
```

### 2.3 请求/响应详情

**initialize:**
```json
→ {"method":"initialize","params":{
     "protocolVersion":"1.0",
     "clientInfo":{"name":"reasonix-gateway","version":"0.1"}
   }}

← {"result":{
     "protocolVersion":"1.0",
     "agentCapabilities":{
       "loadSession":false,
       "promptCapabilities":{"image":false,"audio":false,"embeddedContext":true},
       "mcpCapabilities":{"http":false,"sse":false}
     },
     "agentInfo":{"name":"reasonix","title":"Reasonix","version":"0.52.0"}
   }}
```

**session/new:**
```json
→ {"method":"session/new","params":{
     "cwd":"/path/to/project"   // 可选，默认用 acp --dir 指定的
   }}

← {"result":{"sessionId":"sess_20260527-032254-a1b2c3"}}
```

**session/prompt:**
```json
→ {"method":"session/prompt","params":{
     "sessionId":"sess_xxx",
     "prompt":"帮我写一个快速排序"
   }}

// 流式通知（多条）:
← {"method":"session/update","params":{
     "sessionId":"sess_xxx",
     "update":{
       "sessionUpdate":"agent_message_chunk",
       "content":{"type":"text","text":"好的，"}
     }
   }}
← {"method":"session/update","params":{
     "sessionId":"sess_xxx",
     "update":{
       "sessionUpdate":"agent_message_chunk",
       "content":{"type":"text","text":"我来写一个快速排序..."}
     }
   }}

// 最终响应:
← {"result":{"stopReason":"end_turn"}}
// stopReason: "end_turn" | "cancelled" | "error"
```

**session/cancel:**
```json
→ {"method":"session/cancel","params":{"sessionId":"sess_xxx"}}
```

**session/request_permission (工具审批):**
```json
← {"method":"session/request_permission","params":{
     "sessionId":"sess_xxx",
     "toolCall":{
       "toolCallId":"tc_xxx",
       "title":"execute: rm -rf /tmp/test",
       "kind":"execute",
       "status":"pending"
     },
     "options":[
       {"id":"allow_once","label":"Allow"},
       {"id":"allow_always","label":"Always Allow"},
       {"id":"reject_once","label":"Reject"}
     ]
   }}

→ {"result":{"outcome":{"optionId":"allow_once"}}}
```

### 2.4 ACP 会话特性

| 特性 | 说明 |
|------|------|
| 会话生命周期 | ACP 进程退出 → 所有会话丢失（内存态） |
| 会话持久化 | ACP 模式**不会**自动保存到 ~/.reasonix/sessions/ |
| 多会话 | 单个 ACP 进程可管理多个 session（通过 sessionId 区分） |
| 工具审批 | `--yolo` 自动批准所有工具，否则通过 permission 请求交互 |
| 模型选择 | `--model` 覆盖默认模型 |
| 系统提示追加 | `--system-append` 或 `REASONIX_ACP_SYSTEM_APPEND` 环境变量 |

---

## 3. 配置参考

### 3.1 全局配置 (~/.reasonix/config.json)

```json
{
  "apiKey": "sk-xxx",                    ← DeepSeek API Key
  "model": "deepseek-v4-flash",          ← 默认模型
  "editMode": "review",                  ← 编辑模式: "review" | "yolo"
  "editModeHintShown": true,             ← 是否已显示编辑模式提示
  "mouseClipboardHintShown": true,
  "dashboard": {
    "token": "xxx",                      ← 仪表盘访问 token
    "port": 39363                        ← 仪表盘端口
  }
}
```

### 3.2 关键配置项

| 配置 | 值 | 说明 |
|------|------|------|
| `apiKey` | `sk-xxx` | DeepSeek API Key，首次运行通过向导设置 |
| `model` | `deepseek-v4-flash` | 默认模型。可选: `deepseek-v4-pro` |
| `editMode` | `review` / `yolo` | 文件编辑审批模式。review=需确认，yolo=自动通过 |

### 3.3 命令行参数

| 参数 | 说明 |
|------|------|
| `-m, --model <id>` | 覆盖默认模型 |
| `--effort <level>` | 推理深度: low/medium/high/max |
| `--budget <usd>` | 会话费用上限（美元） |
| `--session <name>` | 指定会话名 |
| `-r, --resume` | 强制恢复（即使空闲） |
| `-c, --continue` | 恢复最近使用的会话 |
| `-n, --new` | 强制新建会话 |
| `--no-session` | 禁用会话持久化 |
| `--yolo` | 自动批准所有工具操作（ACP 模式） |
| `--system-append <prompt>` | 追加系统提示 |
| `--transcript <path>` | JSONL 格式的对话记录输出路径 |

### 3.4 模型切换（TUI 内 slash 命令）

| 命令 | 说明 |
|------|------|
| `/model deepseek-v4-flash` | 切到 Flash |
| `/model deepseek-v4-pro` | 切到 Pro |
| `/pro` | 下一轮用 Pro |
| `/preset max` | 整个 session 用 Pro |
| `/effort high` | 调推理深度 |
| `/new` | 新建会话 |
| `/help` | 查看所有命令 |

---

## 4. Memory 系统

### 4.1 四种记忆类型

| 类型 | 作用域 | 说明 |
|------|--------|------|
| `user` | 全局 | 用户偏好、个人信息 |
| `feedback` | 全局 | 用户纠正、反馈 |
| `project` | 项目级 | 项目特定知识 |
| `reference` | 全局 | 参考资料、约定 |

### 4.2 记忆存储

- 存储位置: `<project>/.reasonix/memory/`（项目级）或 `~/.reasonix/memory/`（全局）
- 注入方式: 记忆内容被钉入前缀（prefix），参与 DeepSeek 前缀缓存
- 操作: TUI 内通过 `remember` / `recall_memory` 工具调用

---

## 5. 设计要点：微信端持久会话

### 5.1 核心需求

微信端的要求：**永远在一个会话中**，不管网关重启、软件重启、系统关闭。

### 5.2 方案分析

**方案 A: ACP 模式 + 外部会话持久化（推荐）**
```
微信消息 → 网关 → ACP 子进程 → DeepSeek
                ↑
        网关维护会话状态：
        1. 保存每轮对话到外部 JSONL
        2. ACP 进程重启时，从 JSONL 重建对话历史
        3. 发送历史到 ACP 作为前缀（利用 DeepSeek 缓存）
```
- 优点: 会话持久化完全由网关控制，不依赖 Reasonix 内部机制
- 缺点: 需要自己管理历史加载和前缀重建

**方案 B: code 模式 + --session + --resume（更简单）**
```
微信消息 → 网关 → reasonix code --session wx-main --resume <dir>
                ↑
        Reasonix 自己管理会话持久化：
        1. ~/.reasonix/sessions/wx-main.jsonl 自动保存
        2. 重启后 --resume 自动恢复
```
- 优点: 利用 Reasonix 原生会话机制，最简单
- 缺点: code 模式有文件系统工具（微信场景可能不需要），session 名绑定目录

**方案 C: run 模式 + 外部上下文管理（最可控）**
```
微信消息 → 网关 → reasonix run "<带上下文的完整 prompt>"
                ↑
        网关完全控制上下文：
        1. 维护完整对话历史
        2. 每次调用时把历史拼进 prompt
        3. 无状态，完全由网关管理
```
- 优点: 最灵活，完全可控
- 缺点: 每次都发完整历史，token 成本高，无法利用 DeepSeek 前缀缓存

### 5.3 推荐方案

**方案 A（ACP + 外部持久化）** 是最佳平衡：

1. 网关用 `reasonix acp --yolo --effort high --dir <project>` 启动子进程
2. 每条微信消息通过 `session/prompt` 发送
3. 网关在外部维护完整的 JSONL 对话历史
4. ACP 进程崩溃/重启 → 网关检测到 → 重新启动 acp → 从 JSONL 重建上下文
5. 利用 DeepSeek 前缀缓存：重建的上下文如果和之前的前缀一致，缓存命中

**关键实现细节:**
- ACP 进程的 `session/new` 创建的是内存态会话
- 网关需要自己维护 `{user_id: session_id}` 映射
- 每轮 `session/prompt` 完成后，从 `session/update` 事件中提取 assistant 回复存入外部 JSONL
- 重启恢复时，把 JSONL 中的历史消息逐条 `session/prompt` 回放（或用 system prompt 注入摘要）
