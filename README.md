# CatPaw Relay

把 Claude Code / Pi 的 Anthropic Messages API 请求（`/v1/messages`）转成 CatPaw 后端协议，让 CatPaw IDE 的内网模型可以在任何兼容 Anthropic API 的客户端中使用。

## 工作原理

Relay 监听本地 `127.0.0.1:8789`，对外暴露标准 Anthropic Messages API，内部根据请求是否包含工具调用，自动走两条通道：

| 场景 | 通道 | 上游端点 | 说明 |
|------|------|---------|------|
| 纯文本轮次 | Gateway | `catpaw.sankuai.com/api/gpt/openai/stream` | IDE 面板端点，官方额度计量 |
| 含工具 / tool_result | Agent | `catpaw-agent.sankuai.com` | FRIDAY agent 中继，支持工具闭环 |

Agent 通道的完整流程：

1. `POST /api/oauth/token-exchange` — SSO 换 agent access_token（~25min 有效，本地缓存 24min）
2. `POST /api/agent/conversation/round` — 开新轮次
3. `POST /api/agent/conversation/event` — 更新会话状态
4. `POST /api/agent/conversation/turn` — SSE 流式推理

工具通过 `toolConfigs.fromClient: true` 注册为客户端工具；模型发起 `tool_use` 后，客户端本地执行，结果以 `tool_result` 回传，形成完整闭环。

## 支持的模型

| 模型 ID | 类型码 |
|---------|--------|
| gpt-6.1-sol | 102 |
| gpt-6.1-luna | 103 |
| gpt-6-sol | 101 |
| gpt-6-luna | 100 |
| gpt-6-astra | 95 |
| gpt-5.6-sol | 82 |
| gpt-5.6-luna | 81 |
| gpt-5.6-terra | 80 |
| deepseek-v4-pro | 64 |
| deepseek-v4-flash | 63 |
| deepseek-v4.1-flash | 97 |
| kimi-k3 | 83 |
| kimi-k2.7-code | 78 |
| longcat-2.0 | 77 |
| glm-5.3 | 89 |
| glm-5.3-flash | 91 |
| glm-5.3-flash-advisor | 96 |
| glm-5.3-flashx | 98 |
| glm-5.2 | 75 |
| glm-5v-turbo | 60 |
| minimax-m3 | 70 |
| grok-4.6 | 92 |
| gemini-3.6-flash | 84 |
| gemini-3.7-flash | 90 |
| gemini-3.8-flash | 94 |
| claude-opus-5.5 | 99 |
| hy4-preview | 93 |

完整列表可通过 `GET /v1/models` 查询。

## 安装

### 前置条件

- Python 3.10+
- CatPaw IDE 已登录（SSO token 自动从 `~/.catpaw/sso_config.json` 或 IDE 的 `state.vscdb` 读取）

### 1. 下载脚本

```bash
# 克隆仓库
git clone https://github.com/WeeeeeKdY/catpaw-relay.git
cd catpaw-relay

# 把主脚本放到 home 目录（可选，方便管理脚本直接找到）
cp catpaw-anthropic-relay.py ~/catpaw-anthropic-relay.py
cp catpaw-relay.sh ~/catpaw-relay.sh
chmod +x ~/catpaw-relay.sh
```

### 2. 启动 Relay

**手动启动：**

```bash
python3 ~/catpaw-anthropic-relay.py --port 8789
```

**通过管理脚本：**

```bash
~/catpaw-relay.sh start    # 启动
~/catpaw-relay.sh status   # 查看状态
~/catpaw-relay.sh log      # 查看日志
~/catpaw-relay.sh restart  # 重启
~/catpaw-relay.sh stop     # 停止
```

### 3. 开机自启（macOS launchd）

