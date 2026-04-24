"""
SQLite 数据存储模块
存储消息记录和 seq 游标
"""

import json
import logging
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)


class Database:
    """SQLite 数据库操作"""

    def __init__(self, db_path: str):
        # 确保目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
        logger.info("数据库初始化完成: %s", db_path)

    def _init_tables(self):
        """创建数据库表"""
        cursor = self.conn.cursor()

        # 消息表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seq INTEGER UNIQUE,
                msgid TEXT,
                action TEXT,
                sender TEXT,
                tolist TEXT,
                roomid TEXT,
                msgtime INTEGER,
                msgtype TEXT,
                summary TEXT,
                raw_data TEXT,
                parsed_content TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # seq 游标表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cursor_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                seq INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # 初始化游标（如果不存在）
        cursor.execute("""
            INSERT OR IGNORE INTO cursor_state (id, seq) VALUES (1, 0)
        """)

        # 索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_msgtime ON messages(msgtime)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_msgtype ON messages(msgtype)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_roomid ON messages(roomid)")

        self.conn.commit()

    def get_seq(self) -> int:
        """获取当前 seq 游标"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT seq FROM cursor_state WHERE id = 1")
        row = cursor.fetchone()
        return row["seq"] if row else 0

    def update_seq(self, seq: int):
        """更新 seq 游标"""
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE cursor_state SET seq = ?, updated_at = ? WHERE id = 1",
            (seq, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        self.conn.commit()

    def save_message(self, seq: int, raw_data: dict, parsed: dict):
        """保存一条消息"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO messages
                (seq, msgid, action, sender, tolist, roomid, msgtime, msgtype, summary, raw_data, parsed_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            self.conn.commit()
        except sqlite3.IntegrityError:
            logger.debug("消息已存在, seq=%d", seq)
        except Exception as e:
            logger.error("保存消息失败, seq=%d: %s", seq, e)

    def get_recent_messages(self, limit: int = 50) -> list[dict]:
        """获取最近的消息"""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM messages ORDER BY msgtime DESC LIMIT ?",
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def query_messages(self, page: int = 1, page_size: int = 50,
                       msgtype: str = "", sender: str = "",
                       roomid: str = "", keyword: str = "") -> dict:
        """
        分页查询消息，支持筛选
        返回: {"total": 总数, "pages": 总页数, "page": 当前页, "messages": [...]}
        """
        conditions = []
        params = []

        if msgtype:
            conditions.append("msgtype = ?")
            params.append(msgtype)
        if sender:
            conditions.append("sender LIKE ?")
            params.append(f"%{sender}%")
        if roomid:
            conditions.append("roomid LIKE ?")
            params.append(f"%{roomid}%")
        if keyword:
            conditions.append("summary LIKE ?")
            params.append(f"%{keyword}%")

        where = " AND ".join(conditions)
        where_clause = f"WHERE {where}" if where else ""

        cursor = self.conn.cursor()

        # 查询总数
        cursor.execute(f"SELECT COUNT(*) as cnt FROM messages {where_clause}", params)
        total = cursor.fetchone()["cnt"]
        pages = max(1, (total + page_size - 1) // page_size)

        # 分页查询
        offset = (page - 1) * page_size
        cursor.execute(
            f"SELECT * FROM messages {where_clause} ORDER BY msgtime DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        )
        messages = [dict(row) for row in cursor.fetchall()]

        return {"total": total, "pages": pages, "page": page, "messages": messages}

    def get_stats(self) -> dict:
        """获取统计信息"""
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) as cnt FROM messages")
        total = cursor.fetchone()["cnt"]

        cursor.execute("SELECT msgtype, COUNT(*) as cnt FROM messages GROUP BY msgtype ORDER BY cnt DESC")
        type_stats = [dict(row) for row in cursor.fetchall()]

        cursor.execute("SELECT seq FROM cursor_state WHERE id = 1")
        seq = cursor.fetchone()["seq"]

        return {"total_messages": total, "current_seq": seq, "type_stats": type_stats}

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")
