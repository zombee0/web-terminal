# web-terminal

浏览器 Web Terminal 服务及其 BoxLite 部署工具。

## 构建部署二进制

```bash
bash build_web_terminal_binary.sh
```

Linux AMD64 二进制输出到 `build/web-terminal-linux-amd64`。`build/` 是本地编译产物目录，不受 Git 跟踪。
部署和更新脚本在该二进制不存在时会自动执行此构建脚本。
