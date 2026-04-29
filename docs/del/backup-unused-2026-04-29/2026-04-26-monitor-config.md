# 监控配置管理页面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建独立的监控配置管理页面，支持 CRUD 监控规则，每条规则独立绑定群聊/私聊来源、钉钉同步目标和字段映射。

**Architecture:** 新建 `monitor_configs` 数据库表存储监控配置，新建 `insurance/monitor_config_db.py` 提供数据库操作，在 `insurance/api.py` 中新增 CRUD API，新建 `web/templates/monitor_config.html` 前端页面（Vue 3 + Tailwind，与现有 insurance.html 风格一致）。修改 `handler.py` 和 `fetcher.py` 从新表读取监控规则，替换旧的 `watch_list` + `dingtalk_targets` 配置。

**Tech Stack:** Python/Flask, MySQL, Vue 3 (CDN), Tailwind CSS (CDN)

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `insurance/monitor_config_db.py` | 新建 | monitor_configs 表的建表 + CRUD |
| `insurance/api.py` | 修改 | 新增 5 个监控配置 API 路由 |
| `web/templates/monitor_config.html` | 新建 | 监控配置管理前端页面 |
| `main.py` | 修改 | 注册 /monitor-config 路由，初始化建表 |
| `insurance/handler.py` | 修改 | `get_watch_config` 和 `_get_bound_targets` 改读新表 |
| `core/fetcher.py` | 修改 | `_check_insurance_trigger` 传递匹配到的 monitor_config_id |

---

### Task 1: 数据库层 — monitor_configs 表

**Files:**
- Create: `insurance/monitor_config_db.py`

- [ ] **Step 1: 创建 monitor_config_db.py**

```python
"""
监控配置数据库操作
管理 monitor_configs 表的建表、增删改查
"""

import json
import logging

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

_JSON_FIELDS = {"rooms", "users", "field_mapping"}


def init_monitor_config_table(db):
    """建表 monitor_configs（幂等）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitor_configs (
                id               INT PRIMARY KEY AUTO_INCREMENT,
                name             VARCHAR(128) NOT NULL DEFAULT '',
                enabled          TINYINT NOT NULL DEFAULT 1,
                rooms            LONGTEXT COMMENT '群聊列表 JSON: [{"id":"wr...","name":"群名"},...]',
                users            LONGTEXT COMMENT '私聊列表 JSON: [{"id":"wm...","name":"人名"},...]',
                dingtalk_doc_url VARCHAR(512) DEFAULT '',
                dingtalk_base_id VARCHAR(128) DEFAULT '',
                dingtalk_sheet_id VARCHAR(128) DEFAULT '',
                field_mapping    LONGTEXT COMMENT '字段映射 JSON: {"OCR字段":"钉钉列名",...}',
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_monitor_enabled (enabled)
            )
        """)
        conn.commit()
        logger.info("monitor_configs 表初始化完成")
    finally:
        conn.close()


def list_monitor_configs(db) -> list:
    """查询全部监控配置，按 id 降序"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM monitor_configs ORDER BY id DESC")
        rows = list(cursor.fetchall())
        for row in rows:
            _deserialize_json(row)
        return rows
    finally:
        conn.close()


def get_monitor_config(db, config_id: int) -> dict:
    """获取单条监控配置"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM monitor_configs WHERE id = %s", (config_id,))
        row = cursor.fetchone()
        if row:
            _deserialize_json(row)
            return dict(row)
        return None
    finally:
        conn.close()


def create_monitor_config(db, data: dict) -> int:
    """新增监控配置，返回新 id"""
    data = _serialize_json(data)
    columns = ", ".join(data.keys())
    placeholders = ", ".join(["%s"] * len(data))
    values = list(data.values())

    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"INSERT INTO monitor_configs ({columns}) VALUES ({placeholders})",
            values
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_monitor_config(db, config_id: int, data: dict) -> bool:
    """更新监控配置，返回是否成功"""
    data = _serialize_json(data)
    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    values = list(data.values()) + [config_id]

    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE monitor_configs SET {set_clause} WHERE id = %s",
            values
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_monitor_config(db, config_id: int) -> bool:
    """删除监控配置，返回是否成功"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM monitor_configs WHERE id = %s", (config_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_enabled_monitor_configs(db) -> list:
    """获取所有启用的监控配置（消息触发时使用）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM monitor_configs WHERE enabled = 1")
        rows = list(cursor.fetchall())
        for row in rows:
            _deserialize_json(row)
        return rows
    finally:
        conn.close()


def _serialize_json(data: dict) -> dict:
    """写入前：dict/list 字段序列化为 JSON 字符串"""
    result = {}
    for k, v in data.items():
        if k in _JSON_FIELDS and isinstance(v, (dict, list)):
            result[k] = json.dumps(v, ensure_ascii=False)
        else:
            result[k] = v
    return result


def _deserialize_json(row: dict):
    """读取后：JSON 字符串字段反序列化"""
    for field in _JSON_FIELDS:
        if field in row and isinstance(row[field], str):
            try:
                row[field] = json.loads(row[field])
            except (TypeError, json.JSONDecodeError):
                row[field] = [] if field != "field_mapping" else {}
```

