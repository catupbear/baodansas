# -*- coding: utf-8 -*-
"""
续保推送群绑定 — 每个企业绑定若干群，续保到期提醒推送到这些群。
与「监控入库」(monitor_configs) 完全隔离：这里只管"提醒发到哪些群"，不触发任何 OCR/入库。
"""
import logging

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)


def init_renewal_push_table(db):
    """创建 renewal_push_configs 表（幂等）。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS renewal_push_configs (
                id            INT PRIMARY KEY AUTO_INCREMENT,
                enterprise_id INT NOT NULL COMMENT '企业ID',
                roomid        VARCHAR(128) NOT NULL COMMENT '续保提醒推送群 roomid',
                room_name     VARCHAR(256) DEFAULT '' COMMENT '群名快照(展示用,以wecom_groups为准)',
                enabled       TINYINT NOT NULL DEFAULT 1,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_ent_room (enterprise_id, roomid),
                KEY idx_ent (enterprise_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='续保推送群绑定(企业↔群,一企业可多群)'
        """)
        conn.commit()
        logger.info("renewal_push_configs 表初始化完成")
    finally:
        conn.close()


def get_push_rooms(db, enterprise_id) -> list:
    """某企业绑定的续保推送群 [{roomid, room_name, enabled}]（含未启用，供管理页展示）。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT roomid, room_name, enabled FROM renewal_push_configs "
            "WHERE enterprise_id=%s ORDER BY id",
            (enterprise_id,))
        return cursor.fetchall()
    finally:
        conn.close()


def set_push_rooms(db, enterprise_id, rooms: list):
    """覆盖式保存某企业的续保推送群。rooms=[{roomid, room_name}]。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM renewal_push_configs WHERE enterprise_id=%s", (enterprise_id,))
        for r in rooms or []:
            rid = (r.get("roomid") or "").strip()
            if not rid:
                continue
            cursor.execute(
                "INSERT INTO renewal_push_configs (enterprise_id, roomid, room_name) VALUES (%s,%s,%s)",
                (enterprise_id, rid, (r.get("room_name") or "").strip()))
        conn.commit()
    finally:
        conn.close()


def get_enabled_rooms_by_enterprise(db, enterprise_id) -> list:
    """某企业启用的推送群 [{roomid, room_name}]（供 policy_expiry_reminder 推送用）。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT roomid, room_name FROM renewal_push_configs "
            "WHERE enterprise_id=%s AND enabled=1 ORDER BY id",
            (enterprise_id,))
        return cursor.fetchall()
    finally:
        conn.close()
