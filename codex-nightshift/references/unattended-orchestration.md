# 无人值守编排协议

本参考只在多任务、通宵或用户离开期间持续执行时读取。单个已冻结切片不需要加载它。

## 0. 首次配置与本轮快照

在用户离开前执行 `python3 scripts/loop_config.py status --project <项目根目录或已创建的 worktree>`。退出码 20 时必须按 `configuration.md` 完成首次引导，不能在无人值守阶段猜测答案。

创建运行目录后立即解析并冻结本轮配置：

```bash
RUN_DIR="$HOME/.claude/codex-runs/<项目>-<时间戳>"
mkdir -p "$RUN_DIR"
python3 scripts/loop_config.py resolve \
  --project <长期集成 worktree> \
  --output "$RUN_DIR/resolved-config.json"
export CODEX_LOOP_RESOLVED_CONFIG="$RUN_DIR/resolved-config.json"
```

后续所有实现、resume、review 和 QA 封装脚本都继承该环境变量。同一轮不得切换快照；完整字段和覆盖规则见 `configuration.md`。

## 1. Preflight：用户离开前冻结运行边界

开始长循环前必须明确：

- 总目标和明确不做的范围
- 基线分支、长期集成分支与 worktree 路径
- 可以做哪些变更：只改文件、允许本地 commit、允许 push、允许开 PR、是否允许合并
- 可运行的测试和缺失的外部依赖
- deadline、每任务最大实现返工次数、最大 review/QA 次数、Codex idle/max-runtime 超时
- 运行级重拆预算（默认最多 2 次）；多任务使用本地 commit checkpoint，明确禁止 commit 时仅运行一个 patch 交付任务
- 哪些任务需要 UI QA；对应的 computer-use 驱动、隔离环境、测试账号/数据 reset、允许和禁止的外部动作
- 哪些决定可以按已有文档推导，哪些必须由用户裁决

把上述边界、已授权操作和验收命令写入 `$RUN_DIR/run-contract.md`，上下文压缩后仍以它为准。已有明确授权不重复询问；只有缺少会改变交付结果的决定时才在用户离开前补齐。若运行期间才发现新的产品歧义，阻塞该任务并继续其他任务。

按 [claude-continuation.md](claude-continuation.md) 安装接续 hook，并在初始化状态后显式绑定当前 Claude Code session。安装不等于启用；确认 `status` 已绑定正确的 state/session，再开始队列。缺少 hook 时仍可按本协议执行，但必须向用户说明提前结束接续未启用。接续不能代替机器保持唤醒，也不能处理 Claude Code 进程已经退出后的自动重启。

## 2. 拆分规则

每个任务都必须：

- 能放进一个新 Codex session，且任务包无需读取整个大任务历史
- 有单一主要交付目标和可执行验收条件
- 写明依赖任务、输入契约、不要做的相邻项
- 对关键不变量给出具体回退验证
- 失败时可以独立阻塞，不损坏已经完成的任务

依赖图必须无环。先登记初始任务图和可独立验收的任务包；冻结的是总目标、范围和验收标准。遇到过大的任务，允许按第 5.5 节在同一目标内继续细分，不必重新征求已经授予的实现授权。改变产品决定、扩大目标或取消验收条件仍属于阻塞项。

## 3. 持久化状态

状态和运行输出应放在 worktree 之外的持久目录，例如：

```bash
RUN_DIR="$HOME/.claude/codex-runs/<项目>-<时间戳>"
STATE="$RUN_DIR/state.json"
mkdir -p "$RUN_DIR/packets" "$RUN_DIR/tasks"
```

初始化：

```bash
python3 scripts/run-state.py init "$STATE" \
  --config "$RUN_DIR/resolved-config.json" \
  --objective "<总目标>" \
  --worktree "<长期集成 worktree>" \
  --branch "<集成分支>" \
  --base-branch "<基线分支>" \
  --checkpoint-mode commit \
  --max-replans 2 \
  --deadline "<ISO-8601，可留空>"
```

