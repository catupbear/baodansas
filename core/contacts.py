"""
通讯录模块
调用企业微信API获取员工、外部联系人、群的昵称
缓存到数据库，避免频繁调用API
"""

import json
import logging
import time
import urllib.request
import urllib.error

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)


class ContactsManager:
    """通讯录管理器"""

    def __init__(self, corpid: str, secret: str, db):
        self.corpid = corpid
        self.secret = secret
        self.db = db
        self._access_token = ""
        self._token_expires = 0
        # 初始化缓存表（contacts_cache 已在 storage/db.py 的 _init_tables 中创建）
        # 此处保留方法调用以兼容旧逻辑，实际建表在 _init_tables 中完成
        self._init_cache_table()

    def _init_cache_table(self):
        """确保昵称缓存表存在（MySQL 版本，通过连接池操作）"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
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
            conn.commit()
        finally:
            conn.close()

    def _get_access_token(self) -> str:
        """获取access_token，带缓存"""
        now = int(time.time())
        if self._access_token and now < self._token_expires:
            return self._access_token

        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.corpid}&corpsecret={self.secret}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errcode") == 0:
                self._access_token = data["access_token"]
                self._token_expires = now + data.get("expires_in", 7200) - 300
                return self._access_token
            else:
                logger.error("获取access_token失败: %s", data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取access_token异常: %s", e)
            return ""

    def get_name(self, user_id: str) -> str:
        """
        获取用户昵称（优先从缓存读取）
        支持内部员工和外部联系人
        """
        if not user_id:
            return ""

        # 先查缓存
        cached = self._get_cache(user_id)
        if cached:
            return cached

        # 判断是否是外部联系人（以 wm/wo/wb 开头）
        if user_id.startswith(("wm", "wo", "wb")):
            name = self._fetch_external_contact(user_id)
        else:
            name = self._fetch_user(user_id)

        if name:
            self._set_cache(user_id, "user", name)

        return name or user_id

    def get_room_name(self, room_id: str) -> str:
        """获取群名称"""
        if not room_id:
            return ""

        cached = self._get_cache(room_id)
        if cached:
            return cached

        name = self._fetch_room_info(room_id)
        if name:
            self._set_cache(room_id, "room", name)

        return name or room_id

    def _fetch_user(self, userid: str) -> str:
        """调用通讯录API获取员工信息"""
        token = self._get_access_token()
        if not token:
            return ""

        url = f"https://qyapi.weixin.qq.com/cgi-bin/user/get?access_token={token}&userid={userid}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errcode") == 0:
                name = data.get("name", "")
                logger.debug("获取员工信息: %s -> %s", userid, name)
                return name
            else:
                logger.debug("获取员工信息失败 %s: %s", userid, data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取员工信息异常 %s: %s", userid, e)
            return ""

    def _fetch_external_contact(self, external_userid: str) -> str:
        """调用外部联系人API获取昵称"""
        token = self._get_access_token()
        if not token:
            return ""

        url = f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/get?access_token={token}&external_userid={external_userid}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errcode") == 0:
                contact = data.get("external_contact", {})
                name = contact.get("name", "")
                logger.debug("获取外部联系人: %s -> %s", external_userid, name)
                return name
            else:
                logger.debug("获取外部联系人失败 %s: %s", external_userid, data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取外部联系人异常 %s: %s", external_userid, e)
            return ""

    def _fetch_room_info(self, roomid: str) -> str:
        """调用会话存档接口获取群信息"""
        token = self._get_access_token()
        if not token:
            return ""

        url = f"https://qyapi.weixin.qq.com/cgi-bin/msgaudit/groupchat/get?access_token={token}"
        body = json.dumps({"roomid": roomid}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errcode") == 0:
                name = data.get("roomname", "")
                members = data.get("members", [])
                if not name and members:
                    # 群没有名称时，用前几个成员名拼接
                    member_names = [m.get("memberid", "") for m in members[:3]]
                    name = "、".join(member_names) + "..."
                logger.debug("获取群信息: %s -> %s", roomid, name)
                return name
            else:
                logger.debug("获取群信息失败 %s: %s", roomid, data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取群信息异常 %s: %s", roomid, e)
            return ""

    def _get_cache(self, contact_id: str) -> str:
        """从数据库永久缓存读取昵称"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT name FROM contacts_cache WHERE id = %s",
                (contact_id,)
            )
            row = cursor.fetchone()
            return row["name"] if row else ""
        finally:
            conn.close()

    def _batch_get_cache(self, ids: list) -> dict:
        """批量从数据库读取昵称，一次 IN 查询"""
        if not ids:
            return {}
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            placeholders = ",".join(["%s"] * len(ids))
            cursor.execute(
                f"SELECT id, name FROM contacts_cache WHERE id IN ({placeholders})",
                ids
            )
            return {row["id"]: row["name"] for row in cursor.fetchall()}
        finally:
            conn.close()

    def _set_cache(self, contact_id: str, contact_type: str, name: str):
        """写入数据库永久缓存"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "REPLACE INTO contacts_cache (id, type, name, updated_at) VALUES (%s, %s, %s, %s)",
                (contact_id, contact_type, name, int(time.time()))
            )
            conn.commit()
        finally:
            conn.close()

    def find_unresolved(self) -> dict:
        """
        从 messages 表中查找所有未缓存的 sender 和 roomid。
        返回: {"users": [id, ...], "rooms": [id, ...]}
        """
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # 查找未缓存的 sender
            cursor.execute("""
                SELECT DISTINCT m.sender
                FROM messages m
                LEFT JOIN contacts_cache c ON m.sender = c.id
                WHERE m.sender != '' AND m.sender IS NOT NULL AND c.id IS NULL
                LIMIT 50
            """)
            users = [row["sender"] for row in cursor.fetchall()]

            # 查找未缓存的 roomid
            cursor.execute("""
                SELECT DISTINCT m.roomid
                FROM messages m
                LEFT JOIN contacts_cache c ON m.roomid = c.id
                WHERE m.roomid != '' AND m.roomid IS NOT NULL AND c.id IS NULL
                LIMIT 50
            """)
            rooms = [row["roomid"] for row in cursor.fetchall()]

            return {"users": users, "rooms": rooms}
        finally:
            conn.close()

    def resolve_unresolved(self) -> dict:
        """
        自动解析未缓存的联系人和群名称。
        每次最多处理 50 个用户 + 50 个群，带间隔避免触发频率限制。
        返回: {"resolved_users": 成功数, "resolved_rooms": 成功数, "failed_users": 失败数, "failed_rooms": 失败数}
        """
        unresolved = self.find_unresolved()
        result = {"resolved_users": 0, "resolved_rooms": 0,
                  "failed_users": 0, "failed_rooms": 0}

        for uid in unresolved["users"]:
            try:
                name = ""
                if uid.startswith(("wm", "wo", "wb")):
                    name = self._fetch_external_contact(uid)
                else:
                    name = self._fetch_user(uid)

                if name:
                    self._set_cache(uid, "user", name)
                    result["resolved_users"] += 1
                    logger.info("自动解析联系人: %s -> %s", uid, name)
                else:
                    result["failed_users"] += 1
                # 每次 API 调用间隔 0.5 秒，避免频率限制
                time.sleep(0.5)
            except Exception as e:
                logger.error("自动解析联系人异常 %s: %s", uid, e)
                result["failed_users"] += 1

        for rid in unresolved["rooms"]:
            try:
                name = self._fetch_room_info(rid)
                if name:
                    self._set_cache(rid, "room", name)
                    result["resolved_rooms"] += 1
                    logger.info("自动解析群名: %s -> %s", rid, name)
                else:
                    result["failed_rooms"] += 1
                time.sleep(0.5)
            except Exception as e:
                logger.error("自动解析群名异常 %s: %s", rid, e)
                result["failed_rooms"] += 1

        return result

    def start_auto_resolve(self, interval: int = 300):
        """
        启动后台线程，定时自动解析未缓存的联系人/群名称。
        interval: 检查间隔秒数，默认 5 分钟
        """
        import threading

        def _worker():
            logger.info("通讯录自动解析线程启动，间隔 %d 秒", interval)
            while True:
                try:
                    unresolved = self.find_unresolved()
                    total = len(unresolved["users"]) + len(unresolved["rooms"])
                    if total > 0:
                        logger.info("发现 %d 个未解析联系人，%d 个未解析群名，开始自动解析",
                                    len(unresolved["users"]), len(unresolved["rooms"]))
                        result = self.resolve_unresolved()
                        logger.info("自动解析完成: 用户 %d 成功/%d 失败, 群 %d 成功/%d 失败",
                                    result["resolved_users"], result["failed_users"],
                                    result["resolved_rooms"], result["failed_rooms"])
                except Exception as e:
                    logger.error("自动解析异常: %s", e)
                time.sleep(interval)

        thread = threading.Thread(target=_worker, daemon=True, name="contacts-auto-resolve")
        thread.start()
        logger.info("通讯录自动解析线程已启动")

    def batch_resolve(self, user_ids: list, room_ids: list) -> dict:
        """
        批量解析昵称（优化版：一次批量查缓存，只对未命中的逐个调 API）
        返回: {"users": {id: name}, "rooms": {id: name}}
        """
        unique_users = [uid for uid in set(user_ids) if uid]
        unique_rooms = [rid for rid in set(room_ids) if rid]

        # 一次性批量查缓存
        all_ids = unique_users + unique_rooms
        cached = self._batch_get_cache(all_ids)

        users = {}
        rooms = {}

        # 用户：缓存命中直接用，未命中才调 API
        for uid in unique_users:
            if uid in cached:
                users[uid] = cached[uid]
            else:
                users[uid] = self.get_name(uid)

        # 群：同上
        for rid in unique_rooms:
            if rid in cached:
                rooms[rid] = cached[rid]
            else:
                rooms[rid] = self.get_room_name(rid)

        return {"users": users, "rooms": rooms}
