"""
统一群信息表（wecom_groups）数据层
全系统群名的唯一权威来源（Single Source of Truth）。

写入方：task_group_sync.py 独立定时任务（增量发现 + 全量刷新）
读取方：监控配置页群列表、台账筛选下拉、台账列表群名、保单入库取名

写入规则（保证数据不杂乱的关键）：
- 拉到群名且有变化   → 更新 name，记 name_updated_at，fail_count 清零
- 拉到群名且无变化   → 只更新 last_synced_at
- 拉取失败          → 不覆盖旧群名，fail_count+1；连续失败 >= 5 次退避为每天试 1 次
- 群不存在/已解散    → 保留最后已知群名，标记 unresolvable，不再高频重试
"""

import logging

import pymysql.cursors

logger = logging.getLogger(__name__)

# 连续失败达到该次数后进入退避（每天只重试 1 次）
FAIL_BACKOFF_THRESHOLD = 5


def _get_table_collation(cursor, table_name: str) -> str:
    """查当前库中某张表的排序规则，不存在返回空"""
    cursor.execute(
        "SELECT TABLE_COLLATION FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table_name,),
    )
    row = cursor.fetchone()
    return (row[0] if row else "") or ""


def init_group_table(db):
    """创建统一群信息表（幂等）。
    排序规则与 messages 表对齐（roomid 会与 messages/insurance_records JOIN，
    不对齐会报 Illegal mix of collations）；已存在的旧表规则不一致时自动转换。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        collation = _get_table_collation(cursor, "messages") or "utf8mb4_unicode_ci"
        charset = collation.split("_")[0]
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS wecom_groups (
                roomid          VARCHAR(128) NOT NULL COMMENT '群ID',
                corpid          VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '解析成功的企业corpid',
                name            VARCHAR(256) NOT NULL DEFAULT '' COMMENT '群名（权威值）',
                owner           VARCHAR(128) DEFAULT '' COMMENT '群主userid（客户群接口可得）',
                member_count    INT          DEFAULT 0 COMMENT '成员数',
                group_type      VARCHAR(16)  DEFAULT '' COMMENT 'internal内部群/external客户群',
                sync_status     VARCHAR(16)  DEFAULT 'pending' COMMENT 'ok/failed/unresolvable/pending',
                fail_count      INT          DEFAULT 0 COMMENT '连续失败次数（用于退避）',
                first_seen_at   DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '首次发现时间',
                name_updated_at DATETIME     NULL COMMENT '群名最近变化时间',
                last_attempt_at DATETIME     NULL COMMENT '最近一次尝试同步时间',
                last_synced_at  DATETIME     NULL COMMENT '最近一次成功同步时间',
                PRIMARY KEY (roomid),
                KEY idx_sync (sync_status, last_attempt_at)
            ) ENGINE=InnoDB DEFAULT CHARSET={charset} COLLATE={collation}
              COMMENT='统一群信息表（task_group_sync维护）'
        """)
        conn.commit()
        # 旧表排序规则不一致时自动转换（表很小，秒级完成）
        current = _get_table_collation(cursor, "wecom_groups")
        if current and current != collation:
            logger.info("wecom_groups 排序规则 %s 与 messages(%s) 不一致，自动转换", current, collation)
            cursor.execute(
                f"ALTER TABLE wecom_groups CONVERT TO CHARACTER SET {charset} COLLATE {collation}"
            )
            conn.commit()
    finally:
        conn.close()


