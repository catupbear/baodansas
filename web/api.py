"""
前端数据查询 API
"""

import hashlib
import json
import os
import threading

import pymysql
import pymysql.cursors
from flask import Blueprint, jsonify, redirect, request, send_file

from storage.db import Database
from auth.decorators import login_required

api_bp = Blueprint("api", __name__, url_prefix="/api")

_db: Database = None
_fetcher = None
_finance_sdk = None
_sdk_config = {}
_contacts = None
_cos = None
_fetch_lock = threading.Lock()
_fetching = False
# corpid -> FinanceSDK 映射（多企业媒体下载用）
_sdk_map = {}
# corpid -> ContactsManager 映射（多企业通讯录解析用）
_contacts_map = {}

# 媒体文件本地临时目录
MEDIA_DIR = "./data/media"


def init_api(db: Database, fetcher=None, finance_sdk=None, sdk_config: dict = None,
             contacts=None, cos_storage=None):
    """初始化 API 模块"""
    global _db, _fetcher, _finance_sdk, _sdk_config, _contacts, _cos
    _db = db
    _fetcher = fetcher
    _finance_sdk = finance_sdk
    _sdk_config = sdk_config or {}
    _contacts = contacts
    _cos = cos_storage


def register_extra_sdk(corpid: str, sdk):
    """注册额外企业的 SDK（多企业媒体下载用）"""
    _sdk_map[corpid] = sdk


def register_extra_contacts(corpid: str, contacts):
    """注册额外企业的通讯录（多企业通讯录解析用）"""
    _contacts_map[corpid] = contacts


# 企业列表（前端切换用）
_enterprises = []


def register_enterprises(enterprises: list):
    """注册企业列表 [{"corpid": "xxx", "name": "午虎科技"}, ...]"""
    global _enterprises
    _enterprises = enterprises


# 全局鉴权：消息查询 API 均需登录
@api_bp.before_request
def _require_login():
    """消息查询 API 统一鉴权"""
    from auth.jwt_utils import verify_token
    from auth.db import get_user_by_id
    from flask import g

    # 媒体文件和企业列表接口跳过 JWT 鉴权（监控页面通过 session 访问）
    if request.path.startswith("/api/media/") or request.path == "/api/enterprises":
        return None

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else request.args.get("token")
    if not token:
        return jsonify({"code": 401, "msg": "未登录"}), 401

    payload = verify_token(token)
    if not payload:
        return jsonify({"code": 401, "msg": "登录已过期，请重新登录"}), 401

    user = get_user_by_id(_db, payload["user_id"])
    if not user or not user.get("enabled"):
        return jsonify({"code": 401, "msg": "账号已禁用"}), 401

    g.current_user = {
        "user_id": payload["user_id"],
        "phone": payload.get("phone", payload.get("username", "")),
        "role": payload["role"],
        "parent_id": user.get("parent_id"),
    }


@api_bp.route("/enterprises")
def get_enterprises():
    """获取企业列表（前端切换用）"""
    return jsonify(_enterprises)


@api_bp.route("/conversations")
def get_conversations():
    """
    获取会话列表
    参数: keyword（搜索关键词）, corpid（企业过滤）
    """
    keyword = request.args.get("keyword", "")
    corpid = request.args.get("corpid", "")
    conversations = _db.get_conversations(keyword=keyword, corpid=corpid)

    # 批量解析昵称（根据 corpid 选择对应的通讯录）
    contacts = _contacts_map.get(corpid, _contacts) if corpid else _contacts
    if contacts and conversations:
        user_ids = []
        room_ids = []
        for conv in conversations:
            if conv["roomid"]:
                room_ids.append(conv["roomid"])
            if conv.get("last_sender"):
                user_ids.append(conv["last_sender"])
            # 私聊会话：收集双方 ID
            for pid in conv.get("peer_ids", []):
                if pid:
                    user_ids.append(pid)
        names = contacts.batch_resolve(user_ids, room_ids)
        for conv in conversations:
            if conv["roomid"]:
                conv["display_name"] = names["rooms"].get(conv["roomid"], conv["roomid"])
            elif conv.get("peer_ids"):
                # 私聊：显示双方名称 "A ↔ B"
                peer_names = [names["users"].get(pid, pid) for pid in conv["peer_ids"]]
                conv["display_name"] = " ↔ ".join(peer_names)
            else:
                conv["display_name"] = conv["conversation_id"]
            conv["last_sender_name"] = names["users"].get(conv.get("last_sender", ""), conv.get("last_sender", ""))

    return jsonify(conversations)


