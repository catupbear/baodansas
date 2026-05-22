# 系统设置（字段配置）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保单识别页面新增系统设置功能，支持多模板管理、简称映射、日期格式、自动计算公式配置。

**Architecture:** 新增两张数据库表（user_field_config + user_active_template），新增 Python 模块 `insurance/field_config_db.py` 处理数据库操作，在 `insurance/api.py` 中添加 API 端点，新增独立前端页面 `web/templates/field_config.html`（基于已确认的原型页面改造），在保单页面导航栏添加入口按钮，导出时集成配置应用逻辑。

**Tech Stack:** Python/Flask, MySQL, Vue 3, Tailwind CSS

**设计文档:** `docs/superpowers/plans/2026-05-07-user-field-config-design.md`
**原型页面:** `web/templates/prototype_field_config.html`

---

### Task 1: 数据库表创建

**Files:**
- Modify: `insurance/db.py` — 在 `init_insurance_tables()` 末尾追加建表语句

- [ ] **Step 1: 在 init_insurance_tables() 中追加两张表的 CREATE TABLE**

在 `insurance/db.py` 的 `init_insurance_tables()` 函数末尾（conn.commit() 之前），追加：

```python
# ---- 用户字段配置表 ----
cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_field_config (
        id              INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键',
        scope           ENUM('global','enterprise','user') NOT NULL COMMENT '配置作用域：global=全局默认, enterprise=企业级, user=员工个人',
        scope_id        INT DEFAULT NULL COMMENT '作用域ID：global时为NULL, enterprise时为企业ID, user时为用户ID',
        template_name   VARCHAR(64) NOT NULL DEFAULT '默认模板' COMMENT '模板名称',
        config_type     VARCHAR(32) NOT NULL COMMENT '配置类型：company_alias/policy_type_alias/date_format/fee_formula',
        config_key      VARCHAR(128) NOT NULL COMMENT '配置项标识',
        config_value    TEXT NOT NULL COMMENT '配置值(JSON)',
        visible_to_employees TINYINT NOT NULL DEFAULT 0 COMMENT '是否对员工可见（仅enterprise scope有效）',
        updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
        UNIQUE KEY uk_scope_tpl (scope, scope_id, template_name, config_type, config_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户字段配置表'
""")

# ---- 用户当前启用模板表 ----
cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_active_template (
        id              INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键',
        user_id         INT NOT NULL COMMENT '用户ID',
        active_source   ENUM('own','enterprise') NOT NULL DEFAULT 'own' COMMENT '启用来源',
        template_name   VARCHAR(64) NOT NULL DEFAULT '默认模板' COMMENT '启用的模板名称',
        updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
        UNIQUE KEY uk_user (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户当前启用的配置模板'
""")
```

- [ ] **Step 2: 验证**

启动服务 `python main.py`，检查日志确认表创建成功，用 MySQL 客户端验证两张表结构。

- [ ] **Step 3: Commit**

```
git add insurance/db.py
git commit -m "feat(field-config): 新增 user_field_config 和 user_active_template 数据库表"
```

---

### Task 2: 数据库操作层

**Files:**
- Create: `insurance/field_config_db.py`

- [ ] **Step 1: 创建 field_config_db.py，实现模板 CRUD**

