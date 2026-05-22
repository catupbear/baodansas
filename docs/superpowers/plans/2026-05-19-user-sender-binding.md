# 员工账号绑定发送人 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在用户管理中为员工账号绑定企业微信发送人，使该发送人的所有保单记录自动归属到对应员工账号。

**Architecture:** users 表新增 `bound_senders` JSON 列存储绑定的发送人ID列表。记录入库时优先按 sender 匹配绑定关系设置 user_id。绑定时可选回填历史记录。

**Tech Stack:** Flask + MySQL + Vue 3 (CDN)

---

### Task 1: 数据库层 — users 表新增 bound_senders 列

**Files:**
- Modify: `auth/db.py:26-73` (init_users_table)
- Modify: `auth/db.py:103-125` (list_users)
- Modify: `auth/db.py:87-95` (get_user_by_id)

- [ ] **Step 1: 在 init_users_table 中新增列迁移**

在 `auth/db.py` 的 `init_users_table` 函数中，在旧列迁移之后、初始超管创建之前，添加 `bound_senders` 列：

```python
# 迁移：添加 bound_senders 列（幂等）
try:
    cursor.execute("""
        ALTER TABLE users ADD COLUMN bound_senders JSON DEFAULT NULL
        COMMENT '绑定的企业微信发送人ID列表'
    """)
    logger.info("users 表新增 bound_senders 列")
except Exception:
    pass  # 列已存在
```

- [ ] **Step 2: 在 list_users 和 get_user_by_id 的 SELECT 中加上 bound_senders**

`list_users` (行108):
```python
sql = "SELECT id, phone, role, parent_id, name, enabled, bound_senders, created_at, updated_at FROM users WHERE 1=1"
```

`get_user_by_id` (行92):
```python
cursor.execute("SELECT id, phone, role, parent_id, name, enabled, bound_senders, created_at, updated_at FROM users WHERE id = %s", (user_id,))
```

- [ ] **Step 3: 在 list_users 中反序列化 bound_senders**

在 `list_users` 的 `for row in rows:` 循环中添加：

```python
# 反序列化 bound_senders
if isinstance(row.get("bound_senders"), str):
    try:
        row["bound_senders"] = json.loads(row["bound_senders"])
    except (TypeError, json.JSONDecodeError):
        row["bound_senders"] = []
elif row.get("bound_senders") is None:
    row["bound_senders"] = []
```

同样在 `get_user_by_id` 返回前添加相同的反序列化逻辑。

需要在文件顶部添加 `import json`（如果还没有的话）。

- [ ] **Step 4: update_user 支持 bound_senders 字段**

在 `update_user` (行148) 中：

```python
allowed = {"name", "role", "parent_id", "enabled", "bound_senders"}
fields = {k: v for k, v in data.items() if k in allowed}
# bound_senders 需要 JSON 序列化
if "bound_senders" in fields and not isinstance(fields["bound_senders"], str):
    fields["bound_senders"] = json.dumps(fields["bound_senders"], ensure_ascii=False)
```

- [ ] **Step 5: 新增按 sender 查找绑定用户的函数**

在 `auth/db.py` 末尾添加：

```python
def find_user_by_sender(db, sender: str, enterprise_id: int = None) -> dict | None:
    """根据企业微信发送人ID查找绑定的用户"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # JSON_CONTAINS 检查 bound_senders 数组是否包含该 sender
        sql = """SELECT id, phone, role, parent_id, name, enabled, bound_senders
                 FROM users
                 WHERE enabled = 1
                   AND bound_senders IS NOT NULL
                   AND JSON_CONTAINS(bound_senders, %s)"""
        params = [json.dumps(sender)]
        if enterprise_id:
            sql += " AND parent_id = %s"
            params.append(enterprise_id)
        sql += " LIMIT 1"
        cursor.execute(sql, params)
        return cursor.fetchone()
    finally:
        conn.close()
```

- [ ] **Step 6: Commit**

```bash
git add auth/db.py
git commit -m "feat: users 表新增 bound_senders 列，支持绑定发送人查询"
```

