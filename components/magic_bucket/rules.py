"""
转发规则数据结构和管理模块
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class RuleType(Enum):
    """规则类型枚举"""

    PREFIX = "prefix"  # 前缀匹配
    KEYWORD = "keyword"  # 关键词匹配


@dataclass
class ForwardRule:
    """转发规则数据类"""

    name: str  # 规则名称
    type: str  # 规则类型: "prefix" 或 "keyword"
    source_groups: List[int]  # 监听的源群号列表
    target_groups: List[int]  # 转发到的目标群号列表
    keywords: List[str]  # 前缀列表或关键词列表
    forward_prefix: str = "[转发]"  # 转发前缀模板
    attach_map_image: bool = False  # 命中时是否附带识别到的地图图片

    def __post_init__(self):
        """数据验证"""
        if not self.name.strip():
            raise ValueError("规则名称不能为空")

        if self.type not in [RuleType.PREFIX.value, RuleType.KEYWORD.value]:
            raise ValueError(f"无效的规则类型: {self.type}")

        if not self.source_groups:
            raise ValueError("源群列表不能为空")

        if not self.target_groups:
            raise ValueError("目标群列表不能为空")

        if not self.keywords:
            raise ValueError("关键词列表不能为空")

    def matches_message(self, message: str) -> bool:
        """检查消息是否匹配此规则"""
        message = message.strip()
        if not message:
            return False

        if self.type == RuleType.PREFIX.value:
            # 前缀匹配：消息以任一关键词开头
            return any(message.startswith(keyword) for keyword in self.keywords)
        elif self.type == RuleType.KEYWORD.value:
            # 关键词匹配：消息包含任一关键词
            return any(keyword in message for keyword in self.keywords)

        return False

    def can_forward_to(self, source_group: int, target_group: int) -> bool:
        """检查是否可以从源群转发到目标群（防循环检查）"""
        return (
            source_group in self.source_groups
            and target_group in self.target_groups
            and source_group != target_group  # 防止转发到自身
        )

    def format_forward_message(
        self, original_message: str, source_group: int, sender_name: str = ""
    ) -> str:
        """格式化转发消息"""
        if not self.forward_prefix:
            return original_message

        # 替换前缀模板中的变量
        prefix = self.forward_prefix.format(
            source_group=source_group, sender=sender_name, rule_name=self.name
        )

        return f"{prefix} {original_message}"

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ForwardRule":
        """从字典创建规则实例"""
        return cls(**data)


class ForwardRuleManager:
    """转发规则管理器"""

    def __init__(self, config: dict | None = None, save: Callable | None = None):
        self.config = config if config is not None else {}
        self._save = save
        self.rules: List[ForwardRule] = []
        self.load_config()

    def load_config(self) -> None:
        """加载配置文件"""
        try:
            # 加载转发规则
            rules_data = self.config.get("rules", [])
            self.rules = []

            for rule_data in rules_data:
                try:
                    rule = ForwardRule.from_dict(rule_data)
                    self.rules.append(rule)
                except Exception as e:
                    print(f"加载规则失败: {rule_data.get('name', 'Unknown')} - {e}")

            print(f"成功加载 {len(self.rules)} 条转发规则")

        except Exception as e:
            print(f"加载配置文件失败: {e}")
            self.rules = []

    def save_config(self) -> bool:
        """保存配置到文件"""
        try:
            # 读取现有配置
            self.config["rules"] = [rule.to_dict() for rule in self.rules]
            if self._save is not None:
                self._save("rules", self.config["rules"])

            return True

        except Exception as e:
            print(f"保存配置文件失败: {e}")
            return False

    def add_rule(self, rule: ForwardRule) -> bool:
        """添加新规则"""
        # 检查规则名是否重复
        if self.get_rule(rule.name):
            print(f"规则名称已存在: {rule.name}")
            return False

        try:
            # 验证规则数据
            rule.__post_init__()
            self.rules.append(rule)
            return self.save_config()
        except Exception as e:
            print(f"添加规则失败: {e}")
            return False

    def remove_rule(self, rule_name: str) -> bool:
        """删除规则"""
        original_count = len(self.rules)
        self.rules = [rule for rule in self.rules if rule.name != rule_name]

        if len(self.rules) < original_count:
            return self.save_config()
        else:
            print(f"未找到规则: {rule_name}")
            return False

    def get_rule(self, rule_name: str) -> Optional[ForwardRule]:
        """获取指定规则"""
        for rule in self.rules:
            if rule.name == rule_name:
                return rule
        return None


    def find_matching_rules(self, message: str, source_group: int) -> List[ForwardRule]:
        """查找匹配消息的规则"""
        matching_rules = []

        for rule in self.rules:
            if source_group in rule.source_groups and rule.matches_message(message):
                matching_rules.append(rule)

        return matching_rules
