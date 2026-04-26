"""
MySQL 数据存储模块
存储消息记录和 seq 游标，使用 PooledDB 连接池管理数据库连接
"""

import json
import logging
from datetime import datetime

import pymysql
import pymysql.cursors
from dbutils.pooled_db import PooledDB

logger = logging.getLogger(__name__)


class Database:
    """MySQL 数据库操作，基于 PooledDB 连接池"""

    def __init__(self, db_config: dict):
        """
        初始化数据库连接池。
        db_config 包含: host, port, user, password, database
        """
        self.pool = PooledDB(
            creator=pymysql,
            maxconnections=0,   # 无限制
            mincached=1,        # 初始化时建立的连接数
            maxcached=5,        # 最多缓存的连接数
            maxshared=0,        # 不共享连接
            blocking=True,      # 连接耗尽时阻塞等待
            maxusage=None,      # 连接使用次数不限
            setsession=[],
            ping=1,             # 使用前检测连接活性
            host=db_config.get('host', '127.0.0.1'),
            port=db_config.get('port', 3306),
            user=db_config.get('user', 'root'),
            password=db_config.get('password', ''),
            database=db_config.get('database', 'pdfocr'),
            charset='utf8mb4',
        )
        self._init_tables()
        logger.info(
            "数据库连接池初始化完成: %s@%s:%s/%s",
            db_config.get('user'),
            db_config.get('host', '127.0.0.1'),
            db_config.get('port', 3306),
            db_config.get('database', 'pdfocr'),
        )

    def _init_tables(self):
        """创建数据库表（MySQL 语法）"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # 消息表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    seq INT UNIQUE,
                    msgid VARCHAR(128),
                    action VARCHAR(64),
                    sender VARCHAR(128),
                    tolist TEXT,
                    roomid VARCHAR(128),
                    msgtime BIGINT,
                    msgtype VARCHAR(64),
                    summary TEXT,
                    raw_data LONGTEXT,
                    parsed_content LONGTEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # seq 游标表（id 固定为 1）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cursor_state (
                    id INT PRIMARY KEY,
                    seq INT DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
            """)

            # 初始化游标（如果不存在）
            cursor.execute("""
                INSERT IGNORE INTO cursor_state (id, seq) VALUES (1, 0)
            """)

            # 通讯录缓存表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contacts_cache (
                    id VARCHAR(128) PRIMARY KEY,
                    type VARCHAR(32),
                    name VARCHAR(128),
                    avatar VARCHAR(512) DEFAULT '',
                    extra TEXT,
                    updated_at INT
                )
            """)

            # 索引（忽略已存在的索引错误）
            for idx_sql in [
                "CREATE INDEX idx_messages_msgtime ON messages(msgtime)",
                "CREATE INDEX idx_messages_msgtype ON messages(msgtype)",
                "CREATE INDEX idx_messages_sender ON messages(sender)",
                "CREATE INDEX idx_messages_roomid ON messages(roomid)",
            ]:
                try:
                    cursor.execute(idx_sql)
                except Exception:
                    pass  # 索引已存在，忽略

            conn.commit()
        finally:
            conn.close()

    def get_seq(self) -> int:
        """获取当前 seq 游标"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT seq FROM cursor_state WHERE id = 1")
            row = cursor.fetchone()
            return row["seq"] if row else 0
        finally:
            conn.close()

    def update_seq(self, seq: int):
        """更新 seq 游标"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "UPDATE cursor_state SET seq = %s, updated_at = %s WHERE id = 1",
                (seq, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
        finally:
            conn.close()

    def save_message(self, seq: int, raw_data: dict, parsed: dict):
        """保存一条消息，seq 重复时忽略（INSERT IGNORE）"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                INSERT IGNORE INTO messages
                (seq, msgid, action, sender, tolist, roomid, msgtime, msgtype, summary, raw_data, parsed_content)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                seq,
                parsed.get("msgid", ""),
                parsed.get("action", ""),
                parsed.get("from", ""),
                json.dumps(parsed.get("tolist", []), ensure_ascii=False),
                parsed.get("roomid", ""),
                parsed.get("msgtime", 0),
                parsed.get("msgtype", ""),
                parsed.get("summary", ""),
                json.dumps(raw_data, ensure_ascii=False),
                json.dumps(parsed.get("content", {}), ensure_ascii=False),
            ))
            conn.commit()
        except Exception as e:
            logger.error("保存消息失败, seq=%d: %s", seq, e)
        finally:
            conn.close()

    def get_recent_messages(self, limit: int = 50) -> list:
        """获取最近的消息"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT * FROM messages ORDER BY msgtime DESC LIMIT %s",
                (limit,)
            )
            return list(cursor.fetchall())
        finally:
            conn.close()

    def query_messages(self, page: int = 1, page_size: int = 50,
                       msgtype: str = "", sender: str = "",
                       roomid: str = "", keyword: str = "") -> dict:
        """
        分页查询消息，支持筛选。
        返回: {"total": 总数, "pages": 总页数, "page": 当前页, "messages": [...]}
        """
        conditions = []
        params = []

        if msgtype:
            conditions.append("msgtype = %s")
            params.append(msgtype)
        if sender:
            conditions.append("sender LIKE %s")
            params.append(f"%{sender}%")
        if roomid:
            conditions.append("roomid LIKE %s")
            params.append(f"%{roomid}%")
        if keyword:
            conditions.append("summary LIKE %s")
            params.append(f"%{keyword}%")

        where = " AND ".join(conditions)
        where_clause = f"WHERE {where}" if where else ""

        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # 查询总数
            cursor.execute(
                f"SELECT COUNT(*) as cnt FROM messages {where_clause}",
                params
            )
            total = cursor.fetchone()["cnt"]
            pages = max(1, (total + page_size - 1) // page_size)

            # 分页查询
            offset = (page - 1) * page_size
            cursor.execute(
                f"SELECT * FROM messages {where_clause} ORDER BY msgtime DESC LIMIT %s OFFSET %s",
                params + [page_size, offset]
            )
            messages = list(cursor.fetchall())

            return {"total": total, "pages": pages, "page": page, "messages": messages}
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """获取统计信息"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            cursor.execute("SELECT COUNT(*) as cnt FROM messages")
            total = cursor.fetchone()["cnt"]

            cursor.execute(
                "SELECT msgtype, COUNT(*) as cnt FROM messages GROUP BY msgtype ORDER BY cnt DESC"
            )
            type_stats = list(cursor.fetchall())

            cursor.execute("SELECT seq FROM cursor_state WHERE id = 1")
            row = cursor.fetchone()
            seq = row["seq"] if row else 0

            return {"total_messages": total, "current_seq": seq, "type_stats": type_stats}
        finally:
            conn.close()

    def close(self):
        """连接池无需手动关闭，保留接口兼容"""
        logger.info("数据库连接池无需手动关闭")
