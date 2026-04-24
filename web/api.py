"""
前端数据查询 API
"""

import threading

from flask import Blueprint, jsonify, request

from storage.db import Database

api_bp = Blueprint("api", __name__, url_prefix="/api")

_db: Database = None
_fetcher = None
_fetch_lock = threading.Lock()
_fetching = False


def init_api(db: Database, fetcher=None):
    """初始化 API 模块"""
    global _db, _fetcher
    _db = db
    _fetcher = fetcher


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
