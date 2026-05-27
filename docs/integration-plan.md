# 微信 ↔ Reasonix 对接方案

> 基于: hermes-gateway-reference.md + reasonix-session-reference.md
> 目标: 微信用户发消息 → Reasonix 处理 → 微信回复，体验对标 Hermes 网关

---

## 1. 整体架构

```
微信用户
   │
   ▼
iLink API (getupdates)              ← transport/ 层（已完成）
   │
   ▼
WeixinAdapter (收发+格式化)          ← adapter/ 层（已完成）
   │
   ▼
ReasonixGateway (核心调度)           ← agent/ 层（本方案）
   ├─ SessionManager                ← user_id → session 映射 + 持久化
   ├─ AcpClient                     ← ACP JSON-RPC 客户端
   ├─ CommandHandler                ← /new /reset /pro /model 等
   ├─ FileHandler                   ← 文件双向传输
   └─ HistoryStore                  ← 对话历史 JSONL 存储
   │
   ▼
Reasonix ACP 子进程 (reasonix acp)   ← 外部进程，通过 stdio 通信
   │
   ▼
DeepSeek API                        ← Reasonix 内部处理
```

---

## 2. 接口选择：ACP 模式

### 为什么选 ACP

| 方案 | 会话持久化 | 工具调用 | 流式响应 | 缓存利用 | 复杂度 |
|------|-----------|---------|---------|---------|--------|
| `reasonix run` | 无状态 | 有 | stdout | 不可 | 最低 |
| `reasonix code --session` | 原生支持 | 有 | TUI | 可 | 中等 |
| **`reasonix acp`** | **网关可控** | **有** | **JSON-RPC** | **可** | **中等** |

- `run` 无状态，每次完整对话历史都要重发，token 成本高
- `code` 绑 TUI，程序化接入困难，session 名绑定目录
- **`acp`** 标准协议，程序化控制，网关自管持久化——最佳选择

### ACP 启动命令

```bash
reasonix acp \
  --dir /root/reasonix-workspace \    # 工作目录（所有微信用户共享）
  --yolo \                            # 自动批准工具（微信场景无法交互确认）
  --effort high \                     # 推理深度
  --model deepseek-v4-flash           # 默认模型
```

---

## 2.5 架构验证：Reasonix QQ 通道参照

Reasonix 已内置 QQ 通道（`/qq connect`），其架构模式与本方案完全同构，验证了可行性。

**QQChannel 类接口**（源码位置: chunk-MVLPXZAA.js:3984）:
```
constructor(callbacks)
  callbacks.onSubmitMessage(msg)  → 注入消息到当前 Reasonix 会话
  callbacks.onError(msg)          → 错误通知
start()                           → 连接 QQ Bot API（WebSocket）
sendResponse(text)                → 发回复到 QQ（自动分块）
stop()                            → 断开
```

**集成方式**（useQQChannel hook，chunk-GNRKXRRE.js:44840）:
```javascript
const channel = new QQChannel({
    onSubmitMessage: (msg) => setQueuedSubmit(msg),
    onError: (msg) => log.pushWarning("QQ", msg),
});
await channel.start();
```

**消息流**:
```
QQ 消息 → handlePrivateMessage → callbacks.onSubmitMessage("[QQ] text")
→ 注入当前 Reasonix 会话 → 模型处理 → channel.sendResponse(reply)
```

**与本方案的对应关系**:
| QQ 通道 | 我们的 ACP 方案 | 语义 |
|---------|----------------|------|
| callbacks.onSubmitMessage | session/prompt | 注入用户消息 |
| channel.sendResponse | session/update 事件 | 提取助手回复 |
| channel.start() | ACP 进程启动 | 建立连接 |
| channel.stop() | ACP 进程退出 | 断开连接 |

**结论**: Reasonix 的通道模式就是"外部消息注入 + 回复提取"。ACP 协议做的本质同一件事。
QQ 通道验证了这个模式可行。Reasonix 是编译后的 JS bundle，无法直接注入 WeixinChannel，
因此采用独立网关 + ACP 协议对接。

---

## 3. 会话持久化策略（核心）

### 3.1 设计目标

微信端永远在一个会话中——网关重启、软件重启、系统关闭后，对话连续。

### 3.2 双层持久化

```
第1层：网关侧（我们自己管）
  ~/.reasonix-gateway/sessions/
  ├── <user_id>.jsonl              ← 完整对话历史（user + assistant 消息）
  ├── <user_id>.meta.json          ← 会话元数据（创建时间、轮数、成本）
  └── <user_id>.state.json         ← 运行状态（acp session_id、进程PID）

第2层：Reasonix 侧（ACP 进程内存）
  ACP 进程内的 session 对象——进程活着就有，进程死了就没了
```

### 3.3 消息流转

