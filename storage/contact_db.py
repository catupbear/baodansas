"""
统一联系人信息表（wecom_contacts）数据层
全系统联系人（内部员工 + 外部联系人）昵称的唯一权威来源（Single Source of Truth）。

写入方：task_contact_sync.py 独立定时任务（增量发现 + 全量刷新）
读取方：core/contacts.py 的 get_name()，优先查本表，查不到再退回 contacts_cache 永久缓存

写入规则（与 storage/group_db.py 的 wecom_groups 一致）：
- 拉到昵称且有变化   → 更新 name，记 name_updated_at，fail_count 清零
- 拉到昵称且无变化   → 只更新 last_synced_at
- 拉取失败          → 不覆盖旧昵称，fail_count+1；连续失败 >= 5 次退避为每天试 1 次
- 联系人不存在/已离职 → 保留最后已知昵称，标记 unresolvable，不再高频重试
"""

import logging

import pymysql.cursors

logger = logging.getLogger(__name__)

# 连续失败达到该次数后进入退避（每天只重试 1 次）
FAIL_BACKOFF_THRESHOLD = 5


def _get_table_collation(cursor, table_name: str) -> str:
    cursor.execute(
        "SELECT TABLE_COLLATION FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table_name,),
    )
    row = cursor.fetchone()
    return (row[0] if row else "") or ""


def init_contact_table(db):
    """创建统一联系人信息表（幂等）。排序规则与 messages 表对齐。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        collation = _get_table_collation(cursor, "messages") or "utf8mb4_unicode_ci"
        charset = collation.split("_")[0]
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS wecom_contacts (
                userid          VARCHAR(128) NOT NULL COMMENT '联系人ID（员工userid或外部external_userid）',
                corpid          VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '解析成功的企业corpid',
                name            VARCHAR(256) NOT NULL DEFAULT '' COMMENT '昵称（权威值）',
                contact_type    VARCHAR(16)  DEFAULT '' COMMENT 'user内部员工/external外部联系人',
                sync_status     VARCHAR(16)  DEFAULT 'pending' COMMENT 'ok/failed/unresolvable/pending',
                fail_count      INT          DEFAULT 0 COMMENT '连续失败次数（用于退避）',
                first_seen_at   DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '首次发现时间',
                name_updated_at DATETIME     NULL COMMENT '昵称最近变化时间',
                last_attempt_at DATETIME     NULL COMMENT '最近一次尝试同步时间',
                last_synced_at  DATETIME     NULL COMMENT '最近一次成功同步时间',
                PRIMARY KEY (userid),
                KEY idx_sync (sync_status, last_attempt_at)
            ) ENGINE=InnoDB DEFAULT CHARSET={charset} COLLATE={collation}
              COMMENT='统一联系人信息表（task_contact_sync维护）'
        """)
        conn.commit()
        current = _get_table_collation(cursor, "wecom_contacts")
        if current and current != collation:
            logger.info("wecom_contacts 排序规则 %s 与 messages(%s) 不一致，自动转换", current, collation)
            cursor.execute(
                f"ALTER TABLE wecom_contacts CONVERT TO CHARACTER SET {charset} COLLATE {collation}"
            )
            conn.commit()
    finally:
        conn.close()


