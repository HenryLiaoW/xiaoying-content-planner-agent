# 测试与评估

## 评估摘要（先看这里）

结论分三档：**已验证**（有真实模型记录或确定性测试）、**部分验证**（只有离线，或真实只有一次且不能排除偶然）、**未验证**。

| 观察项 | 证据 | 结论 |
|---|---|---|
| 目录、文档、可复现 | README；干净目录用 uv 与 pip 各装一遍；公开仓库匿名克隆后测试全部通过（`examples/test-results.txt`） | 已验证 |
| 决策循环由模型驱动 | 真实记录里的重试、降档、改计划、主动停止都由模型发起，代码只做校验、记账与拒绝（`examples/*/trace_review.json`） | 已验证 |
| 正常完成 | 真实：`examples/normal` | 已验证 |
| 缺预算 / 缺截止时间 | 入口层直接返回、不调模型（与模型无关）：`examples/mock/missing_*`、`tests/test_schema.py` | 已验证 |
| 工具失败 | 真实：失败一次后重试（`tool_failure_once`）；持续失败后主动停止并保留已完成部分（`tool_failure_persistent`） | 已验证（各一次） |
| 预算超限 | 离线与测试覆盖"估算超限 → 改计划 → 再估算 → 保存"和"无可行方案 → 返回缺口"。真实模型在预算 10 / 8 下拟计划时就控制在预算内，**没有出现"超限后再改"**：价格表写在提示词里，模型提前算好了 | 部分验证 |
| 截止时间超限后的重规划 | 真实：`examples/deadline_tight`（估算晚 1 小时 → 改简版 → 再估算 → 保存） | 已验证（一次） |
| 重复执行 | 入口层判断、不调模型：`examples/mock/duplicate`、`tests/test_cli.py` | 已验证 |
| 失败后续跑（加分） | 真实：`examples/resume`（只补做失败的主脚本，搜索不重做、不重复计费） | 已验证（一次） |
| 最终候选源码的真实复验 | 尚未进行；命令见 `examples/final/command.txt` | 未验证 |
| 持续故障下及时停止 | 依赖"累计失败次数"提示与动作上限；**缺少跨档位的提前停止策略**（换档位会重置该步骤的重试次数）。改进前模型用满 15 次动作，改进后复测一次，在累计 4 次失败时主动停止 | 部分验证 |
| 稳定性 | 每个真实场景只运行一次；**缺少重复运行统计**（状态、费用、步数的分布） | 未验证 |
| 脚本内容质量 | 脚本由确定性模板生成，不是模型写的；素材匹配是关键词打分 | 未验证 |
| 运营可用性 | 命令行原型，输入输出是 JSON；没有界面，降级前不会先询问运营 | 未验证 |

不能仅因目录和测试齐全就判定本项目达标；上表"部分验证 / 未验证"是如实的边界，对应的改进方向见 [05-retrospective.md](05-retrospective.md)。逐条对照题目与交付规范的自查见 [acceptance-checklist.md](acceptance-checklist.md)。

两类证据，分开存放、分开标注：

- **真实模型**（百炼 qwen-plus，temperature 0）：`examples/normal/` `tool_failure_once/` `over_budget/` `over_budget_b/`（11-09）与 `examples/resume/attempt1` `attempt2`、`examples/tool_failure_persistent/`、`examples/deadline_tight/`（11-10），均由 Henry 在本机运行，trace 为真实决策过程。
- **离线 Mock**（替换模型客户端，其余全部真跑）：`examples/mock/<场景>/`，只验证流程与状态机，**不代表真实模型的决策效果**。

每个场景目录：`input.json`、`output.json`、`trace.jsonl`（追加式事件）、`trace_review.json`（五要素审阅版）、`command.txt`（可复制命令）。

## 自动化测试

共 91 项，全部通过。命令、环境与逐项结果见 [examples/test-results.txt](../examples/test-results.txt)。

