# 车主字段 + 按车牌合并导出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 1) 将车主字段加入导出列和列配置；2) 实现按车牌合并导出功能（可开关），同车牌多条保单合并为一行，保费/保单号等差异字段按险种拆分为独立列。

**Architecture:** 车主字段只需在 field_mapping.py 和 field_config_db.py 的默认列中补充。合并导出在 api.py 的 export_excel 中新增合并逻辑分支，利用 policy_parser.py 已有的 `_get_policy_type_code` 分类险种。合并开关存储在 user_field_config 表，前端在 field_config.html 和 insurance.html 的导出弹窗中提供开关 UI。

**Tech Stack:** Python/Flask, MySQL (user_field_config 表), Vue 3, openpyxl

---

## 文件变更清单

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `insurance/field_mapping.py` | OUTPUT_COLUMNS + DEFAULT_MAPPING 加车主 |
| Modify | `insurance/field_config_db.py` | DEFAULT_COLUMNS 加车主；新增合并相关常量和配置读写 |
| Modify | `insurance/policy_parser.py` | 导出 `_get_policy_type_code` 为公共函数 |
| Modify | `insurance/api.py` | export_excel 新增合并逻辑；新增合并开关 API |
| Modify | `web/templates/field_config.html` | 导出列配置 tab 加合并开关 |
| Modify | `web/templates/insurance.html` | 导出弹窗加合并开关 |

---

### Task 1: 车主字段加入导出映射和默认列配置

**Files:**
- Modify: `insurance/field_mapping.py`
- Modify: `insurance/field_config_db.py`

- [ ] **Step 1: field_mapping.py 加车主**

在 `insurance/field_mapping.py` 中：

1) `OUTPUT_COLUMNS` 列表（第 15-20 行），在 `"被保人"` 之后加 `"车主"`：

```python
OUTPUT_COLUMNS = [
    "承保公司", "保单号", "险种", "车牌", "投保人", "被保人", "车主",
    "签单日期", "起保日期", "终保日期", "保费",
    "佣金率", "佣金", "业务员", "采购费率", "公司利润",
    "跟单人", "跟单人利润", "出单渠道", "车船税",
]
```

2) `DEFAULT_MAPPING` 字典（第 24-46 行），在 `"被保险人"` 映射之后加：

```python
    "车主": "车主",
```

- [ ] **Step 2: field_config_db.py 的 DEFAULT_COLUMNS 加车主**

在 `insurance/field_config_db.py` 的 `DEFAULT_COLUMNS` 列表中，在 `"被保人"` (order: 5) 之后插入车主，后续所有 order +1：

```python
DEFAULT_COLUMNS = [
    {"key": "承保公司", "visible": True, "order": 0, "display_name": "承保公司"},
    {"key": "保单号",   "visible": True, "order": 1, "display_name": "保单号"},
    {"key": "险种",     "visible": True, "order": 2, "display_name": "险种"},
    {"key": "车牌",     "visible": True, "order": 3, "display_name": "车牌"},
    {"key": "投保人",   "visible": True, "order": 4, "display_name": "投保人"},
    {"key": "被保人",   "visible": True, "order": 5, "display_name": "被保人"},
    {"key": "车主",     "visible": True, "order": 6, "display_name": "车主"},
    {"key": "签单日期", "visible": True, "order": 7, "display_name": "签单日期"},
    {"key": "起保日期", "visible": True, "order": 8, "display_name": "起保日期"},
    {"key": "终保日期", "visible": True, "order": 9, "display_name": "终保日期"},
    {"key": "保费",     "visible": True, "order": 10, "display_name": "保费"},
    {"key": "佣金率",   "visible": True, "order": 11, "display_name": "佣金率"},
    {"key": "佣金",     "visible": True, "order": 12, "display_name": "佣金"},
    {"key": "业务员",   "visible": True, "order": 13, "display_name": "业务员"},
    {"key": "采购费率", "visible": True, "order": 14, "display_name": "采购费率"},
    {"key": "公司利润", "visible": True, "order": 15, "display_name": "公司利润"},
    {"key": "跟单人",   "visible": True, "order": 16, "display_name": "跟单人"},
    {"key": "跟单人利润", "visible": True, "order": 17, "display_name": "跟单人利润"},
    {"key": "出单渠道", "visible": True, "order": 18, "display_name": "出单渠道"},
    {"key": "车船税",   "visible": True, "order": 19, "display_name": "车船税"},
]
```

