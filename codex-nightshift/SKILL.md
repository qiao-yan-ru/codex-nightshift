---
name: codex-nightshift
description: 让 Claude Code 在用户离开期间持续编排 Codex CLI 完成大型实现任务：拆成可独立验收的小任务，为实现、代码审查和 UI QA 分别启动新的 Codex session，以持久化队列、watchdog、截图证据、双重验收和有限返工循环推进。适用于“交给 Codex 通宵跑”“我外出时继续做”“拆开后逐个让 Codex 实现”；不用于一次性代码审查或尚未确定目标的产品决策。
---

# Codex Nightshift

Claude Code 是**持久化编排者、仲裁者和最终验收方**；Codex CLI 的独立 session 分别承担实现、语义审查和 UI 黑盒测试。用户离开后，只要仍有可执行任务且未命中全局停止条件，就继续推进。通过小任务、新 session、磁盘状态和 Claude Code 接续 hook 控制上下文与提前停顿。

## 不变量

- 第一次使用或配置被重置后，必须先执行 `scripts/loop_config.py status`。退出码 20 时按 `references/configuration.md` 询问用户并写入配置；不得先启动 session 或静默接受默认值。
- 一轮长任务开始前必须解析并冻结 JSON 配置；实现、review、QA 的 model/reasoning effort、QA 模式、重试预算和 watchdog 均来自该快照，运行中不得静默漂移。
- 一个大任务使用一个长期集成 worktree；已验证的小任务由 Claude Code 做本地 checkpoint，后续新 session 从代码和任务包获取上下文。
- 多任务无人值守运行使用 `checkpoint_mode=commit`，开始前确认已有授权允许本地 checkpoint。明确禁止 commit 时，只能选择 `patch` 单任务交付，不能承诺多任务持续集成或偷偷提交。
- 新任务只能从干净 checkpoint 开始。最终阻塞的任务必须先保存补丁/日志并可恢复地停放半成品，不能污染下一任务。
- 每个新小任务必须调用 `scripts/codex-run.sh`，产生新的 Codex session。
- 同一小任务的普通返工才使用 `scripts/codex-resume.sh`；session 卡死或损坏时，用新的 replacement session 接管。
- 小任务仍然过大、上下文反复耗尽或连续没有可验证进展时，通过 `set ... blocked --replan` 停放并记录细分意图，再执行有界 `split`；保持原目标和验收标准，下游任务必须等待替代子任务全部完成。不得通过改任务 ID 无限刷新预算。
- 每轮实现结束后必须调用 `scripts/codex-review.sh` 启动**全新、只读** reviewer session。reviewer 不得 resume 实现 session，也不读取实现者报告。
- reviewer 只提供结构化 findings，不拥有最终决定。主 agent 只仲裁 findings、确认审查目标未变化并运行确定性门禁；除非证据冲突，不把完整 diff 读入主上下文。
- 任何代码变化都会使旧 review 失效；返工后必须再开一个全新 reviewer session。review 重试同样有上限。
- 按 `qa.mode` 对任务分类；需要 QA 的用户可见流程标记为 `qa_required`。确定性门禁通过后，必须调用 `scripts/codex-qa.sh`，用配置的模型、推理等级和 computer-use 驱动启动**全新、只读代码仓库**的黑盒 QA session。
- Codex QA 必须产出逐用例断言、原始截图及 SHA-256；Claude Code 只先读摘要，再审查截图，并用独立浏览器状态复测关键、高风险和失败流程。两者不是互相替代关系。
- QA 驱动或隔离环境不可用时必须 `blocked` 或记录基础设施错误后换新 QA session；不得用代码阅读、旧截图或模型自述冒充 UI 通过。任何代码变化都会使旧 QA 失效。
- 任务状态必须写入 `scripts/run-state.py` 管理的 JSON，不能只存在于聊天上下文或临时计划里。
- 实现报告和 reviewer/QA verdict 都不算最终验收。非 UI 任务通过确定性门禁后才能 `done`；`qa_required` 任务还必须取得 CC 截图审计和独立复测的 `accept`。
- 重试限制只约束当前任务。耗尽后保存现场、标记 `blocked` 并继续其他 ready 任务；不得据此结束整个队列。重拆也有运行级预算。
- 无人值守运行前按 `references/claude-continuation.md` 安装并显式绑定当前 Claude Code 会话的接续 hook。没有成功启用时说明只能依赖会话内执行纪律，不能声称已有提前结束兜底。用户要求停止时立即解除绑定并保存现场。

