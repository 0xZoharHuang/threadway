# 在同一项目里切换 Codex 与 Claude Code

解压 macOS arm64 发行包，在该目录运行 `./setup-clients.sh` 安装此次验收的官方 CLI。工作区随附的本地版本已准备好这两个客户端。源码版本先运行 `./build.sh`。将发行目录的 `bin` 加入 PATH 后，进入原项目或实际 worktree，运行 `threadway`。也可用 `threadway --project /实际工作目录`，无需先切目录。

1. 选择一项任务，再选要打开的 agent。首次跨工具接力会登记一条原生会话。原聊天作为只读起点；第一次需要回写原工具时，为它建立一条受管会话。
2. 下次仍选择同一任务。双方都没变就打开已有会话；只有一方新增就把缺少的消息带过去；如果目的端已经更靠前，直接打开它。代码和未提交修改在原 worktree。
3. 两边都继续过时，软件显示两条分支。可以沿原分支继续，或明确从其中一边建立新的目标分支。后者有独立任务 ID。不会根据目录、标题、时间或两句相同的话猜测两段独立工作是同一件事。

常用命令：

```sh
threadway scan
threadway list
threadway switch 任务ID --to claude --dry-run
threadway switch 任务ID --to claude
threadway batch --to codex --dry-run
threadway batch --to codex --apply
threadway ui
```

批量预览会刷新工具自己的索引；正式批量登记也不会打开一排终端。管理页在本机按需启动，可检索项目会话、查看双方新增片段、复制继续命令。ID 可以只输入无歧义的前缀。

默认传递可见用户与助手文字。首次接力可以加 `--tools`，要求保留可验证的成对工具调用与结果。Codex 当前导入路径不能保证保留这些工具的类型时，该操作会明确拒绝。压缩或回退过的会话、附件、未明分支、缺失工作目录等也可能无法忠实交接。这里的“原生”指目标客户端能在其列表找到记录、按 ID 恢复且下一轮读到上下文；它不搬运登录、第三方授权或正在执行的进程。

如果写入中断，先重试同一条 `switch`。若目标尚未写入，`threadway cancel 任务ID` 可撤销准备中的操作。若最近一次追加之后目标没有再变动，`threadway undo 任务ID` 可用私有备份撤回这次追加。完整行为见 [英文说明](../README.md)。

Google 目前只提供隔离生成：`threadway stage-google 任务ID --out /一个尚不存在的目录`。已生成 SQLite 步骤和 brain 目录；对最新官方 CLI 的无凭据启动停在认证阶段。Antigravity App 和经过认证的 CLI 模型续聊尚未验收。

在这台 Mac 的本地安装中，终端命令为 `threadway`。在仓库外的目录使用时请用 `--project` 指定目标。查看版本可运行 `threadway doctor`。之后如移除本地 PATH 启动链接，请只移除指向此项目的 `threadway` 链接；桌面端 Codex 账号同步工具独立运行。