@api_bp.route("/messages")
def get_messages():
    """
    分页查询消息
    参数: page, page_size, msgtype, sender, roomid, keyword, order, conversation_id
    """
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)
    msgtype = request.args.get("msgtype", "")
    sender = request.args.get("sender", "")
    roomid = request.args.get("roomid", "")
    keyword = request.args.get("keyword", "")
    order = request.args.get("order", "desc")
    conversation_id = request.args.get("conversation_id", "")
    corpid = request.args.get("corpid", "")

    page_size = min(page_size, 200)

    result = _db.query_messages(
        page=page, page_size=page_size,
        msgtype=msgtype, sender=sender,
        roomid=roomid, keyword=keyword,
        order=order, conversation_id=conversation_id,
        corpid=corpid,
    )

    # 批量解析昵称（根据 corpid 选择对应的通讯录）
    contacts = _contacts_map.get(corpid, _contacts) if corpid else _contacts
    if contacts and result["messages"]:
        user_ids = [m["sender"] for m in result["messages"]]
        room_ids = [m["roomid"] for m in result["messages"]]
        # 收集 tolist 中的用户 ID
        for m in result["messages"]:
            try:
                to_ids = json.loads(m.get("tolist") or "[]")
                user_ids.extend(to_ids)
            except (json.JSONDecodeError, TypeError):
                pass
        names = contacts.batch_resolve(user_ids, room_ids)
        for msg in result["messages"]:
            msg["sender_name"] = names["users"].get(msg["sender"], msg["sender"])
            msg["room_name"] = names["rooms"].get(msg["roomid"], msg["roomid"])
            # 解析接收人名称
            try:
                to_ids = json.loads(msg.get("tolist") or "[]")
                msg["tolist_names"] = [names["users"].get(tid, tid) for tid in to_ids if tid]
            except (json.JSONDecodeError, TypeError):
                msg["tolist_names"] = []

            # 检查是否有 COS URL
            parsed = json.loads(msg.get("parsed_content") or "{}")
            msg["media_url"] = parsed.get("cos_url", "")

    return jsonify(result)


@api_bp.route("/stats")
def get_stats():
    """获取统计信息"""
    stats = _db.get_stats()
    stats["fetching"] = _fetching
    return jsonify(stats)


@api_bp.route("/fetch", methods=["POST"])
def manual_fetch():
    """手动触发拉取消息"""
    global _fetching
    if _fetcher is None:
        return jsonify({"error": "SDK未初始化，无法拉取"}), 503
    if _fetching:
        return jsonify({"error": "正在拉取中，请稍候"}), 429

    def do_fetch():
        global _fetching
        _fetching = True
        try:
            _fetcher.fetch_new_messages()
        finally:
            _fetching = False

    thread = threading.Thread(target=do_fetch, daemon=True)
    thread.start()
    return jsonify({"message": "开始拉取消息"})


# 媒体文件类型对应的扩展名
MEDIA_EXT = {
    "image": ".jpg",
    "voice": ".amr",
    "video": ".mp4",
    "emotion": ".gif",
}


@api_bp.route("/media/<int:msg_seq>")
def get_media(msg_seq):
    """
    下载/预览媒体文件
    如果COS上已有，直接302重定向到COS URL
    否则从SDK下载 → 上传COS → 重定向
    """
    if _finance_sdk is None:
        return jsonify({"error": "SDK未初始化"}), 503

    # 从数据库查消息（通过连接池获取连接）
    conn = _db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT msgtype, parsed_content, raw_data, corpid FROM messages WHERE seq = %s",
            (msg_seq,)
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "消息不存在"}), 404

        msgtype = row["msgtype"]
        parsed = json.loads(row["parsed_content"] or "{}")

        # 如果已有COS URL，直接重定向
        cos_url = parsed.get("cos_url", "")
        if cos_url:
            return redirect(cos_url)

        # 获取 sdkfileid
        sdkfileid = parsed.get("sdkfileid", "")
        if not sdkfileid:
            return jsonify({"error": "该消息无媒体文件"}), 404

        # 文件名和扩展名
        orig_filename = parsed.get("filename", "")
        if orig_filename:
            ext = os.path.splitext(orig_filename)[1] or MEDIA_EXT.get(msgtype, "")
        else:
            ext = MEDIA_EXT.get(msgtype, "")
            orig_filename = f"{msg_seq}{ext}"

        # 用 sdkfileid 的 hash 作为文件名
        file_hash = hashlib.md5(sdkfileid.encode()).hexdigest()
        cos_filename = f"{file_hash}{ext}"
        save_path = os.path.join(MEDIA_DIR, cos_filename)

        # 下载媒体文件到本地（根据 corpid 选择对应的 SDK，失败时尝试其他 SDK）
        if not os.path.exists(save_path):
            os.makedirs(MEDIA_DIR, exist_ok=True)
            msg_corpid = row.get("corpid", "")
            # 构建尝试顺序：优先匹配 corpid 的 SDK，然后主 SDK，最后其他 SDK
            sdks_to_try = []
            if msg_corpid and msg_corpid in _sdk_map:
                sdks_to_try.append(("matched", _sdk_map[msg_corpid]))
            sdks_to_try.append(("main", _finance_sdk))
            for cid, s in _sdk_map.items():
                if s not in [t[1] for t in sdks_to_try]:
                    sdks_to_try.append((cid, s))

            ret = -1
            for sdk_label, sdk in sdks_to_try:
                ret, _ = sdk.get_media_data(
                    sdk_file_id=sdkfileid,
                    proxy=_sdk_config.get("proxy", ""),
                    passwd=_sdk_config.get("proxy_passwd", ""),
                    timeout=_sdk_config.get("timeout", 10),
                    save_path=save_path,
                )
                if ret == 0:
                    # 下载成功，回填 corpid（修复历史消息缺失 corpid 的问题）
                    if not msg_corpid and sdk_label != "main":
                        cursor.execute(
                            "UPDATE messages SET corpid = %s WHERE seq = %s",
                            (sdk_label, msg_seq)
                        )
                        conn.commit()
                    break
            if ret != 0:
                return jsonify({"error": f"下载失败，错误码: {ret}"}), 500

        # 上传到 COS
        if _cos:
            cos_url = _cos.upload_file(save_path, cos_filename)
            if cos_url:
                # 把 COS URL 写回数据库
                parsed["cos_url"] = cos_url
                cursor.execute(
                    "UPDATE messages SET parsed_content = %s WHERE seq = %s",
                    (json.dumps(parsed, ensure_ascii=False), msg_seq)
                )
                conn.commit()
                # 删除本地临时文件
                try:
                    os.remove(save_path)
                except OSError:
                    pass
                return redirect(cos_url)
    finally:
        conn.close()

    # 没有COS时直接返回本地文件
    mime_map = {
        "image": "image/jpeg",
        "voice": "audio/amr",
        "video": "video/mp4",
        "emotion": "image/gif",
        "file": "application/octet-stream",
    }
    mimetype = mime_map.get(msgtype, "application/octet-stream")
    as_attachment = msgtype == "file"
    return send_file(save_path, mimetype=mimetype, as_attachment=as_attachment, download_name=orig_filename)


