# 验收清单（逐条自查）· 第 2 题《内容任务规划 Agent》

对照题目原文、通用考核要求和示例仓库 `lwz1357/agent-campus-submission-example` 的交付规范，逐行给出结果与证据位置。

- 结果写法：`✅` 达成 / `⚠️` 部分达成（写明缺什么）/ `☐` 提交时完成。没有证据的不标达成，不把"计划做"记为"已实现"。
- 来源：**[题]** 第 2 题原文；**[通用]** 通用考核要求；**[示例]** 示例仓库的交付规范；**[自设]** 本项目自己加的标准。
- 路径均相对仓库根目录。"真实"指本机用 qwen-plus 跑出的记录；"离线"指 `examples/mock/`，只验证流程，不代表真实模型的决策效果。
- 汇总：85 行中 **81 行达成、3 行部分达成、1 行在提交时完成**（G1 最终提交号）。部分达成的 3 行（R14、A5、P2）是同一件事：真实模型在**预算**约束下没有走出"先超限、再改计划"。

## 0. 题目原文

> 内容任务规划 Agent
>
> 项目要求
>
> * 用户与目标：帮助运营人员在预算与截止时间内完成一份内容活动方案。
> * 输入与材料：主题、预算、截止时间；合成素材、工具费用与耗时表。提供素材检索、脚本生成、成本估算、方案保存工具。
> * 交付结果:任务计划、步骤状态、工具结果、费用合计、已保存方案或未完成原因。
> * 动态决策:模型根据成本超限、工具错误或部分成功结果，调整计划、选替代方案或停止；保留已成功步骤的结果。
> * 必须验收:正常完成；缺预算或截止时间；工具失败；预算超限；重复执行。失败步骤续跑属于加分项。
>
> 参考实例：预算 10 个模拟单位；初始方案估算 12，替代方案估算 8。模型观察到超预算后改选 8 的方案，再估算和保存。若没有可行方案，返回未完成及缺口；不能把失败步骤记为成功。
> 审核要点：核对预算计算、步骤依赖与实际状态；注入一次失败，观察是否调整计划且保留成功结果。高分依据是合理重规划；无状态、无失败处理只能证明完成串行调用。

## 1. 项目要求逐条 [题]

| 编号 | 要求 | 结果与证据 |
|---|---|---|
| R1 | 服务对象是运营人员；目标是在预算与截止时间内完成一份内容活动方案 | ✅ `docs/01-requirements.md`：用户、"方案的定义（本项目解读）"、价值假设（标明未测量） |
| R2 | 输入包含主题、预算、截止时间 | ✅ `examples/*/input.json`；校验在 `src/schema.py::check_input` |
| R3 | 提供合成素材 | ✅ `data/materials.json`（12 条） |
| R4 | 提供工具费用与耗时表 | ✅ `data/cost_table.json`；估算读取它（`src/plan.py::estimate`） |
| R5 | 素材检索工具可被模型调用 | ✅ 真实：`examples/normal/trace_review.json` 第 3 步，命中 6 条 |
| R6 | 脚本生成工具可被模型调用 | ✅ 真实：同上第 4 步，产物为结构化脚本（正文由模板生成，已在 01 / 05 说明） |
| R7 | 成本估算工具可被模型调用 | ✅ 真实：同上第 2 步，返回费用、完成时间、是否超限 |
| R8 | 方案保存工具可被模型调用 | ✅ 真实：同上第 6 步；库表 `saved_plans` |
| R9 | 交付含任务计划 | ✅ `output.json` → `result.plan`（步骤、依赖、版本） |
| R10 | 交付含步骤状态 | ✅ `result.plan.steps[].status`；只由执行器写入，终报取自数据库 |
| R11 | 交付含工具结果 | ✅ `result.artifacts`；完整过程在 `trace_review.json` |
| R12 | 交付含费用合计 | ✅ `result.spent_cost / remaining_estimate / projected_total`；`tests/test_loop_mock.py::test_ledger_equals_sum_of_done_steps` |
| R13 | 交付含已保存方案或未完成原因 | ✅ 完成时 `result.saved_plan`；否则 `result.unfinished_reason` 与 `user_message` |
| R14 | 动态决策：成本超限时调整 / 选替代 / 停止 | ⚠️ 部分达成。离线 `examples/mock/over_budget`（12→8）与 `mock/infeasible`（停止并报缺口）走通；真实模型在预算 10 / 8 下拟计划时就控制在预算内，没有出现"超限后再改"（价格表写在提示词里）。真实模型在**截止时间**超限下走通了同一机制：`examples/deadline_tight`。见 `docs/03-evaluation.md` |
| R15 | 动态决策：工具错误时调整 / 停止 | ✅ 真实：`examples/tool_failure_once`（重试）、`examples/tool_failure_persistent`（降档后仍失败 → 主动停止） |
| R16 | 动态决策：部分成功时据此调整 | ✅ 真实：`examples/resume/attempt2`（搜索已完成 → 只补做主脚本） |
| R17 | 保留已成功步骤的结果 | ✅ 真实：续跑后账本 1 + 2 = 3，搜索未重做；`tests/test_plan.py`（已花费不被重规划抹掉）、`tests/test_tools.py`（已完成步骤复用不重复计费） |