- [ ] **Step 2: Commit**

```bash
git add insurance/monitor_config_db.py
git commit -m "feat: 新增 monitor_configs 数据库表及 CRUD 操作"
```

---

### Task 2: API 层 — 监控配置 CRUD 接口

**Files:**
- Modify: `insurance/api.py` — 文件末尾新增路由
- Modify: `main.py` — 注册路由 + 建表

- [ ] **Step 1: 在 insurance/api.py 顶部导入新增依赖**

在已有的 `from .db import (...)` 之后追加：

```python
from .monitor_config_db import (
    list_monitor_configs,
    get_monitor_config,
    create_monitor_config,
    update_monitor_config,
    delete_monitor_config,
)
from .dingtalk_sync import parse_dingtalk_doc_url
```

注意：`parse_dingtalk_doc_url` 已在文件顶部导入过，无需重复。只需导入 `monitor_config_db` 的函数。

- [ ] **Step 2: 在 insurance/api.py 末尾追加 5 个路由**

```python
# ============================================================
# 监控配置管理
# ============================================================

@insurance_bp.route("/api/monitor-configs", methods=["GET"])
def list_monitor_configs_api():
    """获取全部监控配置"""
    try:
        configs = list_monitor_configs(_db)
        # 时间戳转字符串
        for c in configs:
            for ts_field in ("created_at", "updated_at"):
                if c.get(ts_field) and not isinstance(c[ts_field], str):
                    c[ts_field] = str(c[ts_field])
        return jsonify({"code": 0, "data": configs})
    except Exception as e:
        logger.exception("获取监控配置列表失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/monitor-configs", methods=["POST"])
def create_monitor_config_api():
    """新增监控配置"""
    try:
        body = request.get_json(force=True) or {}
        name = body.get("name", "").strip()
        if not name:
            return jsonify({"code": 400, "msg": "监控名称不能为空"}), 400

        # 解析钉钉文档 URL
        doc_url = body.get("dingtalk_doc_url", "").strip()
        base_id, sheet_id = "", ""
        if doc_url:
            base_id, sheet_id = parse_dingtalk_doc_url(doc_url)

        config_id = create_monitor_config(_db, {
            "name": name,
            "enabled": 1 if body.get("enabled", True) else 0,
            "rooms": body.get("rooms", []),
            "users": body.get("users", []),
            "dingtalk_doc_url": doc_url,
            "dingtalk_base_id": base_id,
            "dingtalk_sheet_id": sheet_id,
            "field_mapping": body.get("field_mapping", {}),
        })

        config = get_monitor_config(_db, config_id)
        return jsonify({"code": 0, "data": config, "msg": "监控配置已创建"})
    except Exception as e:
        logger.exception("创建监控配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/monitor-configs/<int:config_id>", methods=["PUT"])
def update_monitor_config_api(config_id):
    """更新监控配置"""
    try:
        body = request.get_json(force=True) or {}
        if not body:
            return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

        updates = {}
        if "name" in body:
            updates["name"] = body["name"].strip()
        if "enabled" in body:
            updates["enabled"] = 1 if body["enabled"] else 0
        if "rooms" in body:
            updates["rooms"] = body["rooms"]
        if "users" in body:
            updates["users"] = body["users"]
        if "field_mapping" in body:
            updates["field_mapping"] = body["field_mapping"]
        if "dingtalk_doc_url" in body:
            doc_url = body["dingtalk_doc_url"].strip()
            updates["dingtalk_doc_url"] = doc_url
            if doc_url:
                base_id, sheet_id = parse_dingtalk_doc_url(doc_url)
                updates["dingtalk_base_id"] = base_id
                updates["dingtalk_sheet_id"] = sheet_id
            else:
                updates["dingtalk_base_id"] = ""
                updates["dingtalk_sheet_id"] = ""

        ok = update_monitor_config(_db, config_id, updates)
        if not ok:
            return jsonify({"code": 404, "msg": "监控配置不存在"}), 404

        config = get_monitor_config(_db, config_id)
        return jsonify({"code": 0, "data": config, "msg": "监控配置已更新"})
    except Exception as e:
        logger.exception("更新监控配置 %d 失败", config_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/monitor-configs/<int:config_id>", methods=["DELETE"])
def delete_monitor_config_api(config_id):
    """删除监控配置"""
    try:
        ok = delete_monitor_config(_db, config_id)
        if not ok:
            return jsonify({"code": 404, "msg": "监控配置不存在"}), 404
        return jsonify({"code": 0, "msg": "监控配置已删除"})
    except Exception as e:
        logger.exception("删除监控配置 %d 失败", config_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/monitor-configs/<int:config_id>/toggle", methods=["PUT"])
def toggle_monitor_config_api(config_id):
    """启停监控配置"""
    try:
        config = get_monitor_config(_db, config_id)
        if not config:
            return jsonify({"code": 404, "msg": "监控配置不存在"}), 404

        new_enabled = 0 if config["enabled"] else 1
        update_monitor_config(_db, config_id, {"enabled": new_enabled})
        return jsonify({"code": 0, "data": {"enabled": bool(new_enabled)}, "msg": "已更新"})
    except Exception as e:
        logger.exception("切换监控配置 %d 状态失败", config_id)
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 3: 修改 main.py — 建表 + 注册页面路由**

在 `main.py` 的 `from insurance.db import init_insurance_tables` 后添加导入：

```python
from insurance.monitor_config_db import init_monitor_config_table
```

在 `create_app` 函数中，`init_insurance_tables(db)` 后添加：

```python
init_monitor_config_table(db)
```

在 `@app.route("/insurance")` 路由附近添加页面路由：

```python
@app.route("/monitor-config")
def monitor_config_page():
    return render_template("monitor_config.html")
