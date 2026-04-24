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

logger = logging.getLogger(__name__)


class ContactsManager:
    """通讯录管理器"""

    def __init__(self, corpid: str, secret: str, db):
        self.corpid = corpid
        self.secret = secret
        self.db = db
        self._access_token = ""
        self._token_expires = 0
        # 初始化缓存表
        self._init_cache_table()

    def _init_cache_table(self):
        """创建昵称缓存表"""
        cursor = self.db.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contacts_cache (
                id TEXT PRIMARY KEY,
                type TEXT,
                name TEXT,
                avatar TEXT DEFAULT '',
                extra TEXT DEFAULT '{}',
                updated_at INTEGER
            )
        """)
        self.db.conn.commit()

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
        """从缓存读取昵称（24小时有效）"""
        cursor = self.db.conn.cursor()
        cursor.execute(
            "SELECT name, updated_at FROM contacts_cache WHERE id = ?",
            (contact_id,)
        )
        row = cursor.fetchone()
        if row:
            # 24小时缓存
            if int(time.time()) - row["updated_at"] < 86400:
                return row["name"]
        return ""

    def _set_cache(self, contact_id: str, contact_type: str, name: str):
        """写入缓存"""
        cursor = self.db.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO contacts_cache (id, type, name, updated_at)
            VALUES (?, ?, ?, ?)
        """, (contact_id, contact_type, name, int(time.time())))
        self.db.conn.commit()

    def batch_resolve(self, user_ids: list, room_ids: list) -> dict:
        """
        批量解析昵称
        返回: {"users": {id: name}, "rooms": {id: name}}
        """
        users = {}
        rooms = {}
        for uid in set(user_ids):
            if uid:
                users[uid] = self.get_name(uid)
        for rid in set(room_ids):
            if rid:
                rooms[rid] = self.get_room_name(rid)
        return {"users": users, "rooms": rooms}