```python
"""
用户字段配置 —— 数据库操作层
"""
import json
import logging
import pymysql

logger = logging.getLogger(__name__)


# =========== 模板管理 ===========

def list_templates(db, user_id: int, role: str, parent_id: int = None) -> dict:
    """
    获取模板列表，返回 {"my": [...], "enterprise": [...]}
    my: 用户自己的模板
    enterprise: 用户所属企业中 visible_to_employees=1 的模板（仅 employee 角色）
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 我的模板：按 scope+scope_id 查 DISTINCT template_name
        if role == "super_admin":
            scope, scope_id = "global", None
        elif role == "enterprise":
            scope, scope_id = "enterprise", parent_id or user_id
        else:
            scope, scope_id = "user", user_id

        if scope_id is None:
            cursor.execute(
                "SELECT DISTINCT template_name, visible_to_employees FROM user_field_config WHERE scope = %s AND scope_id IS NULL ORDER BY template_name",
                (scope,)
            )
        else:
            cursor.execute(
                "SELECT DISTINCT template_name, visible_to_employees FROM user_field_config WHERE scope = %s AND scope_id = %s ORDER BY template_name",
                (scope, scope_id)
            )
        my_rows = cursor.fetchall()
        my_templates = [{"name": r["template_name"], "visible": bool(r["visible_to_employees"])} for r in my_rows]

        # 如果没有任何模板，返回一个默认的
        if not my_templates:
            my_templates = [{"name": "默认模板", "visible": False}]

        # 企业模板（仅 employee 可见）
        enterprise_templates = []
        if role == "employee" and parent_id:
            cursor.execute(
                "SELECT DISTINCT template_name FROM user_field_config WHERE scope = 'enterprise' AND scope_id = %s AND visible_to_employees = 1 ORDER BY template_name",
                (parent_id,)
            )
            enterprise_templates = [{"name": r["template_name"]} for r in cursor.fetchall()]

        return {"my": my_templates, "enterprise": enterprise_templates}
    finally:
        conn.close()


def rename_template(db, scope: str, scope_id: int, old_name: str, new_name: str):
    """重命名模板"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        if scope_id is None:
            cursor.execute(
                "UPDATE user_field_config SET template_name = %s WHERE scope = %s AND scope_id IS NULL AND template_name = %s",
                (new_name, scope, old_name)
            )
        else:
            cursor.execute(
                "UPDATE user_field_config SET template_name = %s WHERE scope = %s AND scope_id = %s AND template_name = %s",
                (new_name, scope, scope_id, old_name)
            )
        # 同步更新 user_active_template
        cursor.execute(
            "UPDATE user_active_template SET template_name = %s WHERE template_name = %s",
            (new_name, old_name)
        )
        conn.commit()
    finally:
        conn.close()


def update_template_visibility(db, scope: str, scope_id: int, template_name: str, visible: bool):
    """设置模板员工可见性"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        if scope_id is None:
            cursor.execute(
                "UPDATE user_field_config SET visible_to_employees = %s WHERE scope = %s AND scope_id IS NULL AND template_name = %s",
                (1 if visible else 0, scope, template_name)
            )
        else:
            cursor.execute(
                "UPDATE user_field_config SET visible_to_employees = %s WHERE scope = %s AND scope_id = %s AND template_name = %s",
                (1 if visible else 0, scope, scope_id, template_name)
            )
        conn.commit()
    finally:
        conn.close()


def delete_template(db, scope: str, scope_id: int, template_name: str):
    """删除模板及其所有配置"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        if scope_id is None:
            cursor.execute(
                "DELETE FROM user_field_config WHERE scope = %s AND scope_id IS NULL AND template_name = %s",
                (scope, template_name)
            )
        else:
            cursor.execute(
                "DELETE FROM user_field_config WHERE scope = %s AND scope_id = %s AND template_name = %s",
                (scope, scope_id, template_name)
            )
        conn.commit()
    finally:
        conn.close()


# =========== 配置读写 ===========

def get_template_config(db, scope: str, scope_id: int, template_name: str) -> dict:
    """
    获取指定模板的全部配置，返回按 config_type 分组：
    {
        "company_alias": [{"key": "全称", "value": "简称"}, ...],
        "policy_type_alias": [...],
        "date_format": [...],
        "fee_formula": [...]
    }
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if scope_id is None:
            cursor.execute(
                "SELECT config_type, config_key, config_value FROM user_field_config WHERE scope = %s AND scope_id IS NULL AND template_name = %s ORDER BY id",
                (scope, template_name)
            )
        else:
            cursor.execute(
                "SELECT config_type, config_key, config_value FROM user_field_config WHERE scope = %s AND scope_id = %s AND template_name = %s ORDER BY id",
                (scope, scope_id, template_name)
            )
        rows = cursor.fetchall()

        result = {"company_alias": [], "policy_type_alias": [], "date_format": [], "fee_formula": []}
        for row in rows:
            ct = row["config_type"]
            val = row["config_value"]
            try:
                val = json.loads(val)
            except (TypeError, json.JSONDecodeError):
                pass
            if ct in result:
                result[ct].append({"key": row["config_key"], "value": val})
        return result
    finally:
        conn.close()


def save_template_config(db, scope: str, scope_id: int, template_name: str, config_type: str, items: list, visible: bool = False):
    """
    保存模板的某个 config_type 的全部配置（先删后插，整体替换）。
    items: [{"key": "xxx", "value": "yyy"}, ...]
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        # 删除该模板该类型的旧配置
        if scope_id is None:
            cursor.execute(
                "DELETE FROM user_field_config WHERE scope = %s AND scope_id IS NULL AND template_name = %s AND config_type = %s",
                (scope, template_name, config_type)
            )
        else:
            cursor.execute(
                "DELETE FROM user_field_config WHERE scope = %s AND scope_id = %s AND template_name = %s AND config_type = %s",
                (scope, scope_id, template_name, config_type)
            )
        # 批量插入新配置
        for item in items:
            val = item["value"]
            if not isinstance(val, str):
                val = json.dumps(val, ensure_ascii=False)
            cursor.execute(
                "INSERT INTO user_field_config (scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (scope, scope_id, template_name, config_type, item["key"], val, 1 if visible else 0)
            )
        conn.commit()
    finally:
        conn.close()


def save_full_template(db, scope: str, scope_id: int, template_name: str, config: dict, visible: bool = False):
    """
    保存模板的全部配置。
    config: {"company_alias": [...], "policy_type_alias": [...], "date_format": [...], "fee_formula": [...]}
    """
    for config_type, items in config.items():
        if items is not None:
            save_template_config(db, scope, scope_id, template_name, config_type, items, visible)


# =========== 启用模板 ===========

def get_active_template(db, user_id: int) -> dict:
    """获取用户当前启用的模板，默认返回 {"source": "own", "template_name": "默认模板"}"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT active_source, template_name FROM user_active_template WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        if row:
            return {"source": row["active_source"], "template_name": row["template_name"]}
        return {"source": "own", "template_name": "默认模板"}
    finally:
        conn.close()


def set_active_template(db, user_id: int, source: str, template_name: str):
    """设置用户当前启用的模板"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "REPLACE INTO user_active_template (user_id, active_source, template_name) VALUES (%s, %s, %s)",
            (user_id, source, template_name)
        )
        conn.commit()
    finally:
        conn.close()


# =========== 导出时获取有效配置 ===========

def get_effective_config(db, user_id: int, role: str, parent_id: int = None) -> dict:
    """
    获取用户当前启用模板的有效配置（用于导出/展示）。
    返回与 get_template_config 相同的结构。
    """
    active = get_active_template(db, user_id)

    if active["source"] == "enterprise" and parent_id:
        # 使用企业模板
        return get_template_config(db, "enterprise", parent_id, active["template_name"])
    else:
        # 使用自己的模板
        if role == "super_admin":
            return get_template_config(db, "global", None, active["template_name"])
        elif role == "enterprise":
            return get_template_config(db, "enterprise", parent_id or user_id, active["template_name"])
        else:
            return get_template_config(db, "user", user_id, active["template_name"])
```

