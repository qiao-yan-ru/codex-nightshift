# Codex CLI 参数与陷阱

## 调用形式

```bash
# 可选：仅在 codex 不在 PATH 时设置 CODEX_BIN=/path/to/codex
if [ -n "${CODEX_BIN:-}" ]; then
  CODEX="$CODEX_BIN"
elif command -v codex >/dev/null 2>&1; then
  CODEX="$(command -v codex)"
else
  CODEX="$HOME/.codex/plugins/.plugin-appserver/codex"
fi

# 首次实现
"$CODEX" exec --sandbox workspace-write --skip-git-repo-check --disable hooks \
  -c sandbox_workspace_write.network_access=true \
  -C <worktree 路径> \
  --json --output-last-message <报告文件> \
  - < <任务包文件> > <事件流文件> 2> <stderr 文件>

# 返工（保留它对自身实现的记忆）
"$CODEX" exec resume --skip-git-repo-check --disable hooks \
  -c sandbox_mode="workspace-write" \
  -c sandbox_workspace_write.network_access=true \
  --json --output-last-message <报告文件2> \
  <session-id> - < <反馈文件>

# 独立 review（必须是全新、只读 session）
"$CODEX" -C <worktree路径> -s read-only exec review --uncommitted \
  --disable hooks --json \
  --output-schema scripts/review-result.schema.json \
  --output-last-message <review.json> \
  - < <review任务包>

# UI QA（必须是全新 session；角色配置由 resolved-config 冻结）
export CODEX_LOOP_RESOLVED_CONFIG=<本轮 resolved-config.json>
scripts/codex-qa.sh <worktree路径> <QA任务包> <空输出目录>
```

均可直接复用 `scripts/` 下的封装。

封装脚本额外提供 watchdog，并把最终状态写入输出目录的 `watchdog.json`。`codex-review.sh` 还会校验结构化输出并比较 review 前后的 diff fingerprint；`codex-qa.sh` 还会锁定目标 fingerprint、校验结构化结果与截图哈希。直接复制裸命令不会获得这些保护。

## 陷阱

### 1. 二进制发现顺序

封装脚本依次查找：环境变量 `CODEX_BIN`、PATH 中的 `codex`、旧版默认路径 `~/.codex/plugins/.plugin-appserver/codex`。特殊安装位置设置 `CODEX_BIN=/path/to/codex`，不要修改脚本。若直接复制本页命令，则先按上面的方式设置 `CODEX`。

### 2. 必须显式重定向 stdin

不加 `- < file`（或 `printf ... | codex exec ... -`），它会**永久阻塞**在 `Reading additional input from stdin...`——Bash 工具留的管道不关闭。

### 3. `resume` 不接受 `--sandbox`

```
error: unexpected argument '--sandbox' found
```

它沿用原会话设置；要覆盖用 `-c sandbox_mode="workspace-write"`。

### 4. 本机可能没有 `timeout`

GNU coreutils 未装时脚本里的 `timeout` 检查会失败。用宿主工具自身的超时参数，或后台运行。

### 5. `--output-schema` 是 OpenAI strict 模式

**`properties` 里的每一个键都必须列进 `required`**，否则整个请求 400 `invalid_json_schema`。不用填的字段让模型返回空字符串，不要靠省略。

### 6. session id 从事件流取

```bash
python3 -c "
import json
for l in open('events.jsonl'):
    l=l.strip()
    if l and json.loads(l).get('type')=='thread.started':
        print(json.loads(l)['thread_id'])
"
```

### 7. review session 不能复用实现 session

`codex exec review` 会创建独立 thread，并能用 `--uncommitted`、`--base` 或 `--commit` 选择审查目标。此循环使用 `--uncommitted`，因为任务尚未 checkpoint。不要用 `exec resume <实现 session>` 让实现者审自己，也不要 resume 旧 reviewer；每轮代码变化后重新启动全新 review。

review 使用只读 sandbox，输出目录必须在 worktree 外。否则输出文件会成为被审查的未跟踪改动，导致目标在 review 过程中变化。

### 8. 选择 Astra 不会自动提供 computer-use 环境

