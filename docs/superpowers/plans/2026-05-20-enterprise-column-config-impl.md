# 企业级列配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Excel 导出列和页面列表列的可见性、顺序、显示名称支持按企业配置，复用现有 `user_field_config` 表。

**Architecture:** 在 `field_config_db.py` 新增列配置的读写函数，在 `api.py` 新增 3 个 API（获取/保存/重置），修改 `list_records` 和 `export_excel` 读取列配置，前端 `field_config.html` 新增两个 tab，`insurance.html` 的列表渲染和导出改为动态列。

**Tech Stack:** Python/Flask, MySQL (user_field_config 表), Vue 3, Tailwind CSS

---

## 文件变更清单

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `insurance/field_config_db.py` | 新增 `get_column_config()` / `save_column_config()` / `delete_column_config()` + `CONFIG_TYPES` 扩充 |
| Modify | `insurance/api.py` | 新增 3 个列配置 API；修改 `list_records` 返回列配置；修改 `export_excel` 使用列配置 |
| Modify | `web/templates/field_config.html` | 新增「页面列配置」和「导出列配置」两个 tab |
| Modify | `web/templates/insurance.html` | 列表表头和数据渲染改为动态列配置；导出传入配置列 |

---

### Task 1: 后端 — field_config_db.py 新增列配置函数

**Files:**
- Modify: `insurance/field_config_db.py`

- [ ] **Step 1: 扩展 CONFIG_TYPES，新增默认列配置常量**

在 `insurance/field_config_db.py` 顶部的 `CONFIG_TYPES` 列表中追加两个类型，并新增默认列配置常量：

```python
# 支持的 config_type 类型
CONFIG_TYPES = ["company_alias", "policy_type_alias", "date_format", "fee_formula",
                "list_columns", "export_columns"]

# 默认列配置（与 field_mapping.py 的 OUTPUT_COLUMNS 保持一致）
DEFAULT_COLUMNS = [
    {"key": "承保公司", "visible": True, "order": 0, "display_name": "承保公司"},
    {"key": "保单号",   "visible": True, "order": 1, "display_name": "保单号"},
    {"key": "险种",     "visible": True, "order": 2, "display_name": "险种"},
    {"key": "车牌",     "visible": True, "order": 3, "display_name": "车牌"},
    {"key": "投保人",   "visible": True, "order": 4, "display_name": "投保人"},
    {"key": "被保人",   "visible": True, "order": 5, "display_name": "被保人"},
    {"key": "签单日期", "visible": True, "order": 6, "display_name": "签单日期"},
    {"key": "起保日期", "visible": True, "order": 7, "display_name": "起保日期"},
    {"key": "终保日期", "visible": True, "order": 8, "display_name": "终保日期"},
    {"key": "保费",     "visible": True, "order": 9, "display_name": "保费"},
    {"key": "佣金率",   "visible": True, "order": 10, "display_name": "佣金率"},
    {"key": "佣金",     "visible": True, "order": 11, "display_name": "佣金"},
    {"key": "业务员",   "visible": True, "order": 12, "display_name": "业务员"},
    {"key": "采购费率", "visible": True, "order": 13, "display_name": "采购费率"},
    {"key": "公司利润", "visible": True, "order": 14, "display_name": "公司利润"},
    {"key": "跟单人",   "visible": True, "order": 15, "display_name": "跟单人"},
    {"key": "跟单人利润", "visible": True, "order": 16, "display_name": "跟单人利润"},
    {"key": "出单渠道", "visible": True, "order": 17, "display_name": "出单渠道"},
    {"key": "车船税",   "visible": True, "order": 18, "display_name": "车船税"},
]
```

- [ ] **Step 2: 新增 `get_column_config()` 函数**

在 `insurance/field_config_db.py` 的 `get_effective_config` 函数之后，新增：

