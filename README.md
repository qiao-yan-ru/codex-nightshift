# Codex Nightshift · Codex 夜班

**你休息，Claude Code 值班，带着 Codex 把任务一项项做完。**

A Claude Code skill for unattended Codex task orchestration: fresh sessions for small tasks, independent reviews, persistent recovery, and bounded retries.

给 Claude Code 安装的无人值守 Codex 编排 Skill。Claude Code 把大型实现任务拆成可独立验收的小任务，每个任务启动新的实现 session，并在实现后启动另一个全新、只读的 review session；用户可见流程再启动全新的 GPT-6 Astra computer-use QA session，保存截图供 Claude Code 审核和独立复测。同一任务失败时有限度地返工，验收通过后保存 checkpoint 并继续下一项。单项失败耗尽预算后保存现场，其他可执行任务继续；仍然过大的任务可以在运行级预算内继续细分。

它解决两类长任务问题：单个 Codex session 的上下文不适合承载整项大型交付；用户外出或睡觉时，任务需要依靠磁盘状态、watchdog 和明确停止条件持续推进。显式启用的 Claude Code Stop hook 检查尚未完成的队列，SessionStart hook 在上下文压缩或恢复后注入精简恢复指引。hook 只影响绑定的会话，有无进展保护上限，用户可随时解除；不负责在宿主退出、电脑休眠后自动唤醒。

```text
Claude Code（编排、findings 仲裁、确定性验收、截图审计/独立复测、checkpoint）
  └─ 长期集成 worktree
       ├─ T001 → 新实现 → 新只读 review → 确定性验证 → 新 UI QA → CC 复测 → checkpoint
       ├─ T002 → 新实现 → 新只读 review → 确定性验证 → checkpoint（无 UI）
       └─ T003 → 新实现 → 新只读 review → 确定性验证 → 新 UI QA → CC 复测 → checkpoint
```

### 目录

```text
codex-nightshift/
  SKILL.md                              入口：角色边界、核心循环和停止条件
  references/
    unattended-orchestration.md        无人值守任务队列与恢复协议
    claude-continuation.md              Claude Code 接续 hook 安装、绑定与暂停
    configuration.md                   首次引导、角色配置与冻结快照
    task-packet-template.md             单个小任务的冻结式任务包
    review-packet-template.md           独立 reviewer 的精简输入与仲裁规则
    qa-testing.md                       Astra UI QA、截图证据与 CC 独立复测
    verification-gates.md               独立验收与回退验证
    cli-gotchas.md                      CLI、沙箱和 watchdog 参数
  scripts/
    run-state.py                        原子持久化的任务状态机
    claude-loop-hook.py                 会话结束检查与 compact/resume 恢复提示
    loop_config.py                      配置解析、校验与角色运行参数
    codex-common.sh                     CLI 发现与 watchdog 公共逻辑
    codex-run.sh                        为新任务启动新 session
    codex-resume.sh                     同一任务的原 session 返工
    codex-review.sh                     启动全新只读 reviewer 并锁定 diff fingerprint
    codex-qa.sh                         启动全新 Astra UI QA 并校验证据
    worktree-fingerprint.py             锁定 HEAD、diff 和未跟踪文件
    qa_result.py                        校验 Codex QA 与 CC 复测证据
    review-result.schema.json           reviewer 的结构化输出契约
    qa-result.schema.json               Codex QA 的结构化输出契约
    qa-audit.schema.json                CC 截图审计与独立复测契约
    cold-verify.sh                      根脚本独立冷启动验证
  tests/                                状态机与接续 hook 的隔离回归测试
```

### 安装到 Claude Code

先克隆仓库：

```bash
git clone https://github.com/qiao-yan-ru/codex-nightshift.git
cd codex-nightshift
```

个人级：

```bash
mkdir -p ~/.claude/skills
cp -R codex-nightshift ~/.claude/skills/
```

项目级：

```bash
mkdir -p <repo>/.claude/skills
cp -R codex-nightshift <repo>/.claude/skills/
```

安装后可以这样发起：

> 把这个交付拆成小任务并用 codex-nightshift 持续执行。我离开后继续推进；遇到需要我裁决的任务先标记 blocked，再做其他不受影响的任务。

首次使用先完成 [配置引导](codex-nightshift/references/configuration.md)，再按 [接续说明](codex-nightshift/references/claude-continuation.md) 安装 hook、获取当前 Claude Code session 身份并绑定本轮 state。复制 Skill 不会自动修改 Claude Code 设置，也不会自动启用持续运行。

### 前置条件

- macOS 或 Linux，Python 3.10+、Bash 和 Git。
- Claude Code 有权限维护工作目录和持久化运行状态。
- Codex CLI 已安装并登录。
- 需要 UI QA 时，已为 Codex 配置可用的 computer-use/Playwright/PyAutoGUI/MCP 驱动；模型默认 `gpt-6-astra`，但模型选择本身不会创建浏览器或桌面环境。
- UI QA 使用隔离环境、专用测试账号和可重置数据；driver 能把原始截图导出到运行目录。
- 脚本依次使用 `CODEX_BIN`、PATH 中的 `codex`、旧版默认路径 `~/.codex/plugins/.plugin-appserver/codex`。
- 离开前明确允许的操作范围。多任务运行需要允许隔离 worktree 内的本地 checkpoint commit；禁止 commit 时只能使用 patch 单任务交付模式。push、开 PR、合并和外部系统写入分别按授权执行。

Skill 默认只把通过验收的小任务做成本地 checkpoint；不会把“实现任务”自动扩大成 push、PR 或合并授权。

本地回归测试（使用临时目录和 Git 仓库，不启动真实 Codex/Claude 会话）：

```bash
python3 -m unittest discover -s codex-nightshift/tests -v
```

### License

[MIT](LICENSE) © 2026 qiao-yan-ru