---

### Task 2: API 层 — 绑定发送人接口 + 回填历史

**Files:**
- Modify: `auth/api.py:172-182` (api_update_user)
- Modify: `insurance/db.py` (新增回填函数)

- [ ] **Step 1: update_user API 已支持 bound_senders**

Task 1 中 `update_user` 已允许 `bound_senders` 字段，API 层 `api_update_user` 透传 data，无需额外改动。但需要在更新成功后触发回填。

修改 `auth/api.py` 的 `api_update_user`：

```python
@auth_bp.route("/api/auth/users/<int:user_id>", methods=["PUT"])
@admin_required
def api_update_user(user_id):
    """更新用户信息（name, role, parent_id, enabled, bound_senders）"""
    data = request.get_json(silent=True) or {}
    try:
        update_user(_db, user_id, data)
        # 如果更新了 bound_senders，回填历史保单记录
        if "bound_senders" in data:
            _backfill_sender_binding(user_id, data["bound_senders"])
        return jsonify({"code": 0})
    except Exception as e:
        logger.exception("更新用户 %d 失败", user_id)
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 2: 在 auth/api.py 中实现回填函数**

```python
def _backfill_sender_binding(user_id: int, senders: list):
    """将指定 sender 的历史保单记录关联到该用户"""
    if not senders or not _insurance_db:
        return
    user = get_user_by_id(_db, user_id)
    if not user:
        return
    enterprise_id = user.get("parent_id")
    try:
        conn = _insurance_db.pool.connection()
        try:
            cursor = conn.cursor()
            placeholders = ",".join(["%s"] * len(senders))
            # 只回填同企业下未绑定用户的记录
            sql = f"""UPDATE insurance_records
                      SET user_id = %s
                      WHERE sender IN ({placeholders})
                        AND (user_id IS NULL OR user_id = %s)"""
            params = [user_id] + list(senders) + [user_id]
            if enterprise_id:
                sql += " AND enterprise_id = %s"
                params.append(enterprise_id)
            cursor.execute(sql, params)
            cnt = cursor.rowcount
            conn.commit()
            if cnt:
                logger.info("回填 %d 条保单记录到用户 %d (senders=%s)", cnt, user_id, senders)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("回填发送人绑定失败: %s", e)
```

- [ ] **Step 3: 在 auth/api.py 中注入 insurance db 引用**

在 `auth/api.py` 顶部添加模块级变量和初始化函数：

```python
_insurance_db = None

def set_insurance_db(db):
    """由 main.py 注入 insurance 模块的数据库实例"""
    global _insurance_db
    _insurance_db = db