```python
def get_column_config(db, config_type: str, user_id: int, role: str, parent_id=None) -> dict:
    """
    获取列配置（list_columns 或 export_columns），按优先级查找：
    user → enterprise → global → 代码默认值。

    返回：{"source": "enterprise", "columns": [...]}
    """
    if config_type not in ("list_columns", "export_columns"):
        return {"source": "default", "columns": list(DEFAULT_COLUMNS)}

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 按优先级依次查找
        search_order = []
        if role == "employee" and parent_id is not None:
            search_order.append(("user", user_id, "user"))
            search_order.append(("enterprise", parent_id, "enterprise"))
        elif role == "enterprise":
            search_order.append(("enterprise", parent_id or user_id, "enterprise"))
        elif role == "super_admin":
            search_order.append(("global", None, "global"))

        for scope, scope_id, source_label in search_order:
            sid_cond, sid_params = _scope_id_condition(scope_id)
            cursor.execute(
                f"SELECT config_value FROM user_field_config "
                f"WHERE scope = %s AND {sid_cond} AND config_type = %s AND config_key = 'columns' "
                f"LIMIT 1",
                [scope] + sid_params + [config_type]
            )
            row = cursor.fetchone()
            if row:
                try:
                    columns = json.loads(row["config_value"])
                    return {"source": source_label, "columns": columns}
                except (TypeError, json.JSONDecodeError):
                    pass

        return {"source": "default", "columns": list(DEFAULT_COLUMNS)}
    finally:
        conn.close()
```

- [ ] **Step 3: 新增 `save_column_config()` 和 `delete_column_config()` 函数**

紧接着添加：

```python
def save_column_config(db, config_type: str, scope: str, scope_id, columns: list):
    """
    保存列配置。columns 为 [{"key":"xxx","visible":true,"order":0,"display_name":"xxx"}, ...]
    使用 config_key = 'columns'，整体存为一条 JSON 记录。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        sid_cond, sid_params = _scope_id_condition(scope_id)

        # 先删旧记录
        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND config_type = %s AND config_key = 'columns'",
            [scope] + sid_params + [config_type]
        )

        # 插入新记录
        value = json.dumps(columns, ensure_ascii=False)
        if scope_id is None:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, NULL, %s, %s, 'columns', %s, 1)",
                [scope, DEFAULT_TEMPLATE_NAME, config_type, value]
            )
        else:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, %s, %s, %s, 'columns', %s, 1)",
                [scope, scope_id, DEFAULT_TEMPLATE_NAME, config_type, value]
            )
        conn.commit()
        logger.debug("列配置已保存: scope=%s config_type=%s", scope, config_type)
    finally:
        conn.close()


def delete_column_config(db, config_type: str, scope: str, scope_id):
    """删除列配置，回退到上一级默认。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        sid_cond, sid_params = _scope_id_condition(scope_id)
        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND config_type = %s AND config_key = 'columns'",
            [scope] + sid_params + [config_type]
        )
        conn.commit()
        logger.debug("列配置已删除: scope=%s config_type=%s", scope, config_type)
    finally:
        conn.close()
```

- [ ] **Step 4: Commit**

```bash
git add insurance/field_config_db.py
git commit -m "feat: 新增列配置读写函数（list_columns / export_columns）"
```

---

### Task 2: 后端 — api.py 新增列配置 API

**Files:**
- Modify: `insurance/api.py`

- [ ] **Step 1: 在 api.py 顶部 import 新增函数**

在 `insurance/api.py` 的 import 区域（约第 31-35 行），追加导入：

```python
from .field_config_db import (
    list_templates as list_field_config_templates, rename_template, update_template_visibility, delete_template as delete_template_db,
    get_template_config, save_full_template,
    get_active_template, set_active_template,
    get_effective_config, apply_user_config_to_fields,
    get_column_config, save_column_config, delete_column_config,  # 新增
)
```

- [ ] **Step 2: 新增 3 个列配置 API 路由**

在 `api.py` 的 `field_config` 相关路由区域（约第 2845 行之后），新增：

```python
@insurance_bp.route("/api/insurance/column-config", methods=["GET"])
def get_column_config_api():
    """获取列配置（list_columns 或 export_columns）"""
    try:
        config_type = request.args.get("config_type", "list_columns")
        user = g.current_user
        result = get_column_config(_db, config_type, user["user_id"], user["role"], user.get("parent_id"))
        return jsonify({"code": 0, "data": result})
    except Exception as e:
        logger.exception("获取列配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/column-config", methods=["POST"])
def save_column_config_api():
    """保存列配置"""
    try:
        body = request.get_json(force=True) or {}
        config_type = body.get("config_type", "list_columns")
        columns = body.get("columns", [])

        if config_type not in ("list_columns", "export_columns"):
            return jsonify({"code": 400, "msg": "无效的 config_type"}), 400
        if not columns:
            return jsonify({"code": 400, "msg": "columns 不能为空"}), 400

        scope, scope_id = _get_user_scope()
        save_column_config(_db, config_type, scope, scope_id, columns)
        return jsonify({"code": 0, "msg": "列配置已保存"})
    except Exception as e:
        logger.exception("保存列配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/column-config", methods=["DELETE"])
def delete_column_config_api():
    """重置列配置（删除当前 scope 的配置，回退到上级默认）"""
    try:
        config_type = request.args.get("config_type", "list_columns")
        scope, scope_id = _get_user_scope()
        delete_column_config(_db, config_type, scope, scope_id)
        return jsonify({"code": 0, "msg": "已恢复默认"})
    except Exception as e:
        logger.exception("重置列配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 3: 修改 `list_records` 接口返回列配置**

在 `insurance/api.py` 的 `list_records()` 函数中（约第 244 行 `return jsonify` 之前），追加列配置数据：

将原来的：
```python
        return jsonify({"code": 0, "data": result})
