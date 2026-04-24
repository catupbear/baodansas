"""
消息类型解析器
支持：文本、图片、语音、视频、文件、链接、名片、位置、小程序、撤回等消息类型
"""

import logging

logger = logging.getLogger(__name__)

# 消息类型映射
MSG_TYPE_MAP = {
    "text": "文本",
    "image": "图片",
    "revoke": "撤回消息",
    "agree": "同意会话聊天内容",
    "disagree": "不同意会话聊天内容",
    "voice": "语音",
    "video": "视频",
    "card": "名片",
    "location": "位置",
    "emotion": "表情",
    "file": "文件",
    "link": "链接",
    "weapp": "小程序",
    "chatrecord": "会话记录",
    "todo": "待办",
    "vote": "投票",
    "collect": "填表",
    "redpacket": "红包",
    "meeting": "会议邀请",
    "docmsg": "在线文档",
    "markdown": "Markdown",
    "news": "图文",
    "calendar": "日程",
    "mixed": "混合消息",
    "meeting_voice_call": "音频存档",
    "voip_doc_share": "音频共享文档",
    "external_redpacket": "互通红包",
    "sphfeed": "视频号",
}


class MessageParser:
    """消息解析器"""

    def parse(self, msg_data: dict) -> dict:
        """
        解析消息数据，返回统一格式
        Args:
            msg_data: 解密后的消息JSON字典
        返回: 解析后的消息字典
        """
        msgtype = msg_data.get("msgtype", "unknown")
        action = msg_data.get("action", "send")

        result = {
            "msgid": msg_data.get("msgid", ""),
            "action": action,
            "from": msg_data.get("from", ""),
            "tolist": msg_data.get("tolist", []),
            "roomid": msg_data.get("roomid", ""),
            "msgtime": msg_data.get("msgtime", 0),
            "msgtype": msgtype,
            "msgtype_cn": MSG_TYPE_MAP.get(msgtype, msgtype),
            "summary": "",
            "content": {},
        }

        # 撤回消息
        if action == "recall":
            result["summary"] = "撤回了一条消息"
            result["content"] = {"pre_msgid": msg_data.get("pre_msgid", "")}
            return result

        # 根据消息类型解析
        parser_method = getattr(self, f"_parse_{msgtype}", None)
        if parser_method:
            result["content"], result["summary"] = parser_method(msg_data)
        else:
            result["summary"] = f"[{MSG_TYPE_MAP.get(msgtype, msgtype)}]"
            result["content"] = msg_data.get(msgtype, {})

        return result

    def _parse_text(self, msg_data: dict) -> tuple[dict, str]:
        """解析文本消息"""
        content = msg_data.get("text", {}).get("content", "")
        return {"text": content}, content[:50]

    def _parse_image(self, msg_data: dict) -> tuple[dict, str]:
        """解析图片消息"""
        image = msg_data.get("image", {})
        return {
            "sdkfileid": image.get("sdkfileid", ""),
            "md5sum": image.get("md5sum", ""),
            "filesize": image.get("filesize", 0),
        }, "[图片]"

    def _parse_voice(self, msg_data: dict) -> tuple[dict, str]:
        """解析语音消息"""
        voice = msg_data.get("voice", {})
        return {
            "sdkfileid": voice.get("sdkfileid", ""),
            "voice_size": voice.get("voice_size", 0),
            "play_length": voice.get("play_length", 0),
        }, f"[语音 {voice.get('play_length', 0)}秒]"

    def _parse_video(self, msg_data: dict) -> tuple[dict, str]:
        """解析视频消息"""
        video = msg_data.get("video", {})
        return {
            "sdkfileid": video.get("sdkfileid", ""),
            "filesize": video.get("filesize", 0),
            "play_length": video.get("play_length", 0),
        }, f"[视频 {video.get('play_length', 0)}秒]"

    def _parse_file(self, msg_data: dict) -> tuple[dict, str]:
        """解析文件消息"""
        file_data = msg_data.get("file", {})
        filename = file_data.get("filename", "未知文件")
        return {
            "sdkfileid": file_data.get("sdkfileid", ""),
            "filename": filename,
            "filesize": file_data.get("filesize", 0),
            "md5sum": file_data.get("md5sum", ""),
            "fileext": file_data.get("fileext", ""),
        }, f"[文件] {filename}"

    def _parse_link(self, msg_data: dict) -> tuple[dict, str]:
        """解析链接消息"""
        link = msg_data.get("link", {})
        title = link.get("title", "")
        return {
            "title": title,
            "description": link.get("description", ""),
            "link_url": link.get("link_url", ""),
            "image_url": link.get("image_url", ""),
        }, f"[链接] {title}"

    def _parse_weapp(self, msg_data: dict) -> tuple[dict, str]:
        """解析小程序消息"""
        weapp = msg_data.get("weapp", {})
        title = weapp.get("title", "")
        return {
            "title": title,
            "description": weapp.get("description", ""),
            "username": weapp.get("username", ""),
            "displayname": weapp.get("displayname", ""),
        }, f"[小程序] {title}"

    def _parse_revoke(self, msg_data: dict) -> tuple[dict, str]:
        """解析撤回消息"""
        pre_msgid = msg_data.get("revoke", {}).get("pre_msgid", "")
        return {"pre_msgid": pre_msgid}, "撤回了一条消息"

    def _parse_card(self, msg_data: dict) -> tuple[dict, str]:
        """解析名片消息"""
        card = msg_data.get("card", {})
        corpname = card.get("corpname", "")
        userid = card.get("userid", "")
        return {
            "corpname": corpname,
            "userid": userid,
        }, f"[名片] {corpname} {userid}"

    def _parse_location(self, msg_data: dict) -> tuple[dict, str]:
        """解析位置消息"""
        location = msg_data.get("location", {})
        address = location.get("address", "")
        return {
            "longitude": location.get("longitude", 0),
            "latitude": location.get("latitude", 0),
            "address": address,
            "title": location.get("title", ""),
            "zoom": location.get("zoom", 0),
        }, f"[位置] {address}"

    def _parse_emotion(self, msg_data: dict) -> tuple[dict, str]:
        """解析表情消息"""
        emotion = msg_data.get("emotion", {})
        return {
            "sdkfileid": emotion.get("sdkfileid", ""),
            "type": emotion.get("type", 0),
            "width": emotion.get("width", 0),
            "height": emotion.get("height", 0),
            "imagesize": emotion.get("imagesize", 0),
        }, "[表情]"

    def _parse_chatrecord(self, msg_data: dict) -> tuple[dict, str]:
        """解析会话记录（合并转发）"""
        chatrecord = msg_data.get("chatrecord", {})
        title = chatrecord.get("title", "聊天记录")
        return {
            "title": title,
            "item": chatrecord.get("item", []),
        }, f"[聊天记录] {title}"

    def _parse_mixed(self, msg_data: dict) -> tuple[dict, str]:
        """解析混合消息"""
        mixed = msg_data.get("mixed", {})
        items = mixed.get("item", [])
        return {"item": items}, f"[混合消息] {len(items)}条"

    def _parse_redpacket(self, msg_data: dict) -> tuple[dict, str]:
        """解析红包消息"""
        redpacket = msg_data.get("redpacket", {})
        wish = redpacket.get("wish", "恭喜发财")
        return {
            "type": redpacket.get("type", 0),
            "wish": wish,
            "totalcnt": redpacket.get("totalcnt", 0),
            "totalamount": redpacket.get("totalamount", 0),
        }, f"[红包] {wish}"

    def _parse_docmsg(self, msg_data: dict) -> tuple[dict, str]:
        """解析在线文档消息"""
        doc = msg_data.get("doc", {})
        title = doc.get("title", "")
        return {
            "title": title,
            "doc_creator": doc.get("doc_creator", ""),
            "link_url": doc.get("link_url", ""),
        }, f"[文档] {title}"
