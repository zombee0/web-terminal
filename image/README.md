# Web Terminal 镜像交付

本目录集中管理 Web Terminal 的完整镜像交付内容：

- `Dockerfile` 与 `Dockerfile.dockerignore`：镜像定义和构建上下文过滤规则；
- `build_web_terminal_image.sh`：构建本地 Linux AMD64 镜像；
- `test_web_terminal_image.sh`：执行本地镜像 smoke test；
- `push_web_terminal_image.sh`：测试、标记并推送镜像到 GHCR；
- `agent.market.yaml`：agent.market service manifest；
- `ghcr-publish.md`：完整构建和发布流程；
- `agent-market-integration.md`：镜像与平台的职责边界。

所有命令均可从项目根目录执行，具体步骤见 `ghcr-publish.md`。
