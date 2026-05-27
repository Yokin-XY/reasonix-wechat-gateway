# reasonix-wechat-gateway

微信 ↔ [Reasonix](https://github.com/esengine/DeepSeek-Reasonix) 网关。通过 iLink Bot API 将微信消息路由到 Reasonix（DeepSeek 原生终端编程 Agent），实现微信对话式编程。

## 这是什么

一个独立的 Python 网关进程，桥接微信和 Reasonix：

```
微信用户 → iLink API → 网关 → Reasonix ACP → DeepSeek API → 回复 → 微信
```

支持：文字对话、文件/图片收发、斜杠命令、会话持久化、崩溃恢复。

## 快速开始

### 1. 环境要求

- Python 3.10+
- Node.js 22+
- Reasonix 已安装：`npm install -g reasonix`
- DeepSeek API Key 已配置：`reasonix setup`

### 2. 安装依赖

```bash
pip install aiohttp cryptography
```

### 3. 微信扫码登录

```bash
python main.py --login
```

终端会显示二维码，用微信扫码确认。登录成功后会显示 account_id 和 token。

### 4. 启动网关

```bash
python main.py --account-id <你的account_id> --token <你的token>
```

也可以把凭证存到 `~/.reasonix-gateway/weixin/accounts/` 下，网关会自动加载。

### 5. 安装 Skill（可选）

仓库附带一个 Reasonix skill，教 Reasonix 通过 `MEDIA:` 标签向微信发送文件：

```bash
cp skills/send-file.md ~/.reasonix/skills/
```

## 命令行参数

```
python main.py [选项]

--login              微信扫码登录（首次使用）
--account-id <id>    微信 iLink 账号 ID
--token <token>      微信 iLink Token
--model <model>      Reasonix 模型（默认: deepseek-v4-flash）
--effort <level>     推理深度: low/medium/high/max（默认: high）
--dir <path>         Reasonix 工作目录（默认: /root/reasonix-workspace）
```

## 微信端斜杠命令

| 命令 | 功能 |
|------|------|
| `/new` | 新建会话 |
| `/reset` | 重置会话 |
| `/pro` | 切换到 DeepSeek-V4-Pro |
| `/flash` | 切换到 DeepSeek-V4-Flash |
| `/model <名称>` | 切换到指定模型 |
| `/status` | 查看会话状态 |
| `/help` | 显示帮助 |

## 文件传输

### 入站（微信 → Reasonix）

微信发送的图片/文件/视频/音频会自动下载到工作目录的 `uploads/` 子目录，Reasonix 可以用 `read_file` 工具读取。

### 出站（Reasonix → 微信）

Reasonix 在回复中使用 `MEDIA:/绝对路径` 标签即可发送文件：

```
文件已生成。
MEDIA:/root/reasonix-workspace/output.py
```

配合 `skills/send-file.md` skill，Reasonix 会在用户要求时自动使用此格式。

## 架构

```
reasonix-wechat-gateway/
├── transport/          第1层：iLink API 传输层
│   ├── ilink_api.py    HTTP 核心（getupdates/sendmessage/typing/config）
│   ├── crypto.py       AES-128-ECB 加解密
│   ├── cdn.py          CDN 上传下载
│   ├── context_token.py ContextTokenStore + TypingTicketCache
│   └── account.py      账号凭证持久化
│
├── adapter/            第2层：微信消息适配层
│   ├── weixin_adapter.py WeixinAdapter（收发/格式化/媒体/限速/重试）
│   ├── types.py        MessageEvent/SendResult/MessageType
│   └── dedup.py        消息去重
│
├── agent/              第3层：Reasonix ACP 对接层
│   ├── acp_client.py   ACP JSON-RPC 客户端
│   ├── session_manager.py 会话管理 + 持久化
│   └── command_handler.py 斜杠命令处理
│
├── skills/             Reasonix Skill 文件
│   └── send-file.md    文件发送 skill（网关同步）
│
├── docs/               参考文档
│   ├── hermes-gateway-reference.md
│   ├── reasonix-session-reference.md
│   └── integration-plan.md
│
└── main.py             入口
```

## 会话持久化

对话历史保存在 `~/.reasonix-gateway/sessions/` 下，按微信 user_id 隔离。网关重启后自动恢复会话。

## 致谢

- [Reasonix](https://github.com/esengine/DeepSeek-Reasonix) — DeepSeek 原生终端编程 Agent
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — 网关架构参考
- iLink Bot API — 微信机器人接口

## 许可

MIT
