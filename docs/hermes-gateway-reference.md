# Hermes 网关 Agent 层参考文档

> 来源: `/usr/local/lib/hermes-agent/gateway/run.py` (18,205行) + `session.py` (1,404行)
> 用途: 作为 Reasonix 网关第3层（Agent层）设计时的功能对标参考。
> 不需要照搬代码，需要哪个功能时回来查具体实现。

---

## 1. 总体架构

**GatewayRunner** 是网关主类（run.py），职责：
- 管理所有 platform adapter 的生命周期（connect/disconnect）
- 接收 adapter 产出的 MessageEvent，路由到 agent 处理
- 管理 agent 实例缓存和会话
- 处理斜杠命令（/new, /reset, /model 等）
- 将 agent 回复通过 adapter 发回用户

**核心循环:**
```
adapter.connect() → poll_loop → MessageEvent → _handle_message() → agent → adapter.send()
```

---

## 2. 会话管理 (session.py, 1404行)

### SessionSource 数据类
```python
@dataclass
class SessionSource:
    platform: Platform          # "weixin", "telegram", etc.
    chat_id: str                # 对话标识（微信: user_id 或 room_id）
    chat_name: Optional[str]
    chat_type: str              # "dm", "group", "channel", "thread"
    user_id: Optional[str]      # 发送者 ID
    user_name: Optional[str]
    thread_id: Optional[str]    # 话题/帖子 ID（Telegram/Discord）
    message_id: Optional[str]   # 触发消息的 ID
    guild_id: Optional[str]     # Discord guild / Slack workspace
    # ... 更多可选字段
```

### SessionStore 功能
- `get_or_create_session(source)` — 根据 platform+chat_id+user_id 生成 session_key，查找或创建 session
- `switch_session(session_key, session_id)` — 切换到指定 session（用于 /session 命令）
- session_key 生成规则: `{platform}:{chat_id}:{user_id}` 或 `{platform}:{chat_id}:{thread_id}`
- 会话持久化: JSON 文件存储在 `~/.hermes/sessions/`

### SessionResetPolicy
```python
# config.yaml:
session_reset:
  mode: both          # "both" | "idle" | "schedule"
  idle_minutes: 1440  # 空闲多久后自动重置
  at_hour: 4          # 每天几点重置（schedule 模式）
```
- `mode=both`: 空闲超时 + 定时都触发
- 自动重置时清除 session 历史、model override、reasoning override

---

## 3. 消息处理流水线 (run.py:6420)

### `_handle_message(event: MessageEvent)` 核心流程:

```
1. pre_gateway_dispatch hook
   ↓ 插件可以拦截/修改/跳过消息
2. 用户授权检查 (_is_user_authorized)
   ↓ 未授权 → pairing code / 静默忽略
3. 斜杠命令检测
   ↓ /new, /reset, /model, /pro, /help 等 → 直接处理
4. 运行中 agent 检查
   ↓ 如果有 agent 正在跑 → 中断当前任务
5. 获取/创建 session (session_store.get_or_create_session)
6. 调用 _handle_message_with_agent()
7. 回复发送 (adapter.send)
```

### `_handle_message_with_agent()` 细节 (run.py:7824):

- **Telegram topic 恢复**: 检测并恢复 thread_id 绑定
- **Session auto-reset 处理**: 清除 model override、reasoning override
- **Agent 获取**: `_get_or_create_agent(session_key, session_entry)`
- **对话历史构建**: 从 session store 加载历史消息
- **System prompt 注入**: 平台信息、时间、session context
- **Agent 调用**: `agent.run_conversation(user_message, system_message, history)`
- **回复处理**: 错误重写（provider error → 用户友好文本）、分块、发送

---

## 4. Agent 创建和管理

### `_get_or_create_agent(session_key, session_entry)`:

- **缓存策略**: LRU 缓存，最多 128 个 agent 实例
- **Idle TTL**: 空闲超过 1 小时的 agent 被淘汰
- **Agent 关键参数**:
  - `model` — 可被 session override 覆盖
  - `provider` — 模型提供商
  - `session_id` — 对应 session store 的 session
  - `platform` — "weixin", "telegram" 等
  - `enabled_toolsets` — 平台特定工具集
  - `max_iterations` — 工具调用最大轮数
  - `reasoning_effort` — 推理深度

### Session Model Override:
- 用户发 `/model deepseek-v4-pro` → 仅当前 session 切换模型
- 用户发 `/pro` → 下一轮切换到 Pro 模型
- `/preset max` → 整个 session 都用 Pro

---

## 5. 斜杠命令系统

### 已知网关命令 (`GATEWAY_KNOWN_COMMANDS`):

