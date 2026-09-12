# Web Terminal：构建、测试、发布到 GHCR Public

本指南操作由 **Agent Builder 在自己的开发机 / CI** 执行，不在 agent.market 的运行 Box 内执行。已有完整本地镜像可直接从第 2 步开始。

## 1. 构建完整环境

前提：Docker Engine 与 buildx、Bash、curl；目标运行节点为 linux/amd64。非 amd64 开发机需要配置可用的目标架构构建/运行模拟。无需在宿主机安装 Go。

```bash
export LOCAL_IMAGE=web-terminal:0.1.0
export IMAGE_VERSION=0.1.0
export BASE_IMAGE=python:slim
# 可选：真实源码仓库，用于 GHCR Package 关联
export SOURCE_REPOSITORY=https://github.com/your-org/your-repo
bash image/build_web_terminal_image.sh
```

从 workspace 根目录调用上述脚本即可；脚本也支持其他工作目录下的绝对路径调用。源码以多阶段方式编译，最终镜像从 `python:slim` 安装 Bash、Codex、tmux、locale 和字体，不依赖旧 Box 已安装的软件。它以 `boxlite` 用户运行，默认 ENTRYPOINT 启动 3001 前台服务。

`deploy_web_terminal.py` 和 `update_web_terminal.py` 使用 `build/web-terminal-linux-amd64`；在项目根目录运行 `build_web_terminal_binary.sh` 可单独生成该文件。`build/` 不受 Git 跟踪，Docker 镜像构建也不会覆盖本地产物。

### 可复现性的准确边界