```

在 `main.py` 中初始化 auth 模块时调用 `set_insurance_db(db)`。

- [ ] **Step 4: 新增获取发送人列表的 API**

在 `auth/api.py` 中新增接口，用于前端绑定时选择发送人：

```python
@auth_bp.route("/api/auth/senders", methods=["GET"])
@admin_required
def api_list_senders():
    """获取保单记录中的所有发送人列表（去重）"""
    if not _insurance_db:
        return jsonify({"code": 0, "data": []})
    try:
        conn = _insurance_db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT sender, sender_name, COUNT(*) as record_count
                FROM insurance_records
                WHERE sender IS NOT NULL AND sender != ''
                GROUP BY sender, sender_name
                ORDER BY record_count DESC
            """)
            return jsonify({"code": 0, "data": cursor.fetchall()})
        finally:
            conn.close()
    except Exception as e:
        logger.exception("获取发送人列表失败")
        return jsonify({"code": 500, "msg": str(e)}), 500
```

- [ ] **Step 5: Commit**

```bash
git add auth/api.py
git commit -m "feat: 绑定发送人API + 历史记录回填 + 发送人列表接口"
```

---

### Task 3: main.py 注入 insurance db 到 auth 模块

**Files:**
- Modify: `main.py`

- [ ] **Step 1: 在 main.py 中调用 set_insurance_db**

找到 auth blueprint 注册的位置附近，添加：

```python
from auth.api import set_insurance_db
set_insurance_db(db)
```

其中 `db` 是已有的 `storage.db.Database` 实例。

- [ ] **Step 2: Commit**

```bash
git add main.py
git commit -m "feat: main.py 注入 insurance db 到 auth 模块"
```

---

### Task 4: 消息入库时按 sender 绑定自动设置 user_id

**Files:**
- Modify: `insurance/handler.py:370-380`

- [ ] **Step 1: 在 handler.py 中引入 find_user_by_sender**

在文件顶部添加导入：

```python
from auth.db import find_user_by_sender
```

- [ ] **Step 2: 修改 user_id 赋值逻辑**

在 `handler.py` 第 370-380 行附近，获取 `config_user_id` 之后，增加 sender 绑定查找：

```python
# 2. 获取匹配的监控配置，提取绑定的 user_id 和 enterprise_id
matched_monitors = self._get_matched_monitors(roomid, sender)
config_user_id = None
config_enterprise_id = None
for m in matched_monitors:
    if m.get("user_id"):
        config_user_id = m["user_id"]
    if m.get("enterprise_id"):
        config_enterprise_id = m["enterprise_id"]
    if config_user_id:
        break

# 2.5 按 sender 绑定关系查找用户（优先级高于监控配置）
try:
    bound_user = find_user_by_sender(self.db, sender, config_enterprise_id)
    if bound_user:
        config_user_id = bound_user["id"]
        if not config_enterprise_id and bound_user.get("parent_id"):
            config_enterprise_id = bound_user["parent_id"]
except Exception as e:
    logger.warning("查找 sender 绑定用户失败: %s", e)
```

- [ ] **Step 3: Commit**

```bash
git add insurance/handler.py
git commit -m "feat: 保单入库时按sender绑定关系自动设置user_id"
```

---

### Task 5: 前端 — 用户管理页面添加绑定发送人功能

**Files:**
- Modify: `web/templates/admin_users.html`

- [ ] **Step 1: 编辑用户弹窗中添加绑定发送人区域**

在编辑用户弹窗的"所属企业"下拉之后（约行259），添加绑定发送人区域：

```html
<!-- 绑定发送人（企业管理员和员工） -->
<div v-if="form.role === 'employee' || form.role === 'enterprise'">
    <label class="block text-sm text-gray-500 mb-1">绑定发送人</label>
    <div class="space-y-2">
        <!-- 已绑定列表 -->
        <div v-for="(sid, idx) in form.bound_senders" :key="idx"
             class="flex items-center gap-2 bg-gray-50 rounded-lg px-3 py-1.5">
            <span class="text-sm text-gray-700 flex-1">{{ getSenderLabel(sid) }}</span>
            <button type="button" @click="form.bound_senders.splice(idx, 1)"
                    class="text-gray-400 hover:text-red-500 text-xs cursor-pointer">&times;</button>
        </div>
        <!-- 添加发送人下拉 -->
        <select @change="addSender($event)" class="form-select" style="height:34px;font-size:13px;">
            <option value="">+ 添加发送人</option>
            <option v-for="s in availableSenders" :key="s.sender" :value="s.sender">
                {{ s.sender_name || s.sender }} ({{ s.record_count }}条记录)
            </option>
        </select>
    </div>
</div>
```

- [ ] **Step 2: JS 中添加发送人相关状态和函数**

在 `setup()` 中添加：

```javascript
/* 发送人绑定 */
const allSenders = ref([]);
async function fetchSenders() {
    try {
        const resp = await authFetch('/api/auth/senders');
        const data = await resp.json();
        if (data.code === 0) allSenders.value = data.data || [];
    } catch {}
}

// 过滤掉已绑定的发送人
const availableSenders = computed(() => {
    const bound = new Set(form.bound_senders || []);
    return allSenders.value.filter(s => !bound.has(s.sender));
});

function getSenderLabel(senderId) {
    const s = allSenders.value.find(x => x.sender === senderId);
    return s ? `${s.sender_name || senderId} (${s.record_count}条)` : senderId;
}

function addSender(event) {
    const val = event.target.value;
    if (val && !form.bound_senders.includes(val)) {
        form.bound_senders.push(val);
    }
    event.target.value = '';
}
```

需要从 Vue 导入 `computed`：
```javascript
const { createApp, ref, reactive, computed, onMounted } = Vue;
```

- [ ] **Step 3: 修改 form 初始值和弹窗打开逻辑**

`form` reactive 对象添加 `bound_senders`：
```javascript
const form = reactive({ phone: '', password: '', name: '', role: 'employee', parent_id: null, bound_senders: [] });
```

`openCreateModal` 中：
```javascript
form.bound_senders = [];
fetchSenders();  // 加上这行
```

`openEditModal(user)` 中：
```javascript
form.bound_senders = Array.isArray(user.bound_senders) ? [...user.bound_senders] : [];
fetchSenders();  // 加上这行
```

- [ ] **Step 4: submitUser 提交时带上 bound_senders**

编辑模式的 body 中添加 `bound_senders`：
```javascript
if (isEdit.value) {
    body = {
        name: form.name,
        role: form.role,
        parent_id: (form.role === 'employee' || form.role === 'enterprise') ? form.parent_id : null,
        bound_senders: form.bound_senders,
    };
}
```

创建模式的 body 中也添加：
```javascript
body = {
    phone: form.phone.trim(),
    password: form.password,
    name: form.name,
    role: form.role,
    parent_id: (form.role === 'employee' || form.role === 'enterprise') ? form.parent_id : null,
    bound_senders: form.bound_senders,
};
```

- [ ] **Step 5: 用户列表表格显示绑定的发送人**

在表格中"所属企业"列后面添加一列：

```html
<th>绑定发送人</th>
```

对应的 `<td>`:
```html
<td>
    <div class="flex flex-wrap gap-1">
        <span v-for="sid in (user.bound_senders || [])" :key="sid"
              class="inline-block text-xs bg-blue-50 text-blue-600 px-2 py-0.5 rounded-full">
            {{ getSenderLabel(sid) }}
        </span>
        <span v-if="!user.bound_senders || user.bound_senders.length === 0" class="text-gray-400">-</span>
    </div>
</td>
```

- [ ] **Step 6: return 中暴露新变量**

在 `setup()` 的 return 对象中添加：
```javascript
allSenders, availableSenders, getSenderLabel, addSender,
```

- [ ] **Step 7: Commit**

```bash
git add web/templates/admin_users.html
git commit -m "feat: 用户管理页面支持绑定/解绑发送人"
```

---

### Task 6: create_user 也支持 bound_senders

**Files:**
- Modify: `auth/db.py:128-140` (create_user)
- Modify: `auth/api.py:130-169` (api_create_user)

- [ ] **Step 1: create_user 函数增加 bound_senders 参数**

```python
def create_user(db, phone: str, password: str, role: str, parent_id: int = None, name: str = "", bound_senders: list = None) -> int:
    """创建用户，返回新用户 ID"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        bs_json = json.dumps(bound_senders, ensure_ascii=False) if bound_senders else None
        cursor.execute(
            "INSERT INTO users (phone, password_hash, role, parent_id, name, bound_senders) VALUES (%s, %s, %s, %s, %s, %s)",
            (phone, generate_password_hash(password), role, parent_id, name, bs_json),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()
```

- [ ] **Step 2: api_create_user 传入 bound_senders 并回填**

```python
bound_senders = data.get("bound_senders", [])

try:
    user_id = create_user(_db, phone, password, role, parent_id, name, bound_senders)
    # 回填历史记录
    if bound_senders:
        _backfill_sender_binding(user_id, bound_senders)
    return jsonify({"code": 0, "data": {"id": user_id}})
```

- [ ] **Step 3: Commit**

```bash
git add auth/db.py auth/api.py
git commit -m "feat: 创建用户时也支持绑定发送人 + 回填"
```
