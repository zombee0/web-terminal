# Web Terminal 与 agent.market 的边界

完整镜像采用 **Builder 自建完整镜像 → GHCR Public → agent.market 导入并测试 → 发布**。当前网站先用 python:slim mock 验证发布流程，详见 [mock 后端说明](../../agent.market/backend/README.md)；本文件保留正式镜像的边界。
具体操作以 [ghcr-publish.md](./ghcr-publish.md) 和 [Dockerfile](./Dockerfile) 为准，不维护第二份可能漂移的 Dockerfile 示例。

## Builder 交付什么

1. OCI image：从 `python:slim` 安装 Codex、tmux、Bash 等依赖并编译 Web Terminal。
2. [agent.market.yaml](./agent.market.yaml)：声明 service 模式、镜像引用、启动参数和权限，不提供宿主机脚本。
3. Marketplace profile：服务说明、限制、支持渠道。
4. 验收说明：浏览器终端可访问、输入和 resize 正常、工具可用、非 root、停止后不可访问。

镜像不能包含 Builder 的 Codex 登录状态、PAT、SSH 私钥、客户文件。模型凭据由运行用户在受控会话中授权；Registry 凭据绝不传入运行 Box。

## 平台负责什么

- 后端匿名解析 GHCR，确认架构，锁定 digest，校验 manifest 和配额；
- 将镜像和配置绑定到不可变版本，执行扫描与 BoxLite 测试；
- 管理 Box 创建、受控启动、权限、网络、会话 TTL、日志、停止和删除；
- 通过独立预览域名对 HTTP **和 WebSocket** 鉴权，保留公共 Host 以通过同源校验；
- 以服务端测试证据决定能否发布，Marketplace 只显示 published；
- 从验证版本启动 Employer 会话，不复用 Builder 测试 Box 或凭据。

镜像 ENTRYPOINT 是 Docker 启动入口；平台 manifest command 是运行意图。BoxLite SDK 的实际入口行为需要适配器验证。不要假设 Docker HEALTHCHECK 会自动成为 BoxLite readiness；平台必须显式探测。

服务成功是“会话可交互且受控”，不是“退出 0 并写 result.json”。Web Terminal 目前没有实现 task/event/result 文件协议，因此 manifest 不应声明这些接口。

## 原有开发工具继续保留

`deploy_web_terminal.py` 可创建并初始化新的开发 Box，`update_web_terminal.py` 只对通过 `BOXLITE_BOX_ID` 明确指定的已有 Box 执行 copy_in 和重启，适合镜像尚未构建时快速迭代。它们不是正式交付包，不作为平台部署 API，也不应被原地改成镜像发布脚本。

部署脚本请求 1 CPU、1024 MiB 内存、50 GB 持久磁盘和不设 allowlist 的完整出站网络，并在 Box 内安装 Codex、tmux、UTF-8 locale 与中文字体。更新脚本不会创建 Box 或重新安装这些依赖；使用前应确认 `BOXLITE_BOX_ID`、目标环境和授权。

## 当前限制

- 已实现固定 python:slim mock 的后端与 BoxLite 适配器；完整镜像 Runner、扫描、实时 HTTP/WebSocket 鉴权预览尚待接入并验收。
- 非 root 镜像身份需要在实际 BoxLite exec 中验证；仅设置 HOME 不会降权，不可静默退回 root。
- 出站默认仅允许 api.openai.com，不能承诺覆盖 ChatGPT 登录、Git 克隆或全部 Codex 用法。新增域名必须声明并经平台批准。
- 浏览器 xterm CDN 与 Box 的出站是两条网络路径；镜像字体不等于浏览器装有相同字体。
- 平台必须区分“无出站权限”和“删除整个网络接口”，后者可能使服务预览也失效。无法同时实施隔离和预览时应拒绝启动，不能静默放开网络。
- GHCR 引用被删除后仍可能无法冷启动；digest 不是永久可用性保证。长期托管 Registry 应复制并保留已发布镜像，迁移后重新验证运行引用。

平台契约和验收清单见 [Agent Package Specification](../../agent.market/agent-package-spec.md)。