```

改为：
```python
        # 获取页面列配置
        list_col_config = get_column_config(_db, "list_columns", user["user_id"], user["role"], user.get("parent_id"))
        result["column_config"] = list_col_config

        return jsonify({"code": 0, "data": result})
```

- [ ] **Step 4: 修改 `export_excel` 接口支持列配置**

在 `insurance/api.py` 的 `export_excel()` 函数中（约第 2154 行），将：

```python
        field_names = body.get("field_names", OUTPUT_COLUMNS)
```

改为：

```python
        # 优先使用请求中传入的 field_names（兼容旧前端），否则读取用户的导出列配置
        field_names = body.get("field_names")
        field_display_names = None
        if not field_names:
            user = g.current_user
            export_col_config = get_column_config(_db, "export_columns", user["user_id"], user["role"], user.get("parent_id"))
            export_columns = export_col_config.get("columns", [])
            # 按 order 排序，只取 visible 的列
            visible_cols = sorted([c for c in export_columns if c.get("visible", True)], key=lambda c: c.get("order", 0))
            field_names = [c["key"] for c in visible_cols]
            field_display_names = {c["key"]: c.get("display_name", c["key"]) for c in visible_cols}
        if not field_names:
            field_names = list(OUTPUT_COLUMNS)
```

然后在 success 分支的 headers 生成处（约第 2187 行），将：

```python
            headers = list(field_names)
```

改为：

```python
            # 使用 display_name 作为表头（如有列配置），否则用原始 field_names
            if field_display_names:
                headers = [field_display_names.get(f, f) for f in field_names]
            else:
                headers = list(field_names)
```

- [ ] **Step 5: 修改 `batch_sync_dingtalk` 接口兼容列配置**

在 `insurance/api.py` 的 `batch_sync_dingtalk()` 函数（约第 2047 行），将：

```python
        field_names = body.get("field_names", OUTPUT_COLUMNS)
```

改为：

```python
        field_names = body.get("field_names")
        if not field_names:
            user = g.current_user
            export_col_config = get_column_config(_db, "export_columns", user["user_id"], user["role"], user.get("parent_id"))
            visible_cols = sorted([c for c in export_col_config.get("columns", []) if c.get("visible", True)], key=lambda c: c.get("order", 0))
            field_names = [c["key"] for c in visible_cols] or list(OUTPUT_COLUMNS)
```

- [ ] **Step 6: Commit**

```bash
git add insurance/api.py
git commit -m "feat: 新增列配置 API（GET/POST/DELETE），list_records 返回列配置，export_excel 支持列配置"
```

---

### Task 3: 前端 — field_config.html 新增两个 tab

**Files:**
- Modify: `web/templates/field_config.html`

- [ ] **Step 1: Tab 导航新增两个按钮**

在 `web/templates/field_config.html` 约第 387 行，`自动计算公式` tab 按钮之后新增：

```html
                <button class="tab-btn" :class="{active: activeTab==='listCols'}" @click="activeTab='listCols'">页面列配置</button>
                <button class="tab-btn" :class="{active: activeTab==='exportCols'}" @click="activeTab='exportCols'">导出列配置</button>
