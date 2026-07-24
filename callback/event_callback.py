"""
企业微信「客户联系」应用事件回调（change_external_chat 群变更）。

职责（最小实现）：
  - URL 验证（GET）
  - 接收 change_external_chat(create/update) 事件 → 拉群详情 → 刷新 wecom_groups
    → 群名变化时触发识别群自动匹配（insurance.auto_monitor_match）

本期不处理 change_external_contact（客户变更）等其他事件，未识别事件一律返回 success
（避免企业微信侧持续重试）。设计详见
docs/review/2026-07-22-群名称与客户变更事件回调-方案原型.html
"""

import logging
import threading
import xml.etree.ElementTree as ET

from flask import Blueprint, request

from callback.crypto import WXBizMsgCrypt
from storage import group_db
from insurance import auto_monitor_match

logger = logging.getLogger(__name__)

# 全局引用，在 main.py 中初始化
_crypto: WXBizMsgCrypt = None
_db = None
_corpid = ""
_get_contacts = None   # callable(corpid) -> ContactsManager


def init_event_callback(crypto, db, corpid, get_contacts):
    """初始化事件回调模块。

    Args:
        crypto:       该企业「客户联系」应用专属的 WXBizMsgCrypt（独立 token/aes_key）
        db:           Database 实例
        corpid:       事件回调归属企业 corpid
        get_contacts: 传入 web.api.get_contacts_for_corp，按 corpid 复用已注册的
                      ContactsManager（不重复创建实例、不重复存 secret）
    """
    global _crypto, _db, _corpid, _get_contacts
    _crypto = crypto
    _db = db
    _corpid = corpid
    _get_contacts = get_contacts


def create_event_callback_blueprint(path: str):
    """创建事件回调 Blueprint（GET 验签 + POST 事件分发）。"""
    bp = Blueprint("event_callback", __name__)

    @bp.route(path, methods=["GET"])
    def verify_url():
        msg_signature = request.args.get("msg_signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echostr = request.args.get("echostr", "")

        logger.info("[企业微信回调] 收到URL验证请求: timestamp=%s, nonce=%s", timestamp, nonce)
        ret, reply = _crypto.verify_url(msg_signature, timestamp, nonce, echostr)
        if ret != 0:
            logger.error("[企业微信回调] URL验证失败, 错误码: %d", ret)
            return "验证失败", 403
        logger.info("[企业微信回调] URL验证成功")
        return reply

    @bp.route(path, methods=["POST"])
    def receive_event():
        msg_signature = request.args.get("msg_signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        post_data = request.data.decode("utf-8")

        ret, xml_content = _crypto.decrypt_msg(post_data, msg_signature, timestamp, nonce)
        if ret != 0:
            logger.error("[企业微信回调] 回调消息解密失败, 错误码: %d", ret)
            return "解密失败", 403

        try:
            tree = ET.fromstring(xml_content)
            event = (tree.findtext("Event") or "").strip()
            change_type = (tree.findtext("ChangeType") or "").strip()
            chat_id = (tree.findtext("ChatId") or "").strip()
            logger.info("[企业微信回调] 事件: Event=%s, ChangeType=%s, ChatId=%s",
                        event, change_type, chat_id)

            # 只处理群变更（新建/信息更新），在独立线程里拉详情+落库，避免阻塞回调响应
            if event == "change_external_chat" and change_type in ("create", "update") and chat_id:
                threading.Thread(
                    target=_handle_group_change, args=(chat_id,), daemon=True
                ).start()
        except Exception as e:
            logger.error("[企业微信回调] 解析回调XML失败: %s", e)
            return "解析失败", 500

        # 必须返回 success，否则企业微信会持续重试
        return "success"

    return bp


def _handle_group_change(chat_id: str):
    """拉群详情 → 刷新 wecom_groups → 群名变化时触发识别群自动匹配。"""
    try:
        contacts = _get_contacts(_corpid) if _get_contacts else None
        if contacts is None:
            logger.warning("[企业微信回调] 无可用通讯录，跳过群 %s", chat_id)
            return

        # ContactsManager._fetch_external_room_info 返回群名字符串（同时缓存群成员名）
        name = contacts._fetch_external_room_info(chat_id)
        if not name or name in ("__skipped__", "__unresolvable__"):
            logger.info("[企业微信回调] 群 %s 未取到有效群名(%r)，跳过", chat_id, name)
            return

        # 保留已有 owner/member_count（该接口只给群名，不覆盖为空值）
        existing = group_db.get_group(_db, chat_id)
        name_changed = group_db.update_group_success(
            _db, chat_id, _corpid, name,
            owner=(existing or {}).get("owner", "") or "",
            member_count=(existing or {}).get("member_count", 0) or 0,
            group_type=(existing or {}).get("group_type", "") or "external",
        )
        if name_changed:
            logger.info("[企业微信回调] 群名变化: %s -> 「%s」", chat_id, name)
            auto_monitor_match.check_and_sync_group(_db, chat_id, name, _corpid)
    except Exception:
        logger.exception("[企业微信回调] 处理群变更失败: %s", chat_id)
