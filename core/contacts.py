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

    def __init__(self, corpid: str, secret: str, db, external_secret: str = "",
                 enterprise_name: str = ""):
        self.corpid = corpid
        self.secret = secret
        self.external_secret = external_secret  # 外部联系人应用的 Secret
        self.db = db
        self.enterprise_name = enterprise_name
        # 缓存 key 前缀：多企业时用 corpid 隔离，主企业（第一个初始化的）不加前缀以兼容历史数据
        self._cache_prefix = ""
        self._access_token = ""
        self._token_expires = 0
        self._external_access_token = ""
        self._external_token_expires = 0
        self._log_prefix = f"[{enterprise_name}] " if enterprise_name else ""
        self._fallback_contacts = None  # 跨企业回退用的主企业 contacts 实例
        self._init_cache_table()
        # 有外部应用 Secret 时，清除之前标记为不可解析的外部联系人/群，重新尝试
        if external_secret:
            self._clear_unresolvable()

    def set_cache_prefix(self, prefix: str):
        """设置缓存 key 前缀（多企业隔离用，主企业不设置以兼容历史数据）"""
        self._cache_prefix = prefix

    def set_fallback_contacts(self, contacts):
        """设置跨企业回退的主企业 contacts 实例（用于本企业查询失败时重试）"""
        self._fallback_contacts = contacts

    def _clear_unresolvable(self):
        """清除外部联系人和外部群的 __unresolvable__ 标记，用新 Secret 重试"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor()
            # 只清除可能受益于外部应用 Secret 的标记：
            # wm/wo 前缀（外部联系人）和 wr 前缀（外部群）
            # 排除 extra='90501' 的记录（永久性不可解析：不是外部群）
            prefix = f"{self._cache_prefix}:" if self._cache_prefix else ""
            cursor.execute(f"""
                DELETE FROM contacts_cache
                WHERE name = '__unresolvable__'
                  AND (id LIKE '{prefix}wm%%' OR id LIKE '{prefix}wo%%'
                       OR id LIKE '{prefix}wr%%' OR id LIKE '{prefix}wb%%')
                  AND (extra IS NULL OR extra != '90501')
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
            # wb 前缀是群机器人，直接标记为通知机器人
            name = "通知机器人"
        else:
            name = self._fetch_user(user_id)

        if name:
            self._set_cache(user_id, "user", name)
        else:
            # 缓存失败标记，避免反复重试
            self._set_cache(user_id, "user", "__unresolvable__")
            logger.info("联系人 %s 已标记为不可解析，后续不再请求 API", user_id)

        return name or user_id

    def get_room_name(self, room_id: str) -> str:
        """获取群名称（优先读统一群信息表 wecom_groups，由 task_group_sync 独立任务维护）"""
        if not room_id:
            return ""

        # 统一群信息表优先（群改名后这里是最新值；contacts_cache 是永久缓存不会刷新）
        try:
            from storage.group_db import get_group_name_map
            name = get_group_name_map(self.db, [room_id]).get(room_id, "")
            if name:
                return name
        except Exception:
            pass

        cached = self._get_cache(room_id)
        if cached:
            return cached if cached != "__unresolvable__" else room_id

        name = self._fetch_room_info(room_id)
        if name:
            self._set_cache(room_id, "room", name)
        else:
            # 检查是否已被 _fetch_external_room_info 缓存（如 90501）
            already_cached = self._get_cache(room_id)
            if not already_cached:
                # 缓存失败标记，避免反复重试
                self._set_cache(room_id, "room", "__unresolvable__")
                logger.info("群 %s 已标记为不可解析，后续不再请求 API", room_id)

        return name or room_id

    def get_room_members(self, room_id: str) -> list:
        """获取群成员列表，返回 [{"userid": "xxx", "name": "张三"}, ...]
        优先调用企微 API，获取不到时从 messages 表提取发送人兜底。
        """
        if not room_id:
            return []

        result = self._fetch_room_members_api(room_id)
        if result:
            return result

        # 兜底：从 messages 表查该群所有发送人
        logger.warning("群 %s API 获取成员为空，从消息记录兜底", room_id)
        fallback = self._get_room_members_from_db(room_id)
        logger.info("群 %s 从消息记录提取到 %d 个发送人", room_id, len(fallback))
        return fallback

    def _fetch_room_members_api(self, room_id: str) -> list:
        """通过企微 API 获取群成员"""
        # 先尝试内部群接口
        token = self._get_access_token()
        if not token:
            logger.warning("获取群成员 %s 失败: access_token 为空", room_id)
        else:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/msgaudit/groupchat/get?access_token={token}"
            body = json.dumps({"roomid": room_id}).encode("utf-8")
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("errcode") == 0:
                    members = data.get("members", [])
                    logger.info("内部群接口获取成员成功 %s: %d 人", room_id, len(members))
                    result = []
                    for m in members:
                        mid = m.get("memberid", "")
                        name = self._get_cache(mid) if mid else ""
                        if not name or name == "__unresolvable__":
                            name = self.get_name(mid) if mid else ""
                        result.append({"userid": mid, "name": name or mid})
                    return result
                elif data.get("errcode") in (301059, 301039):
                    # 301059: 非内部群; 301039: 群不在会话存档范围内
                    logger.info("群 %s 非内部群(%s)，尝试外部群接口",
                                room_id, data.get("errcode"))
                else:
                    logger.warning("内部群接口获取成员失败 %s: errcode=%s, errmsg=%s",
                                   room_id, data.get("errcode"), data.get("errmsg"))
                    return []
            except Exception as e:
                logger.error("内部群接口获取成员异常 %s: %s", room_id, e)
                return []

        # 降级：外部群接口
        token = self._get_external_access_token()
        if not token:
            logger.warning("获取外部群成员 %s 失败: external_access_token 为空（未配置 external_secret？）", room_id)
            return []

        url = f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/groupchat/get?access_token={token}"
        body = json.dumps({"chat_id": room_id, "need_name": 1}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errcode") == 0:
                chat = data.get("group_chat", {})
                members = chat.get("member_list", [])
                logger.info("外部群接口获取成员成功 %s: %d 人", room_id, len(members))
                result = []
                for m in members:
                    mid = m.get("userid", "")
                    name = m.get("name", "")
                    if not name:
                        name = self._get_cache(mid) if mid else ""
                        if not name or name == "__unresolvable__":
                            name = mid
                    result.append({"userid": mid, "name": name or mid})
                return result
            elif data.get("errcode") == 92002 and self._fallback_contacts:
                # 92002: not allow to cross corp — 该群不属于本企业，回退到主企业完整查询流程
                logger.info("群 %s 不属于%s（92002 跨企业），回退到主企业查询",
                            room_id, self.enterprise_name or "本企业")
                return self._fallback_contacts.get_room_members(room_id)
            else:
                logger.warning("外部群接口获取成员失败 %s: errcode=%s, errmsg=%s",
                               room_id, data.get("errcode"), data.get("errmsg"))
        except Exception as e:
            logger.error("外部群接口获取成员异常 %s: %s", room_id, e)
        return []

    def _get_room_members_from_db(self, room_id: str) -> list:
        """从 messages 表提取群内所有发送人作为成员列表（兜底方案）"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT DISTINCT sender FROM messages WHERE roomid = %s AND sender IS NOT NULL AND sender != ''",
                (room_id,)
            )
            senders = [row["sender"] for row in cursor.fetchall()]
        finally:
            conn.close()

        result = []
        for sid in senders:
            name = self._get_cache(sid) if sid else ""
            if not name or name == "__unresolvable__":
                name = sid
            result.append({"userid": sid, "name": name or sid})
        return result

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
                errcode = data.get("errcode")
                # 40096 = 无效的 external_userid（ID 截断或已失效），属于已知情况，降级为 DEBUG
                if errcode == 40096:
                    logger.debug("外部联系人 ID 无效(已截断或失效) %s，已跳过", external_userid)
                elif errcode == 84061 and self._fallback_contacts:
                    # 84061: not external contact — 不是本企业的外部联系人，回退到主企业查询
                    logger.info("联系人 %s 不属于%s（84061），回退到主企业查询",
                                external_userid, self.enterprise_name or "本企业")
                    return self._fallback_contacts._fetch_external_contact(external_userid)
                else:
                    logger.warning("获取外部联系人失败 %s: errcode=%s, errmsg=%s",
                                   external_userid, errcode, data.get("errmsg"))
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
        result = self._fetch_external_room_info(roomid)
        # __skipped__ 表示 90501（不是外部群），已在内部缓存，返回空让调用方跳过
        return "" if result == "__skipped__" else result

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
            errcode = data.get("errcode")
            if errcode == 0:
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
            elif errcode == 90501:
                # 90501 = 不是外部群，属于永久性状态，降级为 DEBUG 并标记不可解析
                logger.debug("群 %s 不是外部群(90501)，已跳过", roomid)
                # 直接在此处缓存，并在 extra 字段标记原因，避免重启后重试
                self._set_cache_with_extra(roomid, "room", "__unresolvable__", "90501")
                return "__skipped__"
            else:
                logger.warning("获取外部群信息失败 %s: errcode=%s, errmsg=%s",
                               roomid, data.get("errcode"), data.get("errmsg"))
                return ""
        except Exception as e:
            logger.error("获取外部群信息异常 %s: %s", roomid, e)
            return ""

    def _cache_key(self, contact_id: str) -> str:
        """生成带前缀的缓存 key（多企业隔离）"""
        if self._cache_prefix:
            return f"{self._cache_prefix}:{contact_id}"
        return contact_id

    def _get_cache(self, contact_id: str) -> str:
        """从数据库永久缓存读取昵称"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT name FROM contacts_cache WHERE id = %s",
                (self._cache_key(contact_id),)
            )
            row = cursor.fetchone()
            return row["name"] if row else ""
        finally:
            conn.close()

    def _batch_get_cache(self, ids: list) -> dict:
        """批量从数据库读取昵称，一次 IN 查询。不可解析的返回原 ID。"""
        if not ids:
            return {}
        cache_keys = [self._cache_key(uid) for uid in ids]
        # 建立 cache_key -> 原始 id 的映射
        key_to_id = dict(zip(cache_keys, ids))
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            placeholders = ",".join(["%s"] * len(cache_keys))
            cursor.execute(
                f"SELECT id, name FROM contacts_cache WHERE id IN ({placeholders})",
                cache_keys
            )
            result = {}
            for row in cursor.fetchall():
                original_id = key_to_id.get(row["id"], row["id"])
                if row["name"] == "__unresolvable__":
                    result[original_id] = original_id  # 返回原 ID
                else:
                    result[original_id] = row["name"]
            return result
        finally:
            conn.close()

    def _clear_cache(self, contact_id: str):
        """清除单个缓存条目，使下次 get_name/get_room_name 重新从 API 获取"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM contacts_cache WHERE id = %s",
                (self._cache_key(contact_id),)
            )
            conn.commit()
        finally:
            conn.close()

    def _set_cache(self, contact_id: str, contact_type: str, name: str):
        """写入数据库永久缓存"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "REPLACE INTO contacts_cache (id, type, name, updated_at) VALUES (%s, %s, %s, %s)",
                (self._cache_key(contact_id), contact_type, name, int(time.time()))
            )
            conn.commit()
        finally:
            conn.close()

    def _set_cache_with_extra(self, contact_id: str, contact_type: str, name: str, extra: str):
        """写入数据库永久缓存，附带 extra 信息（如错误码，用于区分不可解析原因）"""
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "REPLACE INTO contacts_cache (id, type, name, extra, updated_at) VALUES (%s, %s, %s, %s, %s)",
                (self._cache_key(contact_id), contact_type, name, extra, int(time.time()))
            )
            conn.commit()
        finally:
            conn.close()

    def find_unresolved(self) -> dict:
        """
        从 messages 表中查找所有未缓存的 sender 和 roomid。
        多企业时只查本企业的消息。
        返回: {"users": [id, ...], "rooms": [id, ...]}
        """
        conn = self.db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # 企业过滤条件
            corpid_filter = ""
            corpid_params = []
            if self._cache_prefix:
                corpid_filter = "AND m.corpid = %s"
                corpid_params = [self.corpid]

            # 缓存 key 前缀（JOIN 时需匹配带前缀的 key）
            if self._cache_prefix:
                join_expr = f"CONCAT('{self._cache_prefix}:', m.sender)"
                join_expr_room = f"CONCAT('{self._cache_prefix}:', m.roomid)"
            else:
                join_expr = "m.sender"
                join_expr_room = "m.roomid"

            # 查找未缓存的 sender
            cursor.execute(f"""
                SELECT DISTINCT m.sender
                FROM messages m
                LEFT JOIN contacts_cache c ON {join_expr} = c.id
                WHERE m.sender != '' AND m.sender IS NOT NULL AND c.id IS NULL
                {corpid_filter}
                LIMIT 50
            """, corpid_params)
            users = [row["sender"] for row in cursor.fetchall()]

            # 查找未缓存的 roomid（统一群信息表 wecom_groups 已有群名的不再重复解析，
            # 群名解析主责已移交 task_group_sync 独立任务，这里只兜底它还没覆盖的）
            cursor.execute(f"""
                SELECT DISTINCT m.roomid
                FROM messages m
                LEFT JOIN contacts_cache c ON {join_expr_room} = c.id
                LEFT JOIN wecom_groups g ON g.roomid = m.roomid AND g.name != ''
                WHERE m.roomid != '' AND m.roomid IS NOT NULL
                  AND c.id IS NULL AND g.roomid IS NULL
                {corpid_filter}
                LIMIT 50
            """, corpid_params)
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

        # wb 前缀是群机器人，直接标记为"通知机器人"
        for uid in wb_users:
            self._set_cache(uid, "user", "通知机器人")
            result["resolved_users"] += 1
            logger.info("自动解析联系人(机器人): %s -> 通知机器人", uid)

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
            logger.info("%s通讯录自动解析线程启动，间隔 %d 秒", self._log_prefix, interval)
            while True:
                try:
                    unresolved = self.find_unresolved()
                    total = len(unresolved["users"]) + len(unresolved["rooms"])
                    if total > 0:
                        logger.info("%s发现 %d 个未解析联系人，%d 个未解析群名，开始自动解析",
                                    self._log_prefix, len(unresolved["users"]), len(unresolved["rooms"]))
                        result = self.resolve_unresolved()
                        logger.info("%s自动解析完成: 用户 %d 成功/%d 失败, 群 %d 成功/%d 失败",
                                    self._log_prefix, result["resolved_users"], result["failed_users"],
                                    result["resolved_rooms"], result["failed_rooms"])
                except Exception as e:
                    logger.error("%s自动解析异常: %s", self._log_prefix, e)
                time.sleep(interval)

        thread_name = f"contacts-auto-resolve-{self.enterprise_name or 'main'}"
        thread = threading.Thread(target=_worker, daemon=True, name=thread_name)
        thread.start()
        logger.info("%s通讯录自动解析线程已启动", self._log_prefix)

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

        # 群：统一群信息表优先（一次批量查），其次 contacts_cache，最后现场解析
        group_names = {}
        if unique_rooms:
            try:
                from storage.group_db import get_group_name_map
                group_names = get_group_name_map(self.db, unique_rooms)
            except Exception:
                pass
        for rid in unique_rooms:
            if rid in group_names:
                rooms[rid] = group_names[rid]
            elif rid in cached:
                rooms[rid] = cached[rid]
            else:
                rooms[rid] = self.get_room_name(rid)

        return {"users": users, "rooms": rooms}
