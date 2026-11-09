# examples/

| 目录 | 来源 | 覆盖 |
|---|---|---|
| `normal/` `tool_failure_once/` `over_budget/` | **真实模型**（qwen-plus，本机运行） | 正常完成；注入一次失败；预算超限 |
| `mock/<场景>/` ×11 | 离线 Mock（只验证流程） | 全部验收点，见 `docs/03-evaluation.md` 场景表 |
| `test-results.txt` | 真实运行 | `pytest -q` 的命令、环境与结果 |

每个场景目录：`input.json` → `output.json`（终报）、`trace.jsonl`（追加式事件）、`trace_review.json`（步骤 / 参数 / 返回摘要 / 后续动作 / 决策依据）、`command.txt`。
重新生成离线产物：`uv run python -m src.run_examples --mock`。