```

- [ ] **Step 2: 新增页面列配置 tab 内容**

在公式 tab 的 `</div>` 结束标签之后（紧接着 Tab 3 的结束），新增：

```html
            <!-- Tab 4: 页面列配置 -->
            <div v-show="activeTab==='listCols'" style="padding:24px;">
                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:16px;">
                    <div style="font-size:13px; color:var(--text-muted);">
                        配置页面列表中显示的字段、顺序和名称
                        <span v-if="listColConfig.source && listColConfig.source !== 'default'" style="margin-left:8px; font-size:11px; background:var(--primary-bg); color:var(--primary); padding:2px 8px; border-radius:4px;">
                            来源: {{ listColConfig.source === 'enterprise' ? '企业配置' : listColConfig.source === 'global' ? '全局配置' : '个人配置' }}
                        </span>
                    </div>
                    <div style="display:flex; gap:8px;">
                        <button class="btn btn-outline btn-sm" @click="resetColumnConfig('list_columns')" :disabled="activeSource === 'enterprise'">恢复默认</button>
                    </div>
                </div>
                <div class="alias-table-wrap">
                    <table class="alias-table">
                        <thead>
                            <tr>
                                <th style="width:50px;">显示</th>
                                <th style="width:60px;">排序</th>
                                <th>字段标识</th>
                                <th style="width:180px;">显示名称</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="(col, idx) in listColItems" :key="col.key">
                                <td><input type="checkbox" v-model="col.visible" :disabled="activeSource === 'enterprise'"></td>
                                <td style="display:flex; gap:2px;">
                                    <button class="btn btn-outline btn-sm" @click="moveCol(listColItems, idx, -1)" :disabled="idx===0 || activeSource === 'enterprise'" style="padding:2px 6px;">↑</button>
                                    <button class="btn btn-outline btn-sm" @click="moveCol(listColItems, idx, 1)" :disabled="idx===listColItems.length-1 || activeSource === 'enterprise'" style="padding:2px 6px;">↓</button>
                                </td>
                                <td style="color:var(--text-muted);">{{ col.key }}</td>
                                <td><input type="text" v-model="col.display_name" :disabled="activeSource === 'enterprise'" :placeholder="col.key" /></td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Tab 5: 导出列配置 -->
            <div v-show="activeTab==='exportCols'" style="padding:24px;">
                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:16px;">
                    <div style="font-size:13px; color:var(--text-muted);">
                        配置 Excel 导出时包含的字段、顺序和列头名称
                        <span v-if="exportColConfig.source && exportColConfig.source !== 'default'" style="margin-left:8px; font-size:11px; background:var(--primary-bg); color:var(--primary); padding:2px 8px; border-radius:4px;">
                            来源: {{ exportColConfig.source === 'enterprise' ? '企业配置' : exportColConfig.source === 'global' ? '全局配置' : '个人配置' }}
                        </span>
                    </div>
                    <div style="display:flex; gap:8px;">
                        <button class="btn btn-outline btn-sm" @click="resetColumnConfig('export_columns')" :disabled="activeSource === 'enterprise'">恢复默认</button>
                    </div>
                </div>
                <div class="alias-table-wrap">
                    <table class="alias-table">
                        <thead>
                            <tr>
                                <th style="width:50px;">导出</th>
                                <th style="width:60px;">排序</th>
                                <th>字段标识</th>
                                <th style="width:180px;">Excel 列头</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="(col, idx) in exportColItems" :key="col.key">
                                <td><input type="checkbox" v-model="col.visible" :disabled="activeSource === 'enterprise'"></td>
                                <td style="display:flex; gap:2px;">
                                    <button class="btn btn-outline btn-sm" @click="moveCol(exportColItems, idx, -1)" :disabled="idx===0 || activeSource === 'enterprise'" style="padding:2px 6px;">↑</button>
                                    <button class="btn btn-outline btn-sm" @click="moveCol(exportColItems, idx, 1)" :disabled="idx===exportColItems.length-1 || activeSource === 'enterprise'" style="padding:2px 6px;">↓</button>
                                </td>
                                <td style="color:var(--text-muted);">{{ col.key }}</td>
                                <td><input type="text" v-model="col.display_name" :disabled="activeSource === 'enterprise'" :placeholder="col.key" /></td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