```

- [ ] **Step 4: Commit**

```bash
git add insurance/api.py insurance/monitor_config_db.py main.py
git commit -m "feat: 新增监控配置 CRUD API 及页面路由"
```

---

### Task 3: 前端页面 — monitor_config.html

**Files:**
- Create: `web/templates/monitor_config.html`

- [ ] **Step 1: 创建 monitor_config.html**

页面结构：
- 顶栏：标题 + 返回首页链接 + 新增按钮
- 列表：卡片式展示每条监控配置（名称、启停开关、来源标签、钉钉目标、操作按钮）
- 弹窗：新增/编辑表单（名称、群聊多选、私聊多选、钉钉文档 URL、字段映射表格）

技术栈与 insurance.html 保持一致：Vue 3 CDN + Tailwind CSS + 相同色彩变量。

前端主要交互逻辑：
1. `onMounted` 时调用 `GET /api/monitor-configs` 加载列表 + `GET /api/insurance/rooms` 加载可选群聊/私聊
2. 新增/编辑弹窗：
   - 名称输入框
   - 群聊多选（从可选列表中选择，已选的显示为标签可移除）
   - 私聊多选（同上）
   - 钉钉文档 URL 输入框
   - 字段映射：表格形式，左列为 OCR 字段名（从 `OUTPUT_COLUMNS` 取），右列为钉钉列名（可自定义输入，默认同名）
3. 保存时 POST/PUT `/api/monitor-configs`
4. 列表中每条有：启停开关、编辑按钮、删除按钮

字段映射的默认值来自 `OUTPUT_COLUMNS`（已有 `/api/insurance/mapping/config` 接口可获取），用户可针对每条监控配置自定义映射。

完整 HTML 文件过长，核心 Vue setup 结构如下（实现时需写完整）：

```javascript
const { createApp, ref, computed, onMounted } = Vue;

