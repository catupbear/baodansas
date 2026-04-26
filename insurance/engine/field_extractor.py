#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字段提取调度器
根据模板指定的规则文件动态加载提取规则，支持按险种回退到基础规则。
"""

import importlib
import inspect
import logging
from typing import Dict, Optional

from insurance.rules.base_rule import BaseRule
from insurance.rules.base_compulsory import BaseCompulsoryRule
from insurance.rules.base_commercial import BaseCommercialRule
from insurance.rules.base_accident import BaseAccidentRule

logger = logging.getLogger(__name__)

# 险种 → 基础规则类映射
BASE_RULES: Dict[str, type] = {
    "compulsory": BaseCompulsoryRule,
    "commercial": BaseCommercialRule,
    "accident": BaseAccidentRule,
}


class FieldExtractor:
    """
    字段提取调度器
    - 优先加载模板指定的保司专属规则文件
    - 加载失败时回退到险种基础规则
    - 基础规则也不存在时回退到 BaseRule
    """

    def __init__(self):
        # 规则实例缓存，避免重复 import 和实例化
        self._rule_cache: Dict[str, BaseRule] = {}

    def load_rule(self, rule_file: str) -> BaseRule:
        """
        加载保司专属规则文件

        Args:
            rule_file: 相对路径，如 "picc/compulsory_v1.py"

        Returns:
            BaseRule 子类实例；加载失败返回 BaseRule()
        """
        # 检查缓存
        if rule_file in self._rule_cache:
            return self._rule_cache[rule_file]

        # 转换文件路径为模块路径：
        #   "picc/compulsory_v1.py" → "insurance.rules.picc.compulsory_v1"
        module_path = rule_file.replace("/", ".").replace("\\", ".")
        if module_path.endswith(".py"):
            module_path = module_path[:-3]
        module_path = f"insurance.rules.{module_path}"

        try:
            module = importlib.import_module(module_path)

            # 在模块中查找 BaseRule 的子类（排除基类本身和中间基类）
            exclude_classes = {BaseRule, BaseCompulsoryRule, BaseCommercialRule, BaseAccidentRule}
            rule_class = None

            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseRule) and obj not in exclude_classes:
                    rule_class = obj
                    break

            if rule_class is None:
                logger.warning("模块 %s 中未找到 BaseRule 子类，使用默认 BaseRule", module_path)
                instance = BaseRule()
            else:
                instance = rule_class()
                logger.info("已加载规则: %s → %s", rule_file, rule_class.__name__)

        except ImportError as e:
            logger.warning("规则模块加载失败: %s, 错误: %s，回退到 BaseRule", module_path, e)
            instance = BaseRule()
        except Exception as e:
            logger.error("规则实例化异常: %s, 错误: %s，回退到 BaseRule", module_path, e)
            instance = BaseRule()

        # 缓存并返回
        self._rule_cache[rule_file] = instance
        return instance

    def load_base_rule(self, policy_type: str) -> BaseRule:
        """
        返回险种基础规则实例

        Args:
            policy_type: 险种类型（compulsory / commercial / accident）

        Returns:
            对应的基础规则实例；未知险种返回 BaseRule()
        """
        cache_key = f"__base__{policy_type}"

        if cache_key in self._rule_cache:
            return self._rule_cache[cache_key]

        rule_cls = BASE_RULES.get(policy_type)
        if rule_cls is None:
            logger.warning("未知险种类型: %s，使用默认 BaseRule", policy_type)
            instance = BaseRule()
        else:
            instance = rule_cls()
            logger.info("已加载基础规则: %s → %s", policy_type, rule_cls.__name__)

        self._rule_cache[cache_key] = instance
        return instance
