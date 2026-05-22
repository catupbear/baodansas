# 企业级列配置方案

> 日期：2026-05-20
> 状态：已确认

## 需求

Excel 导出列、页面列表列的显示/顺序/名称支持按企业配置，配置后企业内所有人生效。

## 设计

### 数据结构

复用现有 `user_field_config` 表，新增两个 `config_type`：

| config_type | 用途 |
|---|---|
| `list_columns` | 页面列表列配置 |
| `export_columns` | Excel 导出列配置 |

每个 config_type 存一条记录，`config_key` = `"columns"`，`config_value` 存 JSON 数组：

```json
[
  {"key": "承保公司", "visible": true, "order": 0, "display_name": "保司"},
  {"key": "保单号",   "visible": true, "order": 1, "display_name": "保单号"},
  {"key": "险种",     "visible": true, "order": 2, "display_name": "险种"},
  {"key": "佣金率",   "visible": false, "order": 10, "display_name": "佣金率"}
]
```

字段说明：
- `key` — 固定字段标识（对应现有 OUTPUT_COLUMNS 的 19 个字段）
- `visible` — 是否显示/导出
- `order` — 排序序号（升序排列）
- `display_name` — 显示名称（页面表头 / Excel 列头）

### 配置生效优先级

沿用现有三层体系，无需改表结构：

```
用户个人配置 > 企业配置 > 全局默认配置
```

- 全局默认 = 现有固定的 19 列顺序和名称（代码兜底，不需要额外配置记录）
- 企业管理员配置后，企业内所有人生效
- 个人可覆盖（当前阶段可先不做个人级别）

### 前端设置 UI

在现有设置页面的模板配置区域新增两个 tab：

- **「页面列配置」** — 配置 list_columns
- **「导出列配置」** — 配置 export_columns

每个 tab 内容：
- 可拖拽排序的列表（每行一个字段）
- 每行：开关（visible）+ 显示名输入框（display_name）
- 底部「恢复默认」按钮

### 影响范围

| 模块 | 改动 |
|---|---|
| `insurance/field_config_db.py` | 新增 `get_column_config()` / `save_column_config()` 方法 |
| `insurance/api.py` | 导出接口读取 export_columns 配置，替代硬编码 OUTPUT_COLUMNS |
| `insurance/api.py` | 列表接口返回 list_columns 配置给前端 |
| `web/templates/insurance.html` | 列表渲染改为动态列；设置区新增两个 tab |
| `insurance/field_mapping.py` | `OUTPUT_COLUMNS` 保留作为默认值兜底 |

### API 设计

#### 获取列配置

```
GET /api/insurance/column-config?config_type=list_columns
```

响应：
```json
{
  "success": true,
  "data": {
    "source": "enterprise",
    "columns": [
      {"key": "承保公司", "visible": true, "order": 0, "display_name": "保司"},
      ...
    ]
  }
}
```

`source` 标识当前生效的配置来源（global / enterprise / user）。

#### 保存列配置

```
POST /api/insurance/column-config
```

请求体：
```json
{
  "config_type": "list_columns",
  "columns": [
    {"key": "承保公司", "visible": true, "order": 0, "display_name": "保司"},
    ...
  ]
}
```

#### 重置为默认

```
DELETE /api/insurance/column-config?config_type=list_columns
```

删除当前 scope 的配置，回退到上一级。
