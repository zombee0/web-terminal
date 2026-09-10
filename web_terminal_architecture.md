# BoxLite Agent Terminal 架构

## 结论

如果 UI 定制是产品硬需求，建议保留自建 Go Web Terminal。

`dev.boxlite.ai` 的官方 `22222` Terminal UI 由 BoxLite Runner 固定提供，无法通过镜像或 Box SDK 修改字体、主题、scrollback、Unicode 行为等配置。

推荐同时保留两种入口：

```text
22222：BoxLite 官方 Terminal，作为稳定兜底
3001：自定义 Go Web Terminal，作为产品主入口
```

## 镜像内容

Box 镜像应包含：

- Codex CLI
- tmux
- Go Web Terminal 二进制
- Bash
- UTF-8 locale
- Web Terminal 启动配置

镜像中不应包含：

- BoxLite API Key
- 用户的 ChatGPT 登录凭据
- 用户的 OpenAI API Key
- 固定用户数据

## 官方 `22222` Terminal

官方 Terminal 的优势：

- BoxLite Runner 官方 WebSocket 实现
- 标准 PTY 生命周期管理
- WebSocket Ping/Pong 保活
- 窗口 resize 支持
- Preview 鉴权集成
- Box 生命周期关联
- 与 BoxLite 内部 API 保持一致

官方 Terminal 的限制：

- UI 配置由 Runner 固定提供
- 无法通过镜像修改主题和字体
- 无法通过 Box SDK 覆盖 xterm 配置
- 不能为单个 Box 注入自定义前端代码

官方 `22222` 应作为基础能力和故障排查入口。

## 自定义 Go `3001` Terminal

Go Terminal 负责提供产品化 UI，同时在 Box 内启动独立 PTY Shell。

实现应使用成熟库：

- `github.com/gorilla/websocket`
- `github.com/creack/pty/v2`

不应继续使用 Python 手写 WebSocket 协议。

Go Terminal 必须支持：

- 标准 WebSocket Upgrade
- BinaryMessage 传输 PTY 原始输出
- Ping/Pong 心跳
- Close 处理
- 输入输出并发写锁
- 窗口 resize
- PTY 退出和清理
- UTF-8 locale
- `TERM=xterm-256color`
- Codex PATH

终端输出应使用 WebSocket 二进制帧，不能把原始 PTY 字节作为文本帧发送，否则浏览器可能因 `Invalid UTF-8 in text frame` 主动关闭连接。

## UI 定制能力

自建 Go Terminal 可以定制：

- 主题颜色
- 字体和中文字体回退
- 光标样式
- scrollback 大小
- 自动 resize
- Unicode 处理
- 断线提示和重连策略
- Agent 专用按钮和状态栏
- Codex 启动入口

当前产品可以使用 xterm.js，并配置 Unicode 11、UTF-8 locale 和中文字体回退。

## Preview 鉴权

自定义 `3001` 服务不应自行实现用户认证，应交给 BoxLite Preview Proxy。

官方 `22222` 支持 BoxLite 的 signed preview URL。

自定义 `3001` 通常使用普通 Preview URL，并携带：

```text
BOXLITE_BOX_AUTH_KEY
```

由于 signed preview URL 目前主要支持官方 Terminal 端口 `22222`，自定义端口需要特别注意 Preview Token 的有效期、URL 泄露和 Auth0 回调配置。

## Box 编排边界

BoxLite SDK 应由平台后端调用，而不是由 Box 内的 Agent 调用：

```text
用户
  ↓
平台后端
  ↓ BoxLite SDK / REST API
创建 Box
  ↓
启动 Box
  ↓
返回 Terminal URL
```

平台后端负责：

- 创建和删除 Box
- 设置 CPU 和 Memory
- 设置网络白名单
- 设置 `auto_stop` 和 `auto_resume`
- 注入必要配置
- 生成 Preview URL
- 处理超时和清理

## 认证模式

### ChatGPT Device Login

Box 内执行：

```bash
codex login --device-auth
```

平台展示授权地址和一次性验证码，用户完成 ChatGPT 登录后，平台轮询：

```bash
codex login status
```

确认登录成功后返回 Terminal URL。

验证码应设置过期时间，不写入普通日志，也不向其他用户展示。

### 平台托管 API

该模式可以先预留但暂不启用：

```text
[x] ChatGPT 登录（Device Login）
[ ] 平台托管 API（即将开放）
```

前后端可以预留：

```json
{
  "auth_mode": "chatgpt_device"
}
```

未来启用时使用：

```json
{
  "auth_mode": "platform_api"
}
```

平台自己的 API Key 必须由平台后端或 Secret Manager 管理，不能暴露给用户、写入镜像、放入 URL 或写入普通日志。

## 推荐用户流程

用户选择：

```text
CPU：1 / 2 / 4
Memory：1 GiB / 2 GiB / 4 GiB
认证方式：ChatGPT 登录
```

平台随后：

1. 创建 Box
2. 等待 Box Ready
3. 确认 Codex 和 tmux 可用
4. 启动 Device Login
5. 等待用户完成认证
6. 创建或确认 `main` tmux 会话
7. 生成 Terminal URL
8. 返回自定义 `3001` URL

用户进入后可以执行：

```bash
tmux attach -t main
codex
```

如果自定义 Terminal 出现问题，用户可以切换到官方 `22222` Terminal 进行排查。

## 最终建议

```text
官方 22222：稳定基础能力和故障排查入口
自建 Go 3001：产品化 UI 和 Agent 工作台入口
```

如果只需要修改颜色和字体，不建议维护自建 Terminal；如果要提供完整的 Agent 工作台、状态栏和专用交互，则值得继续维护 Go 版本。