- [ ] **Step 2: Commit**

```
git add insurance/field_config_db.py
git commit -m "feat(field-config): 新增字段配置数据库操作层 field_config_db.py"
```

---

### Task 3: API 端点

**Files:**
- Modify: `insurance/api.py` — 添加字段配置相关 API 路由

- [ ] **Step 1: 在 insurance/api.py 顶部添加导入**

在已有 import 区域（约第 20-30 行）追加：

```python
from insurance.field_config_db import (
    list_templates, rename_template, update_template_visibility, delete_template,
    get_template_config, save_full_template,
    get_active_template, set_active_template,
    get_effective_config,
)
```

- [ ] **Step 2: 添加辅助函数 _get_user_scope()**

在 `_get_user_ids_filter()` 函数附近添加：

```python
def _get_user_scope():
    """根据当前用户角色返回 (scope, scope_id)"""
    user = g.current_user
    role = user["role"]
    if role == "super_admin":
        return "global", None
    elif role == "enterprise":
        return "enterprise", user.get("parent_id") or user["user_id"]
    else:
        return "user", user["user_id"]
```

- [ ] **Step 3: 添加模板列表 API**

```python
@insurance_bp.route("/api/insurance/field-config/templates", methods=["GET"])
def get_field_config_templates():
    """获取模板列表"""
    try:
        user = g.current_user
        data = list_templates(_db, user["user_id"], user["role"], user.get("parent_id"))
        active = get_active_template(_db, user["user_id"])
        return jsonify({"code": 0, "data": {**data, "active": active}})
    except Exception as e:
        logger.exception("获取模板列表失败")
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 4: 添加模板管理 API（重命名、可见性、删除）**

```python
@insurance_bp.route("/api/insurance/field-config/templates/manage", methods=["PUT"])
def manage_field_config_templates():
    """批量管理模板（重命名、可见性）"""
    try:
        body = request.get_json(force=True) or {}
        actions = body.get("actions", [])
        scope, scope_id = _get_user_scope()

        for action in actions:
            act_type = action.get("type")
            if act_type == "rename":
                rename_template(_db, scope, scope_id, action["old_name"], action["new_name"])
            elif act_type == "visibility":
                update_template_visibility(_db, scope, scope_id, action["name"], action["visible"])
            elif act_type == "delete":
                delete_template(_db, scope, scope_id, action["name"])

        return jsonify({"code": 0, "msg": "模板已更新"})
    except Exception as e:
        logger.exception("管理模板失败")
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 5: 添加配置读取 API**