createApp({
  setup() {
    // 状态
    const configs = ref([]);
    const showModal = ref(false);
    const editingId = ref(null);
    const form = ref({ name: '', rooms: [], users: [], dingtalk_doc_url: '', field_mapping: {} });
    const roomOptions = ref([]);
    const userOptions = ref([]);
    const outputColumns = ref([]);
    const loading = ref(false);

    // 加载列表
    async function loadConfigs() { /* GET /api/monitor-configs */ }

    // 加载可选群聊/私聊
    async function loadOptions() { /* GET /api/insurance/rooms */ }

    // 加载字段列表
    async function loadOutputColumns() { /* GET /api/insurance/mapping/config → output_columns */ }

    // 打开新增弹窗
    function openCreate() {
      editingId.value = null;
      // 初始化 field_mapping：每个 OUTPUT_COLUMN 默认映射同名
      const mapping = {};
      outputColumns.value.forEach(col => { mapping[col] = col; });
      form.value = { name: '', rooms: [], users: [], dingtalk_doc_url: '', field_mapping: mapping };
      showModal.value = true;
    }

    // 打开编辑弹窗
    function openEdit(config) {
      editingId.value = config.id;
      form.value = {
        name: config.name,
        rooms: config.rooms || [],
        users: config.users || [],
        dingtalk_doc_url: config.dingtalk_doc_url || '',
        field_mapping: config.field_mapping || {},
      };
      showModal.value = true;
    }

    // 保存（新增或编辑）
    async function save() { /* POST 或 PUT /api/monitor-configs */ }

    // 删除
    async function remove(id) { /* DELETE /api/monitor-configs/:id */ }

    // 启停
    async function toggle(id) { /* PUT /api/monitor-configs/:id/toggle */ }

    // 群聊/私聊选择器：添加
    function addRoom(option) { /* 从 roomOptions 选中加入 form.rooms */ }
    function removeRoom(index) { /* 从 form.rooms 移除 */ }
    function addUser(option) { /* 从 userOptions 选中加入 form.users */ }
    function removeUser(index) { /* 从 form.users 移除 */ }

    onMounted(() => {
      loadConfigs();
      loadOptions();
      loadOutputColumns();
    });

    return {
      configs, showModal, editingId, form, roomOptions, userOptions,
      outputColumns, loading,
      openCreate, openEdit, save, remove, toggle,
      addRoom, removeRoom, addUser, removeUser,
    };
  }
}).mount('#app');
```

- [ ] **Step 2: Commit**

```bash
git add web/templates/monitor_config.html
git commit -m "feat: 新增监控配置管理前端页面"
```

---

### Task 4: Handler 改造 — 从 monitor_configs 表读取监控规则

**Files:**
- Modify: `insurance/handler.py` — `get_watch_config` 和 `_get_bound_targets` 方法

- [ ] **Step 1: 修改 handler.py 顶部导入**

在已有导入区域添加：

```python
from .monitor_config_db import get_enabled_monitor_configs
```

- [ ] **Step 2: 重写 get_watch_config 方法**

将 `handler.py` 中的 `get_watch_config` 方法替换为：

```python
def get_watch_config(self) -> dict:
    """
    从 monitor_configs 表获取所有启用的监控来源。

    返回: {"rooms": [roomid, ...], "users": [userid, ...]}
    兼容旧逻辑：所有监控配置的来源合并为一个扁平列表。
    """
    try:
        configs = get_enabled_monitor_configs(self.db)
        rooms = set()
        users = set()
        for cfg in configs:
            for r in (cfg.get("rooms") or []):
                if isinstance(r, dict) and r.get("id"):
                    rooms.add(r["id"])
            for u in (cfg.get("users") or []):
                if isinstance(u, dict) and u.get("id"):
                    users.add(u["id"])
        return {"rooms": list(rooms), "users": list(users)}
    except Exception as e:
        logger.error("获取监控列表失败: %s", e)
        return {"rooms": [], "users": []}
```

- [ ] **Step 3: 重写 _get_bound_targets 方法，返回匹配的监控配置列表**

将 `_get_bound_targets` 替换为 `_get_matched_monitors`：

```python
def _get_matched_monitors(self, roomid: str, sender: str) -> list:
    """
    根据 roomid / sender 查找匹配的监控配置。

    返回匹配的监控配置列表（包含 dingtalk_base_id, dingtalk_sheet_id, field_mapping 等）。
    一条消息可能匹配多个监控配置，各自独立同步。
    """
    try:
        configs = get_enabled_monitor_configs(self.db)
    except Exception as e:
        logger.error("获取监控配置失败: %s", e)
        return []

    matched = []
    for cfg in configs:
        room_ids = {r["id"] for r in (cfg.get("rooms") or []) if isinstance(r, dict) and r.get("id")}
        user_ids = {u["id"] for u in (cfg.get("users") or []) if isinstance(u, dict) and u.get("id")}

        if roomid and roomid in room_ids:
            matched.append(cfg)
        elif not roomid and sender and sender in user_ids:
            matched.append(cfg)

    return matched