| 文件 | 覆盖 |
|---|---|
| `test_schema.py` | 11 类格式错误 → `invalid_input`；缺字段 → `needs_input` 且列出缺项；类型错误优先于缺失；校验不改动调用方的原始输入；指纹：表示法差异等价（10 == 10.0、首尾空白、时区）、语义差异不等价（不 casefold） |
| `test_plan.py` | 参考实例 12 → 8；最低可行费用 3；已花费不被重规划抹掉；6 类结构非法；修订不能删 main / 改 done；改档位重置失败步骤；模拟时钟与截止；保存阻塞项 |
| `test_tools.py` | 执行前校验错误码顺序；拒绝不计费；改计划后必须重估；依赖未完成；done 步骤复用不重复计费；素材引用限制；注入 first/always 与重试上限；档位取自计划；调整说明的触发与变更 |
| `test_finish.py` | 无计划 / 未保存 / 估算过期 → `finish_rejected` / `not_saveable`；incomplete 需原因；只做可选脚本不能保存 |
| `test_loop_mock.py` | 11 个场景各一用例：状态、费用、保存、缺口、attempts、调整触发、五要素齐全、事件序号连续；给运营的话里不出现内部编号；转交说明只在出问题时生成；失败步骤不出现在产物中；账本 = done 步骤费用之和 |
| `test_cli.py` | 进程级两次启动：第二次 `served_from=existing`、零新事件、库中 1 任务 1 保存；同 id 不同预算 → `request_conflict`；不同 id 各自执行；无 id 时 `--new-task` 才新建任务；失败任务需显式 `--resume`；退出码；入口层拒绝时不建库；缺密钥返回 `config_error` 且不留任务记录；同一库内真实与离线记录不混用；默认数据库按模式区分；终报记录源码版本 |
| `test_report.py` | 超期相关的话只依据算得出来的事实：最省时的做法也来不及才说"来不及"并给出差多少；当前计划超时但有更省时的做法时不下这个结论；因工具失败结束的任务报失败而不是报超期 |
| `test_trace.py` | 事件日志只追加：再次启动写同一个文件时旧内容一字不动，序号接着往后排 |
| `test_prompt.py` | 提示词模板是独立文件；占位符全部填充；关键数字来自费用表与请求 |

## 场景与验收点对照

| 场景 | 输入 / 参数 | 预期 | 离线实测 | 真实模型实测 | 验收点 |
|---|---|---|---|---|---|
| normal | budget 20 | 估算可行 → 执行 → 保存 → completed | completed，费用 8（主 pro + 预热 lite） | **completed，7 步 0 拒绝，费用 6**（模型只做主脚本，未加可选预热） | 正常完成 |
| tool_failure_once | `--inject generate_script:first` | 搜索保留；脚本失败观察 → 模型重试/换档 → completed | completed，s2 attempts=2，费用 8 | **completed，8 步**：s2 第 1 次 tool_error 不计费 → 模型选择重试（"按规则可重试一次"）→ 成功；s1 结果与费用保留；费用 6 | 工具失败；保留成功结果 |
| tool_failure_persistent | `--inject generate_script:always` | 2 次失败 → `retry_limit_reached` → failed；不记成功 | failed，费用 1，s2=failed 无产物 | **failed，10 步，费用 1**：累计失败 4 次后模型主动 `finish(failed)`，理由"累计失败4次，服务暂时不可用"；搜索结果与 1 元保留（改进后复测，见下） | 工具失败；失败不记成功 |
| over_budget | budget 10，high | 首版 12 超限 → revise → 8 → completed，终报含调整说明 | completed，费用 8，`adjustments[0].trigger=over_budget` | **completed，10 步，费用 9**：模型首版即拟 2 搜索 + pro + lite = 9（在预算内），**未触发重规划**；第 7 步想跳过估算直接保存被拒 → 补估算 → 保存 | 预算超限；参考实例 |
| over_budget_b | budget 8，high | 同上（预算更紧） | — | **completed，8 步，费用 8**：首版即拟 1 搜索 + pro + lite = 8，理由含"满足 high 质量偏好和预算约束"；终报记 `preference_tradeoff` | 预算约束下的规划 |
| infeasible | budget 2 | `min_feasible_cost=3` → incomplete，gap=1，无保存 | incomplete，gap 1.0，user_message 给出建议 | — | 无可行方案返回缺口 |
| missing_budget | 无 budget | `needs_input`，不调模型 | needs_input，无 trace | 同（不调模型，与模式无关） | 缺预算 |
| missing_deadline | 无 deadline | `needs_input` | needs_input | 同 | 缺截止时间 |
| invalid_input | budget 为字符串 | `invalid_input`，退出码 2 | 退出码 2 | 同 | 格式 |
| no_materials | 主题无命中 | 空结果观察 → 模型补搜 → completed | completed，费用 4，trigger=no_materials | — | 素材不足不伪造 |
| deadline_tight | 截止只剩 1.5h | pro 超期 → 模型改 lite → completed | completed，费用 3，trigger=over_deadline | **completed，9 步，费用 3**：首版完整版主脚本 → 估算超时 1 小时 → 模型改为简版 → 再估算通过 → 执行 → 保存；终报记 `over_deadline` | 截止约束；估算后重规划 |
| duplicate | 同输入两次启动 | 第二次 `served_from=existing`，零新事件 | `test_cli.py` 通过；记录见 `examples/mock/duplicate/`（first / second / db_dump） | 同（入口层逻辑） | 重复执行 |
| step_limit | `--max-steps 4` | incomplete，`step_limit_reached`，可续跑 | incomplete，已完成 2 步保留 | — | 步数上限 |
| resume | `--inject generate_script:always` 后不注入 `--resume` | attempt 2，已完成的搜索复用，只补做失败的主脚本 | `test_cli.py` 进程级两次启动通过 | **attempt1 incomplete（`step_limit_reached`，15 步）；attempt2 `--resume` completed（7 步，总费用 3）**：搜索不重做不重复计费，同一任务 attempt_no=2 | 续跑（加分）；工具持续失败 |

