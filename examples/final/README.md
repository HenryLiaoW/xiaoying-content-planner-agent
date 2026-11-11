# examples/final/ · 最终候选源码上的真实模型复验

目的：真实模型的证据要对应提交用的源码。做法：使用全新的独立数据库；输出写到本目录，不覆盖 `examples/` 下任何历史记录；每份 `output.json` 的 `source_version` 是产生它的源码版本（git 短提交号）。

| 轮次 | 源码 | 数据库 | 位置 | 结果 |
|---|---|---|---|---|
| 第一轮 | `c5a3d89` | `out/final.db` | 本目录下 `normal/` `tool_failure_once/` `tool_failure_persistent/` `resume/` `deadline_tight/`；命令 `command.txt` | 5 条中 4 条符合预期；`normal/` 以“模型接口调用失败”结束，暴露了一个缺陷：模型发来的工具参数不是合法 JSON 时，程序把它原样放回对话历史，接口拒绝了下一次请求 |
| 第二轮 | `784452c`（修复上述缺陷之后） | `out/final_rerun.db` | `rerun/` 下的同名子目录；命令 `rerun/command.txt` | 尚未运行 |

第一轮的失败记录原样保留，没有用重跑覆盖。两轮之间源码的差别（`git diff c5a3d89 784452c -- src`）：上述缺陷的修复、事件摘要多保留了几项模型看到的提醒字段、一句给运营的话的措辞。
逐条说明见 [docs/03-evaluation.md](../../docs/03-evaluation.md) 的“最终候选源码的真实复验”。