| 命令 | 功能 | 处理位置 |
|------|------|----------|
| `/new` | 新建 session | gateway 层直接处理 |
| `/reset` | 重置当前 session | gateway 层直接处理 |
| `/model <id>` | 切换 session 模型 | gateway 层直接处理 |
| `/pro` | 下一轮用 Pro 模型 | gateway 层直接处理 |
| `/help` | 显示帮助 | gateway 层直接处理 |
| `/usage` | 显示 API 用量 | gateway 层直接处理 |
| `/stats` | 显示统计 | gateway 层直接处理 |
| `/session [name]` | 切换/列出 session | gateway 层直接处理 |
| `/compact` | 压缩上下文 | 传递给 agent |
| `/skill` | 加载 skill | 传递给 agent |

### 命令处理模式:
1. 消息以 `/` 开头 → 解析命令名
2. 匹配 `GATEWAY_KNOWN_COMMANDS` → gateway 层处理
3. 不匹配 → 当作普通消息传给 agent（agent 内部也有 skill 命令）

---

## 6. 回复发送

### 回复路由:
- 从 `event.source` 取 platform + chat_id
- 调用对应 adapter 的 `send(chat_id, content)` 方法

### 文本分块:
- 每个平台有自己的 `MAX_MESSAGE_LENGTH`（微信: 2000字符）
- 分块策略: Markdown block-aware（不切断代码块）
- 块间延迟: `send_chunk_delay_seconds`（微信默认 1.5s）

### 媒体发送:
- 回复中包含 `MEDIA:/path` 标签 → adapter 解析后上传 CDN 发送
- 支持: 图片、视频、文件、音频
- 自动检测 MIME 类型路由到对应发送方法

### 流式回复:
- 部分平台支持消息编辑（Telegram）
- 微信不支持编辑 → 使用 "send-final-only" 策略
- 中间状态通过 typing indicator 反馈

### 错误处理:
- Provider error → 用户友好文本替换（不暴露原始错误）
- Rate limit → 自动退避+重试
- Auth error → 提示检查 API key
- 原始错误记录到 gateway.log

---

## 7. 主动消息

### Cron Job 触发:
- `cron/` 模块管理定时任务
- 到时间 → 创建独立 agent session → 执行 → 通过 adapter 发送结果
- delivery target 可指定 platform:chat_id

### Hook 系统:
- `pre_gateway_dispatch` — 消息到达 agent 前的拦截点
- 插件可返回 `{action: "skip"/"rewrite"/"allow"}`
- 用途: 客服转接、消息过滤、内容改写

### Background Process Notifications:
- agent 工具执行的后台进程完成时 → 自动通知用户
- `gateway_notify_interval` 控制通知间隔

### 超时和续期:
- `gateway_auto_continue_freshness: 3600` — 1小时内自动续期
- `gateway_timeout: 1800` — 单次 agent 运行超时 30 分钟

---

## 8. 关键配置项

```yaml
# config.yaml 中与网关相关的配置:

agent:
  max_turns: 90              # 工具调用最大轮数
  gateway_timeout: 1800      # agent 运行超时（秒）
  restart_drain_timeout: 180 # 重启排空超时
  api_max_retries: 3         # API 调用重试次数
  tool_use_enforcement: auto # 工具使用策略
  gateway_timeout_warning: 900  # 超时警告阈值

session_reset:
  mode: both
  idle_minutes: 1440
  at_hour: 4

approvals:
  mode: false               # 工具审批模式（false=自动批准）
  timeout: 60

streaming:
  enabled: false            # 流式回复（微信不支持编辑，设为 false）

weixin:
  extra:
    account_id: xxx@im.bot
    dm_policy: open          # "open" | "allowlist" | "disabled"
    group_policy: disabled
    send_chunk_delay_seconds: 1.5
    split_multiline_messages: false
```

---

## 9. 设计要点（对接 Reasonix 时参考）

1. **会话管理**: 按微信 user_id 维护独立 session，每个 user_id 对应一个 Reasonix 子进程
2. **命令系统**: 需要实现 /new（重启 acp 进程）、/reset（清历史）、/model（切模型）、/pro（切Pro）
3. **回复策略**: Reasonix ACP 的 content 事件是流式的 → 累积后一次性发送（微信不支持编辑）
4. **工具审批**: Reasonix 默认 review 模式需要确认 → 微信场景设 yolo 或自定义审批流
5. **错误处理**: Reasonix 子进程崩溃 → 自动重启 + 通知用户
6. **文件传输**: 微信收文件 → 存到 Reasonix 项目目录 → 告知路径；Reasonix 产出文件 → 检测变更 → 微信发送
7. **主动消息**: Reasonix 有异步事件（如长时间任务完成）→ 通过 adapter 推送
8. **限速**: 复用 weixin adapter 的自适应限速策略（1s 起步，翻倍退避，上限 16s）