"—" 表示该场景未用真实模型跑：Mock 已覆盖流程，真实决策以 normal / tool_failure_once / over_budget / over_budget_b / tool_failure_persistent / resume / deadline_tight 为准。token 用量分别约 3.5 万 / 4.1 万 / 5.8 万 / 4.1 万（终报 `llm_usage`），不计入模拟预算。

## 真实模型路径（≥2 条，含五要素）

### 路径 1：normal（`examples/normal/trace_review.json`）

7 个动作，0 次被拒：propose_plan（1 搜索 + 1 pro 主脚本）→ estimate_cost（"总费用 6 < 预算 20，总工时 2.5h < 可用时间 51h"）→ search_materials（命中 6 条）→ generate_script（引用 m01…）→ estimate_cost（剩余 0）→ save_plan → finish(completed)。费用 6 / 20，token 约 3.5 万。
与 Mock 的差异：模型在 balanced 偏好下没有加可选预热脚本，属合法选择，如实记录。

### 路径 2：tool_failure_once（`examples/tool_failure_once/trace_review.json`）

8 个动作。第 4 步 generate_script 被注入故障：观察为 `tool_error, retryable=true, attempts_left=1, 未计费`；第 5 步模型重试同一步骤，理由"重试 s2 脚本生成：服务第一次失败，按规则可重试一次"→ 成功计费 5。终报 s2 `attempts=2, status=done`，账本两笔 1 + 5 = 6，失败那次无记账；`adjustments` 为空（重试不是重规划）。与路径 1 的分叉点就是第 4→5 步：同样的输入，因观察不同而多走了一步。

### 预算重规划是否发生（`examples/over_budget/`、`examples/over_budget_b/`）

**没有发生"先 12 后 8"。** 两次真实运行模型都在 propose_plan 阶段就把费用表算进去了：

| 预算 / 偏好 | 首版计划 | 费用 | 结果 |
|---|---|---|---|
| 20 / balanced（normal） | 1 搜索 + pro 主脚本 | 6 | 直接完成 |
| 10 / high | 2 搜索 + pro 主脚本 + lite 预热 | 9 | 直接完成；第 7 步跳过估算直接保存被拒，补估算后保存 |
| 8 / high | 1 搜索 + pro 主脚本 + lite 预热 | 8 | 直接完成；终报记 `preference_tradeoff`（high 偏好下预热选 lite） |

