# 用三个官方 CLI 接力同一个项目

在项目目录运行 `threadway` 用终端选择任务，或运行 **`threadway ui`** 打开本机管理页。左边搜索任务，右边选择从哪个 agent 接到哪个 agent。预览后可以直接准备原生会话，若服务从交互终端启动，也可以在原终端打开目标 CLI。页面只在回环地址监听，关闭服务后链接即失效。

发行包已带转换引擎。安装 Codex 和 Claude 运行 `./setup-clients.sh`，安装 Google 运行 `./setup-google.sh`，再把发行目录的 `bin` 加入 PATH。源码构建先运行 `./build.sh`。工具使用各 CLI 自身的登录和权限，测试采用隔离目录和本地模拟模型。

```sh
threadway scan --project .
threadway list
threadway switch 任务ID --to agy --dry-run
threadway switch 任务ID --to agy
threadway switch 任务ID --from agy --to claude
threadway batch --to codex --dry-run
threadway batch --to codex --apply
```

- 初始聊天保留原样。第一次回写原工具时，程序会建立受管副本，后续正常切换复用目标原生 ID。
- 多个 agent 各聊了新内容时保留分叉。可选择已有分支，或指定 `--from agy --to claude --branch` 新建接力分支。第三个 agent 也有自己的交接点。
- 默认带可见用户和助手文字。工具历史、附件、压缩、复杂分支和运行中任务的边界见[验收记录](verification.md)。同目录的文件与未提交修改以实际 worktree 为准。
- Google 对应 **Antigravity CLI（agy）**。已验收原生会话登记、官方 ACP 列表与回放、实际下一轮请求和回写。模拟端点测试没有使用个人 Google 账号；不代表 Antigravity 桌面 App 或云端模型已经验收。
- 中断后先重试原命令。可用 `cancel` 撤回尚未写入的准备操作，或用 `undo` 撤回目标没有再被修改的最近一次受管追加。私有恢复目录可能含有对话内容。

本项目使用 Apache-2.0。当前操作系统验收范围和下载见[发布页](https://github.com/0xZoharHuang/threadway/releases)。
