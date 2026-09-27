# 配置、首次引导与推理等级

本参考定义实现、Review、UI QA 三类 Codex session 的模型、推理等级、自动 QA 策略、重试预算与 watchdog。配置只保存偏好和非敏感环境标识；令牌、密码、测试账号密钥仍由宿主凭据系统注入。

## 1. 首次使用必须完成引导

Skill 文件被复制到磁盘时不会自动执行代码，所以可靠的“首次安装”触发点是第一次调用本 skill；安装器若支持 post-install，也可以主动执行同一检查。开始拆任务或启动任何 Codex session 前先运行：

```bash
python3 scripts/loop_config.py status --project <长期集成 worktree>
```

退出码 `20` 表示尚未完成引导。命令会输出 `onboarding_required` 和四个问题；Claude Code 必须询问用户：

1. 自动 QA 是只覆盖 UI 任务、每次询问、仅显式开启，还是关闭？
2. 是否保持 implementation=`gpt-5.6-sol/xhigh`、review=`gpt-6-astra/xhigh`、QA=`gpt-6-astra/high`？
3. 若开启自动 UI QA，使用哪个 computer-use driver；默认测试环境是否放在项目配置中？
4. 是否保持默认重试次数与 watchdog 超时？

不得把“没回答”解释成接受默认值。若用户暂时不配置，可显式执行：

```bash
python3 scripts/loop_config.py defer
```

这会记录 `onboarding.status=deferred` 并关闭自动 QA，使普通实现流程可运行；下次要启用 QA 时重新配置。无人值守运行必须在用户离开前完成本步骤。

推荐配置示例：

```bash
python3 scripts/loop_config.py configure \
  --qa-mode ui_tasks \
  --qa-driver '<已安装的 computer-use 驱动>' \
  --implementation-model gpt-5.6-sol --implementation-effort xhigh \
  --review-model gpt-6-astra --review-effort xhigh \
  --qa-model gpt-6-astra --qa-effort high
```

`ui_tasks` 要求配置 driver。测试 URL、账号别名或 seed 通常因项目不同，建议写入项目 override 的 `qa.default_environment`，不要写真实密码。

## 2. 配置位置与优先级

从低到高依次为：

1. `scripts/config.defaults.json`
2. 用户配置 `~/.claude/codex-nightshift/config.json`
3. 项目配置 `<project>/.claude/codex-nightshift.json`
4. 当前进程的环境变量覆盖

用户配置是完整配置，符合 `config.schema.json`。项目配置是局部 override，符合 `project-config.schema.json`，但必须包含 `schema_version: 1`。例如：

```json
{
  "schema_version": 1,
  "models": {
    "review": {
      "id": "gpt-6-astra",
      "reasoning_effort": "high"
    }
  },
  "qa": {
    "driver": "computer-use",
    "default_environment": "preview:test-account"
  }
}
```

路径可用 `CODEX_LOOP_CONFIG` 和 `CODEX_LOOP_PROJECT_CONFIG` 改写。所有未知字段、非法数值、已知模型不支持的推理等级都会被拒绝，而不是悄悄忽略。

改名前的配置仍可读取：新用户路径不存在时，回退到 `~/.claude/codex-implementation-loop/config.json`；新项目路径不存在时，回退到 `<project>/.claude/codex-implementation-loop.json`。新路径存在就优先采用，不合并新旧文件，也不因新文件无效而静默回退。显式 `--config` 优先于 `CODEX_LOOP_CONFIG`，两者都优先于默认路径；项目路径显式环境变量也优先于新旧默认位置。

`configure`、`defer` 和 `reset-onboarding` 默认写入新的 `codex-nightshift` 用户配置路径，不改写或删除旧配置。`reset-onboarding` 会先读取上述优先级选中的偏好，再写入新目标路径。显式参数或环境变量指定了路径时，读取和写入均尊重该路径。读取兼容本身不会创建、迁移或删除文件。`CODEX_LOOP_*` 环境变量名继续保持兼容。

## 3. QA 模式

- `ui_tasks`：登记任务时必须指定 `--task-kind ui|non-ui`；UI 自动设为 `qa_required`。
- `ask_each_run`：每个任务必须显式指定 `--qa-required` 或 `--no-qa`。
- `explicit_only`：默认不做 QA，只在显式 `--qa-required` 时开启。
- `disabled`：自动 QA 关闭；显式 `--qa-required` 仍是有意覆盖，且必须有 driver/environment 才能运行。

无论模式为何，UI QA 都不会因选了 Astra 而自动获得浏览器。driver 和隔离 environment 缺失时必须阻塞，不得降级成源码阅读。

## 4. 冻结一轮长任务

启动任务图前，把用户、项目和环境变量合并成不可变快照：

```bash
python3 scripts/loop_config.py resolve \
  --project <长期集成 worktree> \
  --output "$RUN_DIR/resolved-config.json"

export CODEX_LOOP_RESOLVED_CONFIG="$RUN_DIR/resolved-config.json"

python3 scripts/run-state.py init "$STATE" \
  --config "$RUN_DIR/resolved-config.json" \
  --objective '<总目标>' \
  --worktree '<长期集成 worktree>' \
  --branch '<集成分支>' \
  --base-branch '<基线分支>'
```

快照的 SHA-256、角色模型和推理等级会写入状态；每个封装脚本另写 `runtime-config.json`。设置 `CODEX_LOOP_RESOLVED_CONFIG` 后，快照是权威来源，不再读取项目或环境覆盖；要改变运行中的模型或预算，应明确停止并生成新快照，不能让同一轮静默漂移。

运行级调度策略由 `run-state.py init --checkpoint-mode commit|patch --max-replans 2` 冻结在 state 中，不是此 JSON 的额外字段。commit 模式用于已授权本地 checkpoint 的多任务运行；patch 模式仅允许一个不提交的任务。重拆次数上限限制整轮运行，耗尽后保留该任务 blocked 并继续其他 ready 任务。接续 hook 的安装和会话绑定见 [claude-continuation.md](claude-continuation.md)，不能只复制 Skill 就假设已启用。

## 5. 临时环境覆盖

未设置冻结快照时，可用以下环境变量覆盖本次命令：

- `CODEX_IMPLEMENTATION_MODEL` / `CODEX_IMPLEMENTATION_REASONING_EFFORT`
- `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_REASONING_EFFORT`
- `CODEX_QA_MODEL` / `CODEX_QA_REASONING_EFFORT`
- `CODEX_QA_DRIVER` / `CODEX_QA_ENVIRONMENT`
- `CODEX_POLL_SECONDS` / `CODEX_IDLE_TIMEOUT_SECONDS`
- `CODEX_MAX_RUNTIME_SECONDS` / `CODEX_TERMINATE_GRACE_SECONDS`

推理等级最终通过 Codex CLI 的 `-c model_reasoning_effort="..."` 传入；模型通过 `-m` 传入。`inherit` 表示不下发对应 CLI 覆盖。不同模型支持的等级不同，配置器会对内置已知模型做兼容性检查。

## 6. 管理命令

```bash
# 查看有效配置、来源和 fingerprint
python3 scripts/loop_config.py status --project <project>

# 校验完整配置或快照
python3 scripts/loop_config.py validate <config.json>

# 保留偏好，重新触发首次引导
python3 scripts/loop_config.py reset-onboarding

# 已存在配置只有显式允许才覆盖
python3 scripts/loop_config.py configure --force ...
```

配置文件损坏、first-run 尚未完成或组合不安全时，封装脚本在创建 session 前失败。首次引导不是一个可在无人值守中自动猜测的阻塞项。