## 2. 必须验收 [题]

| 编号 | 验收点 | 结果与证据 |
|---|---|---|
| A1 | 正常完成 | ✅ 真实：`examples/normal`，completed，费用 6 / 20，已保存 |
| A2 | 缺预算 | ✅ `examples/mock/missing_budget`：`needs_input`，指明缺 budget；不调模型、不计费（该路径与模型无关） |
| A3 | 缺截止时间 | ✅ `examples/mock/missing_deadline`：同上 |
| A4 | 工具失败 | ✅ 真实：`examples/tool_failure_once`、`examples/tool_failure_persistent`；失败不计费、不记为成功 |
| A5 | 预算超限 | ⚠️ 部分达成。同 R14：离线与测试覆盖"超限 → 改方案 → 再估算"和"无可行方案 → 未完成"；真实模型未触发预算重规划（`examples/over_budget`、`examples/over_budget_b` 如实记录） |
| A6 | 重复执行 | ✅ `examples/mock/duplicate`（first / second / db_dump：第二次 `served_from=existing`，库中 1 个任务、1 份保存、3 笔账）；`tests/test_cli.py::test_duplicate_submission_is_idempotent` |
| A7 | （加分）失败步骤续跑 | ✅ 真实：`examples/resume/attempt1`（incomplete）→ `attempt2`（`--resume`，completed，只重做主脚本） |

## 3. 参考实例与审核要点 [题]

| 编号 | 要求 | 结果与证据 |
|---|---|---|
| P1 | 预算 10 下能复现"初始 12 → 替代 8"的费用关系 | ✅ `tests/test_plan.py::test_reference_example_12_then_8`、`examples/mock/over_budget`。真实模型直接给出预算内方案，已如实记录，未伪造 |
| P2 | 观察到超预算后改选、再估算、再保存 | ⚠️ 部分达成。该顺序在离线 `mock/over_budget` 走通；真实模型走通的是截止时间版本（`examples/deadline_tight`：估算超时 → 改计划 → 再估算 → 保存），预算版本未出现 |
| P3 | 无可行方案时返回未完成及缺口 | ✅ `examples/mock/infeasible`：incomplete，缺口 1，无保存 |
| P4 | 不能把失败步骤记为成功 | ✅ 真实：`tool_failure_persistent` 主脚本状态 failed、无产物、未计费；`tests/test_loop_mock.py::test_failed_step_never_recorded_as_success` |
| P5 | 核对预算计算 | ✅ 终报费用 = 账本求和 = 已完成步骤费用之和（`test_ledger_equals_sum_of_done_steps`）；`dump` 可导出账本 |
| P6 | 核对步骤依赖与实际状态 | ✅ `src/tools.py::_precheck`（依赖未完成则拒绝）；`tests/test_tools.py` |
| P7 | 注入一次失败，观察是否调整且保留成功结果 | ✅ 真实：`examples/tool_failure_once`，命令在该目录 `command.txt` |
| P8 | 高分依据：合理重规划 | ✅ 真实：`examples/deadline_tight` 第 2→3 步（估算晚 1 小时 → 主脚本改简版，理由"节省 1 小时工时"）。仅一次运行 |
| P9 | 不是无状态、无失败处理的串行调用 | ✅ 持久化 `src/store.py`、失败处理 `src/tools.py`、重复保护与续跑 `src/main.py`；`docs/03-evaluation.md`"审核要点自查" |

