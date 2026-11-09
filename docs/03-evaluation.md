# 测试与评估

两类证据，分开存放、分开标注：

- **真实模型**（百炼 qwen-plus，temperature 0）：`examples/<场景>/`，由 Henry 在本机运行，trace 为真实决策过程。
- **离线 Mock**（替换模型客户端，其余全部真跑）：`examples/mock/<场景>/`，只验证流程与状态机，**不代表真实模型的决策效果**。

每个场景目录：`input.json`、`output.json`、`trace.jsonl`（追加式事件）、`trace_review.json`（五要素审阅版）、`command.txt`（可复制命令）。

## 自动化测试

命令与实际结果见 [examples/test-results.txt](../examples/test-results.txt)。

| 文件 | 覆盖 |
|---|---|
| `test_schema.py` | 11 类格式错误 → `invalid_input`；缺字段 → `needs_input` 且列出缺项；类型错误优先于缺失；指纹：表示法差异等价（10 == 10.0、首尾空白、时区）、语义差异不等价（不 casefold） |
| `test_plan.py` | 参考实例 12 → 8；最低可行费用 3；已花费不被重规划抹掉；6 类结构非法；修订不能删 main / 改 done；改档位重置失败步骤；模拟时钟与截止；保存阻塞项 |
| `test_tools.py` | 执行前校验错误码顺序；拒绝不计费；改计划后必须重估；依赖未完成；done 步骤复用不重复计费；素材引用限制；注入 first/always 与重试上限；档位取自计划；调整说明的触发与变更 |
| `test_finish.py` | 无计划 / 未保存 / 估算过期 → `finish_rejected` / `not_saveable`；incomplete 需原因；只做可选脚本不能保存 |
| `test_loop_mock.py` | 11 个场景各一用例：状态、费用、保存、缺口、attempts、调整触发、五要素齐全、日志只追加；失败步骤不出现在产物中；账本 = done 步骤费用之和 |
| `test_cli.py` | 进程级两次启动：第二次 `served_from=existing`、零新事件、库中 1 任务 1 保存；同 id 不同预算 → `request_conflict`；不同 id 各自执行；失败任务需显式 `--resume`；退出码 |

## 场景与验收点对照

| 场景 | 输入 / 参数 | 预期 | 离线实测 | 真实模型实测 | 验收点 |
|---|---|---|---|---|---|
| normal | budget 20 | 估算可行 → 执行 → 保存 → completed | completed，费用 8（主 pro + 预热 lite） | **completed，7 步 0 拒绝，费用 6**（模型只做主脚本，未加可选预热） | 正常完成 |
| tool_failure_once | `--inject generate_script:first` | 搜索保留；脚本失败观察 → 模型重试/换档 → completed | completed，s2 attempts=2，费用 8 | 待记录 | 工具失败；保留成功结果 |
| tool_failure_persistent | `--inject generate_script:always` | 2 次失败 → `retry_limit_reached` → failed；不记成功 | failed，费用 1，s2=failed 无产物 | — | 工具失败；失败不记成功 |
| over_budget | budget 10，high | 首版 12 超限 → revise → 8 → completed，终报含调整说明 | completed，费用 8，`adjustments[0].trigger=over_budget` | 待记录 | 预算超限；参考实例 |
| infeasible | budget 2 | `min_feasible_cost=3` → incomplete，gap=1，无保存 | incomplete，gap 1.0，user_message 给出建议 | — | 无可行方案返回缺口 |
| missing_budget | 无 budget | `needs_input`，不调模型 | needs_input，无 trace | 同（不调模型，与模式无关） | 缺预算 |
| missing_deadline | 无 deadline | `needs_input` | needs_input | 同 | 缺截止时间 |
| invalid_input | budget 为字符串 | `invalid_input`，退出码 2 | 退出码 2 | 同 | 格式 |
| no_materials | 主题无命中 | 空结果观察 → 模型补搜 → completed | completed，费用 4，trigger=no_materials | — | 素材不足不伪造 |
| deadline_tight | 截止只剩 1.5h | pro 超期 → 模型改 lite → completed | completed，费用 3，trigger=over_deadline | — | 截止约束 |
| duplicate | 同输入两次启动 | 第二次 `served_from=existing`，零新事件 | `test_cli.py` 通过 | 同（入口层逻辑） | 重复执行 |
| step_limit | `--max-steps 4` | incomplete，`step_limit_reached`，可续跑 | incomplete，已完成 2 步保留 | — | 步数上限 |
| resume | 持续失败后 `--resume` | attempt 2，s1 复用，续跑 | 待补 | — | 续跑（加分） |

"—" 表示该场景未用真实模型跑：Mock 已覆盖流程，真实决策以 normal / tool_failure_once / over_budget 三条为准。

## 真实模型路径（≥2 条，含五要素）

### 路径 1：normal（`examples/normal/trace_review.json`）

7 个动作，0 次被拒：propose_plan（1 搜索 + 1 pro 主脚本）→ estimate_cost（"总费用 6 < 预算 20，总工时 2.5h < 可用时间 51h"）→ search_materials（命中 6 条）→ generate_script（引用 m01…）→ estimate_cost（剩余 0）→ save_plan → finish(completed)。费用 6 / 20，token 约 3.5 万。
与 Mock 的差异：模型在 balanced 偏好下没有加可选预热脚本，属合法选择，如实记录。

### 路径 2：tool_failure_once（`examples/tool_failure_once/trace_review.json`）

待记录。

### 预算重规划是否发生（`examples/over_budget/`）

待记录：模型可能先出 12 再改为 8（出现 revise_plan 与 `adjustments`），也可能直接给出可行方案；两种都合法，这里如实写发生了哪种。

## 审核要点自查

| 审核要点 | 证据 |
|---|---|
| 预算计算 | 终报 `spent_cost` = 账本求和 = done 步骤费用之和（`test_loop_mock.py::test_ledger_equals_sum_of_done_steps`）；`dump` 可导出账本 |
| 步骤依赖与实际状态 | 依赖未完成被拒（`deps_not_done`）；状态只由执行器写；终报 plan 与库一致 |
| 注入一次失败 | `--inject generate_script:first`：s2 第 1 次 failed 不计费，第 2 次成功；s1 结果与费用保留 |
| 合理重规划 | over_budget / deadline_tight / no_materials 三个场景的 revise_plan 均带模型 reason，终报 `adjustments` 记录触发原因与变更 |
| 不是串行调用 | 有持久化状态（SQLite）、失败处理（观察回模型）、重复保护（身份 UNIQUE）、续跑（`--resume`） |