- [ ] **Step 3: Commit**

```bash
git add insurance/field_mapping.py insurance/field_config_db.py
git commit -m "feat: 车主字段加入导出列映射和默认列配置"
git push
```

---

### Task 2: 后端合并导出常量和配置读写

**Files:**
- Modify: `insurance/field_config_db.py`
- Modify: `insurance/policy_parser.py`

- [ ] **Step 1: policy_parser.py 导出险种分类函数**

在 `insurance/policy_parser.py` 中，将 `_get_policy_type_code` 函数（第 429 行）改名为 `get_policy_type_code`（去掉前缀下划线变为公共函数）。同时保留一个 `_get_policy_type_code = get_policy_type_code` 别名以兼容内部调用。

将：
```python
def _get_policy_type_code(policy_type: str) -> tuple:
```
改为：
```python
def get_policy_type_code(policy_type: str) -> tuple:
```

然后在函数定义之后加一行别名：
```python
_get_policy_type_code = get_policy_type_code
```

- [ ] **Step 2: field_config_db.py 新增合并相关常量**

在 `insurance/field_config_db.py` 的 `DEFAULT_COLUMNS` 定义之后，新增：

```python
# ---- 按车牌合并导出 ----

# 共享字段（合并后不拆分，多条记录互相补充）
MERGE_SHARED_FIELDS = ["承保公司", "车牌", "投保人", "被保人", "车主", "业务员", "跟单人", "出单渠道"]

# 需要按险种拆分的差异字段
MERGE_SPLIT_FIELDS = ["保单号", "保费", "签单日期", "起保日期", "终保日期",
                       "佣金率", "佣金", "采购费率", "公司利润", "跟单人利润"]

# 险种前缀（与 policy_parser.get_policy_type_code 的分类对应）
MERGE_PREFIXES = {
    "commercial": "商业险",
    "compulsory": "交强险",
    "accident": "非车险",
    "non_vehicle": "非车险",
}

# 合并后自动生成的差异列名列表
MERGE_EXTRA_COLUMNS = []
for _field in MERGE_SPLIT_FIELDS:
    for _prefix in ["商业险", "交强险", "非车险"]:
        MERGE_EXTRA_COLUMNS.append(f"{_prefix}{_field}")
```

- [ ] **Step 3: field_config_db.py 新增合并开关读写函数**

在 `delete_column_config` 函数之后，新增：

```python
def get_merge_by_plate(db, user_id: int, role: str, parent_id=None) -> bool:
    """获取合并导出开关状态，按优先级查找，默认关闭。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        search_order = []
        if role == "employee" and parent_id is not None:
            search_order.append(("user", user_id))
            search_order.append(("enterprise", parent_id))
        elif role == "enterprise":
            search_order.append(("enterprise", parent_id or user_id))
        elif role == "super_admin":
            search_order.append(("global", None))

        for scope, scope_id in search_order:
            sid_cond, sid_params = _scope_id_condition(scope_id)
            cursor.execute(
                f"SELECT config_value FROM user_field_config "
                f"WHERE scope = %s AND {sid_cond} AND config_type = 'export_columns' "
                f"AND config_key = 'merge_by_plate' LIMIT 1",
                [scope] + sid_params
            )
            row = cursor.fetchone()
            if row:
                return row["config_value"] == "1"
        return False
    finally:
        conn.close()


def save_merge_by_plate(db, scope: str, scope_id, enabled: bool):
    """保存合并导出开关。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        sid_cond, sid_params = _scope_id_condition(scope_id)

        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND config_type = 'export_columns' "
            f"AND config_key = 'merge_by_plate'",
            [scope] + sid_params
        )

        value = "1" if enabled else "0"
        if scope_id is None:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, NULL, %s, 'export_columns', 'merge_by_plate', %s, 1)",
                [scope, DEFAULT_TEMPLATE_NAME, value]
            )
        else:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, %s, %s, 'export_columns', 'merge_by_plate', %s, 1)",
                [scope, scope_id, DEFAULT_TEMPLATE_NAME, value]
            )
        conn.commit()
        logger.debug("合并开关已保存: scope=%s enabled=%s", scope, enabled)
    finally:
        conn.close()
```