```python
@insurance_bp.route("/api/insurance/field-config", methods=["GET"])
def get_field_config():
    """获取指定模板的全部配置"""
    try:
        template_name = request.args.get("template", "默认模板")
        source = request.args.get("source", "own")
        user = g.current_user

        if source == "enterprise" and user.get("parent_id"):
            config = get_template_config(_db, "enterprise", user["parent_id"], template_name)
        else:
            scope, scope_id = _get_user_scope()
            config = get_template_config(_db, scope, scope_id, template_name)

        return jsonify({"code": 0, "data": config})
    except Exception as e:
        logger.exception("获取字段配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 6: 添加保存并应用 API**

```python
@insurance_bp.route("/api/insurance/field-config/save-and-apply", methods=["POST"])
def save_and_apply_field_config():
    """保存模板配置并设为启用"""
    try:
        body = request.get_json(force=True) or {}
        template_name = body.get("template_name", "默认模板")
        config = body.get("config", {})
        visible = body.get("visible", False)

        scope, scope_id = _get_user_scope()
        user = g.current_user

        save_full_template(_db, scope, scope_id, template_name, config, visible)
        set_active_template(_db, user["user_id"], "own", template_name)

        return jsonify({"code": 0, "msg": "已保存并应用"})
    except Exception as e:
        logger.exception("保存配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/field-config/apply", methods=["POST"])
def apply_field_config_template():
    """切换启用模板（不修改内容）"""
    try:
        body = request.get_json(force=True) or {}
        template_name = body.get("template_name", "默认模板")
        source = body.get("source", "own")  # own 或 enterprise

        set_active_template(_db, g.current_user["user_id"], source, template_name)
        return jsonify({"code": 0, "msg": "已切换模板"})
    except Exception as e:
        logger.exception("切换模板失败")
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 7: 添加公式验证 API**

```python
@insurance_bp.route("/api/insurance/field-config/formula/validate", methods=["POST"])
def validate_formula():
    """验证公式合法性并返回示例计算结果"""
    try:
        body = request.get_json(force=True) or {}
        formula = body.get("formula", "")
        if not formula:
            return jsonify({"code": 400, "msg": "公式不能为空"}), 400

        # 示例数据
        sample = {"保费": 5000, "车船税": 300, "佣金率": 0.15, "佣金": 750,
                  "采购费率": 0.05, "公司利润": 200, "跟单利润": 100}

        expr = formula.replace("×", "*").replace("÷", "/")
        for field_name, val in sample.items():
            expr = expr.replace(field_name, str(val))

        # 安全计算：只允许数字和运算符
        import re
        if not re.match(r'^[\d\s\+\-\*/\.\(\)]+$', expr.strip()):
            return jsonify({"code": 400, "msg": "公式包含非法字符"}), 400

        result = eval(expr)  # 已通过正则过滤，安全
        return jsonify({"code": 0, "data": {"result": round(result, 2), "expression": expr}})
    except ZeroDivisionError:
        return jsonify({"code": 400, "msg": "公式存在除零错误"}), 400
    except Exception as e:
        return jsonify({"code": 400, "msg": f"公式错误：{str(e)}"}), 400
```

