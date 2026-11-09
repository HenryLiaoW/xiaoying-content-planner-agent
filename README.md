# 内容任务规划 Agent（小影科技校招笔试 · 题二）

帮运营人员在**预算与截止时间**内完成一份内容活动方案（素材引用 + 脚本），并保存。
模型（百炼 qwen-plus）负责业务选择：拟计划、估算、逐步执行、遇到超预算 / 工具失败 / 无素材时改计划或停止；
代码负责事实与约束：输入校验、费用与时间记账、依赖与状态真实性、保存与结束条件。

> 当前为可运行原型（CLI），不代表运营最终界面。`--mock` 离线模式只验证流程与状态机，不代表真实模型的决策效果。

## 快速运行

Python ≥ 3.10。推荐 [uv](https://docs.astral.sh/uv/)：

```sh
uv sync                                   # 安装依赖（含 pytest）
cp .env.example .env                      # 填入 LLM_API_KEY（百炼国内区）；离线 mock 不需要
uv run python -m src.main --input examples/normal/input.json --out examples/normal        # 真实模型
uv run python -m src.main --input examples/normal/input.json --mock --out out/mock_normal  # 离线 mock
uv run pytest -q
```

不用 uv：`python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt pytest`，然后把 `uv run` 去掉。

### 常用参数

```
python -m src.main --input FILE [--db PATH] [--out DIR] [--now ISO] [--max-steps N]
                   [--mock [--mock-script NAME]] [--inject TOOL:first|always]
                   [--resume | --new-task] [--quiet]
python -m src.main dump --db PATH --run RUN_ID      # 导出某任务在库中的全部记录
```

输出：成功与业务异常都以 JSON 返回（`status` 为 `completed | incomplete | failed | needs_input | request_conflict`），退出码 0；
格式错误返回 `invalid_input`，退出码 2。`--out` 目录下生成 `output.json`、`trace.jsonl`（追加式事件）、`trace_review.json`（五要素审阅版）。

## 项目目录

```
.
├── README.md
├── pyproject.toml / uv.lock / requirements.txt   # uv 工程；requirements.txt 由 uv export 生成
├── .env.example                                  # 变量名与占位值，不含真实密钥
├── data/
│   ├── cost_table.json      # 工具费用与工时表（合成）
│   └── materials.json       # 12 条合成素材（只有描述与标签）
├── src/
│   ├── main.py      CLI、输入校验、身份分流（重复执行 / 续跑 / 新建）
│   ├── schema.py    输入三段校验、规范化 + 指纹、计划/动作/脚本/估算模型、工具 schema
│   ├── store.py     SQLite：runs / plans / steps / estimates / ledger / saved_plans
│   ├── plan.py      计划结构校验、原子修订、三口径估算、最低可行费用
│   ├── tools.py     七个动作：执行前校验 → 执行 → 记账；故障注入；模板脚本
│   ├── llm.py       OpenAI 兼容客户端与 MockLLM（同接口）
│   ├── loop.py      决策循环（每轮一个动作、步数上限）；读取并填充提示词模板
│   ├── prompts/system.md  系统提示词模板（$变量 占位，改措辞不改代码）
│   ├── trace.py     追加式事件日志、审阅版导出
│   ├── report.py    从库组装终报、调整说明、面向运营的结论
│   └── mock_scripts.py  离线脚本（每场景一份，核对收到的观察）
├── tests/
├── examples/        每个场景：input.json / output.json / trace.jsonl / trace_review.json
└── docs/            01-requirements … 05-retrospective, timelog.md
```

## 评审导航

| 检查项 | 入口 |
|---|---|
| 业务目标与范围 | [docs/01-requirements.md](docs/01-requirements.md) |
| 流程、模块、取舍 | [docs/02-solution.md](docs/02-solution.md) |
| 用例、预期、实测、真实路径记录 | [docs/03-evaluation.md](docs/03-evaluation.md) · [examples/](examples/) |
| AI 辅助边界与个人贡献 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 不足与下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |
| 用时记录 | [docs/timelog.md](docs/timelog.md) |

## 提交约定

题号 2；仓库 `https://github.com/HenryLiaoW/xiaoying-content-planner-agent`；最终 commit SHA 以提交页为准。
不提交 `.env`、数据库文件与任何真实密钥。