## 路由

- 首次引导、JSON 配置、模型/推理等级或自动 QA 策略：先读 `references/configuration.md`。
- 多任务、通宵、外出期间继续：先读 `references/unattended-orchestration.md`，按外层循环执行。
- 首次设置持续运行、主会话提前结束或上下文压缩后接续：读 `references/claude-continuation.md`。
- 生成任何小任务包前：读 `references/task-packet-template.md`。
- 启动独立代码审查前：读 `references/review-packet-template.md`。
- 验收实现前：读 `references/verification-gates.md`。
- 任务包含 Web、桌面或其他用户可见流程时：读 `references/qa-testing.md`，冻结 QA 包和双重验收边界。
- CLI 报错、session 恢复或沙箱异常时：读 `references/cli-gotchas.md`。

## 外层循环

1. 在用户离开前完成一次 preflight：冻结目标、基线、权限边界、测试环境、截止时间和停止条件。
2. 把大任务拆成能在一个 Codex session 内完成且可独立验收的有向无环任务图；冻结总目标和验收条件，写好初始任务包并初始化状态。执行中允许在原范围内有界细分过大任务。
3. 建一个长期集成 worktree。默认所有小任务顺序落在这个 worktree；不要为每个任务从旧基线重新切分支。
4. 反复选择下一个依赖已完成的 `pending` 任务：标记 `running`，启动新的实现 session；实现完成后进入 `reviewing`。
5. 启动全新、只读 reviewer session。主 agent 读取结构化结果：确认的缺陷进入 `retrying`；通过或被明确驳回的误报进入 `verifying`；review 基础设施失败只重跑 reviewer，不重跑实现。
6. `verifying` 阶段运行确定性门禁。失败则优先 resume 实现 session；无响应、超时或上下文损坏则启动 replacement session。任务过大时细分；达到预算后停放并置为 `blocked`，立即选择其他 ready 任务。
7. 若任务标记 `qa_required`，启动全新 Codex Astra computer-use QA；Claude Code 审查截图并在独立浏览器状态中复测。失败回到实现/review，基础设施错误只换新 QA session。
8. 所有必需门禁通过后，根据 preflight 授权创建本地 checkpoint，记录 commit，并置为 `done`；立即继续下一个 ready 任务。
9. 所有有效任务完成后跑聚合验证，保存结果并解除接续绑定。被细分替代的原任务记为 `superseded`，不冒充已实现。只有用户预先授权时才 push、开 PR 或合并。

## 无人值守时的歧义策略

不要为了等用户而停住整个队列，也不要替用户发明产品决定。把需要用户裁决的任务标记为 `blocked`，写清问题和可选项，然后继续不依赖它的任务。只有全部剩余任务都被阻塞时才结束运行并汇总。

## 运行纪律

进入无人值守循环后，不要在单个任务完成、一次 Codex 退出或一次返工结束时向用户交付最终答复。先落盘状态，再选择下一任务，直到命中全局停止条件。进度说明、上下文压缩和宿主工具的一次前台等待超时都不是完成信号。压缩后从状态文件、授权记录和当前任务包恢复；不要重读历史长日志。`next` 返回 `park` 时先停放耗尽预算的任务，返回 `replan` 时继续细分；不得反复重试同一条失败的 start 命令或在细分前误判全局阻塞。

## 终止条件

全局停止条件：有效任务全部完成并完成聚合验证；所有剩余任务均无法执行（包括依赖被阻塞）；用户明确停止；达到 deadline 或预先约定的全局预算；影响整个运行的认证/权限问题；连续系统性基础设施失败，或接续 hook 判定反复无进展。单项预算耗尽、单项 QA 环境缺失及单项产品歧义只阻塞对应任务。停止前保存状态并解除接续绑定，最终报告区分 `done`、`superseded`、`blocked` 和未开始任务。

## 单任务内循环

任务包必须冻结范围、契约、不要做的相邻项、回退验证和沙箱边界。实现完成后，独立 reviewer 负责读 diff 找语义缺陷；Claude Code 只对 findings 做证据仲裁，并用脚本或精确命令重做回退验证、边界载荷和 Codex 沙箱看不见的路径。UI 任务再由 Codex 走完整黑盒流程、CC 审图并独立复测。具体方法见对应 references。

Codex 不能写 worktree 所指向主仓库的 `.git` 时，commit、push、PR 由 Claude Code 负责，但外部写操作仍受 preflight 的授权边界约束。