重试预算默认来自冻结配置；命令行 `--max-retries`、`--max-review-attempts`、`--max-qa-attempts` 只用于本轮明确覆盖。`--max-replans` 限制整轮运行的重拆次数；达到上限后只停止进一步重拆，不停止其他可执行任务。`--checkpoint-mode patch` 明确用于禁止 commit 的单任务交付，状态机会拒绝追加第二个任务或 split。

登记任务：

```bash
python3 scripts/run-state.py add "$STATE" \
  --id T001 --title "<标题>" --packet "$RUN_DIR/packets/T001.md" \
  --task-kind non-ui

python3 scripts/run-state.py add "$STATE" \
  --id T002 --title "<标题>" --packet "$RUN_DIR/packets/T002.md" \
  --task-kind ui \
  --depends-on T001

python3 scripts/run-state.py validate "$STATE"
```

状态文件使用临时文件加原子替换写入。不要手工编辑；用 `summary`、`show`、`next`、`start`、`session`、`review-*`、`qa-*`、`split` 和 `set` 子命令操作。同一状态文件只允许一个 Claude Code 编排者写入；原子替换防止半写文件，但不提供多控制器并发调度。所有计划、报告、接续绑定和截图放在 worktree 外，避免污染 checkpoint。

## 4. 长期集成 worktree

默认只创建一个长期 worktree：

```bash
git worktree add -b <集成分支> <worktree路径> <基线分支>
```

每个小任务在同一 worktree 上继续。Claude Code 验收通过后创建 checkpoint，下一 Codex session 通过当前代码、提交历史和任务包获取前序结果。不要让每个任务都从未包含前序工作的旧基线重新切出。

开始每个新任务前检查 `git status --porcelain`。除当前任务正在返工外，worktree 必须干净；如果来源不明的改动仍在，不得继续或擅自覆盖。

## 5. 每轮执行

### 5.1 选择任务

```bash
python3 scripts/run-state.py next "$STATE"
```

- `ready`：执行返回的任务。
- `recover`：上次运行中断，先检查已有进程、watchdog、报告和 worktree，再恢复该任务。
- `park`：当前任务需要停放，例如实现/review/QA 的可用尝试已耗尽。先确认进程结束，保存现场并标记 blocked，再次执行 next。不得重复执行必然超限的 start，也不得直接结束整个队列。
- `replan`：已停放的任务正在等待细分，按第 5.5 节生成子任务并执行 split；这不是整个队列结束。若证据表明不能在原范围内细分，用不带 `--replan` 的 `set ... blocked --note ...` 记录原因并清除重拆意图，再选择其他任务。
- `complete`：进入聚合验证。
- `blocked`：没有可继续的任务，结束并汇总阻塞项。
- `deadline`：已到预设截止时间，不再启动新会话。检查并收束已有 Codex 和它启动的任务进程，收集结果、保存现场、解除接续绑定后结束。

`next` 是调度建议，不负责终止进程或自动清理文件。每次命令失败先分类：任务缺陷、单项环境问题、全局环境问题；只有最后一类可能结束整轮运行。

### 5.2 启动新的 session

```bash
TASK_OUT="$RUN_DIR/tasks/T001/run-1"
python3 scripts/run-state.py start "$STATE" T001 --output-dir "$TASK_OUT"
scripts/codex-run.sh <worktree路径> "$RUN_DIR/packets/T001.md" "$TASK_OUT"

SESSION_ID="$(sed -n '1p' "$TASK_OUT/session-id.txt")"
python3 scripts/run-state.py session "$STATE" T001 "$SESSION_ID" --kind initial
python3 scripts/run-state.py set "$STATE" T001 reviewing
```

每个新的任务必须走 `codex-run.sh`。不得为了省事 resume 上一个任务的 session。

每次 run/resume 使用新的空输出目录；封装脚本会拒绝非空目录，防止旧的报告或 watchdog 状态被误认为本轮结果。