预算每降一档模型砍掉一项不影响必需交付的步骤，reason 均提到预算约束。我们如实记录：真实模型的预算意识体现在规划阶段；"超预算 → 执行被拒 → revise_plan → 再估算 → 保存"这条路径由离线场景 `mock/over_budget`、`mock/deadline_tight` 与 `test_tools.py` / `test_plan.py` 覆盖，代码路径是通的，只是真实模型在**预算**约束下没有走到。同一条路径在**截止时间**约束下由真实模型走到了，见下节。

### 真实路径中"观察 → 调整"的实证

1. tool_failure_once 第 4→5 步：失败观察 → 重试。
2. over_budget 第 7→8 步：`not_saveable`（估算过期）→ 补 estimate_cost → 保存。
3. 三组预算下三个不同计划：输入变化 → 计划变化，均非代码写死。
4. deadline_tight 第 2→3 步：估算超时 1 小时 → 主视频脚本改简版 → 再估算通过。
5. tool_failure_persistent 第 9→10 步：累计失败 4 次 → 主动 `finish(failed)`。

### 截止时间重规划（`examples/deadline_tight/`）

输入：预算 20，模拟当前 09:00，截止 10:30（只剩 1.5 小时）。9 个动作，0 次被拒，token 约 4.5 万。

| 步 | 动作 | 结果 | 模型理由 |
|---|---|---|---|
| 1 | 拟计划：一次素材搜索 + 完整版主视频脚本 | 记为第 1 版 | 满足必需交付物 |
| 2 | 估算 | 预计 11:30 完成，**晚于截止 1 小时**；预算内 | — |
| 3 | 改计划：主视频脚本完整版 → 简版 | 第 2 版 | "节省 1 小时工时，确保在截止时间内完成" |
| 4 | 再估算 | 预计 10:30 完成，通过 | — |
| 5–6 | 素材搜索、写简版主视频脚本 | 花 1 + 2 = 3，模拟时间 10:30 | — |
| 7–9 | 估算、保存、结束 | completed | — |

这是真实模型走出的"估算不通过 → 改计划 → 再估算 → 执行 → 保存"完整路径。调整发生在任何费用产生之前。终报给运营的话："原方案预计 11-10 11:30 完成，晚于截止时间，已调整为主视频脚本由完整版改为简版——这是时间不足下的调整结果。"

与预算实验对照：预算紧时模型在拟计划阶段就避开了超限，时间紧时却先拟了超时的方案再靠估算纠正。单次运行，不对原因下结论；可见的事实是估算这道检查在真实运行中起了作用。

### 续跑与持续故障（`examples/resume/attempt1`、`attempt2`）

输入：预算 20、balanced、`--inject generate_script:always`（脚本服务每次都失败，失败不计费）。

**attempt1（incomplete，15 步，token 约 9.2 万）。** 初始计划：第一次搜素材 + pro 档主视频脚本，估算 6。搜索成功（花 1）后，主视频脚本连续失败，模型依次：原样重试（2 次，触发重试上限）→ 降为 lite 档 → 重估 → 再试失败 → 想少传素材（实际提交的计划修改没有改变任何内容）→ 再试失败 → 想删除主脚本重建（被拒：`required_deliverable_missing`）→ 再试（被拒：`retry_limit_reached`）→ 新增第二次搜素材、改依赖、重估，动作数用满。结果：`incomplete`，原因 `step_limit_reached`；第一次搜素材的结果与 1 元费用保留，失败的 4 次尝试均未计费。

代码守住的约束全部起作用：重试上限、必需交付物不可删、步数上限、不记失败为成功。

**暴露的问题（如实记录）：** 脚本累计失败 4 次，模型始终没有选择 `finish(status="failed")`，而是持续换参数，把 15 步用满。当时的失败观察只写"第几次尝试失败"，没有累计次数；换档位又会重置该步骤的重试计数，模型每次都有新的尝试机会。终报是通用的"动作额度用完"，没有告诉运营"主脚本连续失败"。

**attempt2（`--resume`，completed，7 步，token 约 3.5 万）。** 服务恢复。程序把已有计划、已完成步骤的结果（素材清单）和已花费 1.0 作为"续跑上下文"交给模型。模型：删除上次遗留、尚未执行的第二次搜素材（理由：冗余，省费用与工时）→ 重估（预计 3）→ 用 lite 档写主视频脚本（成功，花 2）→ 保存被拒（估算过期）→ 补估算 → 保存 → `finish(completed)`。