- [ ] **Step 4: Commit**

```bash
git add insurance/field_config_db.py insurance/policy_parser.py
git commit -m "feat: 合并导出常量定义和开关配置读写"
git push
```

---

### Task 3: 后端 api.py 合并导出逻辑和 API

**Files:**
- Modify: `insurance/api.py`

- [ ] **Step 1: 新增 import**

在 `insurance/api.py` 顶部的 import 区域追加：

```python
from .field_config_db import (
    ...,
    get_merge_by_plate, save_merge_by_plate,
    MERGE_SHARED_FIELDS, MERGE_SPLIT_FIELDS, MERGE_PREFIXES, MERGE_EXTRA_COLUMNS,
)
from .policy_parser import get_policy_type_code
```

- [ ] **Step 2: 新增合并开关 API**

在列配置 API 路由之后，新增：

```python
@insurance_bp.route("/api/insurance/merge-config", methods=["GET"])
def get_merge_config_api():
    """获取合并导出开关"""
    try:
        user = g.current_user
        enabled = get_merge_by_plate(_db, user["user_id"], user["role"], user.get("parent_id"))
        return jsonify({"code": 0, "data": {"enabled": enabled}})
    except Exception as e:
        logger.exception("获取合并配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/merge-config", methods=["POST"])
def save_merge_config_api():
    """保存合并导出开关"""
    try:
        body = request.get_json(force=True) or {}
        enabled = body.get("enabled", False)
        scope, scope_id = _get_user_scope()
        save_merge_by_plate(_db, scope, scope_id, enabled)
        return jsonify({"code": 0, "msg": "已保存"})
    except Exception as e:
        logger.exception("保存合并配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 3: 在 export_excel 中新增合并分支**

在 `insurance/api.py` 的 `export_excel()` 函数的 success 分支中（写入行数据之前），新增合并处理。找到 `# 按车牌号排序` 附近的代码，在排序之前插入合并逻辑：

将现有的 success 分支（从 `headers = ...` 到写入行循环结束）替换为：