```
微信消息到达
   │
   ▼
SessionManager.get_or_create(user_id)
   ├─ 有活跃 ACP 进程 → 直接 session/prompt
   └─ 无活跃进程 → 重启 ACP + 恢复历史
   │
   ▼
AcpClient.send_prompt(session_id, message)
   │
   ▼
收集 session/update 事件
   ├─ agent_message_chunk → 累积回复文本
   ├─ tool_call → 记录工具调用
   └─ stopReason: end_turn → 结束
   │
   ▼
HistoryStore.append(user_id, user_msg, assistant_reply)
   │
   ▼
WeixinAdapter.send(chat_id, reply)
```

### 3.4 崩溃恢复流程

```
网关启动
   │
   ▼
扫描 ~/.reasonix-gateway/sessions/*.state.json
   │
   ▼
对每个有活跃标记的会话：
   1. 启动新的 ACP 子进程
   2. session/new 创建新会话
   3. 读取 <user_id>.jsonl 获取历史
   4. 方案A（推荐）：把历史摘要注入 system prompt
      → "此前对话摘要：用户问了X，你回答了Y..."
   5. 方案B：逐条回放历史消息（token 成本高但缓存友好）
   6. 更新 state.json 中的 session_id
```

### 3.5 历史注入策略

**摘要注入（推荐，成本低）:**
```python
# 读取最近 N 轮对话
history = read_last_n_turns(user_id, n=10)

# 构建摘要
summary = "此前对话上下文：\n"
for turn in history:
    summary += f"用户: {turn.user_msg[:100]}\n"
    summary += f"助手: {turn.assistant_msg[:100]}\n"

# 作为 system_append 注入
acp_session.prompt(summary + "\n\n" + new_message)
```

**完整回放（成本高但缓存友好）:**
```python
# 逐条发送历史消息到 ACP（不会触发模型响应，只建立上下文）
for turn in history:
    acp_client.send_prompt(session_id, turn.user_msg)
    # 等待 end_turn，丢弃响应

# 发送新消息
acp_client.send_prompt(session_id, new_message)
```

---

## 4. ACP 客户端设计

### 4.1 类结构

```python
class AcpClient:
    """管理一个 Reasonix ACP 子进程的生命周期。"""

    process: subprocess.Popen       # ACP 子进程
    session_id: str                 # 当前 ACP session ID
    pending_requests: dict          # 等待响应的 request_id → Future
    _reader_task: asyncio.Task      # stdout 读取协程

    async def start(dir, model, effort, yolo)
    async def stop()
    async def initialize()          # 发送 initialize，获取 capabilities
    async def new_session(cwd)      # 发送 session/new，获取 session_id
    async def send_prompt(text)     # 发送 session/prompt，流式收集响应
    async def cancel()              # 发送 session/cancel
    async def _read_loop()          # 持续读取 stdout，分发 response/notification
```

### 4.2 流式响应收集

```python
async def send_prompt(self, text: str) -> AsyncIterator[str]:
    """发送 prompt，流式 yield 文本片段。"""
    self._current_response = ""
    self._response_complete = asyncio.Event()

    # 发送 session/prompt 请求
    await self._send_request("session/prompt", {
        "sessionId": self.session_id,
        "prompt": text,
    })

    # 等待并 yield 响应片段
    while not self._response_complete.is_set():
        chunk = await self._response_queue.get()
        if chunk is None:  # end_turn signal
            break
        yield chunk

    return self._current_response
```

### 4.3 事件分发

```python
# _read_loop 中处理两种消息：

# 1. Response（有 id）— 对应之前的 request
if "id" in msg and "result" in msg:
    future = pending_requests.pop(msg["id"])
    future.set_result(msg["result"])

# 2. Notification（有 method，无 id）— Agent 主动推送
elif "method" in msg:
    if msg["method"] == "session/update":
        update = msg["params"]["update"]
        if update["sessionUpdate"] == "agent_message_chunk":
            text = update["content"]["text"]
            self._response_queue.put_nowait(text)
        elif update["sessionUpdate"] == "tool_call":
            # 记录工具调用
            pass
```

---

## 5. 命令系统

### 5.1 斜杠命令

| 命令 | 实现 | 说明 |
|------|------|------|
| `/new` | 重启 ACP 进程 + 清空历史 | 新会话 |
| `/reset` | 重启 ACP 进程 + 保留历史摘要 | 重置但不丢上下文 |
| `/model <id>` | 重启 ACP + `--model <id>` | 切换模型 |
| `/pro` | 重启 ACP + `--model deepseek-v4-pro` | 切 Pro |
| `/flash` | 重启 ACP + `--model deepseek-v4-flash` | 切 Flash |
| `/effort <level>` | 重启 ACP + `--effort <level>` | 调推理深度 |
| `/status` | 读取 meta.json | 显示会话状态 |
| `/help` | 返回帮助文本 | 帮助 |

### 5.2 为什么多数命令要重启 ACP

ACP 协议不支持运行时切换模型/effort——这些参数在进程启动时确定。
要切换只能重启进程。但因为有历史持久化，重启后可以恢复上下文。

---

## 6. 文件传输

### 6.1 微信 → Reasonix（入站文件）