```

- [ ] **Step 4: 修改 _process 方法中自动同步钉钉的逻辑**

将 `_process` 方法中第 326-335 行的自动同步部分替换为：

```python
            # 8. 自动同步钉钉（按监控配置决定）
            try:
                monitors = self._get_matched_monitors(roomid, sender)
                for monitor in monitors:
                    if not (monitor.get("dingtalk_base_id") and monitor.get("dingtalk_sheet_id")):
                        continue
                    # 使用监控配置自身的字段映射（如果有），否则用全局映射结果
                    monitor_mapping = monitor.get("field_mapping") or {}
                    if monitor_mapping:
                        sync_fields = {}
                        for ocr_field, target_col in monitor_mapping.items():
                            if target_col and ocr_field in parsed_fields:
                                sync_fields[target_col] = parsed_fields[ocr_field]
                    else:
                        sync_fields = mapped_fields

                    self._sync_to_dingtalk_v2(
                        record_id, sync_fields,
                        sender_name=sender_name,
                        monitor=monitor,
                    )
            except Exception as e:
                logger.error("自动同步钉钉失败, record_id=%d: %s", record_id, e)
```

- [ ] **Step 5: 新增 _sync_to_dingtalk_v2 方法**

在 `_sync_to_dingtalk` 方法下方添加：

```python
def _sync_to_dingtalk_v2(
    self, record_id: int, sync_fields: dict,
    sender_name: str = "", monitor: dict = None,
):
    """
    按监控配置同步到指定钉钉文档。

    Args:
        record_id:   数据库记录 ID
        sync_fields: 经过字段映射后的导出字段
        sender_name: 发送人姓名
        monitor:     匹配的监控配置（含 dingtalk_base_id, dingtalk_sheet_id 等）
    """
    from insurance.dingtalk_sync import DingTalkTableService

    if not monitor:
        return

    try:
        # 读取钉钉应用凭证
        app_cfg = self.app_config.get("dingtalk", {})
        if not app_cfg or not app_cfg.get("app_key"):
            app_cfg = get_insurance_config(self.db, "dingtalk_app", {})
        if not isinstance(app_cfg, dict):
            return

        app_key = app_cfg.get("app_key", "")
        app_secret = app_cfg.get("app_secret", "")
        operator_id = app_cfg.get("operator_id", "")

        if not (app_key and app_secret and operator_id):
            logger.warning("钉钉应用凭证未完整配置")
            return

        base_id = monitor["dingtalk_base_id"]
        sheet_id = monitor["dingtalk_sheet_id"]

        invoice_fields = {k: v for k, v in sync_fields.items() if v}
        if sender_name:
            invoice_fields["发送人"] = sender_name
        field_names = list(invoice_fields.keys())
        invoice = {"fields": invoice_fields}

        svc = DingTalkTableService(app_key, app_secret, base_id, sheet_id, operator_id)
        result = svc.append_invoices(field_names, [invoice])

        logger.info(
            "同步钉钉成功(v2), record_id=%d monitor=%s inserted=%s",
            record_id, monitor.get("name", monitor.get("id")),
            result.get("inserted_count", 0),
        )

        update_insurance_record(self.db, record_id, {
            "dingtalk_synced": 1,
            "dingtalk_target_id": str(monitor.get("id", "")),
        })

    except Exception as e:
        logger.error(
            "同步钉钉失败(v2), record_id=%d monitor=%s: %s",
            record_id, monitor.get("name", ""), e, exc_info=True,
        )
```

- [ ] **Step 6: 保留旧 _sync_to_dingtalk 和 _get_bound_targets 不删除**

旧方法保留兼容手动同步场景（insurance/api.py 中的 `sync_record_to_dingtalk` 和 `batch_sync_dingtalk` 仍在调用）。

- [ ] **Step 7: Commit**

```bash
git add insurance/handler.py
git commit -m "feat: handler 改用 monitor_configs 表驱动监控和同步"
```

---

### Task 5: 在保单识别页面添加跳转入口

**Files:**
- Modify: `web/templates/insurance.html` — 顶栏添加链接

- [ ] **Step 1: 在 insurance.html 顶栏区域添加「监控配置」链接**

在页面 header 区域（标题旁边）添加一个跳转按钮：

```html
<a href="/monitor-config"
   class="inline-flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border"
   style="border-color: var(--border); color: var(--text-secondary);"
   onmouseover="this.style.borderColor='var(--primary)'; this.style.color='var(--primary)'"
   onmouseout="this.style.borderColor='var(--border)'; this.style.color='var(--text-secondary)'">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>
        <circle cx="12" cy="12" r="3"/>
    </svg>
    监控配置