上例最后两步只适用于 `codex-run.sh` 退出码为 0 且 session id 非空的情况。非零退出、空 session id 或 `watchdog.json` 报超时时，不得进入 `reviewing`；先登记已取得的 session id，再把任务置为 `retrying` 或 `blocked`，并保留原输出目录供恢复审计。

### 5.3 独立 review 与仲裁

实现 session 结束后，按 `review-packet-template.md` 生成精简 review 包。不要把实现者报告交给 reviewer。reviewer 必须使用全新 session、只读沙箱和当前未提交 diff：

```bash
REVIEW_OUT="$RUN_DIR/tasks/T001/review-1"
python3 scripts/run-state.py review-start "$STATE" T001 \
  --output-dir "$REVIEW_OUT" --target uncommitted
scripts/codex-review.sh <worktree路径> "$RUN_DIR/packets/T001-review.md" "$REVIEW_OUT"

REVIEW_SESSION="$(sed -n '1p' "$REVIEW_OUT/session-id.txt")"
python3 scripts/run-state.py review-session "$STATE" T001 "$REVIEW_SESSION"
```

`codex-review.sh` 会在 review 前后计算目标 fingerprint；期间有任何代码变化就返回 10，该结论不得使用。主 agent 只读取 `review.json`：

- `pass`：记录接受并进入 `verifying`。
- `changes_requested`：定点检查 findings 的证据。确认后进入 `retrying`；误报可覆盖为接受，但必须记录理由。
- `blocked`：若确实缺少契约或用户决定，阻塞任务；能从冻结文档裁决则记录理由后处理。
- 超时、无 session、JSON 损坏或目标变化：以 `error` 结束本轮 review，再开一个全新 reviewer；不要因此重跑实现。

```bash
# reviewer 通过
python3 scripts/run-state.py review-finish "$STATE" T001 \
  --decision accept --result "$REVIEW_OUT/review.json"

# finding 被确认，需要实现返工
python3 scripts/run-state.py review-finish "$STATE" T001 \
  --decision retry --result "$REVIEW_OUT/review.json" \
  --note "<已确认 finding 与复现证据>"

# reviewer 基础设施失败；任务保持 reviewing，可重新 review
python3 scripts/run-state.py review-finish "$STATE" T001 \
  --decision error --note "<超时/输出损坏/目标变化>"
```

reviewer verdict 不是最终验收。主 agent 是仲裁者，但不应重新通读完整 diff；只在 findings 与代码证据冲突、确定性门禁失败或 reviewer 明显不可靠时读取相关切片。每次返工改变代码后，旧 review 失效，必须再启动全新 reviewer session。不要 resume reviewer。若所有 reviewer 都因基础设施失败而达到次数上限，只能 `set ... blocked`，或在主 agent 已完成等价人工检查时用 `set ... verifying --note "<绕过理由与证据>"` 明确留下审计记录。

### 5.4 确定性验证与实现返工

进入 `verifying` 后，Claude Code 只读取结构化 review、watchdog 状态和必要的事件摘要；不要把完整 JSONL、完整 diff 或长日志灌进主上下文。按 `verification-gates.md` 运行确定性验证。

普通实现缺陷：

```bash
python3 scripts/run-state.py set "$STATE" T001 retrying --note "<失败断言与根因>"
RETRY_OUT="$RUN_DIR/tasks/T001/run-2"
python3 scripts/run-state.py start "$STATE" T001 \
  --output-dir "$RETRY_OUT" --resume-session "$SESSION_ID" --note "resume 原 session"
scripts/codex-resume.sh "$SESSION_ID" <反馈文件> "$RETRY_OUT"
python3 scripts/run-state.py set "$STATE" T001 reviewing
```

如果缺陷来自上一节 `review-finish --decision retry`，任务已经是 `retrying`，省略第一条 `set ... retrying`，直接启动返工轮次。

session 无响应、watchdog 超时或上下文损坏：不要 resume；确认旧进程及其遗留写入进程已停止，生成恢复任务包，调用 `codex-run.sh` 创建新 session，并以 `--kind replacement` 登记。恢复包必须包含当前代码状态、已验证部分、失败证据和剩余目标。如果已经表现出任务过大或重复无进展，优先按下一节细分，避免以相同范围反复重开：