`python:slim` 是 Docker Official Image，也是 BoxLite CodeBox 的默认 image，不是 BoxLite 专有发行版。开发部署脚本从 BoxLite 的 `python` image 创建 Box，再在运行时安装依赖；这条路径不同于 Dockerfile 的完整镜像构建，不能作为镜像可复现性的证据。参见 [BoxLite Python SDK](https://github.com/boxlite-ai/boxlite/blob/main/docs/reference/python/README.md)。

开发默认使用滚动基础镜像和官方 Codex 安装器。因此“按已发布 digest 运行同一环境”成立，“从源码重新构建逐字节相同镜像”尚不成立。发布记录应保存：

- BASE_IMAGE、GO_IMAGE 的实际 digest；release 构建时通过同名变量传入 digest；
- 源码版本、构建架构、构建日志；
- 镜像中的 `/home/boxlite/codex-build-version.txt`、`/usr/share/web-terminal/packages.txt`；
- 最终 registry manifest digest。

可设置 `CODEX_EXPECTED_VERSION` 为选定版本的完整 `codex --version` 输出，安装结果不同则构建失败；它是版本断言，不是固定版本下载机制。严格可复现构建还需固定经过校验的 Codex 制品和系统包来源。本次依照 [OpenAI 安装器文档](https://learn.chatgpt.com/zh-Hans/docs/config-file/environment-variables) 使用无人值守安装，并保留默认 `.local/bin` 路径。

## 2. 验证本地镜像

```bash
LOCAL_IMAGE=web-terminal:0.1.0 bash image/test_web_terminal_image.sh
```

脚本检查架构、非 root 身份、Codex/tmux、UTF-8，然后启动容器，在随机分配的 **127.0.0.1** 端口轮询 /healthz 和首页，退出时只停止自身创建的测试容器。它不会验证 BoxLite，也不会访问模型 API。

手动测试（终端 A，前台运行）：

```bash
docker run --rm -p 127.0.0.1:3001:3001 "$LOCAL_IMAGE"
```

终端 B：

```bash
curl -fsS http://127.0.0.1:3001/healthz
```

浏览器打开 http://127.0.0.1:3001，检查输入、中文、窗口 resize，并在终端中运行 `id`、`codex --version`、`tmux -V`。当前终端每次连接创建新 Shell；安装 tmux 不等于自动恢复会话，可手动 `tmux new -A -s work`。xterm 资源从 jsDelivr 加载，浏览器需可访问该 CDN。

执行镜像内诊断时必须覆盖 ENTRYPOINT：

```bash
docker run --rm --entrypoint /bin/bash "$LOCAL_IMAGE" -ec 'id; command -v codex; codex --version; tmux -V'
```

## 3. 准备推送权限

1. 在 GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) 创建短有效期 PAT。
2. 最小 scope 为 `write:packages`。不要为单纯推送申请 `delete:packages` 或额外的 `repo` 权限；GitHub UI 自动勾选 repo 时检查并取消不需要的权限。
3. token 所属账号必须有目标 namespace/package 的写权限。组织必须允许创建 Package；启用 SSO 时还需对 token 授权该组织的 SSO。
4. Package 已存在时检查 Manage access / 仓库继承权限；有 scope 不等于有目标 Package 权限。
5. GitHub Actions 可改用 workflow 的 `GITHUB_TOKEN`，声明 `contents: read` 和 `packages: write`，并授予该仓库相应 Package 访问权限。本指南使用本地 PAT 路径。

依据：[GitHub Container registry 文档](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。

## 4. 登录并推送已经测试的镜像

以下 owner 和 username 请替换成真实值；owner 为小写 GitHub 用户或组织，username 为 PAT 所属用户。

```bash
export GHCR_OWNER=your-org
export GHCR_USERNAME=your-github-user
export GHCR_IMAGE="ghcr.io/${GHCR_OWNER}/web-terminal"
export IMAGE_VERSION=0.1.0
export LOCAL_IMAGE=web-terminal:0.1.0

read -rsp "GHCR PAT: " GHCR_PAT
echo
printf '%s' "$GHCR_PAT" | docker login ghcr.io --username "$GHCR_USERNAME" --password-stdin
unset GHCR_PAT

bash image/push_web_terminal_image.sh
```

推送脚本重新执行本地 smoke test、tag、push，并显示 registry manifest 信息；**不会重新构建镜像，也不会把 Package 改为 public**。不要复用已发布版本 tag，不要把 PAT 写进源码、镜像 ARG/ENV、manifest 或 agent.market 表单。

Docker 登录信息由开发者本机凭据存储保管；完成后可按团队凭据管理规则退出登录。镜像公开前须自行确认层内没有登录凭据、客户文件、终端历史或私有资料，并完成漏洞 / secret 扫描。

## 5. 在 GitHub 设置 Package 为 Public

首次命令行推送的 Package 默认 private。进入：

```text
GitHub → 个人 / Organization → Packages → web-terminal
→ Package settings → Change visibility → Public
```

需要该 Package 的管理权限。源码仓库 public 不代表 Package public；公开镜像会公开其中的软件和文件，镜像层中删除过的敏感文件也可能仍可恢复。

## 6. 匿名验证和锁定 digest

不覆盖现有 Docker 登录配置，创建专用空配置目录：

```bash
anonymous_config="$(mktemp -d)"
docker --config "$anonymous_config" pull --platform linux/amd64 "${GHCR_IMAGE}:${IMAGE_VERSION}"
docker buildx imagetools inspect "${GHCR_IMAGE}:${IMAGE_VERSION}"
```

该目录是无凭据的临时配置；不应复制原 Docker config 到其中。不要用本地 image ID 代替 registry digest。记录 inspect 输出的顶层 `Digest: sha256:...`，并再用 digest 做匿名 pull：

```bash
# 用上一条输出的真实 64 位摘要替换该占位值
export IMAGE_DIGEST=sha256:REPLACE_WITH_REAL_64_HEX_DIGEST
docker --config "$anonymous_config" pull --platform linux/amd64 "${GHCR_IMAGE}@${IMAGE_DIGEST}"
```

如果匿名 pull 失败，先检查 Package 可见性、引用、网络和保留策略；不要把推送 PAT 交给平台作为补救。

## 7. 交给 agent.market

用真实 image digest 替换 [agent.market.yaml](./agent.market.yaml) 中的占位符。交付：

- 完整公开 OCI image 引用与 linux/amd64 平台；
- service manifest（command、资源、出站权限、3001 HTTP/WebSocket、readiness）；
- 上架资料（能力、限制、支持链接）；
- 验收步骤；不是任务型的 result.json 协议。

平台负责从自己所在网络匿名拉取、锁定 digest、在干净 Box 中测试，并提供 **经过鉴权** 的短期预览。public image 不等于 public terminal。Builder 不提交 BoxLite REST token 或启动 Box 的脚本。

Docker ENTRYPOINT 不代表任意 BoxLite SDK 的 `box.start()` 都会自动执行它。平台适配器必须针对部署的 BoxLite 版本确认入口行为：只启动一次服务，不能先自动运行 ENTRYPOINT 再重复 exec。不把未经验证的 SDK 启动示例当成已打通链路。

当前仓库提供页面、契约与镜像工具，尚未实现平台 Runner。正式上架前必须完成 BoxLite 集成测试、鉴权代理、WebSocket/PTY、资源策略、扫描和回收验证；不能用 Docker smoke 结果替代。