```

- [ ] **Step 3: 在 Vue setup 中新增列配置相关数据和方法**

在 `field_config.html` 的 Vue `setup()` 中（找到现有的 `data` / `ref` 声明区域），新增：

```javascript
        // === 列配置 ===
        const listColConfig = ref({ source: 'default', columns: [] });
        const exportColConfig = ref({ source: 'default', columns: [] });
        const listColItems = ref([]);
        const exportColItems = ref([]);

        // 默认列（兜底）
        const DEFAULT_COLUMNS = [
            {key:'承保公司',visible:true,order:0,display_name:'承保公司'},
            {key:'保单号',visible:true,order:1,display_name:'保单号'},
            {key:'险种',visible:true,order:2,display_name:'险种'},
            {key:'车牌',visible:true,order:3,display_name:'车牌'},
            {key:'投保人',visible:true,order:4,display_name:'投保人'},
            {key:'被保人',visible:true,order:5,display_name:'被保人'},
            {key:'签单日期',visible:true,order:6,display_name:'签单日期'},
            {key:'起保日期',visible:true,order:7,display_name:'起保日期'},
            {key:'终保日期',visible:true,order:8,display_name:'终保日期'},
            {key:'保费',visible:true,order:9,display_name:'保费'},
            {key:'佣金率',visible:true,order:10,display_name:'佣金率'},
            {key:'佣金',visible:true,order:11,display_name:'佣金'},
            {key:'业务员',visible:true,order:12,display_name:'业务员'},
            {key:'采购费率',visible:true,order:13,display_name:'采购费率'},
            {key:'公司利润',visible:true,order:14,display_name:'公司利润'},
            {key:'跟单人',visible:true,order:15,display_name:'跟单人'},
            {key:'跟单人利润',visible:true,order:16,display_name:'跟单人利润'},
            {key:'出单渠道',visible:true,order:17,display_name:'出单渠道'},
            {key:'车船税',visible:true,order:18,display_name:'车船税'},
        ];

        async function loadColumnConfig(configType) {
            try {
                const resp = await fetch(`/api/insurance/column-config?config_type=${configType}`);
                const data = await resp.json();
                if (data.code === 0) {
                    const config = data.data;
                    const columns = config.columns && config.columns.length ? config.columns : JSON.parse(JSON.stringify(DEFAULT_COLUMNS));
                    // 按 order 排序
                    columns.sort((a, b) => (a.order || 0) - (b.order || 0));
                    if (configType === 'list_columns') {
                        listColConfig.value = config;
                        listColItems.value = columns;
                    } else {
                        exportColConfig.value = config;
                        exportColItems.value = columns;
                    }
                }
            } catch (e) {
                console.error('加载列配置失败:', e);
            }
        }

        async function saveColumnConfig(configType) {
            const items = configType === 'list_columns' ? listColItems.value : exportColItems.value;
            // 重新编号 order
            const columns = items.map((col, idx) => ({ ...col, order: idx }));
            try {
                const resp = await fetch('/api/insurance/column-config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config_type: configType, columns }),
                });
                const data = await resp.json();
                if (data.code === 0) {
                    saveMsg.value = '列配置已保存';
                    setTimeout(() => { saveMsg.value = ''; }, 2000);
                }
            } catch (e) {
                console.error('保存列配置失败:', e);
            }
        }

        async function resetColumnConfig(configType) {
            if (!confirm('确认恢复默认列配置？')) return;
            try {
                await fetch(`/api/insurance/column-config?config_type=${configType}`, { method: 'DELETE' });
                await loadColumnConfig(configType);
            } catch (e) {
                console.error('重置列配置失败:', e);
            }
        }

        function moveCol(items, idx, direction) {
            const targetIdx = idx + direction;
            if (targetIdx < 0 || targetIdx >= items.length) return;
            const temp = items[idx];
            items[idx] = items[targetIdx];
            items[targetIdx] = temp;
        }
```

- [ ] **Step 4: 在 onMounted 中加载列配置**

在 `field_config.html` 的 `onMounted`（或初始化代码处），追加：

```javascript
            loadColumnConfig('list_columns');
            loadColumnConfig('export_columns');
```

- [ ] **Step 5: 在现有保存按钮逻辑中追加列配置保存**

找到现有的 `saveConfig` / `handleSave` 函数（保存按钮的处理函数），在其内部追加：

```javascript
            // 保存列配置
            if (activeTab.value === 'listCols') {
                await saveColumnConfig('list_columns');
                return;
            }
            if (activeTab.value === 'exportCols') {
                await saveColumnConfig('export_columns');
                return;
            }
```

- [ ] **Step 6: return 中暴露新变量**

在 `setup()` 的 `return { ... }` 中追加：

```javascript
            listColConfig, exportColConfig, listColItems, exportColItems,
            loadColumnConfig, saveColumnConfig, resetColumnConfig, moveCol,