@api_bp.route("/contacts/resolve", methods=["POST"])
def resolve_contacts():
    """批量解析昵称"""
    if _contacts is None:
        return jsonify({"error": "通讯录模块未初始化"}), 503

    data = request.get_json() or {}
    user_ids = data.get("user_ids", [])
    room_ids = data.get("room_ids", [])
    result = _contacts.batch_resolve(user_ids, room_ids)
    return jsonify(result)


@api_bp.route("/contacts/sync", methods=["POST"])
def sync_contacts():
    """手动同步通讯录：强制刷新所有群和联系人的名称缓存"""
    if _contacts is None:
        return jsonify({"error": "通讯录模块未初始化"}), 503
    if _db is None:
        return jsonify({"error": "数据库未初始化"}), 503

    conn = _db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 所有群 ID
        cursor.execute(
            "SELECT DISTINCT roomid FROM messages WHERE roomid IS NOT NULL AND roomid != ''"
        )
        room_ids = [r["roomid"] for r in cursor.fetchall()]
        # 所有用户 ID
        cursor.execute(
            "SELECT DISTINCT sender FROM messages WHERE sender IS NOT NULL AND sender != ''"
        )
        user_ids = [r["sender"] for r in cursor.fetchall()]
    finally:
        conn.close()

    synced_rooms = 0
    synced_users = 0

    for rid in room_ids:
        try:
            name = _contacts.get_room_name(rid)
            if name and name != rid:
                synced_rooms += 1
        except Exception:
            pass

    for uid in user_ids:
        try:
            name = _contacts.get_name(uid)
            if name and name != uid:
                synced_users += 1
        except Exception:
            pass

    total = len(room_ids) + len(user_ids)
    synced = synced_rooms + synced_users
    return jsonify({
        "success": True,
        "message": f"同步完成：共 {total} 项，成功解析 {synced} 项（群 {synced_rooms}/{len(room_ids)}，联系人 {synced_users}/{len(user_ids)}）",
        "synced_rooms": synced_rooms,
        "synced_users": synced_users,
        "total_rooms": len(room_ids),
        "total_users": len(user_ids),
    })


@api_bp.route("/contacts/update", methods=["POST"])
def update_contact():
    """手动设置联系人/群名称（解决企微 API 获取失败的情况）"""
    if _contacts is None:
        return jsonify({"error": "通讯录模块未初始化"}), 503

    data = request.get_json() or {}
    contact_id = data.get("id", "").strip()
    name = data.get("name", "").strip()
    contact_type = data.get("type", "user")  # 'user' 或 'room'

    if not contact_id or not name:
        return jsonify({"error": "id 和 name 不能为空"}), 400

    try:
        _contacts._set_cache(contact_id, contact_type, name)

        # 同步更新 insurance_records 表中的冗余名称字段
        if _db:
            try:
                conn = _db.pool.connection()
                try:
                    cursor = conn.cursor()
                    if contact_type == "room":
                        cursor.execute(
                            "UPDATE insurance_records SET room_name = %s WHERE roomid = %s",
                            (name, contact_id)
                        )
                    else:
                        cursor.execute(
                            "UPDATE insurance_records SET sender_name = %s WHERE sender = %s",
                            (name, contact_id)
                        )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass  # 非关键操作，不影响主流程

        return jsonify({"success": True, "message": f"已更新: {contact_id} → {name}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