def discover_new_rooms(db) -> int:
    """
    增量发现：把 messages 里新出现、wecom_groups 还没有的 roomid 插入为 pending。
    顺带把 contacts_cache 里已有的群名带过来作为初始值（避免存量群等一轮全量刷新才有名字）。
    返回新发现的群数量。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        # contacts_cache 里额外企业的群 key 带 "corpid:" 前缀，SUBSTRING_INDEX 取后半段匹配
        cursor.execute("""
            INSERT IGNORE INTO wecom_groups (roomid, name, sync_status)
            SELECT m.roomid,
                   COALESCE(NULLIF(NULLIF(c.name, '__unresolvable__'), ''), ''),
                   'pending'
            FROM (SELECT DISTINCT roomid FROM messages
                  WHERE roomid IS NOT NULL AND roomid != '') m
            LEFT JOIN contacts_cache c
                   ON c.id = m.roomid AND c.type = 'room'
            WHERE m.roomid NOT IN (SELECT roomid FROM wecom_groups)
        """)
        inserted = cursor.rowcount
        conn.commit()
        return inserted
    finally:
        conn.close()


def insert_room_ids(db, roomids: list, corpid: str = "", group_type: str = "") -> int:
    """
    批量插入群 ID（已存在的忽略），供客户群列表发现使用。
    新群以 pending 状态入表，由增量解析补齐群名等详情。
    返回新插入的数量。
    """
    ids = [r for r in set(roomids or []) if r]
    if not ids:
        return 0
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT IGNORE INTO wecom_groups (roomid, corpid, group_type, sync_status) "
            "VALUES (%s, %s, %s, 'pending')",
            [(rid, corpid, group_type) for rid in ids],
        )
        inserted = cursor.rowcount
        conn.commit()
        return inserted
    finally:
        conn.close()


def cleanup_pending_external(db, allowed_corpids: list) -> int:
    """
    清理不在白名单企业的、由客户群列表发现插入的待解析群
    （sync_status='pending' 且 group_type='external' 只可能来自列表发现；
    messages 增量发现插入的群 group_type 为空，不受影响）。
    返回删除数量。
    """
    if not allowed_corpids:
        return 0
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        ph = ",".join(["%s"] * len(allowed_corpids))
        cursor.execute(
            f"DELETE FROM wecom_groups "
            f"WHERE sync_status = 'pending' AND group_type = 'external' "
            f"AND corpid NOT IN ({ph})",
            allowed_corpids,
        )
        deleted = cursor.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def get_groups_to_sync(db, full: bool = False, limit: int = 500, force: bool = False,
                       corpids: list = None) -> list:
    """
    取待同步的群列表。
    full=False（增量）：只取 pending 状态的新群。
    full=True（全量刷新）：取待同步的群，排除
      - unresolvable（已确认解散/不存在，每天试 1 次）
      - 连续失败 >= 阈值 且 24 小时内已尝试过的（退避）
    force=True（强制全量，启动时用）：忽略退避。
    corpids 非空时（全量）：严格只取这些企业名下的群（corpid 匹配，
      客户群列表发现插入的群带 corpid，会被包含）。messages 增量发现的
      corpid 为空的 pending 群由增量轮解析，解析成功后 corpid 补上才进全量。
    """
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
                SELECT roomid, corpid, name FROM wecom_groups
                WHERE 1=1 {corp_filter}
                ORDER BY last_attempt_at IS NULL DESC, last_attempt_at ASC
                LIMIT %s
            """, params + [limit])
        elif full:
            cursor.execute(f"""
                SELECT roomid, corpid, name FROM wecom_groups
                WHERE NOT (
                    (sync_status = 'unresolvable' OR fail_count >= {FAIL_BACKOFF_THRESHOLD})
                    AND last_attempt_at IS NOT NULL
                    AND last_attempt_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
                ) {corp_filter}
                ORDER BY last_attempt_at IS NULL DESC, last_attempt_at ASC
                LIMIT %s
            """, params + [limit])
        else:
            cursor.execute(
                f"SELECT roomid, corpid, name FROM wecom_groups "
                f"WHERE sync_status = 'pending' {corp_filter} "
                f"ORDER BY first_seen_at ASC LIMIT %s",
                params + [limit],
            )
        return list(cursor.fetchall())
    finally:
        conn.close()


def update_group_success(db, roomid: str, corpid: str, name: str,
                         owner: str = "", member_count: int = 0,
                         group_type: str = "") -> bool:
    """
    同步成功：更新群信息。群名有变化时记 name_updated_at。
    返回群名是否发生了变化。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT name FROM wecom_groups WHERE roomid = %s", (roomid,))
        row = cursor.fetchone()
        old_name = (row["name"] if row else "") or ""
        name_changed = bool(name) and name != old_name

        if name_changed:
            cursor.execute("""
                UPDATE wecom_groups
                SET corpid = %s, name = %s, owner = %s, member_count = %s,
                    group_type = %s, sync_status = 'ok', fail_count = 0,
                    name_updated_at = NOW(), last_attempt_at = NOW(), last_synced_at = NOW()
                WHERE roomid = %s
            """, (corpid, name, owner, member_count, group_type, roomid))
        else:
            cursor.execute("""
                UPDATE wecom_groups
                SET corpid = %s, owner = %s, member_count = %s,
                    group_type = %s, sync_status = 'ok', fail_count = 0,
                    last_attempt_at = NOW(), last_synced_at = NOW()
                WHERE roomid = %s
            """, (corpid, owner, member_count, group_type, roomid))
        conn.commit()
        return name_changed
    finally:
        conn.close()


def update_group_failure(db, roomid: str, permanent: bool = False):
    """同步失败：不覆盖旧群名，只累计失败次数。permanent=True 表示群不存在/已解散。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        status = "unresolvable" if permanent else "failed"
        cursor.execute("""
            UPDATE wecom_groups
            SET sync_status = %s, fail_count = fail_count + 1, last_attempt_at = NOW()
            WHERE roomid = %s
        """, (status, roomid))
        conn.commit()
    finally:
        conn.close()


def get_group(db, roomid: str) -> dict | None:
    """取单个群的完整记录，不存在返回 None。
    事件回调刷新群名时用它保留已有的 owner/member_count，避免被清零。"""
    if not roomid:
        return None
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM wecom_groups WHERE roomid = %s", (roomid,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_group_name_map(db, roomids: list) -> dict:
    """批量取群名，返回 {roomid: name}（只含有名字的群）。"""
    ids = [r for r in set(roomids or []) if r]
    if not ids:
        return {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        ph = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"SELECT roomid, name FROM wecom_groups WHERE roomid IN ({ph}) AND name != ''",
            ids,
        )
        return {r["roomid"]: r["name"] for r in cursor.fetchall()}
    finally:
        conn.close()


def get_sync_stats(db) -> dict:
    """同步状态统计（运维观测用）：{status: count}"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT sync_status, COUNT(*) AS cnt FROM wecom_groups GROUP BY sync_status"
        )
        return {r["sync_status"]: r["cnt"] for r in cursor.fetchall()}
    finally:
        conn.close()
