"""
前端数据查询 API
"""

from flask import Blueprint, jsonify, request

from storage.db import Database

api_bp = Blueprint("api", __name__, url_prefix="/api")

_db: Database = None


def init_api(db: Database):
    """初始化 API 模块"""
    global _db
    _db = db


@api_bp.route("/messages")
def get_messages():
    """
    分页查询消息
    参数: page, page_size, msgtype, sender, roomid, keyword
    """
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)
    msgtype = request.args.get("msgtype", "")
    sender = request.args.get("sender", "")
    roomid = request.args.get("roomid", "")
    keyword = request.args.get("keyword", "")

    page_size = min(page_size, 200)

    result = _db.query_messages(
        page=page, page_size=page_size,
        msgtype=msgtype, sender=sender,
        roomid=roomid, keyword=keyword,
    )
    return jsonify(result)


@api_bp.route("/stats")
def get_stats():
    """获取统计信息"""
    return jsonify(_db.get_stats())