## 4. 通用考核要求 [通用]

| 编号 | 要求 | 结果与证据 |
|---|---|---|
| G1 | 提交题号 + 仓库 URL + 最终 commit SHA | ☐ 提交页填写。本文件无法写出自己所在提交的 SHA；以最后一次推送后的 `git rev-parse HEAD` 为准 |
| G2 | AI 辅助如实记录 | ✅ `docs/04-ai-usage.md` |
| G3 | 实际用时与未完成部分 | ✅ `docs/timelog.md`、`docs/05-retrospective.md` |
| G4 | 框架取舍可解释 | ✅ `docs/02-solution.md`"取舍"（手写循环而非 LangGraph、SQLite 而非 JSON） |
| G5 | 可复制命令 | ✅ README；干净目录用 uv 与 pip 各走通一遍，公开仓库匿名克隆后再走通一遍 |
| G6 | 业务工具可本地 / Mock | ✅ 四个业务工具均本地实现 |
| G7 | 离线 Mock 入口并标明只验证流程 | ✅ `--mock`；README、`docs/03`、`examples/README.md`、离线终报的 `note` 字段 |
| G8 | 决策循环真实存在 | ✅ `src/loop.py`；重试、降档、停止均由模型发起（见真实 trace）；边界见 `docs/02` |
| G9 | ≥2 条真实路径，五要素齐全 | ✅ `trace_review.json` 含步骤 / 参数 / 返回摘要 / 后续动作 / 决策依据；真实记录清单见 `examples/README.md` |
| G10 | 步数上限，到上限返回明确未完成 | ✅ 真实：`examples/resume/attempt1`（15 次动作，`step_limit_reached`）；离线 `mock/step_limit` |
| G11 | examples ≥3 组并覆盖全部验收点 | ✅ `examples/README.md`；`docs/03` 场景表逐项引用 |
| G12 | 真实与 Mock 分开存放并标注 | ✅ `examples/<场景>/` 与 `examples/mock/`；两种模式默认使用不同数据库，同一库内不跨模式复用（`tests/test_cli.py::test_real_and_mock_records_are_not_mixed`） |
| G13 | 不提交真实密钥 | ✅ 提交历史扫描无密钥；`.env` 已忽略；`.env.example` 仅占位符 |

## 5. 示例仓库交付规范 [示例]

| 编号 | 要求 | 结果与证据 |
|---|---|---|
| E1 | 根目录有 `README.md` | ✅ |
| E2 | 有 `src/`（源码，含入口） | ✅ 入口 `src/main.py` |
| E3 | 有 `tests/`（自动化测试） | ✅ `uv run pytest`，91 项 |
| E4 | 有 `examples/`（输入、输出、记录） | ✅ |
| E5 | 有 `data/`（合成素材与费用表） | ✅ |
| E6 | `docs/` 含 01–05 五份文档 | ✅ |
| E7 | 依赖文件，版本固定 | ✅ `requirements.txt`（全部固定版本）、`pyproject.toml`、`uv.lock` |
| E8 | `.env.example` 不含真实密钥 | ✅ 仅变量名与占位值 |
| E9 | `.gitignore` | ✅ 含 `.env`、`.venv/`、`__pycache__/`、`*.db`、`out/` |
| E10 | README 快速运行 | ✅ Python 版本、安装、可复制命令、是否需要密钥；干净目录实测通过 |
| E11 | 输出形式说明 | ✅ README：JSON 状态、格式错误退出码 2 |
| E12 | 目录树 | ✅ |
| E13 | 评审导航表 | ✅ 链接全部有效 |
| E14 | 声明 Mock 与真实 | ✅ README、`examples/README.md` |
| E15 | 提交约定；评审能读取仓库 | ✅ 公开仓库，匿名克隆验证可读 |
| E16 | `01-requirements.md` 各项 | ✅ 用户、价值假设、输入、输出、范围、不包含、基础验收、效果边界 |
| E17 | `02-solution.md` 各项 | ✅ 流程图、模块与文件对应、状态值、取舍、局限 |
| E18 | `03-evaluation.md` 各项 | ✅ 开头评估摘要（含未验证项）；场景表（预期 / 离线实测 / 真实实测）；测试总数与通过数与 `test-results.txt` 一致 |
| E19 | `04-ai-usage.md` 记录至少一个具体的个人修改 | ✅ 动作额度提醒由剩 1 次改为剩 3 次，提交 `c0eb18b` |
| E20 | `05-retrospective.md`，不把计划写成已实现 | ✅ 已知不足与现状一致 |
| E21 | 示例输入输出成对 | ✅ 每个目录含 input / output / trace / trace_review / command |
| E22 | `test-results.txt` 含命令、环境、逐项结果、通过数 | ✅ |
| E23 | 测试覆盖正常、缺字段、无资料、工具失败、非法输入、输入不被修改 | ✅ `tests/test_loop_mock.py`、`tests/test_schema.py`（含 `test_check_input_does_not_modify_caller_input`） |
| E24 | 隔离环境无凭据可跑离线部分 | ✅ 干净目录、无 `.env` 实测通过；缺密钥时真实模式返回 `config_error` 且不留任务记录（`test_config_error_leaves_no_run_behind`） |