```bash
python3 scripts/run-state.py set "$STATE" T001 retrying --note "watchdog 超时"
REPLACEMENT_OUT="$RUN_DIR/tasks/T001/run-2"
python3 scripts/run-state.py start "$STATE" T001 --output-dir "$REPLACEMENT_OUT"
scripts/codex-run.sh <worktree路径> <恢复任务包> "$REPLACEMENT_OUT"
REPLACEMENT_SESSION="$(sed -n '1p' "$REPLACEMENT_OUT/session-id.txt")"
python3 scripts/run-state.py session "$STATE" T001 "$REPLACEMENT_SESSION" --kind replacement
python3 scripts/run-state.py set "$STATE" T001 reviewing
```

达到 `max_retries_per_task + 1` 次执行、review/QA 次数耗尽或出现单项阻塞后，停止尝试当前任务并停放。以下操作仅用于本循环独占且不存在用户改动的 worktree；先确认没有存活的写入进程：

```bash
BLOCKED_PATCH="$RUN_DIR/tasks/T001/blocked.patch"
BLOCKED_STATUS="$RUN_DIR/tasks/T001/blocked-status.txt"
PARK_MANIFEST="$RUN_DIR/tasks/T001/parked.md"
git -C <worktree路径> diff HEAD --binary > "$BLOCKED_PATCH"
git -C <worktree路径> status --porcelain > "$BLOCKED_STATUS"
STASH_REF=""
if [ -s "$BLOCKED_STATUS" ]; then
  git -C <worktree路径> stash push -u -m "codex-loop:T001:blocked"
  STASH_REF="$(git -C <worktree路径> rev-parse 'stash@{0}')"
fi
cat > "$PARK_MANIFEST" <<EOF
任务：T001
原因：<已确认的阻塞或任务过大原因>
旧写入进程：已确认退出
补丁：$BLOCKED_PATCH
状态清单：$BLOCKED_STATUS
stash commit：$STASH_REF
实现与审查日志：$RUN_DIR/tasks/T001/
EOF
python3 scripts/run-state.py set "$STATE" T001 blocked \
  --note "<最终阻塞原因>" --artifact "$PARK_MANIFEST" --stash-ref "$STASH_REF"
python3 scripts/run-state.py next "$STATE"
```

manifest 必须非空并保存在 worktree 外；即使没有代码变更，也要保存阻塞原因和日志位置。`diff HEAD` 包含已暂存和未暂存的受跟踪变更，`stash -u` 另外保存未跟踪文件；记录稳定的 stash commit，不能只记录以后会移动的 `stash@{0}` 名称。不要假装空补丁包含未跟踪文件。若 worktree 含有不属于本循环的改动，禁止 stash 或覆盖，保存诊断并停止该共享工作区的推进。

上述恢复干净 checkpoint 的要求用于 commit 多任务模式。patch 单任务模式没有其他任务会接着写同一 worktree：保存非空的外部交付/阻塞清单后，可以保留未提交成果并标记 blocked，无需 stash，也不得替用户提交。在原尝试预算内恢复同一任务时继续已有成果；首次开始仍需确认基线干净。

单项预算耗尽不是全局停止条件。停放后继续其他 ready 任务；上游 blocked 导致其下游不可执行时也算依赖阻塞，不要求把每个下游手动改成 blocked。不得通过 blocked → pending 清零预算。

### 5.5 对过大任务有界重拆

触发依据：实现者明确报告范围无法在单次 session 内完成；上下文耗尽后接替仍遇到相同问题；连续尝试在相同断言失败且没有新的已验证成果。事件文件增长仅表示进程有输出，不能作为实质进展证明。普通可定位缺陷仍走返工，不为每次失败重拆。

1. 确认旧进程结束，按上节保存半成品、恢复干净 checkpoint。在同一条 blocked 命令中加 `--replan`，持久化下一步要细分的意图：