```python
            # 检查是否启用按车牌合并
            merge_enabled = body.get("merge_by_plate", False)

            if merge_enabled:
                # ---- 按车牌合并导出 ----
                # 1. 按车牌分组
                plate_groups = {}
                for inv in invoices:
                    fields = inv.get("fields", {}) if isinstance(inv, dict) else {}
                    if user_config and any(user_config.get(k) for k in user_config):
                        fields = apply_user_config_to_fields(user_config, fields)
                    plate = fields.get("车牌", "") or fields.get("车牌号", "") or ""
                    if not plate:
                        plate = "__无车牌__"
                    if plate not in plate_groups:
                        plate_groups[plate] = []
                    plate_groups[plate].append(fields)

                # 2. 合并每组
                merged_rows = []
                for plate, group in plate_groups.items():
                    merged = {}
                    # 共享字段：互相补充
                    for f in MERGE_SHARED_FIELDS:
                        for record in group:
                            val = record.get(f, "")
                            if val:
                                merged[f] = val
                                break

                    # 差异字段：按险种分配
                    for record in group:
                        policy_type = record.get("险种", "") or record.get("险种类型", "")
                        type_code, _ = get_policy_type_code(policy_type)
                        prefix = MERGE_PREFIXES.get(type_code, "非车险")
                        for f in MERGE_SPLIT_FIELDS:
                            col_name = f"{prefix}{f}"
                            val = record.get(f, "")
                            if val and not merged.get(col_name):
                                merged[col_name] = val

                    # 车船税（不拆分）
                    for record in group:
                        val = record.get("车船税", "")
                        if val:
                            merged["车船税"] = val
                            break

                    # 保留其他自定义字段（非共享非差异的字段互相补充）
                    all_known = set(MERGE_SHARED_FIELDS) | set(MERGE_SPLIT_FIELDS) | {"车船税", "险种", "险种类型"}
                    for record in group:
                        for k, v in record.items():
                            if k not in all_known and k not in merged and v:
                                merged[k] = v

                    merged_rows.append(merged)

                # 3. 排序（按车牌）
                merged_rows.sort(key=lambda r: r.get("车牌", ""))

                # 4. 写入表头和数据
                if field_display_names:
                    headers = [field_display_names.get(f, f) for f in field_names]
                else:
                    headers = list(field_names)
                ws.append(headers)

                for row_data in merged_rows:
                    row = []
                    for col in field_names:
                        row.append(row_data.get(col, ""))
                    ws.append(row)
            else:
                # ---- 普通导出（原逻辑） ----
                if field_display_names:
                    headers = [field_display_names.get(f, f) for f in field_names]
                else:
                    headers = list(field_names)
                ws.append(headers)

                invoices.sort(key=lambda inv: (inv.get("fields", {}) if isinstance(inv, dict) else {}).get("车牌号", "") or "")
                for inv in invoices:
                    fields = inv.get("fields", {}) if isinstance(inv, dict) else {}
                    if user_config and any(user_config.get(k) for k in user_config):
                        fields = apply_user_config_to_fields(user_config, fields)
                    row = []
                    for col in field_names:
                        if col == "文件名":
                            row.append(fields.get("文件名", "") or inv.get("file_name", inv.get("filename", "")))
                        else:
                            row.append(fields.get(col, ""))
                    ws.append(row)
```

- [ ] **Step 4: Commit**

```bash
git add insurance/api.py
git commit -m "feat: export_excel 新增按车牌合并导出逻辑和合并开关 API"
git push
```

---

### Task 4: 前端 field_config.html 导出列配置加合并开关

**Files:**
- Modify: `web/templates/field_config.html`

- [ ] **Step 1: 导出列配置 tab 顶部加合并开关**

在 `web/templates/field_config.html` 的导出列配置 tab（`v-show="activeTab==='exportCols'"`）中，在描述文字的 `</div>` 和表格之间，插入合并开关：

找到「配置 Excel 导出时包含的字段、顺序和列头名称」描述 div 的结束 `</div>` 之后、`<div class="alias-table-wrap">` 之前，插入：

```html
                <div style="display:flex; align-items:center; gap:12px; margin-bottom:16px; padding:12px 16px; background:var(--surface-secondary); border-radius:8px; border:1px solid var(--border-light);">
                    <label style="display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; font-weight:500;" @click="mergeByPlate = !mergeByPlate">
                        <div style="position:relative; width:36px; height:20px; flex-shrink:0;">
                            <div style="position:absolute; inset:0; border-radius:10px; transition:background 0.2s;" :style="{ background: mergeByPlate ? 'var(--primary)' : '#d1d5db' }"></div>
                            <div style="position:absolute; width:16px; height:16px; top:2px; border-radius:8px; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,0.2); transition:left 0.2s;" :style="{ left: mergeByPlate ? '18px' : '2px' }"></div>
                        </div>
                        按车牌合并导出
                    </label>
                    <span style="font-size:11px; color:var(--text-muted);">开启后，同车牌的多条保单合并为一行，保费/保单号等按险种拆分为独立列</span>
                    <button v-if="mergeByPlate" class="btn btn-outline btn-sm" @click="injectMergeColumns" style="margin-left:auto;">补充合并字段</button>
                </div>
```