```
微信收到文件
   │
   ▼
WeixinAdapter 下载到临时目录 /tmp/weixin-files/xxx.jpg
   │
   ▼
FileHandler.copy_to_workspace(tmp_path, user_id)
   │
   ▼
复制到 Reasonix 工作目录: /root/reasonix-workspace/uploads/<user_id>/xxx.jpg
   │
   ▼
向 ACP 发送提示: "用户发送了文件: uploads/<user_id>/xxx.jpg"
```

### 6.2 Reasonix → 微信（出站文件）

```
Reasonix 工具调用产出文件（写入 /root/reasonix-workspace/ 下）
   │
   ▼
FileHandler.detect_new_files(workspace_dir, before_snapshot)
   │
   ▼
对比文件系统快照，发现新增/修改的文件
   │
   ▼
WeixinAdapter.send_document(chat_id, file_path)
```

---

## 7. 主动消息

### 7.1 Reasonix 工具执行通知

ACP 的 `session/update` 事件中包含工具调用状态。对于长时间运行的工具（shell 命令），
可以中间通知用户：

```
session/update: tool_call status=in_progress → 微信: "正在执行: git clone ..."
session/update: tool_call status=completed   → 微信: "执行完成 ✓"
```

### 7.2 超时保护

单次 `session/prompt` 设置超时（默认 300 秒）。超时后：
1. 发送 `session/cancel`
2. 通知用户: "请求超时，已取消。请简化你的问题。"

---

## 8. 项目结构（完整）

```
/root/reasonix-gateway/
├── transport/                     ← 第1层：iLink API（已完成）
│   ├── ilink_api.py
│   ├── crypto.py
│   ├── cdn.py
│   ├── context_token.py
│   ├── account.py
│   └── __init__.py
│
├── adapter/                       ← 第2层：微信适配（已完成）
│   ├── weixin_adapter.py
│   ├── types.py
│   ├── dedup.py
│   └── __init__.py
│
├── agent/                         ← 第3层：Reasonix 对接（待实现）
│   ├── acp_client.py             ← ACP JSON-RPC 客户端
│   ├── session_manager.py        ← 会话管理 + 持久化
│   ├── command_handler.py        ← 斜杠命令处理
│   ├── file_handler.py           ← 文件双向传输
│   ├── history_store.py          ← 对话历史 JSONL 存储
│   └── __init__.py
│
├── docs/                          ← 参考文档（已完成）
│   ├── hermes-gateway-reference.md
│   ├── reasonix-session-reference.md
│   └── integration-plan.md       ← 本文件
│
├── config.yaml                    ← 网关配置
│   # account_id: 新微信账号
│   # workspace_dir: /root/reasonix-workspace
│   # model: deepseek-v4-flash
│   # effort: high
│   # yolo: true
│   # max_history_turns: 20
│   # prompt_timeout: 300
│
├── main.py                        ← 入口
└── README.md
```

---

## 9. 实施计划

```
阶段1: ACP 客户端 (acp_client.py)
  - 实现 JSON-RPC 2.0 over stdio
  - initialize / session/new / session/prompt / session/cancel
  - 流式响应收集
  - 超时和错误处理
  验证: Python 脚本能启动 acp、发送消息、收到回复

阶段2: 会话管理 (session_manager.py + history_store.py)
  - user_id → session 映射
  - JSONL 对话历史读写
  - 崩溃恢复（重启 ACP + 历史注入）
  验证: 杀掉 ACP 进程后重启，能恢复对话

阶段3: 命令系统 (command_handler.py)
  - /new /reset /pro /model /status /help
  - 模型切换（重启 ACP）
  验证: 微信发 /pro 能切模型

阶段4: 入口和集成 (main.py)
  - 启动 WeixinAdapter + AcpClient
  - 消息路由: 微信 → ACP → 微信
  - 配置文件加载
  验证: 微信发消息 → Reasonix 回复

阶段5: 文件和主动消息 (file_handler.py)
  - 入站文件: 微信 → 工作目录
  - 出站文件: 工作目录变更 → 微信发送
  - 工具执行通知
```

---

## 10. 与 Hermes 网关的对标

| 功能 | Hermes 网关 | Reasonix 网关（本方案） |
|------|------------|----------------------|
| 会话存储 | SQLite SessionDB | JSONL 文件 |
| Agent 实例 | AIAgent Python 类 | ACP 子进程 |
| 会话恢复 | 从 DB 加载历史 | 从 JSONL 恢复 + 摘要注入 |
| 模型切换 | session model override | 重启 ACP 进程 |
| 斜杠命令 | gateway 层直接处理 | command_handler 处理 |
| 媒体发送 | MEDIA: 标签解析 | 复用 adapter 层 |
| 主动消息 | cron/hook | ACP 事件通知 |
| 错误处理 | provider error → 友好文本 | ACP error → 友好文本 |
| 限速 | 自适应退避 | 复用 adapter 层 |
| 文件传输 | MEDIA: 标签 | 文件系统监听 |