- [ ] **Step 8: 添加默认值 API（预填充用）**

```python
@insurance_bp.route("/api/insurance/field-config/defaults", methods=["GET"])
def get_field_config_defaults():
    """获取系统默认的公司/险种映射（用于预填充）"""
    try:
        from insurance.policy_parser import COMPANY_SHORT_MAP

        # 公司映射：将关键词映射转为全称→简称（需从 COMPANY_BASES 取全称）
        company_defaults = []
        seen_shorts = set()
        for keyword, short in COMPANY_SHORT_MAP.items():
            if short not in seen_shorts:
                company_defaults.append({"key": keyword, "value": short})
                seen_shorts.add(short)

        # 险种映射
        policy_type_defaults = [
            {"key": "机动车交通事故责任强制保险", "value": "交强险"},
            {"key": "机动车商业保险", "value": "商业险"},
            {"key": "驾乘人员意外伤害保险", "value": "驾乘险"},
            {"key": "新能源汽车商业保险", "value": "新能源商业险"},
        ]

        return jsonify({"code": 0, "data": {
            "company_alias": company_defaults,
            "policy_type_alias": policy_type_defaults,
        }})
    except Exception as e:
        logger.exception("获取默认值失败")
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 9: 添加页面路由**

```python
@insurance_bp.route("/insurance/settings")
def field_config_page():
    """系统设置页面"""
    return render_template("field_config.html")
```

确保文件顶部已有 `from flask import render_template`（检查现有 import）。

- [ ] **Step 10: Commit**

```
git add insurance/api.py
git commit -m "feat(field-config): 新增系统设置 API 端点（模板管理+配置读写+公式验证）"
```

---

### Task 4: 前端页面

**Files:**
- Create: `web/templates/field_config.html` — 基于原型页面改造，接入真实 API

- [ ] **Step 1: 复制原型并改造为正式页面**

将 `web/templates/prototype_field_config.html` 复制为 `web/templates/field_config.html`，然后进行以下改造：

1. 添加 Jinja2 模板标签 `{% raw %}` / `{% endraw %}` 包裹（避免 Vue 的 `{{ }}` 与 Jinja 冲突）
2. 将所有模拟数据（hardcoded 的公司列表、模板列表等）替换为 API 调用
3. 添加 token 认证逻辑（从 localStorage 读取 token，添加到 API 请求头）

主要需替换的部分：

**a. 添加 API 请求工具函数：**

```javascript
// API 基础方法
const token = localStorage.getItem('token');
const apiHeaders = { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token };

async function api(url, options = {}) {
    const res = await fetch(url, { headers: apiHeaders, ...options });
    const data = await res.json();
    if (data.code === 401) { window.location.href = '/login'; return null; }
    return data;
}
```

**b. 替换模板列表为 API 加载：**

```javascript
// 加载模板列表
async function loadTemplates() {
    const res = await api('/api/insurance/field-config/templates');
    if (res && res.code === 0) {
        myTemplates.value = res.data.my;
        enterpriseTemplates.value = res.data.enterprise;
        activeTemplateName.value = res.data.active.template_name;
        activeSource.value = res.data.active.source;
    }
}
```

**c. 替换配置加载为 API：**

```javascript
// 加载当前模板配置
async function loadConfig() {
    const source = activeSource.value;
    const res = await api(`/api/insurance/field-config?template=${encodeURIComponent(activeTemplateName.value)}&source=${source}`);
    if (res && res.code === 0) {
        const data = res.data;
        // 填充简称映射
        const companies = data.company_alias || [];
        companyFullNamesData.value = companies.map(c => c.key).join('\n');
        companyShortNamesData.value = companies.map(c => c.value).join('\n');
        // ... 同理填充险种、日期格式、公式规则
    }
}
```

**d. 替换保存逻辑为 API：**

```javascript
async function saveConfig() {
    const config = buildConfigFromForm(); // 从表单构建配置对象
    const res = await api('/api/insurance/field-config/save-and-apply', {
        method: 'POST',
        body: JSON.stringify({
            template_name: activeTemplateName.value,
            config: config,
            visible: currentTemplateVisible.value,
        })
    });
    if (res && res.code === 0) {
        // 提示成功
    }
}
```

**e. 页面初始化时调用：**

```javascript
onMounted(async () => {
    await loadTemplates();
    await loadConfig();
    await loadDefaults(); // 加载系统默认值用于新模板预填充
});
```

- [ ] **Step 2: 验证页面可正常访问和交互**

启动服务，访问 `/insurance/settings`，验证：
- 模板下拉加载正常
- 三个 Tab 切换正常
- 保存并应用功能正常
- 管理模板弹窗正常

- [ ] **Step 3: Commit**

```
git add web/templates/field_config.html
git commit -m "feat(field-config): 新增系统设置前端页面（模板管理+简称映射+日期格式+自动计算公式）"
```

---

### Task 5: 保单页面添加入口按钮

**Files:**
- Modify: `web/templates/insurance.html` — 在导航栏添加 ⚙ 系统设置 按钮

- [ ] **Step 1: 在 nav-header 的右侧按钮组中添加入口**

在 `web/templates/insurance.html` 的导航栏右侧按钮区域（约第 287 行，「设置」按钮附近），添加系统设置入口按钮。所有角色可见（不限 super_admin）：

```html
<!-- 系统设置 -->
<a href="/insurance/settings"
   class="btn btn-outline" style="background:rgba(255,255,255,0.15);color:#fff;border-color:rgba(255,255,255,0.25);padding:6px 12px;font-size:12px;text-decoration:none;"
   title="系统设置">
    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"/>
    </svg>
    系统设置