- [ ] **Step 2: Vue setup 新增合并相关数据和方法**

在 `field_config.html` 的 Vue setup 中，在列配置相关变量附近新增：

```javascript
        // 合并开关
        const mergeByPlate = ref(false);

        // 合并差异字段列表
        const MERGE_SPLIT_FIELDS = ['保单号', '保费', '签单日期', '起保日期', '终保日期',
                                     '佣金率', '佣金', '采购费率', '公司利润', '跟单人利润'];
        const MERGE_PREFIXES_LIST = ['商业险', '交强险', '非车险'];
        const MERGE_EXTRA_COLUMNS = [];
        for (const field of MERGE_SPLIT_FIELDS) {
            for (const prefix of MERGE_PREFIXES_LIST) {
                MERGE_EXTRA_COLUMNS.push(`${prefix}${field}`);
            }
        }

        // 加载合并开关
        async function loadMergeConfig() {
            try {
                const resp = await authFetch('/api/insurance/merge-config');
                const data = await resp.json();
                if (data.code === 0) {
                    mergeByPlate.value = data.data.enabled;
                }
            } catch (e) {
                console.error('加载合并配置失败:', e);
            }
        }

        // 保存合并开关
        async function saveMergeConfig() {
            try {
                await authFetch('/api/insurance/merge-config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: mergeByPlate.value }),
                });
            } catch (e) {
                console.error('保存合并配置失败:', e);
            }
        }

        // 补充合并差异字段到导出列配置
        function injectMergeColumns() {
            const existingKeys = new Set(exportColItems.value.map(c => c.key));
            let maxOrder = exportColItems.value.length;
            let added = 0;
            for (const col of MERGE_EXTRA_COLUMNS) {
                if (!existingKeys.has(col)) {
                    exportColItems.value.push({
                        key: col, visible: true, order: maxOrder++,
                        display_name: col, custom: true,
                    });
                    added++;
                }
            }
            if (added > 0) {
                showToast(`已补充 ${added} 个合并字段`);
            } else {
                showToast('合并字段已存在，无需补充');
            }
        }
```

- [ ] **Step 3: saveColumnConfig 中保存合并开关**

找到 `saveColumnConfig` 函数，在其最后（成功提示之后）追加：

```javascript
                // 导出列配置保存时同时保存合并开关
                if (configType === 'export_columns') {
                    await saveMergeConfig();
                }
```

- [ ] **Step 4: onMounted 加载合并配置**

在 onMounted 中追加：

```javascript
            loadMergeConfig();
```

- [ ] **Step 5: return 暴露新变量**

在 return 中追加：

```javascript
            mergeByPlate, loadMergeConfig, saveMergeConfig, injectMergeColumns,
```

- [ ] **Step 6: Commit**

```bash
git add web/templates/field_config.html
git commit -m "feat: 导出列配置页新增按车牌合并开关和补充合并字段功能"
git push
```

---

### Task 5: 前端 insurance.html 导出弹窗加合并开关

**Files:**
- Modify: `web/templates/insurance.html`

- [ ] **Step 1: 导出弹窗 UI 加合并开关**

在 `web/templates/insurance.html` 的导出弹窗中（约第 1733 行 `<div class="px-5 py-4 space-y-4">` 内），在「选择要导出的表头字段」之前插入合并开关：