```bash
# 编辑 plist 中的路径（把 YOUR_USERNAME 替换为实际用户名）
sed -i '' "s/YOUR_USERNAME/$(whoami)/g" com.catpaw.relay.plist.example

# 如果你的 python3 不在 /usr/bin/python3，也需要修改
# 比如用 conda: sed -i '' 's|/usr/bin/python3|/opt/anaconda3/bin/python3|g' com.catpaw.relay.plist.example

# 安装
cp com.catpaw.relay.plist.example ~/Library/LaunchAgents/com.catpaw.relay.plist
launchctl load ~/Library/LaunchAgents/com.catpaw.relay.plist
```

### 4. 验证

```bash
curl http://127.0.0.1:8789/health
# → {"ok": true, "token_loaded": true}
```

## 使用方式

### Claude Code

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8789 claude --model gpt-6-sol
```

### Pi / Lyu-code

#### 方式 A：环境变量

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8789 pi --model gpt-6-sol
```

#### 方式 B：Pi Provider 扩展（推荐）

Provider 扩展让 relay 在 Pi 中作为一等公民模型提供，支持 tab 补全选择：

```bash
# 将 provider/ 目录复制到 Pi 的扩展目录
cp -r provider ~/.pi/agent/extensions/catpaw-relay-provider
```

然后在 `~/.pi/agent/settings.json` 的 `packages` 中添加（如果还没有）：

```json
{
  "packages": [
    "~/.pi/agent/extensions/catpaw-relay-provider"
  ]
}
```

之后可以直接使用：

```bash
pi --model catpaw-relay/gpt-6-sol
pi --model catpaw-relay/gpt-5.6-luna
pi --model catpaw-relay/deepseek-v4-pro
```

### 任何 OpenAI/Anthropic 兼容客户端

只要客户端支持自定义 `base_url`，指向 `http://127.0.0.1:8789` 即可。Relay 完整实现了 Anthropic Messages API 的 stream 和 non-stream 模式，包括工具调用闭环。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CATPAW_MIS_ID` | 空 | SSO misId，未设置时从 sso_config.json / state.vscdb 自动读取 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/v1/models` | 列出可用模型 |
| POST | `/v1/messages` | Anthropic Messages API（stream / non-stream） |

## 会话管理

Relay 内置会话管理：

- 相同的 (模型 + system prompt + 首条用户消息) 映射到同一个 CatPaw conversationId
- 增量发送：只发送上游未见过的新轮次，避免重复
- 长对话压缩：超过一定轮次后，将历史压缩为一条快照重新导入
- 自动重试：上游会话忙碌时（"会话正在被处理"）自动退避重试或新建会话
- 会话 TTL：4 小时无活动自动过期

## 架构

```
┌──────────────────┐     Anthropic Messages API     ┌──────────────────┐
│  Claude Code /   │ ─────────────────────────────→ │  catpaw-anthropic│
│  Pi / 任意客户端  │    http://127.0.0.1:8789      │  -relay.py       │
└──────────────────┘                                └────────┬─────────┘
                                                             │
                                              ┌──────────────┴──────────────┐
                                              │                             │
                                     纯文本轮次                     含工具轮次
                                              │                             │
                                              ▼                             ▼
                                    ┌─────────────────┐         ┌─────────────────┐
                                    │  CatPaw Gateway  │         │  CatPaw Agent   │
                                    │  /api/gpt/openai │         │  /api/agent/*   │
                                    │  /stream         │         │  (工具闭环)      │
                                    └─────────────────┘         └─────────────────┘
```

## 故障排查

**Token 未加载：**
```bash
# 确认 CatPaw IDE 已登录
ls ~/.catpaw/sso_config.json
# 如果文件不存在或 ssoid 为空，重新登录 CatPaw IDE
```

**端口被占用：**
```bash
# 检查端口
lsof -i :8789
# 使用其他端口
python3 catpaw-anthropic-relay.py --port 8790
```

**Agent 通道报 "会话正在被处理"：**
- Relay 会自动退避重试（0/2/4 秒），如果仍然失败，会丢弃旧会话创建新会话
- 如果持续出现，可能是上游确实在处理上一轮，等几秒再发请求

## 许可证

MIT