</a>
```

插入位置：在「字段映射」按钮之后、「设置」按钮之前。

- [ ] **Step 2: Commit**

```
git add web/templates/insurance.html
git commit -m "feat(field-config): 保单页面导航栏添加系统设置入口按钮"
```

---

### Task 6: 导出集成 — 应用配置到 Excel 导出

**Files:**
- Modify: `insurance/api.py` — 修改 export_excel 端点，导出时应用用户配置

- [ ] **Step 1: 创建公式计算引擎函数**

在 `insurance/field_config_db.py` 末尾追加：

```python
import re

def evaluate_formula(formula: str, fields: dict) -> str:
    """
    计算公式，返回结果字符串。
    formula: 如 "保费 × 0.18" 或 "(保费 + 车船税) × 0.10"
    fields: 当前记录的字段值 {"保费": "5000", "车船税": "300", ...}
    失败时返回空字符串。
    """
    if not formula:
        return ""
    try:
        expr = formula.replace("×", "*").replace("÷", "/")
        for field_name, val in fields.items():
            if field_name in expr:
                # 尝试转数字
                try:
                    num = float(str(val).replace(",", ""))
                except (ValueError, TypeError):
                    num = 0
                expr = expr.replace(field_name, str(num))

        # 安全校验
        if not re.match(r'^[\d\s\+\-\*/\.\(\)]+$', expr.strip()):
            return ""
        result = eval(expr)
        if isinstance(result, float):
            return str(round(result, 2))
        return str(result)
    except Exception:
        return ""


def apply_user_config_to_fields(config: dict, fields: dict) -> dict:
    """
    将用户配置应用到一条记录的字段上：
    1. 承保公司/险种替换为简称
    2. 日期字段格式化
    3. 计算字段求值
    返回修改后的 fields（副本）。
    """
    result = dict(fields)

    # 1. 公司简称
    company_map = {item["key"]: item["value"] for item in config.get("company_alias", [])}
    if result.get("承保公司") in company_map:
        result["承保公司"] = company_map[result["承保公司"]]

    # 2. 险种简称
    policy_map = {item["key"]: item["value"] for item in config.get("policy_type_alias", [])}
    if result.get("险种") in policy_map:
        result["险种"] = policy_map[result["险种"]]

    # 3. 日期格式化
    date_formats = {item["key"]: item["value"] for item in config.get("date_format", [])}
    for date_field, fmt in date_formats.items():
        raw = result.get(date_field, "")
        if raw:
            result[date_field] = _format_date(raw, fmt)

    # 4. 公式计算
    company_short = result.get("承保公司", "")
    policy_short = result.get("险种", "")
    formula_map = {}
    for item in config.get("fee_formula", []):
        formula_map[item["key"]] = item["value"]

    for calc_field in ["佣金率", "佣金", "采购费率", "公司利润", "跟单利润"]:
        formula_key = f"{calc_field}:{company_short}:{policy_short}"
        formula_data = formula_map.get(formula_key)
        if formula_data:
            display = formula_data.get("display", "") if isinstance(formula_data, dict) else str(formula_data)
            calc_result = evaluate_formula(display, result)
            if calc_result:
                result[calc_field] = calc_result

    return result


