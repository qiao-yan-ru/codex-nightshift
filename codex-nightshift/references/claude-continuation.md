# Claude Code 提前结束与上下文压缩后的接续

本 Skill 的主循环仍由 Claude Code 连续执行。`scripts/claude-loop-hook.py` 为**明确绑定的 Claude Code 会话和 run**增加结束前检查，并在上下文压缩或恢复会话后重新注入落盘状态位置。它不启动常驻进程，不代替主循环，也不创建 Codex worker。

## 安装一次，再绑定每次运行

脚本适用于 macOS/Linux，使用 Python 标准库。以下示例从已安装的 Skill 目录执行。安装写入的是明确指定的 Claude Code settings；仓库包含脚本不表示 hooks 已经启用。

```bash
python3 scripts/claude-loop-hook.py install \
  --settings "$HOME/.claude/settings.json"
```

`install` 保留其他 settings 和 hooks，只追加自己的 `SessionStart`、`Stop` command hooks；首次修改已有文件时保存原文件备份。相同路径、相同 bindings 目录下重复执行不会重复追加。若原 settings 不是合法 JSON、hooks 结构损坏或目标 settings 禁用了全部 hooks，安装报错并保留原文件。

若只对某个项目安装，可指定该项目的 `.claude/settings.local.json`。不要同时在多个设置层级重复安装同一份 hook。hooks 使用安装时的 Python 和脚本绝对路径；移动或重装 Skill 后，检查 `/hooks` 中的旧路径，只删除对应旧 command 条目，再重新安装。

**首次安装后先启动一个 Claude Code 会话，或明确恢复目标会话。** `SessionStart` 从官方 stdin 的 `session_id` 取得准确身份，经 `CLAUDE_ENV_FILE` 给本会话后续 Bash 命令设置：

- `CODEX_LOOP_CC_SESSION_ID`：当前 Claude Code session ID。
- `CODEX_LOOP_BINDINGS_DIR`：本次 hook 使用的绑定目录，默认 `~/.claude/codex-loop-bindings`。

`CODEX_LOOP_*` 与 `codex-loop-bindings` 是保持兼容的技术名称；Codex Nightshift 改名后继续使用，无需迁移既有绑定。

