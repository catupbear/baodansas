"""
导出行为审计模块（内部功能，仅超级管理员可查看）

记录系统所有导出行为（页面导出 + 群命令导出）：操作人、所属企业、导出参数、
数据条数，并将导出产物副本异步上传 COS（export_audit/ 目录）供回看。

设计约束：
- 审计绝不阻断客户导出主流程：record_export() 内部整体 try/except，任何异常只打日志
- 副本上传在后台单线程队列执行，主流程只做一次轻量 INSERT 后立即返回
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

# 模块级引用，由 init_export_audit() 注入
_db = None
_cos = None

# 副本上传队列：单线程串行消费（与保单识别 Queue 同思路，控制内存与并发）
_upload_executor = None
_executor_lock = threading.Lock()

# 导出类型常量
EXPORT_TYPE_LEDGER_EXCEL = "ledger_excel"        # 页面台账 Excel
EXPORT_TYPE_ENTERPRISE_REPORT = "enterprise_report"  # 企业经营报告
EXPORT_TYPE_BATCH_ZIP = "batch_zip"              # 批量下载保单原文 ZIP
EXPORT_TYPE_LEDGER_CMD = "ledger_cmd"            # 群命令台账
EXPORT_TYPE_RENEWAL = "renewal"                  # 续保任务导出


def init_export_audit(db, cos_storage=None):
    """注入依赖并建表（幂等），在 main.py 启动时调用"""
    global _db, _cos
    _db = db
    _cos = cos_storage
    init_export_records_table(db)


def init_export_records_table(db):
    """建立导出审计记录表（幂等）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS export_records (
                id              INT PRIMARY KEY AUTO_INCREMENT,
                user_id         INT DEFAULT NULL COMMENT '操作人 users.id（群命令为绑定账号）',
                user_name       VARCHAR(64)  DEFAULT '' COMMENT '操作人姓名（冗余，防账号删除后无法追溯）',
                enterprise_id   INT          DEFAULT NULL COMMENT '导出数据所属企业',
                enterprise_name VARCHAR(128) DEFAULT '' COMMENT '企业名称（冗余）',
                export_type     VARCHAR(32)  NOT NULL COMMENT 'ledger_excel/enterprise_report/batch_zip/ledger_cmd/renewal',
                trigger_way     VARCHAR(16)  NOT NULL DEFAULT 'web' COMMENT 'web=页面 / cmd=群命令',
                params          TEXT COMMENT '导出时的完整参数 JSON',
                row_count       INT          DEFAULT 0 COMMENT '本次导出数据条数',
                filename        VARCHAR(256) DEFAULT '' COMMENT '导出文件名',
                file_size       INT          DEFAULT 0 COMMENT '文件大小（字节）',
                cos_key         VARCHAR(512) DEFAULT '' COMMENT '审计副本 COS 路径',
                cos_url         TEXT COMMENT '审计副本访问地址',
                copy_status     VARCHAR(16)  DEFAULT 'none' COMMENT '副本上传状态 pending/done/failed/none',
                status          VARCHAR(16)  DEFAULT 'success' COMMENT 'success / failed',
                error_message   TEXT COMMENT '失败原因',
                roomid          VARCHAR(128) DEFAULT '' COMMENT '群命令场景：触发群ID',
                trigger_text    VARCHAR(128) DEFAULT '' COMMENT '群命令场景：原始命令文本',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '导出时间',
                INDEX idx_export_enterprise (enterprise_id),
                INDEX idx_export_type (export_type),
                INDEX idx_export_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='导出行为审计记录（内部功能）'
        """)
        conn.commit()
        logger.info("export_records 表初始化完成")
    finally:
        conn.close()


def _get_executor():
    """懒加载后台上传线程池（单线程串行）"""
    global _upload_executor
    if _upload_executor is None:
        with _executor_lock:
            if _upload_executor is None:
                _upload_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="export-audit-upload")
    return _upload_executor


def record_export(*, user_id=None, user_name="", enterprise_id=None, enterprise_name="",
                  export_type, trigger_way="web", params=None, row_count=0,
                  filename="", file_bytes=None, cos_key="", cos_url="",
                  status="success", error_message="", roomid="", trigger_text=""):
    """
    记录一次导出行为。内部整体 try/except，任何异常只打日志，绝不影响导出主流程。

    - file_bytes 传入时：先写库（copy_status=pending）立即返回，
      bytes 交给后台单线程队列异步上传 COS，完成后回写 cos_key/cos_url/copy_status
    - cos_url 传入时（群命令场景，文件已在 COS）：直接记录，copy_status=done
    - 都不传：copy_status=none（如 ZIP 类型 / 失败记录）
    """
    try:
        if _db is None:
            return None

        # 冗余补齐操作人姓名 / 企业名称（查不到不影响记录）
        if user_id and not user_name:
            user_name = _lookup_user_name(user_id)
        if enterprise_id and not enterprise_name:
            enterprise_name = _lookup_enterprise_name(enterprise_id)

        if file_bytes and _cos:
            copy_status = "pending"
        elif cos_url:
            copy_status = "done"
        elif file_bytes and not _cos:
            copy_status = "failed"
            error_message = error_message or "COS 未配置，副本未上传"
        else:
            copy_status = "none"

        params_json = ""
        try:
            params_json = json.dumps(params or {}, ensure_ascii=False, default=str)
        except Exception:
            params_json = str(params)[:2000]

        file_size = len(file_bytes) if file_bytes else 0

        conn = _db.pool.connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO export_records
                    (user_id, user_name, enterprise_id, enterprise_name, export_type,
                     trigger_way, params, row_count, filename, file_size,
                     cos_key, cos_url, copy_status, status, error_message,
                     roomid, trigger_text)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, (user_name or "")[:64], enterprise_id, (enterprise_name or "")[:128],
                  export_type, trigger_way, params_json, int(row_count or 0),
                  (filename or "")[:256], file_size, (cos_key or "")[:512], cos_url or "",
                  copy_status, status, error_message or "", (roomid or "")[:128],
                  (trigger_text or "")[:128]))
            record_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        # 副本交给后台队列异步上传，客户主流程零等待
        if copy_status == "pending":
            _get_executor().submit(_upload_copy, record_id, file_bytes,
                                   export_type, enterprise_id, user_id, filename)
        return record_id
    except Exception as e:
        logger.warning("导出审计记录失败（不影响主流程）type=%s: %s", export_type, e)
        return None


def _upload_copy(record_id, file_bytes, export_type, enterprise_id, user_id, filename):
    """后台线程：上传审计副本到 COS 并回写记录"""
    try:
        ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else "xlsx"
        cos_key_suffix = (f"export_audit/{datetime.now().strftime('%Y%m')}/"
                          f"{enterprise_id or 0}/{int(time.time())}_{export_type}_{user_id or 0}.{ext}")
        url = _cos.upload_bytes(file_bytes, cos_key_suffix, download_filename=filename or "")
        if url:
            _update_copy_status(record_id, "done", cos_key_suffix, url, "")
        else:
            _update_copy_status(record_id, "failed", "", "", "副本上传 COS 失败")
    except Exception as e:
        logger.warning("导出审计副本上传失败 record_id=%s: %s", record_id, e)
        try:
            _update_copy_status(record_id, "failed", "", "", f"副本上传异常: {str(e)[:200]}")
        except Exception:
            pass


def _update_copy_status(record_id, copy_status, cos_key, cos_url, error_message):
    conn = _db.pool.connection()
    try:
        cursor = conn.cursor()
        if error_message:
            cursor.execute("""
                UPDATE export_records SET copy_status=%s, cos_key=%s, cos_url=%s,
                    error_message=CONCAT(IFNULL(error_message,''), %s)
                WHERE id=%s
            """, (copy_status, cos_key, cos_url, error_message, record_id))
        else:
            cursor.execute("""
                UPDATE export_records SET copy_status=%s, cos_key=%s, cos_url=%s WHERE id=%s
            """, (copy_status, cos_key, cos_url, record_id))
        conn.commit()
    finally:
        conn.close()


def _lookup_user_name(user_id) -> str:
    try:
        from auth.db import get_user_by_id
        user = get_user_by_id(_db, user_id)
        if user:
            return user.get("name") or user.get("phone") or ""
    except Exception:
        pass
    return ""


def _lookup_enterprise_name(enterprise_id) -> str:
    try:
        from auth.db import get_enterprise_by_id
        ent = get_enterprise_by_id(_db, enterprise_id)
        if ent:
            return ent.get("name") or ent.get("phone") or ""
    except Exception:
        pass
    return ""


# ------------------------------------------------------------------ #
# 查询（仅供超管审计页面使用）
# ------------------------------------------------------------------ #

def query_export_records(db, page=1, page_size=20, enterprise_id=None,
                         export_type="", trigger_way="", user_keyword="",
                         date_start="", date_end=""):
    """分页查询导出审计记录，附带汇总统计（总次数/总条数/群命令次数/失败次数）"""
    where = ["1=1"]
    args = []
    if enterprise_id:
        where.append("enterprise_id = %s")
        args.append(int(enterprise_id))
    if export_type:
        where.append("export_type = %s")
        args.append(export_type)
    if trigger_way:
        where.append("trigger_way = %s")
        args.append(trigger_way)
    if user_keyword:
        where.append("user_name LIKE %s")
        args.append(f"%{user_keyword}%")
    if date_start:
        where.append("created_at >= %s")
        args.append(f"{date_start} 00:00:00")
    if date_end:
        where.append("created_at <= %s")
        args.append(f"{date_end} 23:59:59")
    where_sql = " AND ".join(where)

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(f"""
            SELECT COUNT(*) AS total,
                   IFNULL(SUM(row_count), 0) AS total_rows,
                   IFNULL(SUM(trigger_way = 'cmd'), 0) AS cmd_count,
                   IFNULL(SUM(status = 'failed'), 0) AS failed_count
            FROM export_records WHERE {where_sql}
        """, args)
        summary = cursor.fetchone() or {}

        offset = (max(int(page), 1) - 1) * int(page_size)
        cursor.execute(f"""
            SELECT * FROM export_records WHERE {where_sql}
            ORDER BY id DESC LIMIT %s OFFSET %s
        """, args + [int(page_size), offset])
        rows = cursor.fetchall() or []
        for r in rows:
            try:
                r["params"] = json.loads(r.get("params") or "{}")
            except Exception:
                pass
            if isinstance(r.get("created_at"), datetime):
                r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        return {
            "total": int(summary.get("total", 0)),
            "list": rows,
            "summary": {
                "total_exports": int(summary.get("total", 0)),
                "total_rows": int(summary.get("total_rows", 0)),
                "cmd_count": int(summary.get("cmd_count", 0)),
                "failed_count": int(summary.get("failed_count", 0)),
            },
        }
    finally:
        conn.close()


def get_export_record(db, record_id):
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM export_records WHERE id = %s", (record_id,))
        return cursor.fetchone()
    finally:
        conn.close()