| 检查点 | 结果 |
|---|---|
| 已完成的搜索是否重做 / 重复计费 | 否；账本 = 搜索 1 + 脚本 2 = 3 |
| 是否仍是同一任务 | 是，同一 run_id，`attempt_no=2`，`served_from=resumed` |
| 档位 | 沿用上次降级后的 lite（预算 20 本可用 pro；模型理由是"成本低、工时短、素材足以支撑简版"），属模型的合法选择 |

**据此做的改进（commit `310912e`）：**

1. 失败观察附带本次运行的累计失败次数；累计 ≥3 次时提示"多次换参数后仍失败通常说明服务暂时不可用"（只给信息，不替模型决定）。提示词补充：换档、换素材后仍失败，可以 `finish(status="failed")` 并稍后续跑。
2. 调整记录（`adjustments`）改存数据库并跨启动保留。attempt1 里"主脚本 pro→lite"的调整在 attempt2 终报里原本丢失，现已修复。

因此 `examples/resume/` 两份记录是改进**之前**的真实行为，保留原样，不重写。

**复测（`examples/tool_failure_persistent/`，改进之后，同样的持续故障、新的 request_id）：** 10 步、`failed`、费用 1、token 约 5.3 万（改进前 15 步、约 9.2 万）。过程：pro 档写主视频脚本失败 2 次（第 2 次已自行减少素材）→ 降为 lite 档、重估 → lite 失败（观察出现"累计失败 3 次；多次换参数后仍失败通常说明服务暂时不可用"）→ 只用 1 条素材再试，失败（累计 4 次）→ `finish(failed)`，理由"累计失败4次，服务暂时不可用"，与观察措辞一致。已完成的搜索结果与 1 元费用保留，可 `--resume`。

这次运行还暴露了终报文案的两处问题，已修复（保留本次记录原样，其中的 `user_message` 是修复前的）：

1. 写的是"连续 2 次失败"，实际累计 4 次（完整版 2 次、简版 2 次）——换档位重置了重试计数，文案读的是重置后的数。现在每个步骤按档位累计失败次数（`Step.failures`，换档位、续跑都不清零），文案写"累计失败 4 次（完整版 2 次、简版 2 次）"。
2. 文案里出现了内部步骤编号，并原样附上了模型的说明（含工具名）。现在给运营的话只用可读名称（"素材搜索「查询词」""主视频脚本生成"），模型的原话只留在 `result.unfinished_reason` 与 `model_summary` 供审阅。测试 `test_user_message_has_no_internal_ids` 对全部离线场景检查。

3. 排查问题的人需要的细节（任务号、工具名、步骤编号、模型原话）改放进新增的 `handoff_note`（转交说明），只在失败 / 动作用完 / 模型接口出错时生成，运营文案末尾提示可转给负责人。示例见 `examples/mock/tool_failure_persistent/output.json`。

局限：只有一次运行；第 4 次失败恰好也是 lite 档重试次数用尽的时刻，无法把"累计提示的作用"与"重试上限的作用"分开，只能说改进后模型在该点主动收手，而改进前没有。

## 审核要点自查

| 审核要点 | 证据 |
|---|---|
| 预算计算 | 终报 `spent_cost` = 账本求和 = done 步骤费用之和（`test_loop_mock.py::test_ledger_equals_sum_of_done_steps`）；`dump` 可导出账本 |
| 步骤依赖与实际状态 | 依赖未完成被拒（`deps_not_done`）；状态只由执行器写；终报 plan 与库一致 |
| 注入一次失败 | `--inject generate_script:first`：s2 第 1 次 failed 不计费，第 2 次成功；s1 结果与费用保留 |
| 合理重规划 | over_budget / deadline_tight / no_materials 三个场景的 revise_plan 均带模型 reason，终报 `adjustments` 记录触发原因与变更 |
| 不是串行调用 | 有持久化状态（SQLite）、失败处理（观察回模型）、重复保护（身份 UNIQUE）、续跑（`--resume`） |
