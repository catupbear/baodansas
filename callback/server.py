"""
Flask 回调接收服务
处理企业微信的 URL 验证（GET）和消息回调（POST）
"""

import logging
import xml.etree.ElementTree as ET

from flask import Blueprint, request

from callback.crypto import WXBizMsgCrypt
from core.notify import notify_error

logger = logging.getLogger(__name__)

callback_bp = Blueprint("callback", __name__)

# 全局引用，在 main.py 中初始化
_crypto: WXBizMsgCrypt = None
_on_message_callback = None


def init_callback(crypto: WXBizMsgCrypt, on_message_callback=None):
    """初始化回调模块"""
    global _crypto, _on_message_callback
    _crypto = crypto
    _on_message_callback = on_message_callback


@callback_bp.route("/callback", methods=["GET"])
def verify_url():
    """
    企业微信验证回调URL（GET请求）
    参数：msg_signature, timestamp, nonce, echostr
    返回解密后的 echostr 明文
    """
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echostr = request.args.get("echostr", "")

    logger.info("收到URL验证请求: timestamp=%s, nonce=%s", timestamp, nonce)

    ret, reply_echostr = _crypto.verify_url(msg_signature, timestamp, nonce, echostr)
    if ret != 0:
        logger.error("URL验证失败, 错误码: %d", ret)
        return "验证失败", 403

    logger.info("URL验证成功")
    return reply_echostr


@callback_bp.route("/callback", methods=["POST"])
def receive_callback():
    """
    接收企业微信回调事件（POST请求）
    收到 msgaudit_notify 事件后触发消息拉取
    """
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    post_data = request.data.decode("utf-8")

    logger.info("收到回调通知: timestamp=%s", timestamp)

    # 解密回调消息
    ret, xml_content = _crypto.decrypt_msg(post_data, msg_signature, timestamp, nonce)
    if ret != 0:
        logger.error("回调消息解密失败, 错误码: %d", ret)
        notify_error("回调服务", "receive_callback", f"回调消息解密失败, 错误码: {ret}")
        return "解密失败", 403

    # 解析XML
    try:
        tree = ET.fromstring(xml_content)
        msg_type = tree.find("MsgType").text
        event = tree.find("Event")
        event_type = event.text if event is not None else ""

        logger.info("回调事件: MsgType=%s, Event=%s", msg_type, event_type)

        # 会话存档回调事件
        if msg_type == "event" and event_type == "msgaudit_notify":
            logger.info("收到会话存档通知，开始拉取新消息")
            if _on_message_callback:
                _on_message_callback()
    except Exception as e:
        logger.error("解析回调XML失败: %s", e)
        notify_error("回调服务", "receive_callback", f"解析回调XML失败: {e}")
        return "解析失败", 500

    # 必须返回 "success" 或空字符串，否则企业微信会重试
    return "success"


def create_callback_blueprint(path, crypto, on_message_callback, name_suffix):
    """为指定路径创建独立的回调 Blueprint（支持多组回调配置）"""
    bp = Blueprint(f"callback{name_suffix}", __name__)

    @bp.route(path, methods=["GET"])
    def verify_url():
        msg_signature = request.args.get("msg_signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echostr = request.args.get("echostr", "")

        logger.info("[%s] 收到URL验证请求: timestamp=%s, nonce=%s", path, timestamp, nonce)

        ret, reply_echostr = crypto.verify_url(msg_signature, timestamp, nonce, echostr)
        if ret != 0:
            logger.error("[%s] URL验证失败, 错误码: %d", path, ret)
            return "验证失败", 403

        logger.info("[%s] URL验证成功", path)
        return reply_echostr

    @bp.route(path, methods=["POST"])
    def receive_callback():
        msg_signature = request.args.get("msg_signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        post_data = request.data.decode("utf-8")

        logger.info("[%s] 收到回调通知: timestamp=%s", path, timestamp)

        ret, xml_content = crypto.decrypt_msg(post_data, msg_signature, timestamp, nonce)
        if ret != 0:
            logger.error("[%s] 回调消息解密失败, 错误码: %d", path, ret)
            return "解密失败", 403

        try:
            tree = ET.fromstring(xml_content)
            msg_type = tree.find("MsgType").text
            event = tree.find("Event")
            event_type = event.text if event is not None else ""

            logger.info("[%s] 回调事件: MsgType=%s, Event=%s", path, msg_type, event_type)

            if msg_type == "event" and event_type == "msgaudit_notify":
                logger.info("[%s] 收到会话存档通知，开始拉取新消息", path)
                if on_message_callback:
                    on_message_callback()
        except Exception as e:
            logger.error("[%s] 解析回调XML失败: %s", path, e)
            return "解析失败", 500

        return "success"

    return bp