`codex-qa.sh` 的模型和推理等级来自 JSON 配置，但模型名本身不会创建浏览器、桌面、Playwright 或截图导出器。必须在宿主中预先配置可用 driver，并在 JSON 中登记 driver/environment；未使用冻结快照时也可通过 `CODEX_QA_DRIVER` 和 `CODEX_QA_ENVIRONMENT` 临时覆盖。代码仓库保持 `read-only`；driver 应在自身的隔离环境里操作应用并把截图写入脚本指定的 evidence 目录。

driver 不可用、不能截图、账号无权或目标环境不可达时，QA 必须返回 `blocked`/coverage gap，或者脚本以基础设施错误退出；不得降级成读代码后声称 UI 通过。

QA 每轮也必须是全新 session，不能 resume。实现发生任何变化后，旧 QA 结果和截图全部失效。

### 9. 模型与推理等级由配置器下发

封装脚本在 `exec` 前添加全局参数 `-m <model>` 和 `-c model_reasoning_effort="<level>"`，并把有效值写入 `runtime-config.json`。配置层和首次引导见 `configuration.md`。不要只在任务包文字里要求“极高推理”；文字不能替代 CLI 配置。

## 沙箱能力边界（决定了分工）

`--sandbox workspace-write` 下 codex：

| 能做 | 不能做 |
| --- | --- |
| `pnpm install` / `build` / `typecheck` / `lint` | **跑不了 Docker**（配置要落 `~/.colima`，不可写） |
| 单元测试 | **起不了 PostgreSQL**（`initdb` 的默认 SysV 共享内存被禁） |
| 读写工作目录与 `/tmp` | **写不了 `.git`**（worktree 的 `.git` 指向主仓库，在可写区外） |

**因此：集成测试与 commit/push/PR 全是主 agent 的活。** 任务包里要写明这一点，否则它会花大量时间试图修基础设施。

`-c sandbox_workspace_write.network_access=true` 是装依赖所必需的。

## 事件流与报告

`--json` 输出 JSONL 事件：`thread.started` / `turn.started` / `item.started` / `item.completed` / `turn.completed`，异常时是 `error` / `turn.failed`。

`item.completed` 里的 `item.type` 有 `agent_message`（它的说明）、`command_execution`（命令与输出）、`file_change`（改文件）。

**监看进度**：数事件流行数是否增长；`item.command_execution` 计数反映工作量。

**读报告**：`--output-last-message` 指定的文件是它的最终报告。

## 后台运行

一次实现、review 或 UI QA 通常几分钟到十几分钟。无人值守循环应使用封装脚本，让 watchdog 负责判断完成、失败和超时：

- `CODEX_POLL_SECONDS`：轮询间隔，默认 20 秒
- `CODEX_IDLE_TIMEOUT_SECONDS`：事件流无增长超时，默认 1200 秒；`0` 表示禁用
- `CODEX_MAX_RUNTIME_SECONDS`：单次运行总时限，默认 7200 秒；`0` 表示禁用
- `CODEX_TERMINATE_GRACE_SECONDS`：发送 TERM 后的等待时间，默认 10 秒

watchdog 超时返回 124，并在 `watchdog.json` 中写 `idle-timeout` 或 `max-runtime`。实现超时后检查 worktree 再决定 replacement session；review 超时则记录 review `error` 并启动全新 reviewer；QA 超时则记录 QA `error` 并启动全新 QA session。不要把 review/QA 基础设施错误直接当成代码缺陷。

命令如果由宿主工具转入后台，仍以 `watchdog.json`、进程存活和 JSONL 是否增长为准，不要仅凭前台调用超时判断 Codex 已停止。

## 本机作为备选验证环境

Docker daemon 常常没运行，但 PostgreSQL 二进制通常可用：

```bash
initdb -D /tmp/<dir> --auth=trust --no-locale --encoding=UTF8
pg_ctl -D /tmp/<dir> -l /tmp/<dir>.log -o '-p <port> -k /tmp' start
createdb -h /tmp -p <port> <dbname>
```

注意这样建的集群超级用户是**当前系统用户**，不是 `postgres`——连接串用户名要对应。

多个临时库用完即删：`DROP DATABASE IF EXISTS <db>`。