def discover_new_contacts(db) -> int:
    """
    增量发现：把 messages 里新出现、wecom_contacts 还没有的 sender 插入为 pending。
    顺带把 contacts_cache 里已有的昵称带过来作为初始值（避免存量联系人等一轮全量刷新才有昵称）。
    返回新发现的联系人数量。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT IGNORE INTO wecom_contacts (userid, name, contact_type, sync_status)
            SELECT m.sender,
                   COALESCE(NULLIF(NULLIF(c.name, '__unresolvable__'), ''), ''),
                   CASE WHEN m.sender LIKE 'wm%' OR m.sender LIKE 'wo%' THEN 'external' ELSE 'user' END,
                   'pending'
            FROM (SELECT DISTINCT sender FROM messages
                  WHERE sender IS NOT NULL AND sender != '') m
            LEFT JOIN contacts_cache c
                   ON c.id = m.sender AND c.type = 'user'
            WHERE m.sender NOT IN (SELECT userid FROM wecom_contacts)
              AND m.sender NOT LIKE 'wb%'
        """)
        inserted = cursor.rowcount
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_contacts_to_sync(db, full: bool = False, limit: int = 500, force: bool = False,
                         corpids: list = None) -> list:
    """取待同步的联系人列表，规则与 group_db.get_groups_to_sync 一致。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        corp_filter, params = "", []
        if corpids:
            ph = ",".join(["%s"] * len(corpids))
            corp_filter = f"AND corpid IN ({ph})"
            params = list(corpids)
        if full and force:
            cursor.execute(f"""
                SELECT userid, corpid, name, contact_type FROM wecom_contacts
                WHERE 1=1 {corp_filter}
                ORDER BY last_attempt_at IS NULL DESC, last_attempt_at ASC
                LIMIT %s
            """, params + [limit])
        elif full:
            cursor.execute(f"""
                SELECT userid, corpid, name, contact_type FROM wecom_contacts
                WHERE NOT (
                    (sync_status = 'unresolvable' OR fail_count >= {FAIL_BACKOFF_THRESHOLD})
                    AND last_attempt_at IS NOT NULL
                    AND last_attempt_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
                ) {corp_filter}
                ORDER BY last_attempt_at IS NULL DESC, last_attempt_at ASC
                LIMIT %s
            """, params + [limit])
        else:
            # 增量（pending）不按 corp_filter 过滤：discover_new_contacts 是联系人的唯一
            # 发现来源，新发现的联系人 corpid 为空（不像群还有客户群列表 API 能带上 corpid），
            # 过滤会导致新联系人永远进不了同步、昵称一直空着
            cursor.execute(
                "SELECT userid, corpid, name, contact_type FROM wecom_contacts "
                "WHERE sync_status = 'pending' "
                "ORDER BY first_seen_at ASC LIMIT %s",
                [limit],
            )
        return list(cursor.fetchall())
    finally:
        conn.close()


def update_contact_success(db, userid: str, corpid: str, name: str) -> bool:
    """同步成功：更新联系人信息。昵称有变化时记 name_updated_at。返回昵称是否发生了变化。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT name FROM wecom_contacts WHERE userid = %s", (userid,))
        row = cursor.fetchone()
        old_name = (row["name"] if row else "") or ""
        name_changed = bool(name) and name != old_name

        if name_changed:
            cursor.execute("""
                UPDATE wecom_contacts
                SET corpid = %s, name = %s, sync_status = 'ok', fail_count = 0,
                    name_updated_at = NOW(), last_attempt_at = NOW(), last_synced_at = NOW()
                WHERE userid = %s
            """, (corpid, name, userid))
        else:
            cursor.execute("""
                UPDATE wecom_contacts
                SET corpid = %s, sync_status = 'ok', fail_count = 0,
                    last_attempt_at = NOW(), last_synced_at = NOW()
                WHERE userid = %s
            """, (corpid, userid))
        conn.commit()
        return name_changed
    finally:
        conn.close()


def update_contact_failure(db, userid: str, permanent: bool = False):
    """同步失败：不覆盖旧昵称，只累计失败次数。permanent=True 表示联系人不存在/已离职。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        status = "unresolvable" if permanent else "failed"
        cursor.execute("""
            UPDATE wecom_contacts
            SET sync_status = %s, fail_count = fail_count + 1, last_attempt_at = NOW()
            WHERE userid = %s
        """, (status, userid))
        conn.commit()
    finally:
        conn.close()


def get_contact_name(db, userid: str) -> str:
    """取单个联系人昵称，查不到返回空字符串。"""
    if not userid:
        return ""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT name FROM wecom_contacts WHERE userid = %s AND name != ''", (userid,)
        )
        row = cursor.fetchone()
        return row["name"] if row else ""
    finally:
        conn.close()


def get_contact_name_map(db, userids: list) -> dict:
    """批量取联系人昵称，返回 {userid: name}（只含有昵称的联系人）。"""
    ids = [u for u in set(userids or []) if u]
    if not ids:
        return {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        ph = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"SELECT userid, name FROM wecom_contacts WHERE userid IN ({ph}) AND name != ''",
            ids,
        )
        return {r["userid"]: r["name"] for r in cursor.fetchall()}
    finally:
        conn.close()


def get_sync_stats(db) -> dict:
    """同步状态统计（运维观测用）：{status: count}"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT sync_status, COUNT(*) AS cnt FROM wecom_contacts GROUP BY sync_status"
        )
        return {r["sync_status"]: r["cnt"] for r in cursor.fetchall()}
    finally:
        conn.close()