这两个变量只提供身份与目录，不会自动绑定任何任务。已经运行中的会话如果没有执行过新装的 `SessionStart`，不能假装已取得身份；先启动/恢复会话，再检查变量。不要通过查找最近修改的 transcript、最近 session 或进程名称推测 ID。官方接口见 [SessionStart](https://code.claude.com/docs/en/hooks#sessionstart)、[持久化环境变量](https://code.claude.com/docs/en/hooks#persist-environment-variables)。

完成任务拆分、状态初始化、运行配置与 `$RUN_DIR/run-contract.md` 落盘后，在**负责此次编排的 Claude Code Bash 工具内**执行：

```bash
python3 scripts/claude-loop-hook.py arm "$RUN_DIR/state.json" \
  --config "$RUN_DIR/resolved-config.json"
python3 scripts/claude-loop-hook.py status
```

`arm` 在缺少当前会话 ID 时失败。外部终端可用 `--session-id <已核实的当前ID>`；它是显式输入，不是自动发现接口。缺少可执行队列、已有暂停/截止/全阻塞状态也会拒绝绑定。一个会话只能绑定一个 run，同一绑定目录内一个 run 只能由一个会话编排；已有绑定必须先显式 `disarm` 才能换 run 或交接。不要通过更换绑定目录让两个编排者操作同一 state。

安装与后续命令默认使用相同 bindings 目录。若安装时使用 `--bindings-dir /绝对路径`，从该 hook 启动的 Claude Code Bash 会继承它；外部终端必须对 `arm/status/disarm` 传入同一路径。`status` 只报告落盘绑定，不证明宿主进程或 hook 当前仍在运行；安装后应在 Claude Code 的 `/hooks` 确认条目已加载。

## 结束前检查

`Stop` 只处理与绑定完全匹配的主会话，忽略未绑定会话、其他会话和 subagent。它调用只读的 `run-state.py next`，不直接改任务状态、不绕过授权。

| next 结果 | 接续动作 |
|---|---|
| `ready` | 要求读取下一个任务包，为新小任务启动新 Codex session |
| `recover` | 要求检查现有 worker 与输出，再恢复实现、审查或验证阶段 |
| `park` | 要求保存失败现场并安全停放，随后继续其他独立任务 |
| `replan` | 要求按原目标与剩余重拆预算执行 split，再选取下一任务 |
| `complete` | 要求执行并记录整体集成验收、整理摘要，再显式解除绑定 |
| `deadline` / `blocked` | 解除绑定并提醒保存现场、汇总结果；截止后不启动新工作 |
| run 为 `paused` / `stopped` / `cancelled` 等 | 解除绑定，允许结束 |

可继续的动作使用官方 `decision: "block"` 与 `reason` 返回具体下一步。主循环应主动执行任务，不要把“完成一个小任务后结束整轮”当作正常调度方法；此 hook 用来纠正偶发提前收尾。[Stop 接口](https://code.claude.com/docs/en/hooks#stop-decision-control)

脚本对同一状态连续接续默认最多 3 次，`arm --max-stalled-stops N` 可设置为 1–7。计数依据任务状态、尝试次数、session、checkpoint、停放/重拆记录，以及当前 worker 输出文件的大小/修改时间；单改备注、状态时间戳、历史条目不会算作进展。脚本不读取完整事件流。

若当前 watchdog 记录 `running`、进程仍存在且尚在已配置的 idle/runtime 时限内，安静等待不增加无进展计数。Claude Code 应在同一轮持续检查/等待现有 worker，不能重复启动。两种 watchdog 时限均为 0 时没有这种等待豁免。超过接续上限只表示未观察到状态或输出变化，**不证明 worker 已退出**；hook 会解除绑定并提示先检查进程、预算与 state。核查完成后才能显式重新绑定，不能为了规避计数自动反复 `arm`。

## Compact 与交接

`SessionStart` 在 `compact`、`resume` 等场景中，对同一已绑定会话注入精简恢复信息：state、配置文件或 state 内的配置快照、worktree、checkpoint 模式、deadline、当前 action 与任务包，以及存在时的 `run-contract.md` 路径。不会注入整份历史日志，也不会自动重新启用已解除的绑定。

恢复后先读 `run-contract.md` 的目标、验收命令、授权范围与全局预算，再读 state 摘要和当前任务包。仅按需要读取最新审查/验收证据；核实当前 worker 是否还在运行。新的 Codex 子任务始终使用全新 session，同一个子任务的返工才可按主协议 resume。

手动 `/clear`、fork 或恢复到其他 session ID 时，不自动迁移旧绑定。需要交接时先确认旧编排者已停止，保存摘要并显式解除旧绑定，然后在新会话核实身份、读取落盘状态并重新 `arm`。检查所有绑定可用：

```bash
python3 scripts/claude-loop-hook.py status --all
python3 scripts/claude-loop-hook.py disarm \
  --session-id <旧会话ID> --reason handoff
```

## 主动停止与能力边界

用户说暂停、停止或取消时，Claude Code 必须先解除绑定，再按主协议保存现场和汇总；不等待完成整个队列，也不得自行重新绑定：

```bash
python3 scripts/claude-loop-hook.py disarm --reason user-stop
```

整体验收完成后使用 `disarm --reason completed`。全局预算耗尽或需要新的授权时也应解除绑定并记录原因。解除绑定本身不会杀掉 Codex worker，worker 的收尾/终止与状态登记仍按主协议执行。若从外部终端操作，显式传入已核实的 `--session-id`。

Claude Code 用户中断不触发 `Stop`；API 错误走 `StopFailure`。本脚本不会把中断改成继续，也无法在 Claude Code 退出、电脑睡眠、网络/额度错误后自动唤醒。官方还对连续 hook 接续设有宿主上限（当前文档为 8 次）；本地状态进展不会解除宿主的保护，因此不能承诺靠 Stop hook 永久运行。[官方 Stop 与 StopFailure 文档](https://code.claude.com/docs/en/hooks#stop)

hook 遇到损坏状态或脚本错误会输出诊断并允许宿主正常结束，避免无限阻止停止。检查原因并从落盘状态恢复，不要把 `armed: true` 当作执行成功。当前实现的测试只模拟 hook stdin/settings/state，不会安装到用户配置或启动真实 Claude/Codex。