</a>
```

- [ ] **Step 2: Commit**

```bash
git add web/templates/insurance.html
git commit -m "feat: 保单识别页顶栏添加监控配置入口"
```

---

### Task 6: 数据迁移 — 旧 watch_list 迁移到新表

**Files:**
- Modify: `insurance/monitor_config_db.py` — 添加迁移函数
- Modify: `main.py` — 启动时调用迁移

- [ ] **Step 1: 在 monitor_config_db.py 中添加迁移函数**

```python
def migrate_from_watch_list(db):
    """
    一次性迁移：将旧 insurance_config 中的 watch_list + dingtalk_targets 迁移到 monitor_configs 表。
    如果 monitor_configs 已有数据则跳过（说明已迁移过）。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT COUNT(*) as cnt FROM monitor_configs")
        if cursor.fetchone()["cnt"] > 0:
            return  # 已有数据，跳过迁移

        # 读取旧配置
        cursor.execute("SELECT config_value FROM insurance_config WHERE config_key = 'watch_list'")
        row = cursor.fetchone()
        if not row:
            return
        try:
            watch_list = json.loads(row["config_value"])
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(watch_list, list) or not watch_list:
            return

        # 读取旧钉钉目标
        cursor.execute("SELECT config_value FROM insurance_config WHERE config_key = 'dingtalk_targets'")
        row2 = cursor.fetchone()
        dingtalk_targets = []
        if row2:
            try:
                dingtalk_targets = json.loads(row2["config_value"])
            except (TypeError, json.JSONDecodeError):
                pass

        target_map = {t["id"]: t for t in dingtalk_targets if isinstance(t, dict) and t.get("id")}

        # 按 target_ids 分组：相同 target_ids 组合的监控项合并为一条 monitor_config
        from collections import defaultdict
        groups = defaultdict(lambda: {"rooms": [], "users": []})

        for w in watch_list:
            if not isinstance(w, dict) or not w.get("id"):
                continue
            # target_ids 列表转为排序后的 tuple 作为分组 key
            tids = tuple(sorted(w.get("target_ids", [])))
            if w.get("type") == "room":
                groups[tids]["rooms"].append({"id": w["id"], "name": w.get("name", w["id"])})
            elif w.get("type") == "user":
                groups[tids]["users"].append({"id": w["id"], "name": w.get("name", w["id"])})

        # 为每个分组创建一条 monitor_config
        idx = 0
        for tids, sources in groups.items():
            idx += 1
            # 取第一个目标的信息
            doc_url, base_id, sheet_id = "", "", ""
            if tids and tids[0] in target_map:
                t = target_map[tids[0]]
                doc_url = t.get("doc_url", "")
                base_id = t.get("base_id", "")
                sheet_id = t.get("sheet_id", "")

            cursor.execute("""
                INSERT INTO monitor_configs (name, enabled, rooms, users,
                    dingtalk_doc_url, dingtalk_base_id, dingtalk_sheet_id, field_mapping)
                VALUES (%s, 1, %s, %s, %s, %s, %s, %s)
            """, (
                f"迁移配置{idx}",
                json.dumps(sources["rooms"], ensure_ascii=False),
                json.dumps(sources["users"], ensure_ascii=False),
                doc_url, base_id, sheet_id,
                json.dumps({}, ensure_ascii=False),
            ))

        conn.commit()
        logger.info("已从旧 watch_list 迁移 %d 条监控配置到 monitor_configs", idx)
    except Exception as e:
        logger.warning("迁移旧监控配置失败（可忽略）: %s", e)
    finally:
        conn.close()
```

- [ ] **Step 2: 在 main.py 中调用迁移**

在 `init_monitor_config_table(db)` 之后添加：

```python
from insurance.monitor_config_db import migrate_from_watch_list
migrate_from_watch_list(db)
```

- [ ] **Step 3: Commit**

```bash
git add insurance/monitor_config_db.py main.py
git commit -m "feat: 旧 watch_list 配置自动迁移到 monitor_configs 表"
```
