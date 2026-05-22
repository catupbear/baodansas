# 用户字段配置功能设计（系统设置）

> 日期：2026-05-07
> 状态：已确认

## 一、需求概述

在保单识别页面右上角新增 **⚙ 系统设置** 入口，打开独立配置页面 `/insurance/settings`。支持多模板管理，所有角色均可创建/编辑模板，企业管理员可设置模板对员工可见。包含三个功能模块：

1. **简称映射** — 承保公司全称↔简称、险种全称↔简称（双 textarea 批量编辑）
2. **日期格式** — 按时间字段独立配置日期显示格式
3. **自动计算公式** — 按具体公司+具体险种配置费率/佣金等字段的计算公式

## 二、模板系统

### 2.1 模板概念

- 每个用户可创建多个配置模板，同时只有一个「启用中」的模板
- 「保存并应用」= 保存当前模板内容 + 设为启用模板
- 企业管理员可设置模板的「员工可见」属性，员工可在模板下拉中看到并选用企业模板（只读）

### 2.2 权限

| 角色 | 能力 |
|------|------|
| 所有角色 | 创建、编辑、删除自己的模板，选择启用模板 |
| enterprise | 额外可设置模板「员工可见」，员工可选用（只读） |
| employee | 下拉中可看到企业可见模板，选用后只读 |

### 2.3 顶部操作栏

```
┌───────────────────────────────────────────────────────────┐
│  ← 返回保单台账   ⚙ 系统设置                               │
│                    [默认模板 ▾]  [管理模板]  [保存并应用]     │
└───────────────────────────────────────────────────────────┘
```

- 模板下拉：我的模板 + 企业模板（员工可见），底部有「+ 新建模板」
- 管理模板按钮：打开弹窗，可重命名、设置员工可见、删除
- 保存并应用：保存当前内容 + 设为启用模板

## 三、数据库设计

### 3.1 新增表：user_field_config

```sql
CREATE TABLE user_field_config (
  id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键',
  scope ENUM('global','enterprise','user') NOT NULL COMMENT '配置作用域：global=全局默认, enterprise=企业级, user=员工个人',
  scope_id INT DEFAULT NULL COMMENT '作用域ID：global时为NULL, enterprise时为企业ID, user时为用户ID',
  template_name VARCHAR(64) NOT NULL DEFAULT '默认模板' COMMENT '模板名称',
  config_type VARCHAR(32) NOT NULL COMMENT '配置类型：company_alias=承保公司简称, policy_type_alias=险种简称, date_format=日期格式, fee_formula=费率公式',
  config_key VARCHAR(128) NOT NULL COMMENT '配置项标识：公司全称/险种全称/字段名/公司:险种等',
  config_value TEXT NOT NULL COMMENT '配置值(JSON)：简称字符串/日期格式/公式定义等',
  visible_to_employees TINYINT NOT NULL DEFAULT 0 COMMENT '是否对员工可见（仅enterprise scope有效）',
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
  UNIQUE KEY uk_scope_tpl (scope, scope_id, template_name, config_type, config_key) COMMENT '同一作用域同模板下同类型同标识唯一'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户字段配置表：支持多模板 + 三级作用域';
```

### 3.2 新增表：user_active_template

```sql
CREATE TABLE user_active_template (
  id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键',
  user_id INT NOT NULL COMMENT '用户ID',
  active_source ENUM('own','enterprise') NOT NULL DEFAULT 'own' COMMENT '启用来源：own=自己的模板, enterprise=企业模板',
  template_name VARCHAR(64) NOT NULL DEFAULT '默认模板' COMMENT '启用的模板名称',
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
  UNIQUE KEY uk_user (user_id) COMMENT '每个用户只有一个启用模板'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户当前启用的配置模板';
```

### 3.3 config_type 与 config_key 规范

| config_type | config_key 格式 | config_value 示例 |
|---|---|---|
| `company_alias` | 公司全称 | `"人保PICC"` |
| `policy_type_alias` | 险种全称 | `"商业险"` |
| `date_format` | 时间字段名（签单日期/起保日期/终保日期） | `"YYYY-MM-DD"` |
| `fee_formula` | `{字段名}:{公司简称}:{险种简称}` | `{"tokens":["保费","*","0.18"], "display":"保费 × 0.18"}` |

## 四、页面设计

### 4.1 三个 Tab

见原型文件 `web/templates/prototype_field_config.html`

### 4.2 模板管理弹窗

- 列表显示所有「我的模板」
- 每行：可编辑模板名 + 员工可见复选框（仅企业管理员）+ 删除按钮
- 至少保留一个模板

## 五、API 设计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/insurance/field-config/templates` | 获取模板列表（我的 + 可见的企业模板） |
| POST | `/api/insurance/field-config/templates` | 创建新模板 |
| PUT | `/api/insurance/field-config/templates/<name>` | 更新模板（重命名、可见性） |
| DELETE | `/api/insurance/field-config/templates/<name>` | 删除模板 |
| GET | `/api/insurance/field-config?template=<name>` | 获取指定模板的全部配置 |
| PUT | `/api/insurance/field-config` | 保存模板配置内容 |
| POST | `/api/insurance/field-config/apply` | 保存并应用（设为启用模板） |
| GET | `/api/insurance/field-config/active` | 获取当前启用模板信息 |
| POST | `/api/insurance/field-config/formula/validate` | 验证公式合法性 + 示例计算 |
| GET | `/api/insurance/field-config/defaults` | 获取系统默认值（公司/险种预填充） |

## 六、与现有系统的集成

### 6.1 导出时应用配置

在导出 Excel / 同步钉钉时：
1. 读取当前用户的启用模板配置
2. 承保公司、险种字段替换为简称
3. 日期字段按配置格式化
4. 计算字段按公式引擎求值填充

### 6.2 与现有 company_aliases 的关系

- **现有 company_aliases**：OCR 识别时将不同写法归一化为标准全称，属于识别层
- **新增简称映射**：将标准全称映射为用户自定义的显示简称，属于展示层

### 6.3 预填充默认值

- 承保公司简称：从 `policy_parser.py` 的 `COMPANY_SHORT_MAP` 提取
- 险种简称：从 `policy_parser.py` 的险种分类提取
- 日期格式：默认全部为 `YYYY-MM-DD`
