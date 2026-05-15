"""
MySQL 数据存储模块
存储消息记录和 seq 游标，使用 PooledDB 连接池管理数据库连接
"""

import json
import logging
from datetime import datetime

import pymysql
import pymysql.cursors
try:
    from dbutils.pooled_db import PooledDB          # DBUtils >= 2.0
except ImportError:
    from DBUtils.PooledDB import PooledDB            # DBUtils 1.x

logger = logging.getLogger(__name__)


class Database:
    """MySQL 数据库操作，基于 PooledDB 连接池"""

    def __init__(self, db_config: dict, main_corpid: str = ""):
        """
        初始化数据库连接池。
        db_config 包含: host, port, user, password, database
        main_corpid: 主企业 corpid，用于回填历史消息
        """
        self._main_corpid = main_corpid
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
                    seq INT,
                    corpid VARCHAR(64) DEFAULT '',
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
                "CREATE INDEX idx_messages_corpid ON messages(corpid)",
            ]:
                try:
                    cursor.execute(idx_sql)
                except Exception:
                    pass  # 索引已存在，忽略

            # 迁移：seq 从全局唯一改为 (corpid, seq) 联合唯一（支持多企业 seq 独立）
            try:
                cursor.execute("ALTER TABLE messages DROP INDEX seq")
                logger.info("已删除 messages.seq 唯一索引")
            except Exception:
                pass  # 索引不存在，忽略
            try:
                cursor.execute("CREATE UNIQUE INDEX uk_corpid_seq ON messages(corpid, seq)")
                logger.info("已创建 messages(corpid, seq) 联合唯一索引")
            except Exception:
                pass  # 索引已存在，忽略

            # 迁移：为 messages 表添加 corpid 字段（已有表兼容）
            try:
                cursor.execute(
                    "ALTER TABLE messages ADD COLUMN corpid VARCHAR(64) DEFAULT '' AFTER seq"
                )
                logger.info("messages 表已添加 corpid 字段")
            except Exception:
                pass  # 字段已存在，忽略

            # 迁移：回填历史消息的 corpid（空值 → 主企业 corpid）
            if self._main_corpid:
                cursor.execute(
                    "UPDATE messages SET corpid = %s WHERE corpid = '' OR corpid IS NULL",
                    (self._main_corpid,)
                )
                if cursor.rowcount > 0:
                    logger.info("已回填 %d 条历史消息的 corpid 为 %s", cursor.rowcount, self._main_corpid)

            conn.commit()
        finally:
            conn.close()

    def get_seq(self, cursor_id: int = 1) -> int:
        """获取指定企业的 seq 游标（默认 id=1 兼容老逻辑）"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT seq FROM cursor_state WHERE id = %s", (cursor_id,))
            row = cursor.fetchone()
            if row:
                return row["seq"]
            # 新企业首次拉取，插入初始游标
            cursor.execute(
                "INSERT INTO cursor_state (id, seq) VALUES (%s, 0)", (cursor_id,)
            )
            conn.commit()
            return 0
        finally:
            conn.close()

    def update_seq(self, seq: int, cursor_id: int = 1):
        """更新指定企业的 seq 游标（默认 id=1 兼容老逻辑）"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "UPDATE cursor_state SET seq = %s, updated_at = %s WHERE id = %s",
                (seq, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cursor_id)
            )
            conn.commit()
        finally:
            conn.close()

    def save_message(self, seq: int, raw_data: dict, parsed: dict, corpid: str = ""):
        """保存一条消息，seq 重复时忽略（INSERT IGNORE）"""
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                INSERT IGNORE INTO messages
                (seq, corpid, msgid, action, sender, tolist, roomid, msgtime, msgtype, summary, raw_data, parsed_content)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                seq,
                corpid,
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

    def get_conversations(self, keyword: str = "", corpid: str = "") -> list:
        """
        获取会话列表，按最后消息时间倒序。
        私聊按双方 ID 排序组合分组（A→B 和 B→A 合并为一个会话）。
        corpid: 按企业过滤，空字符串表示不过滤
        """
        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            conditions = []
            params = []
            if keyword:
                conditions.append("(summary LIKE %s OR sender LIKE %s OR roomid LIKE %s)")
                params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
            if corpid:
                conditions.append("corpid = %s")
                params.append(corpid)
            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            # 私聊：用 LEAST/GREATEST 把双方 ID 排序组合，确保 A→B 和 B→A 归为同一个会话
            # tolist 是 JSON 数组如 '["userid"]'，用 JSON_UNQUOTE(JSON_EXTRACT(...)) 提取第一个元素
            sql = f"""
                SELECT
                    CASE
                        WHEN roomid = '' OR roomid IS NULL THEN
                            CONCAT('private_',
                                LEAST(sender, COALESCE(JSON_UNQUOTE(JSON_EXTRACT(tolist, '$[0]')), sender)),
                                '_',
                                GREATEST(sender, COALESCE(JSON_UNQUOTE(JSON_EXTRACT(tolist, '$[0]')), sender))
                            )
                        ELSE roomid
                    END AS conversation_id,
                    MAX(COALESCE(roomid, '')) AS roomid,
                    MAX(msgtime) AS last_time,
                    COUNT(*) AS msg_count,
                    MAX(id) AS last_msg_id
                FROM messages
                {where_clause}
                GROUP BY conversation_id
                ORDER BY last_time DESC
            """
            cursor.execute(sql, params)
            conversations = list(cursor.fetchall())

            if not conversations:
                return []

            # 批量查最后一条消息的详情
            last_ids = [conv["last_msg_id"] for conv in conversations if conv.get("last_msg_id")]
            last_msg_map = {}
            if last_ids:
                placeholders = ",".join(["%s"] * len(last_ids))
                cursor.execute(
                    f"SELECT id, sender, msgtype, summary, tolist FROM messages WHERE id IN ({placeholders})",
                    last_ids
                )
                for row in cursor.fetchall():
                    last_msg_map[row["id"]] = row

            # 合并结果
            for conv in conversations:
                msg = last_msg_map.get(conv.get("last_msg_id"), {})
                conv["last_sender"] = msg.get("sender", "")
                conv["last_msgtype"] = msg.get("msgtype", "")
                conv["last_summary"] = msg.get("summary", "")
                # 私聊会话提取双方 ID
                if not conv["roomid"] and conv["conversation_id"].startswith("private_"):
                    parts = conv["conversation_id"].replace("private_", "", 1).split("_", 1)
                    conv["peer_ids"] = parts  # 前端用于显示双方名称
                del conv["last_msg_id"]

            return conversations
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
                       roomid: str = "", keyword: str = "",
                       order: str = "desc",
                       conversation_id: str = "",
                       corpid: str = "") -> dict:
        """
        分页查询消息，支持筛选。
        order: "asc" 按时间正序（聊天模式），"desc" 按时间倒序（默认）
        conversation_id: 私聊会话ID（private_xxx 格式），优先于 roomid
        返回: {"total": 总数, "pages": 总页数, "page": 当前页, "messages": [...]}
        """
        conditions = []
        params = []

        # 私聊会话：conversation_id 格式为 private_<idA>_<idB>
        if conversation_id and conversation_id.startswith("private_"):
            parts = conversation_id.replace("private_", "", 1).split("_", 1)
            conditions.append("(roomid = '' OR roomid IS NULL)")
            if len(parts) == 2:
                # 双方对话：A→B 或 B→A（同时校验 tolist 包含对方）
                conditions.append(
                    "((sender = %s AND JSON_CONTAINS(tolist, JSON_QUOTE(%s)))"
                    " OR (sender = %s AND JSON_CONTAINS(tolist, JSON_QUOTE(%s))))"
                )
                params.extend([parts[0], parts[1], parts[1], parts[0]])
            else:
                # 兼容旧格式 private_<sender>
                conditions.append("sender = %s")
                params.append(parts[0])
        elif roomid:
            conditions.append("roomid = %s")
            params.append(roomid)

        if msgtype:
            conditions.append("msgtype = %s")
            params.append(msgtype)
        if sender:
            conditions.append("sender LIKE %s")
            params.append(f"%{sender}%")
        if keyword:
            conditions.append("summary LIKE %s")
            params.append(f"%{keyword}%")
        if corpid:
            conditions.append("corpid = %s")
            params.append(corpid)

        where = " AND ".join(conditions)
        where_clause = f"WHERE {where}" if where else ""

        order_dir = "ASC" if order == "asc" else "DESC"

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

            # 分页查询（只查前端需要的字段，避免传输 raw_data 等大字段）
            offset = (page - 1) * page_size
            cursor.execute(
                f"""SELECT seq, msgid, action, sender, tolist, roomid, msgtime, msgtype,
                           summary, parsed_content
                    FROM messages {where_clause} ORDER BY msgtime {order_dir} LIMIT %s OFFSET %s""",
                params + [page_size, offset]
            )
            messages = list(cursor.fetchall())

            return {"total": total, "pages": pages, "page": page, "messages": messages}
        finally:
            conn.close()

    # 统计信息缓存（10秒 TTL，按 corpid 分别缓存）
    _stats_cache = {}
    _stats_cache_time = {}

    def get_stats(self, corpid: str = "", cursor_id: int = 1) -> dict:
        """获取统计信息（带 10 秒内存缓存，支持按企业过滤）"""
        import time as _time
        now = _time.time()
        cache_key = corpid or "__all__"
        if cache_key in self._stats_cache and now - self._stats_cache_time.get(cache_key, 0) < 10:
            return dict(self._stats_cache[cache_key])

        conn = self.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            if corpid:
                cursor.execute("SELECT COUNT(*) as cnt FROM messages WHERE corpid = %s", (corpid,))
                total = cursor.fetchone()["cnt"]
                cursor.execute(
                    "SELECT msgtype, COUNT(*) as cnt FROM messages WHERE corpid = %s GROUP BY msgtype ORDER BY cnt DESC",
                    (corpid,)
                )
            else:
                cursor.execute("SELECT COUNT(*) as cnt FROM messages")
                total = cursor.fetchone()["cnt"]
                cursor.execute(
                    "SELECT msgtype, COUNT(*) as cnt FROM messages GROUP BY msgtype ORDER BY cnt DESC"
                )
            type_stats = list(cursor.fetchall())

            cursor.execute("SELECT seq FROM cursor_state WHERE id = %s", (cursor_id,))
            row = cursor.fetchone()
            seq = row["seq"] if row else 0

            result = {"total_messages": total, "current_seq": seq, "type_stats": type_stats}
            Database._stats_cache[cache_key] = result
            Database._stats_cache_time[cache_key] = now
            return dict(result)
        finally:
            conn.close()

    def close(self):
        """连接池无需手动关闭，保留接口兼容"""
        logger.info("数据库连接池无需手动关闭")