```

- [ ] **Step 7: Commit**

```bash
git add web/templates/field_config.html
git commit -m "feat: 系统设置页新增「页面列配置」和「导出列配置」tab"
```

---

### Task 4: 前端 — insurance.html 列表和导出使用动态列

**Files:**
- Modify: `web/templates/insurance.html`

- [ ] **Step 1: 新增列配置相关 ref**

在 `web/templates/insurance.html` 的 Vue `setup()` 中，在 `outputColumns` ref 定义附近（约第 2441 行），新增：

```javascript
        // 列配置（从后端 list_records 返回或单独请求）
        const listColumnConfig = ref([]);  // [{key, visible, order, display_name}, ...]
        // 计算实际显示的列（按 order 排序，只取 visible 的）
        const visibleListColumns = computed(() => {
            if (!listColumnConfig.value.length) {
                // 未加载列配置，使用默认 outputColumns
                return outputColumns.value.map(col => ({ key: col, display_name: col }));
            }
            return listColumnConfig.value
                .filter(c => c.visible !== false)
                .sort((a, b) => (a.order || 0) - (b.order || 0))
                .map(c => ({ key: c.key, display_name: c.display_name || c.key }));
        });
```

- [ ] **Step 2: 在 loadRecords 中读取 column_config**

在 `insurance.html` 中找到加载记录列表的函数（搜索 `loadRecords` 或 `/api/insurance/records` 的 fetch 调用），在接收到响应数据后追加：

```javascript
                // 读取列配置
                if (data.data.column_config && data.data.column_config.columns) {
                    listColumnConfig.value = data.data.column_config.columns;
                }
```

- [ ] **Step 3: 修改表头渲染使用动态列**

将 `insurance.html` 约第 1128 行的：

```html
<th v-for="col in outputColumns" :key="col" class="text-gray-600 font-medium" style="min-width:80px;">{{ col }}</th>
```

改为：

```html
<th v-for="col in visibleListColumns" :key="col.key" class="text-gray-600 font-medium" style="min-width:80px;">{{ col.display_name }}</th>
```

- [ ] **Step 4: 修改表体渲染使用动态列**

将 `insurance.html` 约第 1173 行的：

```html
<td v-for="col in outputColumns" :key="col" class="text-gray-700 text-sm">
```

改为：

```html
<td v-for="col in visibleListColumns" :key="col.key" class="text-gray-700 text-sm">
```

同时将该 `<td>` 内部所有引用 `col` 的地方改为 `col.key`：
- 第 1174 行：`listEditing.col === col` → `listEditing.col === col.key`
- 第 1191 行：`@click="startListEdit(record, col)"` → `@click="startListEdit(record, col.key)"`
- 第 1193 行：`(record.display_fields || {})[col]` → `(record.display_fields || {})[col.key]`

- [ ] **Step 5: 修改导出逻辑传入列配置**

在 `insurance.html` 中搜索导出 Excel 的函数（搜索 `export/excel` 的 fetch 调用），找到 `field_names` 的构造逻辑。将 `field_names` 从前端直接传 `selectedFields` 或 `outputColumns` 的逻辑，改为不传 `field_names`（让后端从列配置读取），或者保持现有逻辑（兼容 — 前端仍然可以传 `field_names`，后端会优先使用）。

**推荐做法：** 保持现有前端导出逻辑不变（前端仍传 `field_names`），后端的列配置作为默认兜底。这样不会破坏现有功能。

- [ ] **Step 6: return 中暴露新变量**

在 `setup()` 的 `return { ... }` 中追加：

```javascript
            listColumnConfig, visibleListColumns,
```

- [ ] **Step 7: Commit**

```bash
git add web/templates/insurance.html
git commit -m "feat: 保单列表页使用动态列配置渲染表头和数据"
```

---

### Task 5: 验证和文档

- [ ] **Step 1: 启动服务验证**

```bash
python main.py
```

验证点：
1. 打开 `/insurance` 页面，列表正常显示 19 列（默认配置）
2. 打开 `/field-config` 页面，能看到「页面列配置」和「导出列配置」两个 tab
3. 在「页面列配置」中关闭某列、修改显示名、调整顺序，保存
4. 刷新 `/insurance` 页面，验证列变化生效
5. 导出 Excel，验证列配置生效
6. 点击「恢复默认」，验证恢复正常

- [ ] **Step 2: 创建更新记录**

创建 `docs/changelog/2026-05/2026-05-20-HH-mm更新记录.md`，内容包括：
- 需求说明：企业级列配置
- 数据库变更：无（复用 user_field_config 表）
- 代码变更：field_config_db.py、api.py、field_config.html、insurance.html
- 部署步骤：无特殊步骤，正常部署即可

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "docs: 企业级列配置更新记录"
```