def _format_date(raw: str, fmt: str) -> str:
    """将日期字符串按格式转换"""
    import re as _re
    # 尝试解析常见日期格式
    m = _re.match(r'(\d{4})[年/\-](\d{1,2})[月/\-](\d{1,2})', raw)
    if not m:
        return raw
    y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
    fmt_map = {
        "YYYY-MM-DD": f"{y}-{mo}-{d}",
        "YYYY/MM/DD": f"{y}/{mo}/{d}",
        "YYYY年MM月DD日": f"{y}年{mo}月{d}日",
        "MM-DD-YYYY": f"{mo}-{d}-{y}",
        "DD/MM/YYYY": f"{d}/{mo}/{y}",
        "YYYYMMDD": f"{y}{mo}{d}",
    }
    return fmt_map.get(fmt, raw)
```

- [ ] **Step 2: 修改 export_excel 端点**

在 `insurance/api.py` 的 `export_excel` 函数中（约第 2109 行），在写入 Excel 行之前，应用用户配置：

```python
# 在 for inv in invoices 循环之前，获取用户配置
from insurance.field_config_db import get_effective_config, apply_user_config_to_fields
user = g.current_user
user_config = get_effective_config(_db, user["user_id"], user["role"], user.get("parent_id"))

# 在 for inv in invoices 循环内，fields 获取之后：
fields = inv.get("fields", {}) if isinstance(inv, dict) else {}
if user_config:
    fields = apply_user_config_to_fields(user_config, fields)
```

注意：需确保只在有配置时才应用，无配置时保持原行为不变。

- [ ] **Step 3: 验证导出功能**

1. 在系统设置中配置一条公司简称映射
2. 导出 Excel 验证简称是否生效
3. 配置一条公式规则，验证计算字段是否填充

- [ ] **Step 4: Commit**

```
git add insurance/field_config_db.py insurance/api.py
git commit -m "feat(field-config): 导出Excel时应用用户配置（简称/日期格式/公式计算）"
```

---

### Task 7: 更新日志与清理

**Files:**
- Create: `docs/changelog/2026-05/` 下的更新记录
- Delete: `web/templates/prototype_field_config.html`（原型文件，正式页面已创建）

- [ ] **Step 1: 创建更新记录**

创建 `docs/changelog/2026-05/2026-05-07-系统设置功能.md`：

```markdown
# 2026-05-07 系统设置（字段配置）功能

## 需求说明

新增系统设置功能，支持用户自定义保单字段的显示和计算规则：
- 多模板管理：创建/编辑/删除/切换配置模板
- 简称映射：承保公司和险种的全称↔简称映射
- 日期格式：按字段独立配置日期显示格式
- 自动计算公式：按公司+险种配置佣金率等字段的计算公式
- 企业管理员可设置模板对员工可见

## 数据库变更

新增两张表：
- `user_field_config` — 用户字段配置表（支持多模板 + 三级作用域）
- `user_active_template` — 用户当前启用的配置模板

表在服务启动时自动创建（init_insurance_tables），无需手动迁移。

## 代码变更

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `insurance/db.py` | 修改 | init_insurance_tables() 追加两张表 |
| `insurance/field_config_db.py` | 新增 | 字段配置数据库操作层 |
| `insurance/api.py` | 修改 | 新增系统设置 API 端点 + 导出集成 |
| `web/templates/field_config.html` | 新增 | 系统设置前端页面 |
| `web/templates/insurance.html` | 修改 | 导航栏添加系统设置入口 |

## 部署步骤

1. 更新代码：`git pull`
2. 重启服务：`python main.py`（表自动创建，无需手动 SQL）
3. 验证：访问 `/insurance/settings` 确认页面正常
```

- [ ] **Step 2: 删除原型文件**

```
git rm web/templates/prototype_field_config.html
```

- [ ] **Step 3: Final Commit**

```
git add docs/
git commit -m "docs: 添加系统设置功能更新记录，清理原型文件"
```
