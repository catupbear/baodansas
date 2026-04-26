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

    def __init__(self, corpid: str, secret: str, db, external_secret: str = ""):
        self.corpid = corpid
        self.secret = secret
        self.external_secret = external_secret  # 外部联系人应用的 Secret
        self.db = db
        self._access_token = ""
        self._token_expires = 0
        self._external_access_token = ""
        self._external_token_expires = 0
        self._init_cache_table()
        # 有外部应用 Secret 时，清除之前标记为不可解析的外部联系人/群，重新尝试
        if external_secret:
            self._clear_unresolvable()

    def _clear_unresolvable(self):
        """清除外部联系人和外部群的 __unresolvable__ 标记，用新 Secret 重试"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor()
            # 只清除可能受益于外部应用 Secret 的标记：
            # wm/wo 前缀（外部联系人）和 wr 前缀（外部群）
            cursor.execute("""
                DELETE FROM contacts_cache
                WHERE name = '__unresolvable__'
                  AND (id LIKE 'wm%%' OR id LIKE 'wo%%' OR id LIKE 'wr%%' OR id LIKE 'wb%%')
            """)
            deleted = cursor.rowcount
            conn.commit()
            if deleted:
                logger.info("已清除 %d 条外部联系人/群的不可解析缓存，将使用外部应用 Secret 重试", deleted)
        finally:
            conn.close()

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

    def _get_external_access_token(self) -> str:
        """获取外部联系人应用的 access_token"""
        if not self.external_secret:
            return ""
        now = int(time.time())
        if self._external_access_token and now < self._external_token_expires:
            return self._external_access_token

        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.corpid}&corpsecret={self.external_secret}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errcode") == 0:
                self._external_access_token = data["access_token"]
                self._external_token_expires = now + data.get("expires_in", 7200) - 300
                return self._external_access_token
            else:
                logger.error("获取外部应用access_token失败: %s", data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取外部应用access_token异常: %s", e)
            return ""

    def get_name(self, user_id: str) -> str:
        """
        获取用户昵称（优先从缓存读取）
        支持内部员工和外部联系人
        """
        if not user_id:
            return ""

        # 先查缓存（包括失败标记）
        cached = self._get_cache(user_id)
        if cached:
            return cached if cached != "__unresolvable__" else user_id

        # 判断是否是外部联系人（wm/wo 开头）
        if user_id.startswith(("wm", "wo")):
            name = self._fetch_external_contact(user_id)
        elif user_id.startswith("wb"):
            # wb 前缀是外部群的微信成员，通过查找所在群的成员列表获取名称
            name = self._resolve_wb_member(user_id)
        else:
            name = self._fetch_user(user_id)

        if name:
            self._set_cache(user_id, "user", name)
        else:
            # 缓存失败标记，避免反复重试
            self._set_cache(user_id, "user", "__unresolvable__")

        return name or user_id

    def get_room_name(self, room_id: str) -> str:
        """获取群名称"""
        if not room_id:
            return ""

        cached = self._get_cache(room_id)
        if cached:
            return cached if cached != "__unresolvable__" else room_id

        name = self._fetch_room_info(room_id)
        if name:
            self._set_cache(room_id, "room", name)
        else:
            # 缓存失败标记，避免反复重试
            self._set_cache(room_id, "room", "__unresolvable__")

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
                logger.warning("获取员工信息失败 %s: errcode=%s, errmsg=%s",
                               userid, data.get("errcode"), data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取员工信息异常 %s: %s", userid, e)
            return ""

    def _fetch_external_contact(self, external_userid: str) -> str:
        """调用外部联系人API获取昵称（优先用外部应用 token）"""
        token = self._get_external_access_token() or self._get_access_token()
        if not token:
            logger.warning("获取外部联系人 %s 失败: access_token 为空", external_userid)
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
                logger.warning("获取外部联系人失败 %s: errcode=%s, errmsg=%s",
                               external_userid, data.get("errcode"), data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取外部联系人异常 %s: %s", external_userid, e)
            return ""

    def _resolve_wb_member(self, user_id: str) -> str:
        """
        wb 前缀是外部群的微信成员，无法通过通讯录 API 直接查询。
        通过查找该用户所在的群，调用客户群接口获取群成员列表来间接获取名称。
        _fetch_external_room_info 会自动缓存所有群成员名称。
        """
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            # 找到该用户所在的群（取最近的一个）
            cursor.execute(
                "SELECT DISTINCT roomid FROM messages WHERE sender = %s AND roomid != '' AND roomid IS NOT NULL LIMIT 1",
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                return ""
            roomid = row["roomid"]
        finally:
            conn.close()

        # 调用外部群接口，会顺便缓存所有成员名称
        self._fetch_external_room_info(roomid)

        # 检查缓存是否已被填充
        cached = self._get_cache(user_id)
        if cached and cached != "__unresolvable__":
            return cached
        return ""

    def _fetch_room_info(self, roomid: str) -> str:
        """
        获取群信息：先用会话存档接口（内部群），
        失败(301059)时降级到客户群接口（外部群）。
        """
        # 先尝试内部群接口
        token = self._get_access_token()
        if token:
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
                        member_names = [m.get("memberid", "") for m in members[:3]]
                        name = "、".join(member_names) + "..."
                    logger.debug("获取群信息(内部群): %s -> %s", roomid, name)
                    return name
                elif data.get("errcode") != 301059:
                    # 非 301059 错误，不再尝试外部群接口
                    logger.warning("获取群信息失败 %s: errcode=%s, errmsg=%s",
                                   roomid, data.get("errcode"), data.get("errmsg"))
                    return ""
                # 301059 = only support inner room，继续尝试外部群接口
            except Exception as e:
                logger.error("获取群信息异常 %s: %s", roomid, e)
                return ""

        # 降级：用外部应用 token 调客户群接口
        return self._fetch_external_room_info(roomid)

    def _fetch_external_room_info(self, roomid: str) -> str:
        """
        调用客户群接口获取外部群信息。
        同时把群成员名称缓存下来（解决 wb 前缀成员无法通过通讯录 API 获取名称的问题）。
        """
        token = self._get_external_access_token()
        if not token:
            logger.warning("获取外部群信息 %s 失败: 外部应用 access_token 为空", roomid)
            return ""

        url = f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/groupchat/get?access_token={token}"
        body = json.dumps({"chat_id": roomid, "need_name": 1}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errcode") == 0:
                chat = data.get("group_chat", {})
                name = chat.get("name", "")

                # 缓存群成员名称（包括 wb 前缀的微信用户）
                members = chat.get("member_list", [])
                cached_count = 0
                for m in members:
                    member_id = m.get("userid", "")
                    member_name = m.get("name", "")
                    if member_id and member_name:
                        # 检查是否已缓存
                        existing = self._get_cache(member_id)
                        if not existing or existing == "__unresolvable__":
                            self._set_cache(member_id, "user", member_name)
                            cached_count += 1
                if cached_count:
                    logger.info("从外部群 %s 缓存了 %d 个成员名称", roomid, cached_count)

                if not name and members:
                    member_names = [m.get("name", m.get("userid", "")) for m in members[:3]]
                    name = "、".join(n for n in member_names if n)
                    if name:
                        name += "..."
                logger.debug("获取群信息(外部群): %s -> %s", roomid, name)
                return name
            else:
                logger.warning("获取外部群信息失败 %s: errcode=%s, errmsg=%s",
                               roomid, data.get("errcode"), data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取外部群信息异常 %s: %s", roomid, e)
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
        """批量从数据库读取昵称，一次 IN 查询。不可解析的返回原 ID。"""
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
            result = {}
            for row in cursor.fetchall():
                if row["name"] == "__unresolvable__":
                    result[row["id"]] = row["id"]  # 返回原 ID
                else:
                    result[row["id"]] = row["name"]
            return result
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
        遇到权限错误（48002）时，批量标记同类 ID 为不可解析，不再逐个重试。
        """
        unresolved = self.find_unresolved()
        result = {"resolved_users": 0, "resolved_rooms": 0,
                  "failed_users": 0, "failed_rooms": 0}

        # 按前缀分组用户
        external_users = [uid for uid in unresolved["users"] if uid.startswith(("wm", "wo"))]
        other_users = [uid for uid in unresolved["users"] if not uid.startswith(("wm", "wo"))]

        # 先尝试一个外部联系人，检查是否有权限
        external_api_forbidden = False
        if external_users:
            test_uid = external_users[0]
            name = self._fetch_external_contact(test_uid)
            if name:
                self._set_cache(test_uid, "user", name)
                result["resolved_users"] += 1
                logger.info("自动解析联系人: %s -> %s", test_uid, name)
                external_users = external_users[1:]
            else:
                # 检查是否是权限问题（48002），如果是则批量标记所有 wm/wo 前缀
                external_api_forbidden = True
                logger.warning("外部联系人 API 无权限(48002)，批量标记 %d 个 wm/wo 前缀 ID 为不可解析",
                               len(external_users))
                for uid in external_users:
                    self._set_cache(uid, "user", "__unresolvable__")
                    result["failed_users"] += 1
                external_users = []
            time.sleep(0.3)

        # 继续处理剩余的外部联系人（如果有权限）
        if not external_api_forbidden:
            for uid in external_users:
                name = self._fetch_external_contact(uid)
                if name:
                    self._set_cache(uid, "user", name)
                    result["resolved_users"] += 1
                    logger.info("自动解析联系人: %s -> %s", uid, name)
                else:
                    self._set_cache(uid, "user", "__unresolvable__")
                    result["failed_users"] += 1
                time.sleep(0.3)

        # 分离 wb 前缀和普通用户
        wb_users = [uid for uid in other_users if uid.startswith("wb")]
        normal_users = [uid for uid in other_users if not uid.startswith("wb")]

        # 处理普通用户
        for uid in normal_users:
            name = self._fetch_user(uid)
            if name:
                self._set_cache(uid, "user", name)
                result["resolved_users"] += 1
                logger.info("自动解析联系人: %s -> %s", uid, name)
            else:
                self._set_cache(uid, "user", "__unresolvable__")
                result["failed_users"] += 1
            time.sleep(0.3)

        # 处理 wb 前缀（外部群微信成员）：通过查找所在群的成员列表获取
        for uid in wb_users:
            name = self._resolve_wb_member(uid)
            if name:
                # _resolve_wb_member 内部已写缓存
                result["resolved_users"] += 1
                logger.info("自动解析联系人(群成员): %s -> %s", uid, name)
            else:
                self._set_cache(uid, "user", "__unresolvable__")
                result["failed_users"] += 1
            time.sleep(0.3)

        # 处理群：先试一个，判断是否普遍 301059
        room_api_forbidden = False
        rooms = unresolved["rooms"]
        if rooms:
            test_rid = rooms[0]
            name = self._fetch_room_info(test_rid)
            if name:
                self._set_cache(test_rid, "room", name)
                result["resolved_rooms"] += 1
                logger.info("自动解析群名: %s -> %s", test_rid, name)
                rooms = rooms[1:]
            else:
                # 第一个就失败，再试第二个确认是不是普遍问题
                if len(rooms) > 1:
                    time.sleep(0.3)
                    name2 = self._fetch_room_info(rooms[1])
                    if name2:
                        # 第二个成功，说明不是普遍问题，只标记第一个
                        self._set_cache(test_rid, "room", "__unresolvable__")
                        self._set_cache(rooms[1], "room", name2)
                        result["failed_rooms"] += 1
                        result["resolved_rooms"] += 1
                        logger.info("自动解析群名: %s -> %s", rooms[1], name2)
                        rooms = rooms[2:]
                    else:
                        # 连续两个都失败，批量标记所有群
                        room_api_forbidden = True
                        logger.warning("群信息 API 连续失败，批量标记 %d 个群 ID 为不可解析", len(rooms))
                        for rid in rooms:
                            self._set_cache(rid, "room", "__unresolvable__")
                            result["failed_rooms"] += 1
                        rooms = []
                else:
                    self._set_cache(test_rid, "room", "__unresolvable__")
                    result["failed_rooms"] += 1
                    rooms = []
            time.sleep(0.3)

        # 继续处理剩余群
        if not room_api_forbidden:
            for rid in rooms:
                name = self._fetch_room_info(rid)
                if name:
                    self._set_cache(rid, "room", name)
                    result["resolved_rooms"] += 1
                    logger.info("自动解析群名: %s -> %s", rid, name)
                else:
                    self._set_cache(rid, "room", "__unresolvable__")
                    result["failed_rooms"] += 1
                time.sleep(0.3)

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