```bash
python3 scripts/run-state.py set "$STATE" T001 blocked --replan \
  --artifact "$PARK_MANIFEST" --stash-ref "$STASH_REF" \
  --note "<范围过大证据；下一步在原验收范围内细分>"
```

此时 next 返回 replan，接续 hook 也会提醒先完成细分；不能因为唯一任务暂时标为 blocked 就结束。已达到重拆预算时不要添加 --replan，按普通 blocked 停放并继续其他可执行任务。

2. 写至少两个更小的任务包，合起来覆盖原范围与全部验收条件。保留父任务包；最后一个子任务承担必要的组合验收。每个子任务继承原任务的前置依赖和 QA 要求，不得借重拆降级 QA。
3. 保存 JSON 计划，例如：

```json
{
  "tasks": [
    {"id": "T001a", "title": "完成原任务的第一部分", "packet": "packets/T001a.md", "task_kind": "non-ui", "depends_on": []},
    {"id": "T001b", "title": "完成剩余部分与组合验收", "packet": "packets/T001b.md", "task_kind": "non-ui", "depends_on": ["T001a"]}
  ]
}
```

packet 相对路径以计划文件所在目录为基准；UI 父任务的子任务应按实际界面范围填写 task_kind，已要求的 QA 会继续继承。

```bash
python3 scripts/run-state.py split "$STATE" T001 \
  --plan "$RUN_DIR/T001-split.json" --note "<范围过大证据与验收覆盖说明>"
python3 scripts/run-state.py next "$STATE"
```

`split` 原子保存替代关系，将父任务标为 `superseded`，并把依赖父任务的下游改为等待所有替代子任务；父任务既不算 done，也不阻塞已经替代它的子任务。每个子任务使用新的实现 session，不能 resume 父任务。运行级 `max_replans` 防止不断换 ID 重置尝试次数；预算不足时保留 blocked，继续其他 ready 任务。已经完成的任务不重拆，不扩大总目标。

### 5.6 UI QA 与双重验收

任务没有标记 `qa_required` 时可跳过本节。`qa.mode=ui_tasks` 会根据 `--task-kind ui` 自动标记；其他模式按 `configuration.md` 处理。已标记的任务在确定性门禁通过后，必须按 `qa-testing.md`：

1. 用配置指定的模型、推理等级和已配置的 computer-use 驱动启动全新 session 执行黑盒 QA。
2. 校验结构化结果、截图文件及哈希，并确认 QA 前后 worktree fingerprint 未变化。
3. 由 Claude Code 审查截图，并用独立 profile/状态复测关键、高风险和失败流程。
4. 缺陷回到 `retrying`，重新走实现、全新 review、确定性验证和全新 QA；纯 QA 基础设施故障只换新 QA session。

状态依次为 `verifying -> qa_running -> qa_auditing`。不要 resume QA session，也不要把旧截图当作代码变化后的证据。完整命令、QA 包和 `cc-audit.json` 契约见 `qa-testing.md`。

### 5.7 通过与 checkpoint

只有独立验证通过后才能；`qa_required` 任务还必须已有 `qa-audit --decision accept`：

```bash
git -C <worktree路径> add <本任务文件>
git -C <worktree路径> commit -m "<任务 checkpoint>"
python3 scripts/run-state.py set "$STATE" T001 done --commit <commit-sha>
```

不要使用会夹带其他任务或用户改动的宽泛 `git add -A`。commit 模式登记 done 时校验提交确为当前 HEAD 且 worktree 干净；不得把旧 commit 当作新交付。确实无需代码变更的任务须在 note 记录已满足验收的证据。

明确禁止 commit 时在 init 选择 patch 模式，只能交付一个任务，验证通过后导出完整补丁、未跟踪文件归档和验证清单，并以非空 `--artifact <交付清单>` 登记 done。该模式保留工作区成果、不创建 commit，也不承诺随后启动依赖它的任务。不能在多任务队列中简单省略 commit 再声称 checkpoint 干净。

