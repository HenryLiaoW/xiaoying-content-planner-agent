# examples/

| 目录 | 来源 | 覆盖 |
|---|---|---|
| `normal/` | **真实模型**（qwen-plus，本机运行） | 正常完成 |
| `tool_failure_once/` | 真实模型 | 脚本服务失败一次 → 模型重试 → 完成 |
| `tool_failure_persistent/` | 真实模型 | 脚本服务持续失败 → 累计 4 次后模型主动 failed，已完成部分保留 |
| `over_budget/` `over_budget_b/` | 真实模型 | 预算 10 / 8：模型在拟计划阶段就控制在预算内 |
| `deadline_tight/` | 真实模型 | 估算超时 1 小时 → 模型把主脚本改为简版 → 再估算通过 → 完成 |
| `resume/attempt1` `attempt2` | 真实模型 | 持续失败用完动作额度 → `--resume` 续跑，只补做失败步骤，不重复计费 |
| `mock/<场景>/` ×11 | 离线 Mock（只验证流程） | 全部验收点，见 `docs/03-evaluation.md` 场景表 |
| `test-results.txt` | 真实运行 | `uv run pytest` 的命令、环境与结果 |

真实记录按运行当时的代码原样保留，不事后改写；之后的修复见 `docs/03-evaluation.md`。

每个场景目录：`input.json` → `output.json`（终报）、`trace.jsonl`（追加式事件）、`trace_review.json`（步骤 / 参数 / 返回摘要 / 后续动作 / 决策依据）、`command.txt`。
重新生成离线产物：`uv run python -m src.run_examples --mock`。
