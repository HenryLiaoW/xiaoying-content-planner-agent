"""提示词模板：独立文件、占位符全部填充、关键数字来自费用表与请求。"""
from src.loop import build_system_prompt, load_prompt_template
from src.plan import CostTable


def test_prompt_renders_all_placeholders(req):
    text = build_system_prompt(req, CostTable(), max_steps=15)
    assert "$" not in text  # 没有漏填的占位符
    assert "预算：10" in text and "一键 Vlog 模板双11推广" in text and "high" in text
    assert "费用 5，工时 2h" in text and "费用 2，工时 1h" in text and "费用 1，工时 0.5h" in text
    assert "动作上限：15" in text and "最多尝试 2 次" in text


def test_prompt_template_is_a_separate_file():
    tpl = load_prompt_template()
    assert "$budget" in tpl.template and "$max_steps" in tpl.template
    # 决策指引在模板里标明是“选项”；措辞是否过于指令化需要人工审阅，这里不做断言
    assert "这些是选项，不是规则" in tpl.template and "你可以" in tpl.template
