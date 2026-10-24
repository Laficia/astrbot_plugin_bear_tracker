"""Shared, restricted text templates for group alerts and WebUI previews."""
from string import Formatter

DEFAULT_TEMPLATE = "库啵，发现一只{等级}S: {怪名}出现在[{大区简称}-{服务器}] {地图} ({坐标})"
DEFAULT_TRIGGER_TEMPLATE = "库啵，{等级}S: {怪名}可以触发啦，目前有概率出现在{服务器列表}等，冒险者大人们快去尝试呀！"
MAX_TEMPLATE_LENGTH = 2000
SAMPLE_VALUES = {"等级": "50", "怪名": "虚无探索者", "大区": "豆豆柴", "大区简称": "狗",
                 "服务器": "伊修加德", "地图": "西萨纳兰", "坐标": "18.4, 15.1",
                 "X": "18.4", "Y": "15.1", "分线": ""}
VARIABLES = [{"name": name, "sample": value} for name, value in SAMPLE_VALUES.items()]
TRIGGER_SAMPLE_VALUES = {name: SAMPLE_VALUES[name] for name in ("等级", "怪名", "大区", "大区简称", "服务器", "地图", "分线")}
TRIGGER_SAMPLE_VALUES["服务器列表"] = "[狗-伊修加德]、[狗-水晶塔]"
TRIGGER_VARIABLES = [{"name": name, "sample": value} for name, value in TRIGGER_SAMPLE_VALUES.items()]


def validate_template(template, kind="sighting"):
    if kind not in ("sighting", "trigger"):
        raise ValueError("未知模板类型")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("播报模板不能为空")
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise ValueError(f"播报模板不能超过{MAX_TEMPLATE_LENGTH}个字符")
    template = template.replace("\r\n", "\n").replace("\r", "\n")
    try:
        fields = list(Formatter().parse(template))
    except ValueError as exc:
        raise ValueError("模板花括号不完整；显示花括号请使用 {{ 和 }}") from exc
    for _, field, spec, conversion in fields:
        if field is None:
            continue
        if field not in (TRIGGER_SAMPLE_VALUES if kind == "trigger" else SAMPLE_VALUES):
            raise ValueError("不支持的模板变量：{" + field + "}")
        if spec or conversion is not None:
            raise ValueError("模板变量只需填写名称，例如 {怪名}；不支持格式表达式")
    return template


def render_template(template, values, kind="sighting"):
    return validate_template(template, kind).format_map(values)