## 6. 本项目自设标准 [自设]

| 编号 | 要求 | 结果与证据 |
|---|---|---|
| S1 | 恰好一份主脚本；不能删主脚本 | ✅ `tests/test_plan.py`、`tests/test_finish.py`；真实记录中删除被拒（`resume/attempt1` 第 12 步） |
| S2 | finish 会被拒绝 | ✅ `tests/test_finish.py` |
| S3 | 费用三口径 | ✅ `tests/test_plan.py` |
| S4 | 计费动作执行前校验，拒绝不计费 | ✅ `src/tools.py::_precheck`；`tests/test_tools.py` |
| S5 | 预算与截止时间各自硬约束 | ✅ 无放行开关；真实 `examples/deadline_tight` |
| S6 | 缺必填字段不调模型 | ✅ `tests/test_schema.py`；`mock/missing_*` 无 trace；`tests/test_cli.py::test_exit_codes`（不建库） |
| S7 | 身份、冲突、`--new-task` | ✅ `tests/test_cli.py`（参数冲突、模式冲突、`test_new_task_flag_creates_a_separate_run`）。原定的示例目录未做，以测试为证 |
| S8 | 失败任务需显式 `--resume` | ✅ `tests/test_cli.py::test_failed_task_needs_explicit_resume` |
| S9 | 步数上限计所有动作；每轮一个动作 | ✅ `src/loop.py`；`tests/test_loop_mock.py`（step_limit 场景） |
| S10 | 日志只追加；后续动作由后续事件推导 | ✅ `src/trace.py`；`tests/test_trace.py`（再次启动时旧内容原样保留） |
| S11 | Mock 只替换模型客户端并核对观察 | ✅ `src/llm.py`、`src/mock_scripts.py` |
| S12 | 至少 normal 与 tool_failure_once 两条真实记录；over_budget 如实 | ✅ 见 `examples/README.md` |
| S13 | token 成本另行记录 | ✅ 终报 `llm_usage`；`docs/03` |
| S14 | 措辞可验证 | ✅ 全文检索无"全部兜底""零改代码"等说法 |
| S15 | 预算 / 时间不够不报错，附调整说明与面向运营的结论 | ✅ 真实 `deadline_tight` 的 `adjustments` 与 `user_message`；离线 `over_budget`、`infeasible`；`tests/test_report.py`（只在最省时的做法也来不及时才说来不及） |

## 7. 汇总

| 部分 | 总行数 | ✅ | ⚠️ | ☐ 提交时 |
|---|---|---|---|---|
| 1 项目要求 | 17 | 16 | 1 | 0 |
| 2 必须验收 | 7 | 6 | 1 | 0 |
| 3 参考实例与审核要点 | 9 | 8 | 1 | 0 |
| 4 通用考核 | 13 | 12 | 0 | 1 |
| 5 示例仓库规范 | 24 | 24 | 0 | 0 |
| 6 自设标准 | 15 | 15 | 0 | 0 |
| **合计** | **85** | **81** | **3** | **1** |