```html
                    <div v-if="exportPendingType === 'records' || exportPendingType === 'success'" style="display:flex; align-items:center; gap:10px; padding:10px 14px; background:#f8fafc; border-radius:8px; border:1px solid #e2e8f0; margin-bottom:4px;">
                        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; font-weight:500;" @click="exportMergeByPlate = !exportMergeByPlate">
                            <div style="position:relative; width:36px; height:20px; flex-shrink:0;">
                                <div style="position:absolute; inset:0; border-radius:10px; transition:background 0.2s;" :style="{ background: exportMergeByPlate ? '#f97316' : '#d1d5db' }"></div>
                                <div style="position:absolute; width:16px; height:16px; top:2px; border-radius:8px; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,0.2); transition:left 0.2s;" :style="{ left: exportMergeByPlate ? '18px' : '2px' }"></div>
                            </div>
                            按车牌合并
                        </label>
                        <span style="font-size:11px; color:#94a3b8;">同车牌多保单合并为一行</span>
                    </div>
```

- [ ] **Step 2: Vue setup 新增合并开关 ref**

在 insurance.html 的 Vue setup 中，在 `exportModalVisible` 附近新增：

```javascript
        const exportMergeByPlate = ref(false);
```

- [ ] **Step 3: 加载合并默认值**

在 `exportRecordsExcel` 函数中（打开弹窗时），在 `exportModalVisible.value = true;` 之前追加加载合并配置：

```javascript
            // 加载合并默认值
            try {
                const mergeResp = await authFetch('/api/insurance/merge-config');
                const mergeData = await mergeResp.json();
                if (mergeData.code === 0) exportMergeByPlate.value = mergeData.data.enabled;
            } catch (e) {}
```

注意：需要将 `exportRecordsExcel` 改为 `async function`。

同时在手动上传的 `exportExcel` 函数中也加载合并默认值（该函数也需改为 async）。

- [ ] **Step 4: confirmExport 传递合并开关**

在 `confirmExport` 函数中，修改调用 `doExportRecordsExcel` 和 `doExportExcel` 时传递合并参数：

将：
```javascript
            if (type === 'records') {
                await doExportRecordsExcel(selectedCols);
            } else {
                await doExportExcel(type, selectedCols);
            }
```

改为：
```javascript
            if (type === 'records') {
                await doExportRecordsExcel(selectedCols, exportMergeByPlate.value);
            } else {
                await doExportExcel(type, selectedCols, exportMergeByPlate.value);
            }
```

- [ ] **Step 5: doExportRecordsExcel 传递合并参数到后端**

在 `doExportRecordsExcel` 函数签名中加参数：

```javascript
        async function doExportRecordsExcel(selectedCols, mergeByPlate = false) {
```

在该函数调用 `/api/insurance/export/excel` 的 `JSON.stringify` 中追加 `merge_by_plate` 参数：

将：
```javascript
                    body: JSON.stringify({ invoices: data, field_names: selectedCols, export_type: 'success' }),
```

改为：
```javascript
                    body: JSON.stringify({ invoices: data, field_names: selectedCols, export_type: 'success', merge_by_plate: mergeByPlate }),
```

- [ ] **Step 6: doExportExcel 传递合并参数到后端**

同样修改 `doExportExcel` 函数签名和 fetch body。

找到 `doExportExcel` 函数签名（约第 3568 行）：

```javascript
        async function doExportExcel(type, selectedCols, mergeByPlate = false) {
```

找到该函数中调用 `/api/insurance/export/excel` 的地方，在 body 中追加 `merge_by_plate: mergeByPlate`。

- [ ] **Step 7: return 暴露**

在 return 中追加 `exportMergeByPlate`。

- [ ] **Step 8: Commit**

```bash
git add web/templates/insurance.html
git commit -m "feat: 导出弹窗新增按车牌合并开关"
git push
```

---

### Task 6: 验证和文档

- [ ] **Step 1: Python 编译验证**

```bash
python3 -c "
import py_compile
py_compile.compile('insurance/api.py', doraise=True)
py_compile.compile('insurance/field_config_db.py', doraise=True)
py_compile.compile('insurance/policy_parser.py', doraise=True)
print('OK')
"
```

- [ ] **Step 2: 创建更新记录**

创建 `docs/changelog/2026-05/2026-05-20-18-00更新记录.md`。

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "docs: 车主字段 + 按车牌合并导出更新记录"
git push
```
