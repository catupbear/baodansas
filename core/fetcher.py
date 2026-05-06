"""
消息拉取模块
收到回调后，基于 seq 游标分页拉取聊天记录
"""

import json
import logging
import threading

from core.decryptor import MessageDecryptor
from core.parser import MessageParser
from sdk.finance_sdk import FinanceSDK
from storage.db import Database

logger = logging.getLogger(__name__)


class MessageFetcher:
    """消息拉取器"""

    def __init__(self, finance_sdk: FinanceSDK, decryptor: MessageDecryptor,
                 parser: MessageParser, db: Database,
                 limit: int = 1000, proxy: str = "", passwd: str = "", timeout: int = 10):
        self.finance_sdk = finance_sdk
        self.decryptor = decryptor
        self.parser = parser
        self.db = db
        self.limit = limit
        self.proxy = proxy
        self.passwd = passwd
        self.timeout = timeout
        self._lock = threading.Lock()

    def fetch_new_messages(self):
        """
        拉取新消息（回调触发时调用）
        从上次的 seq 开始，循环拉取直到没有新数据
        """
        with self._lock:
            self._do_fetch()

    def _do_fetch(self):
        """执行拉取逻辑"""
        seq = self.db.get_seq()
        total_count = 0

        while True:
            logger.info("拉取消息, seq=%d, limit=%d", seq, self.limit)

            ret, data = self.finance_sdk.get_chat_data(
                seq, self.limit, self.proxy, self.passwd, self.timeout
            )
            if ret != 0:
                logger.error("拉取消息失败, 错误码: %d", ret)
                break

            chat_list = data.get("chatdata", [])
            if not chat_list:
                logger.info("没有新消息")
                break

            for item in chat_list:
                self._process_item(item)
                # 更新 seq 为当前消息的 seq
                seq = item.get("seq", seq)

            total_count += len(chat_list)

            # 更新游标
            self.db.update_seq(seq)

            # 如果拉取数量小于 limit，说明没有更多数据
            if len(chat_list) < self.limit:
                break

        logger.info("本次共拉取 %d 条消息", total_count)

    def rescan_missed_insurance(self, lookback_days: int = 5):
        """
        补扫历史消息中漏掉的保单 PDF。
        查找最近 lookback_days 天内，在监控群/用户中的 PDF 文件消息，
        且未在 insurance_records 表中生成记录的，重新入队识别。
        """
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            logger.info("补扫跳过：insurance_handler 未绑定")
            return

        watch = handler.get_watch_config()
        watch_rooms = watch.get("rooms", [])
        watch_users = watch.get("users", [])
        if not watch_rooms and not watch_users:
            logger.info("补扫跳过：没有配置监控群/用户")
            return

        import time
        cutoff_ms = int((time.time() - lookback_days * 86400) * 1000)

        conn = self.db.pool.connection()
        try:
            import pymysql.cursors
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # 查找监控范围内的 PDF 文件消息
            conditions = []
            params = [cutoff_ms]

            if watch_rooms:
                placeholders = ",".join(["%s"] * len(watch_rooms))
                conditions.append(f"m.roomid IN ({placeholders})")
                params.extend(watch_rooms)

            if watch_users:
                placeholders = ",".join(["%s"] * len(watch_users))
                conditions.append(f"(m.roomid = '' AND m.sender IN ({placeholders}))")
                params.extend(watch_users)

            where_source = " OR ".join(conditions)

            sql = f"""
                SELECT m.seq, m.roomid, m.sender, m.parsed_content
                FROM messages m
                LEFT JOIN insurance_records ir ON ir.msg_seq = m.seq
                WHERE m.msgtime >= %s
                  AND m.msgtype = 'file'
                  AND ({where_source})
                  AND ir.id IS NULL
                ORDER BY m.seq ASC
            """
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            logger.info("补扫完成：没有遗漏的保单 PDF")
            return

        import json
        enqueued = 0
        for row in rows:
            try:
                content = json.loads(row.get("parsed_content", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue

            filename = content.get("filename", "")
            if not filename.lower().endswith(".pdf"):
                continue

            logger.info("补扫入队: seq=%d, room=%s, file=%s",
                        row["seq"], row["roomid"], filename)
            handler.enqueue({
                "seq": row["seq"],
                "roomid": row["roomid"],
                "sender": row["sender"],
                "filename": filename,
                "sdkfileid": content.get("sdkfileid", ""),
                "filesize": content.get("filesize", 0),
            })
            enqueued += 1

        logger.info("补扫完成：共 %d 条遗漏 PDF 已入队", enqueued)

    def _process_item(self, item: dict):
        """处理单条加密消息"""
        msg_seq = item.get("seq", 0)
        encrypt_random_key = item.get("encrypt_random_key", "")
        encrypt_chat_msg = item.get("encrypt_chat_msg", "")
        publickey_ver = item.get("publickey_ver", 1)

        # 解密消息
        ret, decrypted_json = self.decryptor.decrypt_message(encrypt_random_key, encrypt_chat_msg)
        if ret != 0:
            logger.error("解密消息失败, seq=%d", msg_seq)
            return

        # 解析消息
        try:
            msg_data = json.loads(decrypted_json)
            parsed = self.parser.parse(msg_data)
            logger.info("消息[seq=%d]: %s", msg_seq, parsed.get("summary", ""))

            # 存储消息
            self.db.save_message(msg_seq, msg_data, parsed)

            # 检查是否需要触发保单识别
            self._check_insurance_trigger(msg_seq, msg_data, parsed)

            # 检查是否需要触发保险报价
            self._check_quote_trigger(msg_seq, msg_data, parsed)
        except json.JSONDecodeError as e:
            logger.error("消息JSON解析失败, seq=%d: %s", msg_seq, e)

    def _check_insurance_trigger(self, seq: int, msg_data: dict, parsed: dict):
        """检查是否需要触发保单识别（支持群聊 + 私聊）"""
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            return

        msgtype = parsed.get("msgtype", "")
        roomid = parsed.get("roomid", "")
        sender = parsed.get("from", "")
        content = parsed.get("content", {})

        # 条件1：文件类型 + PDF后缀
        if msgtype != "file":
            return

        filename = content.get("filename", "")
        if not filename.lower().endswith(".pdf"):
            return

        # 条件2：在监控列表中（群聊匹配 roomid，私聊匹配 sender）
        watch = handler.get_watch_config()
        logger.info(
            "保单触发检查: seq=%d, roomid=%r, sender=%r, watch_rooms=%s, watch_users=%s",
            seq, roomid, sender, watch.get("rooms", []), watch.get("users", []),
        )
        is_watched = False
        if roomid and roomid in watch.get("rooms", []):
            is_watched = True
        elif not roomid and sender and sender in watch.get("users", []):
            is_watched = True

        if not is_watched:
            logger.info("保单触发检查: seq=%d 未匹配任何监控配置，跳过", seq)
            return

        source_desc = f"room={roomid}" if roomid else f"user={sender}"
        logger.info("触发保单识别: seq=%d, %s, file=%s", seq, source_desc, filename)
        handler.enqueue({
            "seq": seq,
            "roomid": roomid,
            "sender": sender,
            "filename": filename,
            "sdkfileid": content.get("sdkfileid", ""),
            "filesize": content.get("filesize", 0),
        })

    def _check_quote_trigger(self, seq: int, msg_data: dict, parsed: dict):
        """检查是否需要触发保险报价（图片/文本消息）"""
        handler = getattr(self, "quote_handler", None)
        if not handler:
            return

        msgtype = parsed.get("msgtype", "")
        roomid = parsed.get("roomid", "")
        sender = parsed.get("from", "")
        content = parsed.get("content", {})

        # 仅处理图片和文本消息
        if msgtype not in ("image", "text"):
            return

        # 仅处理群聊消息
        if not roomid:
            return

        handler.check_and_enqueue({
            "msgtype": msgtype,
            "roomid": roomid,
            "sender": sender,
            "content": content,
            "seq": seq,
        })