checkpoint 后再次确认 worktree 干净。回退验证产生的临时破坏、未跟踪文件或未提交修改必须恢复；否则不能开始下一任务。

标记完成后立即选择下一任务，不要输出最终答复或等待用户。

## 6. Watchdog

`codex-run.sh`、`codex-resume.sh`、`codex-review.sh` 和 `codex-qa.sh` 都会写 `watchdog.json`。默认值来自冻结配置；未冻结的一次性调用可用以下环境变量覆盖：

- `CODEX_POLL_SECONDS`：检查周期，默认 20
- `CODEX_IDLE_TIMEOUT_SECONDS`：事件流无增长多久判定卡住，默认 1200；设为 0 禁用
- `CODEX_MAX_RUNTIME_SECONDS`：单次运行最长时间，默认 7200；设为 0 禁用
- `CODEX_TERMINATE_GRACE_SECONDS`：TERM 后等待时间，默认 10

watchdog 超时返回 124。实现/返工超时后先确认 Codex 进程已结束和 worktree 是否留下部分修改，再决定 replacement session；review 超时不代表实现失败，记录 review `error` 后换全新 reviewer；QA 超时记录 QA `error` 后换全新 QA session。

## 7. 主上下文与断点恢复

为避免 Claude Code 自身上下文膨胀，固定保留恢复入口 `$RUN_DIR/run-contract.md` 和状态文件路径：

- 任务间只保留 `run-state.py summary`、当前任务包、`review.json`、`qa-summary.md`、验证证据和短报告
- 完整 diff 由独立 reviewer 读取；主上下文通常只读取 findings 和必要代码切片
- 原始 JSONL、长测试日志和旧报告留在运行目录，需要时按关键词读取
- 每次状态变化立即落盘，不在一轮结束时才批量记录
- 验证、审图和独立 UI 复测的详细记录也写到磁盘；支持独立子 agent 时可委托其按原验收标准执行并返回短结果，Claude Code 保留仲裁权，不能以委托代替验证证据
- 上下文压缩或恢复会话后，先读 hook 注入的恢复指引、run-contract，再运行 `summary` 和 `next`；只加载当前任务包及最新结果，恢复 `CODEX_LOOP_RESOLVED_CONFIG`，不要重新初始化状态或凭压缩摘要猜测权限
- 对 `recover` 先区分进程仍在跑、已完成尚未登记、已失败待接替；对 `park` 先停放；对 `replan` 继续尚未完成的细分。压缩不是新任务，不因失去聊天记忆重开仍在运行的 Codex
- 显式换到新的 Claude Code session 时，先停用旧绑定并确认旧编排者退出，再绑定新 session；同一状态禁止双编排者写入

接续 hook 在 Claude Code 尝试正常结束时检查剩余任务，并在 compact/resume 后提供恢复入口；它不监控已退出的宿主，也不替代 Codex watchdog。机器休眠、断电、用户中断或宿主退出后的重新启动仍需外部运行环境支持。见 [claude-continuation.md](claude-continuation.md)。

## 8. 结束与汇总

只在以下情况下结束外层循环：

- 所有有效任务完成且聚合验证已执行；superseded 原任务仅保留审计记录
- 所有剩余任务均不可执行，包括其依赖被 blocked
- 用户明确停止，或到达 deadline、预先约定的全局预算
- 影响整个运行的认证、权限或外部系统问题需要用户介入
- 连续系统性基础设施故障使进一步执行没有意义
- 接续 hook 达到无进展保护上限，需保存原因，不能声称完成

结束前保存现场并解除接续绑定。单任务实现/review/QA 预算耗尽、重拆预算耗尽及单项环境问题都不单独结束整个队列。最终报告列出：完成任务及 commit/patch、superseded 替代关系、阻塞任务及所需用户决定、未开始任务、聚合测试结果、QA/CC 证据摘要与运行目录。没有通过聚合验证或必需 QA 时，不得把运行状态描述成完成。
