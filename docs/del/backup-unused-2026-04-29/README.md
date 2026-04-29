# 无用代码备份 (2026-04-29)

本目录存放从项目中清理的无用代码，如需恢复请按以下说明操作。

---

## 备份清单

### 1. handler_legacy_dingtalk.py
- **来源**: `insurance/handler.py` 第 787-916 行
- **内容**: `_get_bound_targets()` + `_sync_to_dingtalk()` 两个方法
- **废弃原因**: 已被 `_get_matched_monitors()` + `_sync_to_dingtalk_v2()` 替代
- **恢复方式**: 将两个方法复制回 `insurance/handler.py` 的 `InsuranceHandler` 类中（放在 `_sync_to_dingtalk_v2` 方法之前即可）

### 2. db_unused_functions.py
- **来源**: `insurance/db.py` 第 389-424 行
- **内容**: `sync_policy_fields()` + `query_policy_fields()` 两个函数
- **废弃原因**: 全项目无调用，字段同步逻辑已内置于 `save_insurance_record()` / `update_insurance_record()` 中自动处理
- **恢复方式**: 将两个函数复制回 `insurance/db.py` 末尾（`_upsert_policy_fields` 函数之后），需确保文件顶部有 `import pymysql.cursors` 且 `_COLUMN_TO_OCR` 字典存在

### 3. policy_parser_unused.py
- **来源**: `insurance/policy_parser.py` 第 2024-2035 行
- **内容**: `get_all_field_names()` 函数
- **废弃原因**: 调试用工具函数，全项目无调用
- **恢复方式**: 将函数复制回 `insurance/policy_parser.py` 末尾（`FIELD_ORDER` 列表之后），需确保 `from typing import List, Dict, Any` 已导入

### 4. 已完成的实施计划文档（6321 行）
| 文件 | 行数 | 内容 |
|------|------|------|
| `2026-04-26-insurance-engine.md` | 3700 | 保单识别引擎重构计划 |
| `2026-04-26-insurance-ocr-integration.md` | 1802 | OCR 集成实施计划 |
| `2026-04-26-monitor-config.md` | 819 | 监控配置重构计划 |

- **原路径**: `docs/superpowers/plans/`
- **废弃原因**: 功能已全部完成并上线，计划文档仅有历史参考价值
- **恢复方式**: 移回 `docs/superpowers/plans/` 目录
