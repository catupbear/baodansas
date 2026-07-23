"""
保单识别模块 — 数据库层
负责保单识别记录和配置项的增删改查（MySQL 版本）
"""

import json
import logging
from datetime import datetime

import pymysql
import pymysql.cursors

from .field_mapping import apply_mapping
from .field_config_db import get_column_config

import re as _re

logger = logging.getLogger(__name__)


def _date_to_iso(date_str: str) -> str:
    """将 YYYY-MM-DD 保持原样；将 YYYY年M月D日 转为 YYYY-MM-DD"""
    m = _re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', date_str)
    if m:
        return date_str
    m = _re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日?', date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return date_str


# MySQL 表达式：将 "YYYY年M月D日" 格式的字段转为 DATE 类型以便范围比较
_CN_DATE_TO_DATE = (
    "STR_TO_DATE(CONCAT("
    "SUBSTRING_INDEX({col}, '年', 1), '-',"
    "SUBSTRING_INDEX(SUBSTRING_INDEX({col}, '月', 1), '年', -1), '-',"
    "SUBSTRING_INDEX(SUBSTRING_INDEX({col}, '日', 1), '月', -1)"
    "), '%%Y-%%m-%%d')"
)


# JSON 字段列表，保存时自动序列化，读取时由调用方自行处理
_JSON_FIELDS = {"parsed_fields", "mapped_fields", "display_fields", "manual_fields"}

# 部分企业不需要系统自动识别某些字段，只允许人工手动填写；
# 已被标记为手动修改过的字段(manual_fields)不受影响，保留用户填写的值。
# key: enterprise_id
DISABLED_AUTO_PARSED_FIELDS_BY_ENTERPRISE = {
    24: {"业务员", "销售渠道"},  # 文十七科技有限公司：业务员/出单渠道 只允许手动填写
}
# 对应上面 parsed_fields 字段映射后的输出列名（"业务员"映射后仍叫"业务员"，"销售渠道"映射后叫"出单渠道"）
DISABLED_AUTO_OUTPUT_FIELDS_BY_ENTERPRISE = {
    24: {"业务员", "出单渠道"},
}


def _strip_disabled_fields(d: dict, disabled_keys, manual_fields) -> dict:
    """剔除企业配置里不需要系统自动识别的字段，但保留用户手动修改过的字段值。"""
    if not disabled_keys or not isinstance(d, dict):
        return d
    manual_set = set(manual_fields) if manual_fields else set()
    to_strip = set(disabled_keys) - manual_set
    if not to_strip:
        return d
    return {k: v for k, v in d.items() if k not in to_strip}

# ------------------------------------------------------------------ #
# 保单字段关联表：OCR 字段名 → 数据库列名 映射
# ------------------------------------------------------------------ #
_OCR_TO_COLUMN = {
    "保单号": "policy_no",
    "保险公司": "company",
    "保险公司简称": "company_short",
    "险种类型": "policy_type",
    "被保险人": "insured",
    "投保人": "applicant",
    "车主": "owner",
    "证件号码": "id_number",
    "车牌号": "plate_no",
    "车架号VIN": "vin",
    "发动机号": "engine_no",
    "厂牌型号": "vehicle_model",
    "核定载客": "passenger_cap",
    "核定载质量": "load_cap",
    "使用性质": "usage_type",
    "机动车种类": "vehicle_type",
    "初次登记日期": "first_reg_date",
    "保费合计": "total_premium",
    "不含税保费": "premium_no_tax",
    "增值税额": "tax_amount",
    "车船税": "vehicle_tax",
    "保险期间": "insurance_period",
    "保险起期": "start_date",
    "保险止期": "end_date",
    "签单日期": "sign_date",
    "收费确认时间": "confirm_time",
    "销售渠道": "sales_channel",
    "中介机构": "agency",
    "业务员": "salesperson",
    "投保确认码": "confirm_code",
    "争议解决方式": "dispute_resolution",
    "制单人": "creator",
    "经办人": "handler",
    "保司公司名称": "insurer_name",
    "保司地址": "insurer_address",
    "保司联系电话": "insurer_phone",
}
# 反向映射：列名 → OCR 字段名
_COLUMN_TO_OCR = {v: k for k, v in _OCR_TO_COLUMN.items()}

# 车牌省份简称 繁体 → 简体 映射（客户繁体输入法输入"粵B0U9U0"、保司PDF为繁体等场景）
_PLATE_TRAD_TO_SIMP = str.maketrans("粵蘇魯贛瓊貴雲遼寧陝晉滬閩", "粤苏鲁赣琼贵云辽宁陕晋沪闽")

# pf.plate_no 的 SQL 侧归一化表达式（去半/全角连字符、半/全角空格、间隔点，转大写），
# 与 normalize_plate_keyword 的 Python 侧归一化对应，兼容"粤B-0U9U0"/"粤B 0U9U0"/小写等存储写法
_PF_PLATE_NORM_SQL = (
    "UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
    "pf.plate_no,'-',''),'－',''),' ',''),'　',''),'·',''),'.',''))"
)


def normalize_plate_keyword(plate: str) -> str:
    """车牌归一化：繁体省份字转简体、去连字符/空格/间隔点、转大写"""
    if not plate:
        return ""
    plate = str(plate).translate(_PLATE_TRAD_TO_SIMP)
    return _re.sub(r"[-－\s　·.]", "", plate).upper()


def init_insurance_tables(db):
    """
    建立保单识别所需的数据库表（MySQL 语法）。
    db 是 storage.db.Database 实例，通过 db.pool 获取连接。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 保单识别记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS insurance_records (
                id                 INT PRIMARY KEY AUTO_INCREMENT,
                msg_seq            INT,
                roomid             VARCHAR(64),
                room_name          VARCHAR(128),
                sender             VARCHAR(64),
                sender_name        VARCHAR(128),
                filename           VARCHAR(256),
                filesize           INT DEFAULT 0,
                cos_url            TEXT,
                ocr_engine         VARCHAR(32),
                doc_category       VARCHAR(64),
                confidence         FLOAT DEFAULT 0,
                parsed_fields      LONGTEXT,
                mapped_fields      LONGTEXT,
                dingtalk_synced    TINYINT DEFAULT 0,
                dingtalk_target_id VARCHAR(64),
                dingtalk_record_id VARCHAR(128),
                status             VARCHAR(32) DEFAULT 'pending',
                error_message      TEXT,
                source             VARCHAR(32) DEFAULT 'auto',
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)

        # 前端可配置项表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS insurance_config (
                id           INT PRIMARY KEY AUTO_INCREMENT,
                config_key   VARCHAR(128) UNIQUE,
                config_value LONGTEXT,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)

        # 保单字段关联表（拆 parsed_fields JSON 为独立列，方便联表查询）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS insurance_policy_fields (
                id                 INT PRIMARY KEY AUTO_INCREMENT,
                record_id          INT NOT NULL UNIQUE COMMENT '关联 insurance_records.id',
                policy_no          VARCHAR(128) DEFAULT '' COMMENT '保单号',
                company            VARCHAR(128) DEFAULT '' COMMENT '保险公司',
                company_short      VARCHAR(64)  DEFAULT '' COMMENT '保险公司简称',
                policy_type        VARCHAR(128) DEFAULT '' COMMENT '险种类型',
                insured            TEXT COMMENT '被保险人',
                applicant          TEXT COMMENT '投保人',
                owner              TEXT COMMENT '车主',
                id_number          VARCHAR(64)  DEFAULT '' COMMENT '证件号码',
                plate_no           VARCHAR(32)  DEFAULT '' COMMENT '车牌号',
                vin                VARCHAR(64)  DEFAULT '' COMMENT '车架号VIN',
                engine_no          VARCHAR(64)  DEFAULT '' COMMENT '发动机号',
                vehicle_model      TEXT COMMENT '厂牌型号',
                passenger_cap      VARCHAR(32)  DEFAULT '' COMMENT '核定载客',
                load_cap           VARCHAR(32)  DEFAULT '' COMMENT '核定载质量',
                usage_type         VARCHAR(64)  DEFAULT '' COMMENT '使用性质',
                vehicle_type       VARCHAR(64)  DEFAULT '' COMMENT '机动车种类',
                first_reg_date     VARCHAR(32)  DEFAULT '' COMMENT '初次登记日期',
                total_premium      VARCHAR(64)  DEFAULT '' COMMENT '保费合计',
                premium_no_tax     VARCHAR(64)  DEFAULT '' COMMENT '不含税保费',
                tax_amount         VARCHAR(64)  DEFAULT '' COMMENT '增值税额',
                vehicle_tax        VARCHAR(64)  DEFAULT '' COMMENT '车船税',
                insurance_period   TEXT COMMENT '保险期间',
                start_date         VARCHAR(32)  DEFAULT '' COMMENT '保险起期',
                end_date           VARCHAR(32)  DEFAULT '' COMMENT '保险止期',
                sign_date          VARCHAR(32)  DEFAULT '' COMMENT '签单日期',
                confirm_time       VARCHAR(64)  DEFAULT '' COMMENT '收费确认时间',
                sales_channel      TEXT COMMENT '销售渠道',
                agency             TEXT COMMENT '中介机构',
                salesperson        TEXT COMMENT '业务员',
                confirm_code       VARCHAR(128) DEFAULT '' COMMENT '投保确认码',
                dispute_resolution TEXT COMMENT '争议解决方式',
                creator            TEXT COMMENT '制单人',
                handler            TEXT COMMENT '经办人',
                insurer_name       TEXT COMMENT '保司公司名称',
                insurer_address    TEXT COMMENT '保司地址',
                insurer_phone      VARCHAR(32) DEFAULT '' COMMENT '保司联系电话',
                sign_date_iso      DATE DEFAULT NULL COMMENT '签单日期(ISO格式，用于索引查询)',
                start_date_iso     DATE DEFAULT NULL COMMENT '起保日期(ISO格式，用于索引查询)',
                end_date_iso       DATE DEFAULT NULL COMMENT '止保日期(ISO格式，用于索引查询)',
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        # 关联表索引（常用查询字段）
        _pf_indexes = {
            "idx_pf_record_id": "CREATE UNIQUE INDEX idx_pf_record_id ON insurance_policy_fields(record_id)",
            "idx_pf_policy_no": "CREATE INDEX idx_pf_policy_no ON insurance_policy_fields(policy_no)",
            "idx_pf_plate_no": "CREATE INDEX idx_pf_plate_no ON insurance_policy_fields(plate_no)",
            "idx_pf_company_short": "CREATE INDEX idx_pf_company_short ON insurance_policy_fields(company_short)",
            "idx_pf_applicant": "CREATE INDEX idx_pf_applicant ON insurance_policy_fields(applicant)",
            "idx_pf_insured": "CREATE INDEX idx_pf_insured ON insurance_policy_fields(insured)",
            "idx_pf_start_date_iso": "CREATE INDEX idx_pf_start_date_iso ON insurance_policy_fields(start_date_iso)",
            "idx_pf_sign_date_iso": "CREATE INDEX idx_pf_sign_date_iso ON insurance_policy_fields(sign_date_iso)",
            "idx_pf_end_date_iso": "CREATE INDEX idx_pf_end_date_iso ON insurance_policy_fields(end_date_iso)",
        }
        try:
            cursor.execute(
                "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='insurance_policy_fields'"
            )
            _existing_indexes = {r[0] for r in cursor.fetchall()}
        except Exception:
            _existing_indexes = set()  # 探测失败时保守回退到原逐条尝试
        for idx_name, idx_sql in _pf_indexes.items():
            if idx_name in _existing_indexes:
                continue
            try:
                cursor.execute(idx_sql)
            except Exception:
                pass

        # 迁移：为已有表添加 ISO 日期列（幂等）
        for iso_col in ["sign_date_iso", "start_date_iso", "end_date_iso"]:
            try:
                cursor.execute(
                    f"ALTER TABLE insurance_policy_fields ADD COLUMN {iso_col} DATE DEFAULT NULL"
                )
            except Exception:
                pass
            try:
                cursor.execute(
                    f"CREATE INDEX idx_pf_{iso_col} ON insurance_policy_fields({iso_col})"
                )
            except Exception:
                pass

        # 迁移：为已有表添加保司联系电话列（幂等）
        try:
            cursor.execute(
                "ALTER TABLE insurance_policy_fields ADD COLUMN insurer_phone VARCHAR(32) DEFAULT '' COMMENT '保司联系电话'"
            )
        except Exception:
            pass

        # 用户字段配置表（支持全局/企业/个人三级作用域）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_field_config (
                id              INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键',
                scope           ENUM('global','enterprise','user') NOT NULL COMMENT '配置作用域：global=全局默认, enterprise=企业级, user=员工个人',
                scope_id        INT DEFAULT NULL COMMENT '作用域ID：global时为NULL, enterprise时为企业ID, user时为用户ID',
                template_name   VARCHAR(64) NOT NULL DEFAULT '默认模板' COMMENT '模板名称',
                config_type     VARCHAR(32) NOT NULL COMMENT '配置类型：company_alias/policy_type_alias/date_format/fee_formula',
                config_key      VARCHAR(128) NOT NULL COMMENT '配置项标识',
                config_value    TEXT NOT NULL COMMENT '配置值(JSON)',
                visible_to_employees TINYINT NOT NULL DEFAULT 0 COMMENT '是否对员工可见（仅enterprise scope有效）',
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
                UNIQUE KEY uk_scope_tpl (scope, scope_id, template_name, config_type, config_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户字段配置表'
        """)

        # 用户当前启用的配置模板表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_active_template (
                id              INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键',
                user_id         INT NOT NULL COMMENT '用户ID',
                active_source   ENUM('own','enterprise') NOT NULL DEFAULT 'own' COMMENT '启用来源',
                template_name   VARCHAR(64) NOT NULL DEFAULT '默认模板' COMMENT '启用的模板名称',
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
                UNIQUE KEY uk_user (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户当前启用的配置模板'
        """)

        # 回填已有数据的 ISO 日期列（后台线程，Python端逐行转换，跳过异常格式）
        conn.commit()  # 先提交前面的 DDL

        def _backfill_iso_dates(db_ref):
            """后台线程：回填 ISO 日期列"""
            import time as _t
            _t.sleep(3)
            backfill_conn = db_ref.pool.connection()
            try:
                bc = backfill_conn.cursor(pymysql.cursors.DictCursor)
                for cn_col, iso_col in [
                    ("sign_date", "sign_date_iso"),
                    ("start_date", "start_date_iso"),
                    ("end_date", "end_date_iso"),
                ]:
                    try:
                        bc.execute(
                            f"SELECT id, {cn_col} AS val FROM insurance_policy_fields "
                            f"WHERE {iso_col} IS NULL AND {cn_col} != ''"
                        )
                        rows = bc.fetchall()
                        if not rows:
                            continue
                        logger.info("后台回填 %s → %s，共 %d 条", cn_col, iso_col, len(rows))
                        done = 0
                        for row in rows:
                            iso_val = _date_to_iso(row["val"])
                            if not _re.match(r'\d{4}-\d{2}-\d{2}', iso_val):
                                continue
                            try:
                                bc.execute(
                                    f"UPDATE insurance_policy_fields SET {iso_col} = %s WHERE id = %s",
                                    (iso_val, row["id"])
                                )
                                done += 1
                                if done % 100 == 0:
                                    backfill_conn.commit()
                            except Exception:
                                pass
                        backfill_conn.commit()
                        logger.info("后台回填 %s 完成，处理 %d 条", iso_col, done)
                    except Exception as e:
                        logger.warning("后台回填 %s 失败: %s", iso_col, e)
            finally:
                backfill_conn.close()

        import threading as _threading
        _threading.Thread(target=_backfill_iso_dates, args=(db,), daemon=True).start()

        # 迁移：为已有表添加保司公司名称/保司地址列（幂等）
        for new_col in ["insurer_name", "insurer_address"]:
            try:
                cursor.execute(
                    f"ALTER TABLE insurance_policy_fields ADD COLUMN {new_col} TEXT COMMENT '保司信息'"
                )
            except Exception:
                pass

        # 迁移：将已有 VARCHAR(500) 列改为 TEXT，避免行大小超限
        # insured/applicant 建了索引(idx_pf_insured/idx_pf_applicant)，TEXT列做索引
        # 必须指定前缀长度，MODIFY 在当前索引存在时必然失败——不放进重试名单，
        # 避免每次worker启动都去抢一次注定失败的表级锁（2026-07-14锁雪崩故障根因之一）
        _pf_to_text = [
            "owner", "vehicle_model",
            "insurance_period", "sales_channel", "agency",
            "salesperson", "dispute_resolution", "creator", "handler",
            "insurer_name", "insurer_address",
        ]
        try:
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='insurance_policy_fields' "
                "AND DATA_TYPE != 'text' AND COLUMN_NAME IN (%s)"
                % ",".join(["%s"] * len(_pf_to_text)),
                _pf_to_text,
            )
            _cols_need_migration = {r[0] for r in cursor.fetchall()}
        except Exception:
            _cols_need_migration = set(_pf_to_text)  # 探测失败时保守回退到原逐列尝试
        for col in _cols_need_migration:
            try:
                cursor.execute(
                    f"ALTER TABLE insurance_policy_fields MODIFY {col} TEXT"
                )
            except Exception:
                pass

        # 索引（忽略已存在的索引错误）
        for idx_sql in [
            "CREATE INDEX idx_insurance_roomid ON insurance_records(roomid)",
            "CREATE INDEX idx_insurance_status ON insurance_records(status)",
            "CREATE INDEX idx_insurance_created_at ON insurance_records(created_at)",
        ]:
            try:
                cursor.execute(idx_sql)
            except Exception:
                pass  # 索引已存在，忽略

        # 为 insurance_records 表添加新字段（幂等，已存在则跳过）
        new_columns = [
            ("raw_text", "LONGTEXT COMMENT 'OCR提取原文'"),
            ("company_short", "VARCHAR(64) DEFAULT '' COMMENT '保险公司简称（冗余列，加速统计）'"),
            ("is_abnormal", "TINYINT DEFAULT 0 COMMENT '是否需人工补充（预计算，加速列表筛选）'"),
            ("hint", "VARCHAR(64) DEFAULT '' COMMENT '提示信息（如：保单无投保人）'"),
            ("display_fields", "TEXT COMMENT '映射后的展示字段JSON（列表轻量展示用）'"),
            ("file_md5", "VARCHAR(32) DEFAULT NULL COMMENT 'PDF文件MD5（用于去重）'"),
            ("user_id", "INT DEFAULT NULL COMMENT '所属用户ID'"),
            ("enterprise_id", "INT DEFAULT NULL COMMENT '所属企业ID（关联 enterprises 表）'"),
            ("manual_fields", "LONGTEXT COMMENT '手动修改过的字段名列表JSON'"),
            ("abnormal_override_reason", "VARCHAR(128) DEFAULT NULL COMMENT '手动标记正常的原因（非NULL时强制is_abnormal=0）'"),
            ("policy_count", "TINYINT DEFAULT 1 COMMENT '同一PDF中的保单总数'"),
            ("policy_index", "TINYINT DEFAULT 1 COMMENT '当前记录在同一PDF中的序号（从1开始）'"),
            ("page_range", "VARCHAR(255) DEFAULT '' COMMENT '提取数据来源页码范围（如1-2）'"),
            ("ocr_text", "LONGTEXT COMMENT 'OCR识别原文（pdfplumber+ocr模式下保存OCR文本）'"),
            ("deleted_at", "DATETIME DEFAULT NULL COMMENT '软删除时间（非NULL表示已删除，列表/统计默认过滤）'"),
            ("first_recognized_at", "DATETIME DEFAULT NULL COMMENT '首次识别成功完成时间（重新识别不覆盖，updated_at会被重新识别刷新）'"),
            ("seal_paid", "TINYINT NOT NULL DEFAULT 0 COMMENT '公户车盖章跟进：是否已付（0未付/1已付）'"),
            ("seal_paid_at", "DATETIME DEFAULT NULL COMMENT '公户车盖章跟进：标记已付时间'"),
            ("seal_paid_by", "VARCHAR(64) DEFAULT '' COMMENT '公户车盖章跟进：标记已付操作人姓名'"),
        ]
        for col_name, col_def in new_columns:
            try:
                cursor.execute(f"ALTER TABLE insurance_records ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass  # 字段已存在，忽略

        # 新增索引
        for idx_sql in [
            "CREATE INDEX idx_insurance_company_short ON insurance_records(company_short)",
            "CREATE INDEX idx_insurance_is_abnormal ON insurance_records(is_abnormal)",
            # 复合索引：加速列表页常见筛选组合（status + is_abnormal + created_at 范围）
            "CREATE INDEX idx_insurance_status_abnormal_created ON insurance_records(status, is_abnormal, created_at)",
            "CREATE INDEX idx_insurance_file_md5 ON insurance_records(file_md5)",
            "CREATE INDEX idx_insurance_user_id ON insurance_records(user_id)",
            "CREATE INDEX idx_insurance_enterprise_id ON insurance_records(enterprise_id)",
            # 复合索引：加速按企业/用户权限过滤 + 时间排序的常见查询
            "CREATE INDEX idx_insurance_eid_del_created ON insurance_records(enterprise_id, deleted_at, created_at)",
            "CREATE INDEX idx_insurance_uid_del_created ON insurance_records(user_id, deleted_at, created_at)",
            # 覆盖索引：列表统计（Tab计数/engine_stats/room_stats/COUNT）全部走纯索引扫描，
            # 避免回表扫 1GB 大行数据（行含大JSON字段），统计耗时 1.25s → 0.14s
            "CREATE INDEX idx_stats_covering ON insurance_records"
            "(deleted_at, status, doc_category, is_abnormal, ocr_engine, enterprise_id, user_id, roomid, room_name)",
        ]:
            try:
                cursor.execute(idx_sql)
            except Exception:
                pass

        # 修复：将 file_md5 的 UNIQUE INDEX 降级为普通 INDEX（多保单PDF共享同一MD5）
        try:
            cursor.execute("SHOW INDEX FROM insurance_records WHERE Key_name = 'idx_insurance_file_md5' AND Non_unique = 0")
            if cursor.fetchone():
                cursor.execute("DROP INDEX idx_insurance_file_md5 ON insurance_records")
                cursor.execute("CREATE INDEX idx_insurance_file_md5 ON insurance_records(file_md5)")
                logger.info("已将 file_md5 唯一索引降级为普通索引（支持多保单PDF）")
        except Exception:
            pass

        # 备注快捷选择 - 选项池表（按 scope 隔离，每日清空重导）
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS insurance_remark_options (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    scope VARCHAR(16) NOT NULL DEFAULT 'global' COMMENT 'global/enterprise/user',
                    scope_id VARCHAR(64) DEFAULT NULL COMMENT '企业parent_id或user_id',
                    template_name VARCHAR(64) NOT NULL DEFAULT '默认模板' COMMENT '所属模板',
                    handler VARCHAR(64) DEFAULT '' COMMENT '经办人',
                    company VARCHAR(128) DEFAULT '' COMMENT '投保公司',
                    policy_type VARCHAR(64) DEFAULT '' COMMENT '险种名称',
                    remark TEXT COMMENT '备注信息',
                    batch_no VARCHAR(32) DEFAULT '' COMMENT '导入批次(时间戳)',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    KEY idx_remark_opt_scope (scope, scope_id, template_name, handler, company, policy_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='备注快捷选择选项池'
                """
            )
        except Exception:
            pass
        # 旧表升级：补 template_name 列（幂等）
        try:
            cursor.execute(
                "ALTER TABLE insurance_remark_options "
                "ADD COLUMN template_name VARCHAR(64) NOT NULL DEFAULT '默认模板' COMMENT '所属模板' AFTER scope_id"
            )
        except Exception:
            pass

        # 报错反馈表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS error_reports (
                id          INT PRIMARY KEY AUTO_INCREMENT,
                record_id   INT,
                user_id     INT,
                user_name   VARCHAR(128),
                enterprise  VARCHAR(128),
                filename    VARCHAR(256),
                policy_no   VARCHAR(128),
                description TEXT,
                status      VARCHAR(16) DEFAULT 'unread',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                KEY idx_er_status (status),
                KEY idx_er_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户报错反馈'
        """)

        # 内部管理备注表(仅超管,每日追加日志) —— 见 insurance/internal_notes_db.py
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS insurance_internal_notes (
                id          BIGINT PRIMARY KEY AUTO_INCREMENT,
                record_id   INT NOT NULL COMMENT '关联 insurance_records.id',
                content     TEXT COMMENT '备注内容',
                attribution VARCHAR(16) NOT NULL DEFAULT '' COMMENT '问题归属: our_pending/our_fixed/customer/空',
                author_id   INT DEFAULT NULL COMMENT '操作人 user_id',
                author_name VARCHAR(64) DEFAULT NULL COMMENT '操作人姓名',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                KEY idx_notes_record (record_id),
                KEY idx_notes_record_day (record_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='保单内部管理备注(仅超管,每日追加日志)'
        """)

        # FlowBot 群通知推送日志（发送时落库，回调按 taskId 更新结果/触发重发）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS flowbot_push_log (
                id          INT PRIMARY KEY AUTO_INCREMENT,
                task_id     VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'FlowBot 任务ID',
                robot_id    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '机器人ID',
                source      VARCHAR(16) NOT NULL DEFAULT 'notify' COMMENT '来源: notify=群通知(失败自动重发) monitor=监控页手动发送(失败手动重发)',
                roomid      VARCHAR(128) NOT NULL DEFAULT '' COMMENT '目标群roomid（监控页发送时记录，供聊天记录合并显示）',
                group_name  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '目标群名',
                message     TEXT COMMENT '消息正文',
                payload     TEXT COMMENT '发送任务 taskList JSON（失败重发用）',
                status      TINYINT NOT NULL DEFAULT 0 COMMENT '0=推送中 1=成功 2=失败',
                retry_count INT NOT NULL DEFAULT 0 COMMENT '回调失败后的重发次数',
                error_msg   VARCHAR(500) NOT NULL DEFAULT '' COMMENT '失败原因',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_fbl_task (task_id),
                KEY idx_fbl_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='FlowBot群通知推送日志'
        """)

        # 旧表升级：补 source/roomid 列（幂等，监控页发送消息功能用）
        for alter_sql in [
            "ALTER TABLE flowbot_push_log ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'notify' "
            "COMMENT '来源: notify=群通知(失败自动重发) monitor=监控页手动发送(失败手动重发)' AFTER robot_id",
            "ALTER TABLE flowbot_push_log ADD COLUMN roomid VARCHAR(128) NOT NULL DEFAULT '' "
            "COMMENT '目标群roomid（监控页发送时记录，供聊天记录合并显示）' AFTER source",
            "CREATE INDEX idx_fbl_room ON flowbot_push_log(roomid, created_at)",
        ]:
            try:
                cursor.execute(alter_sql)
            except Exception:
                pass  # 字段/索引已存在，忽略

        conn.commit()
        logger.info("保单识别数据库表初始化完成（DDL）")
    finally:
        conn.close()

    # AI 质检相关表结构（insurance_records 新列 + ai_check_log）
    try:
        from .ai_db import init_ai_tables
        init_ai_tables(db)
    except Exception as e:
        logger.warning("AI质检表初始化失败（不影响主流程）: %s", e)

    # 数据回填放到后台线程，避免阻塞服务启动
    def _backfill_insurance_data(db_ref):
        backfill_conn = db_ref.pool.connection()
        try:
            bc = backfill_conn.cursor(pymysql.cursors.DictCursor)

            # 回填 company_short
            try:
                bc.execute("""
                    UPDATE insurance_records
                    SET company_short = JSON_UNQUOTE(JSON_EXTRACT(parsed_fields, '$.保险公司简称'))
                    WHERE company_short = ''
                      AND parsed_fields IS NOT NULL
                      AND JSON_EXTRACT(parsed_fields, '$.保险公司简称') IS NOT NULL
                      AND JSON_UNQUOTE(JSON_EXTRACT(parsed_fields, '$.保险公司简称')) != ''
                """)
                if bc.rowcount:
                    logger.info("回填 company_short %d 条", bc.rowcount)
                backfill_conn.commit()
            except Exception:
                backfill_conn.rollback()

            # 回填 is_abnormal
            try:
                bc.execute("""
                    SELECT id, parsed_fields, status, doc_category, user_id
                    FROM insurance_records
                    WHERE is_abnormal = 0
                      AND status IN ('done', 'success')
                      AND (doc_category IS NULL OR doc_category = '' OR doc_category = '保单')
                      AND parsed_fields IS NOT NULL
                """)
                rows = bc.fetchall()
                _visible_cache = {}
                updated = 0
                for row in rows:
                    pf_str = row.get("parsed_fields") if isinstance(row, dict) else row[1]
                    try:
                        pf = json.loads(pf_str) if isinstance(pf_str, str) else (pf_str or {})
                    except (TypeError, json.JSONDecodeError):
                        pf = {}
                    uid = row.get("user_id") if isinstance(row, dict) else row[4]
                    if uid not in _visible_cache:
                        _visible_cache[uid] = _get_visible_export_keys(db_ref, uid)
                    abnormal, hint = _compute_abnormal(
                        pf,
                        row.get("status") if isinstance(row, dict) else row[2],
                        row.get("doc_category") if isinstance(row, dict) else row[3],
                        visible_export_keys=_visible_cache[uid],
                    )
                    if abnormal:
                        rid = row.get("id") if isinstance(row, dict) else row[0]
                        bc.execute(
                            "UPDATE insurance_records SET is_abnormal = 1, hint = %s WHERE id = %s",
                            (hint, rid)
                        )
                        updated += 1
                if updated:
                    logger.info("回填 is_abnormal %d 条", updated)
                backfill_conn.commit()
            except Exception:
                backfill_conn.rollback()
                logger.exception("回填 is_abnormal 失败")

            # 反向修正：已标记异常的记录按当前模板重新计算
            try:
                bc.execute("""
                    SELECT id, parsed_fields, status, doc_category, user_id, abnormal_override_reason
                    FROM insurance_records
                    WHERE is_abnormal = 1
                      AND status IN ('done', 'success')
                      AND (abnormal_override_reason IS NULL OR abnormal_override_reason = '')
                      AND parsed_fields IS NOT NULL
                """)
                abnormal_rows = bc.fetchall()
                _visible_cache_rev = {}
                reverted = 0
                for row in abnormal_rows:
                    pf_str = row.get("parsed_fields") if isinstance(row, dict) else row[1]
                    try:
                        pf = json.loads(pf_str) if isinstance(pf_str, str) else (pf_str or {})
                    except (TypeError, json.JSONDecodeError):
                        pf = {}
                    uid = row.get("user_id") if isinstance(row, dict) else row[4]
                    if uid not in _visible_cache_rev:
                        _visible_cache_rev[uid] = _get_visible_export_keys(db_ref, uid)
                    override = row.get("abnormal_override_reason") if isinstance(row, dict) else row[5]
                    abnormal, hint = _compute_abnormal(
                        pf,
                        row.get("status") if isinstance(row, dict) else row[2],
                        row.get("doc_category") if isinstance(row, dict) else row[3],
                        abnormal_override_reason=override,
                        visible_export_keys=_visible_cache_rev[uid],
                    )
                    if not abnormal:
                        rid = row.get("id") if isinstance(row, dict) else row[0]
                        bc.execute(
                            "UPDATE insurance_records SET is_abnormal = 0, hint = %s WHERE id = %s",
                            (hint, rid)
                        )
                        reverted += 1
                    elif hint != (row.get("hint", "") if isinstance(row, dict) else ""):
                        rid = row.get("id") if isinstance(row, dict) else row[0]
                        bc.execute(
                            "UPDATE insurance_records SET hint = %s WHERE id = %s",
                            (hint, rid)
                        )
                if reverted:
                    logger.info("反向修正 is_abnormal 1→0 %d 条（模板不再要求该字段）", reverted)
                backfill_conn.commit()
            except Exception:
                backfill_conn.rollback()
                logger.exception("反向修正 is_abnormal 失败")

            # 修正已标记正常但 is_abnormal 仍为 1 的历史数据（分批避免锁超时）
            try:
                total_fixed = 0
                while True:
                    bc.execute("""
                        UPDATE insurance_records
                        SET is_abnormal = 0, hint = ''
                        WHERE is_abnormal = 1
                          AND abnormal_override_reason IS NOT NULL
                          AND abnormal_override_reason != ''
                        LIMIT 200
                    """)
                    affected = bc.rowcount
                    backfill_conn.commit()
                    if not affected:
                        break
                    total_fixed += affected
                if total_fixed:
                    logger.info("修正已标记正常但 is_abnormal=1 的记录 %d 条", total_fixed)
            except Exception:
                backfill_conn.rollback()
                logger.exception("修正 abnormal_override 失败")

            # 回填 display_fields
            try:
                bc.execute("""
                    SELECT id, parsed_fields, company_short
                    FROM insurance_records
                    WHERE parsed_fields IS NOT NULL
                      AND (display_fields IS NULL OR display_fields = '')
                """)
                rows = bc.fetchall()
                backfilled = 0
                for row in rows:
                    pf_str = row.get("parsed_fields") if isinstance(row, dict) else row[1]
                    cs = row.get("company_short") if isinstance(row, dict) else row[2]
                    try:
                        pf = json.loads(pf_str) if isinstance(pf_str, str) else (pf_str or {})
                    except (TypeError, json.JSONDecodeError):
                        continue
                    df = apply_mapping(pf, cs or "")
                    rid = row.get("id") if isinstance(row, dict) else row[0]
                    bc.execute(
                        "UPDATE insurance_records SET display_fields = %s WHERE id = %s",
                        (json.dumps(df, ensure_ascii=False), rid)
                    )
                    backfilled += 1
                if backfilled:
                    logger.info("回填 display_fields %d 条", backfilled)
                backfill_conn.commit()
            except Exception:
                backfill_conn.rollback()
                logger.exception("回填 display_fields 失败")

            # 回填 display_fields 中缺少的车主字段（分批避免锁超时）
            try:
                bc.execute("""
                    SELECT r.id, pf.owner
                    FROM insurance_records r
                    JOIN insurance_policy_fields pf ON pf.record_id = r.id
                    WHERE pf.owner IS NOT NULL AND pf.owner != ''
                      AND r.display_fields IS NOT NULL AND r.display_fields != ''
                      AND (JSON_EXTRACT(r.display_fields, '$."车主"') IS NULL
                           OR JSON_EXTRACT(r.display_fields, '$."车主"') = '')
                """)
                owner_rows = bc.fetchall()
                backfilled_owner = 0
                for i, row in enumerate(owner_rows):
                    rid = row.get("id") if isinstance(row, dict) else row[0]
                    owner = row.get("owner") if isinstance(row, dict) else row[1]
                    bc.execute(
                        'UPDATE insurance_records SET display_fields = JSON_SET(display_fields, \'$."车主"\', %s) WHERE id = %s',
                        (owner, rid)
                    )
                    backfilled_owner += 1
                    if backfilled_owner % 200 == 0:
                        backfill_conn.commit()
                if backfilled_owner:
                    backfill_conn.commit()
                    logger.info("回填 display_fields 车主字段 %d 条", backfilled_owner)
            except Exception:
                backfill_conn.rollback()
                logger.exception("回填 display_fields 车主字段失败")

            # 回填 insurance_policy_fields
            try:
                bc.execute("""
                    SELECT r.id, r.parsed_fields
                    FROM insurance_records r
                    LEFT JOIN insurance_policy_fields pf ON pf.record_id = r.id
                    WHERE r.parsed_fields IS NOT NULL
                      AND r.parsed_fields != ''
                      AND pf.id IS NULL
                """)
                rows = bc.fetchall()
                backfilled_pf = 0
                for row in rows:
                    pf_str = row.get("parsed_fields") if isinstance(row, dict) else row[1]
                    try:
                        pf = json.loads(pf_str) if isinstance(pf_str, str) else (pf_str or {})
                    except (TypeError, json.JSONDecodeError):
                        continue
                    rid = row.get("id") if isinstance(row, dict) else row[0]
                    try:
                        _insert_policy_fields(bc, rid, pf)
                        backfilled_pf += 1
                    except Exception as e:
                        logger.warning("回填 policy_fields 失败, record_id=%s: %s", rid, e)
                if backfilled_pf:
                    logger.info("回填 insurance_policy_fields %d 条", backfilled_pf)
                backfill_conn.commit()
            except Exception:
                backfill_conn.rollback()
                logger.exception("回填 insurance_policy_fields 失败")

            logger.info("保单数据回填完成")
        except Exception:
            logger.exception("保单数据回填异常")
        finally:
            backfill_conn.close()

    import threading as _threading
    _threading.Thread(target=_backfill_insurance_data, args=(db,), daemon=True).start()


# ------------------------------------------------------------------ #
# 关联表操作（内部函数，由 save/update_insurance_record 自动调用）
# ------------------------------------------------------------------ #

def _parsed_fields_to_row(parsed_fields: dict) -> dict:
    """将 OCR parsed_fields 字典转为关联表的列名-值字典"""
    row = {}
    for ocr_key, col_name in _OCR_TO_COLUMN.items():
        val = parsed_fields.get(ocr_key, "")
        if val:
            row[col_name] = str(val)[:500]  # 截断防溢出
    # 车牌繁体省份字转简体入库（部分保司PDF为繁体），格式保持原样，避免简体搜不到
    if row.get("plate_no"):
        row["plate_no"] = row["plate_no"].translate(_PLATE_TRAD_TO_SIMP)
    # 自动生成 ISO 格式日期列（用于索引查询，避免 STR_TO_DATE）
    for cn_col, iso_col in [
        ("sign_date", "sign_date_iso"),
        ("start_date", "start_date_iso"),
        ("end_date", "end_date_iso"),
    ]:
        cn_val = row.get(cn_col, "")
        if cn_val:
            iso_val = _date_to_iso(cn_val)
            if _re.match(r'\d{4}-\d{2}-\d{2}', iso_val):
                # 校验是真实有效日期，避免"2026-06-80"这类OCR错值插入DATE列报1292、导致commit失败
                try:
                    from datetime import date as _pydate
                    _pydate(int(iso_val[0:4]), int(iso_val[5:7]), int(iso_val[8:10]))
                    row[iso_col] = iso_val
                except ValueError:
                    pass
    return row


def _insert_policy_fields(cursor, record_id: int, parsed_fields: dict):
    """向关联表插入一条记录（内部使用，需外部传入 cursor）"""
    row = _parsed_fields_to_row(parsed_fields)
    if not row:
        return
    row["record_id"] = record_id
    columns = ", ".join(row.keys())
    placeholders = ", ".join(["%s"] * len(row))
    cursor.execute(
        f"INSERT INTO insurance_policy_fields ({columns}) VALUES ({placeholders})",
        list(row.values())
    )


def _upsert_policy_fields(cursor, record_id: int, parsed_fields: dict):
    """向关联表插入或更新一条记录（内部使用，需外部传入 cursor）"""
    row = _parsed_fields_to_row(parsed_fields)
    if not row:
        return
    # 检查是否已存在
    cursor.execute(
        "SELECT id FROM insurance_policy_fields WHERE record_id = %s",
        (record_id,)
    )
    existing = cursor.fetchone()
    if existing:
        # 更新
        set_clause = ", ".join([f"{k} = %s" for k in row.keys()])
        values = list(row.values()) + [record_id]
        cursor.execute(
            f"UPDATE insurance_policy_fields SET {set_clause} WHERE record_id = %s",
            values
        )
    else:
        # 插入
        row["record_id"] = record_id
        columns = ", ".join(row.keys())
        placeholders = ", ".join(["%s"] * len(row))
        cursor.execute(
            f"INSERT INTO insurance_policy_fields ({columns}) VALUES ({placeholders})",
            list(row.values())
        )


# 异常检测必检字段（与前端 REQUIRED_FIELDS 保持一致）
_REQUIRED_FIELDS = ['承保公司', '保单号', '险种', '车牌', '投保人', '被保人', '签单日期', '起保日期', '终保日期', '保费']
# 导出列名 → OCR 字段名的反向映射
_EXPORT_TO_OCR = {
    '承保公司': ['保险公司', '保险公司简称'],
    '保单号': ['保单号'],
    '险种': ['险种类型'],
    '车牌': ['车牌号'],
    '投保人': ['投保人'],
    '被保人': ['被保险人'],
    '签单日期': ['签单日期'],
    '起保日期': ['保险起期'],
    '终保日期': ['保险止期'],
    '保费': ['保费合计'],
}


def _get_visible_export_keys(db, user_id) -> set:
    """根据 user_id 查其导出模板中 visible=true 的列 key 集合。
    无用户上下文时返回 None，调用方回退到硬编码全量检查。
    """
    if not user_id:
        return None
    try:
        from auth.db import get_user_by_id
        user = get_user_by_id(db, user_id)
        if not user:
            return None
        config = get_column_config(
            db, "export_columns",
            user["id"], user["role"], user.get("parent_id"),
        )
        return {c["key"] for c in config.get("columns", []) if c.get("visible")}
    except Exception:
        logger.debug("获取用户 %s 导出模板失败，回退全量检查", user_id, exc_info=True)
        return None


def _compute_abnormal(parsed_fields: dict, status: str, doc_category: str,
                      abnormal_override_reason: str = None,
                      visible_export_keys: set = None) -> tuple:
    """
    计算记录是否异常，返回 (is_abnormal: bool, hint: str)。
    逻辑与前端 isRecordAbnormal / getRecordHint 保持一致。
    如果 abnormal_override_reason 不为空，强制返回正常。

    visible_export_keys: 用户导出模板中 visible=true 的列 key 集合。
        传入时只检查 _REQUIRED_FIELDS 与该集合的交集；
        为 None 时检查全部 _REQUIRED_FIELDS（兼容无用户上下文的场景）。
    """
    # 手动标记正常的记录，直接返回正常
    if abnormal_override_reason:
        return False, ''
    if status not in ('done', 'success'):
        return False, ''
    cat = doc_category or ''
    if cat and cat != '保单':
        return False, ''
    fields = parsed_fields or {}

    # 需人工补充：固定检查全部核心必填列（_REQUIRED_FIELDS 的 10 列），
    # 不再按用户导出模板筛选。模板筛选会让同一记录在"入库/启动回填(模板口径)"
    # 与"浏览惰性修正(固定列口径)"之间被反复改写，导致历史区间统计数字漂移。
    # visible_export_keys 参数保留仅为向后兼容，判定时忽略。
    check_fields = _REQUIRED_FIELDS

    missing = []
    for col in check_fields:
        if fields.get(col):
            continue
        ocr_keys = _EXPORT_TO_OCR.get(col, [])
        if any(fields.get(k) for k in ocr_keys):
            continue
        missing.append(col)

    # 日期完整性校验：日期不全（如"2026/05/"缺日）视为异常
    _DATE_FIELDS = {'保险起期': '起保日期', '保险止期': '终保日期', '签单日期': '签单日期'}
    incomplete_dates = []
    for ocr_key, label in _DATE_FIELDS.items():
        # 固定口径：三个日期列均检查完整性，不再按模板跳过
        val = fields.get(ocr_key, '')
        if val and not _re.search(r'\d{1,2}[日号]|\d{4}[-/]\d{1,2}[-/]\d{1,2}', val):
            incomplete_dates.append(label)

    # 提示信息：逐项列出所有缺失的必填字段 + 日期不全，方便用户知道缺什么去补。
    policy_type = fields.get('险种类型', '')
    is_compulsory = '交强' in policy_type or '交通事故责任强制' in policy_type
    is_new_car = bool(fields.get('新车未上牌'))
    hint_parts = []
    # 暂未上牌新车：车牌缺失单独提示"新车无车牌"（仍计入需人工补充）
    if is_new_car and '车牌' in missing:
        hint_parts.append('新车无车牌')
    if missing:
        labels = []
        for col in missing:
            # 暂未上牌新车的车牌已用"新车无车牌"提示，跳过
            if col == '车牌' and is_new_car:
                continue
            # 交强险无投保人用更友好的措辞，其余直接列字段名
            if col == '投保人' and is_compulsory:
                labels.append('投保人(交强险通常无)')
            else:
                labels.append(col)
        if labels:
            hint_parts.append('缺少：' + '、'.join(labels))
    if incomplete_dates:
        hint_parts.append('日期不全：' + '、'.join(incomplete_dates))
    hint = '；'.join(hint_parts)

    if not missing and not incomplete_dates:
        return False, hint

    return True, hint


def _extract_company_short(data: dict) -> str:
    """从 parsed_fields 中提取保险公司简称，用于填充 company_short 冗余列"""
    pf = data.get("parsed_fields")
    if isinstance(pf, dict):
        return pf.get("保险公司简称", "") or ""
    if isinstance(pf, str):
        try:
            pf_dict = json.loads(pf)
            return pf_dict.get("保险公司简称", "") or ""
        except (TypeError, json.JSONDecodeError):
            pass
    return ""


def check_file_md5_exists(db, file_md5: str) -> dict | None:
    """
    检查指定 MD5 的文件是否已有成功识别的记录。
    只有 status='done' 的记录才视为已处理，避免跳过之前失败/处理中的文件。
    返回已有记录（dict）或 None。
    """
    if not file_md5:
        return None
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, filename, status FROM insurance_records WHERE file_md5 = %s AND status = 'done' AND deleted_at IS NULL LIMIT 1",
            (file_md5,)
        )
        return cursor.fetchone()
    finally:
        conn.close()


def soft_delete_record(db, record_id: int) -> bool:
    """
    软删除一条保单识别记录（设置 deleted_at=NOW()）。
    列表、统计、保司下拉、MD5 去重检测均会过滤掉已软删除记录。
    返回 True 表示本次成功删除，False 表示记录不存在或已被删除。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE insurance_records SET deleted_at = NOW() WHERE id = %s AND deleted_at IS NULL",
            (record_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def backfill_records_by_sources(db, room_ids: list, user_ids: list,
                                new_user_id: int = None, new_enterprise_id: int = None) -> int:
    """
    回填历史保单记录的 user_id 和 enterprise_id。
    根据监控配置的群聊/私聊来源匹配 insurance_records 中的记录并更新。
    返回受影响的行数。

    â ï¸ 只在 new_user_id/new_enterprise_id 显式给出真实值时才写入对应列，
    传 None 表示"这次不改这一列"、不会清空已有值。以前的实现无条件把两列一起 SET，
    导致监控配置只绑了企业没绑具体员工时，保存/编辑该配置会把这个群/私聊范围内全部
    记录的 user_id 强制清空——即使这些记录本来是通过更精细的 sender 级绑定(user_sender_binding)
    正确关联到了具体员工，也会被这个更粗粒度的群级回填覆盖掉。2026-07-14 因为这个bug
    连续两次误清空了 24081+661 条记录的 user_id，历史上7月1号也发生过一次(17481条)。
    """
    if not room_ids and not user_ids:
        return 0
    set_clauses = []
    set_params = []
    if new_user_id is not None:
        set_clauses.append("user_id = %s")
        set_params.append(new_user_id)
    if new_enterprise_id is not None:
        set_clauses.append("enterprise_id = %s")
        set_params.append(new_enterprise_id)
    if not set_clauses:
        return 0
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        conditions = []
        params = []
        if room_ids:
            ph = ",".join(["%s"] * len(room_ids))
            conditions.append(f"roomid IN ({ph})")
            params.extend(room_ids)
        if user_ids:
            ph = ",".join(["%s"] * len(user_ids))
            conditions.append(f"(sender IN ({ph}) AND (roomid IS NULL OR roomid = ''))")
            params.extend(user_ids)
        where = " OR ".join(conditions)
        sql = f"UPDATE insurance_records SET {', '.join(set_clauses)} WHERE ({where})"
        cursor.execute(sql, set_params + params)
        conn.commit()
        affected = cursor.rowcount
        if affected > 0:
            logger.info("回填历史记录 user_id=%s enterprise_id=%s，影响 %d 条",
                        new_user_id, new_enterprise_id, affected)
        return affected
    finally:
        conn.close()


def save_insurance_record(db, record: dict) -> int:
    """
    插入一条保单识别记录，返回新记录的 id。
    JSON 字段（parsed_fields / mapped_fields）若传入 dict/list，自动序列化。
    """
    data = dict(record)

    # 按企业配置剔除不需要系统自动识别的字段（如文十七的 业务员/出单渠道），新记录尚无手动字段
    _eid0 = data.get("enterprise_id")
    _disabled_pf0 = DISABLED_AUTO_PARSED_FIELDS_BY_ENTERPRISE.get(_eid0)
    _disabled_out0 = DISABLED_AUTO_OUTPUT_FIELDS_BY_ENTERPRISE.get(_eid0)
    if _disabled_pf0 or _disabled_out0:
        if isinstance(data.get("parsed_fields"), dict):
            data["parsed_fields"] = _strip_disabled_fields(data["parsed_fields"], _disabled_pf0, [])
        if isinstance(data.get("mapped_fields"), dict):
            data["mapped_fields"] = _strip_disabled_fields(data["mapped_fields"], _disabled_out0, [])
        if isinstance(data.get("display_fields"), dict):
            data["display_fields"] = _strip_disabled_fields(data["display_fields"], _disabled_out0, [])

    # 自动填充 company_short
    if "company_short" not in data or not data["company_short"]:
        cs = _extract_company_short(data)
        if cs:
            data["company_short"] = cs

    # 自动计算 is_abnormal 和 hint（基于用户导出模板）
    pf = data.get("parsed_fields")
    if isinstance(pf, str):
        try:
            pf = json.loads(pf)
        except (TypeError, json.JSONDecodeError):
            pf = {}
    visible_keys = _get_visible_export_keys(db, data.get("user_id"))
    abnormal, hint = _compute_abnormal(
        pf if isinstance(pf, dict) else {},
        data.get("status", "pending"),
        data.get("doc_category", ""),
        visible_export_keys=visible_keys,
    )
    data["is_abnormal"] = 1 if abnormal else 0
    data["hint"] = hint

    # 首次识别完成时间：仅在插入时状态已经是done才落（如"手动上传"一次性写入的场景）；
    # 正常流程是先插入processing、再由update_insurance_record补done，走下面COALESCE分支
    if data.get("status") == "done" and not data.get("first_recognized_at"):
        data["first_recognized_at"] = datetime.now()

    # 自动计算 display_fields（映射后的展示字段）
    if isinstance(pf, dict) and pf:
        data["display_fields"] = apply_mapping(pf, data.get("company_short", ""))

    for field in _JSON_FIELDS:
        if field in data and not isinstance(data[field], str):
            data[field] = json.dumps(data[field], ensure_ascii=False)

    # updated_at 由 MySQL DEFAULT 自动填充，无需手动传入
    # created_at 允许显式传入（重新识别新增保单时沿用历史创建时间）
    data.pop("updated_at", None)
    if "created_at" in data and not data["created_at"]:
        data.pop("created_at", None)

    columns = ", ".join(data.keys())
    # MySQL 参数占位符为 %s
    placeholders = ", ".join(["%s"] * len(data))
    values = list(data.values())

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            f"INSERT INTO insurance_records ({columns}) VALUES ({placeholders})",
            values
        )
        record_id = cursor.lastrowid

        # 同步关联表（有 parsed_fields 时）
        if isinstance(pf, dict) and pf:
            try:
                _insert_policy_fields(cursor, record_id, pf)
            except Exception as e:
                logger.warning("同步关联表失败(insert), record_id=%d: %s", record_id, e)

        conn.commit()
        logger.debug("保单记录已插入, id=%d", record_id)
    finally:
        conn.close()

    # 加油包消费：标准版企业当月用量超出月额度(3000)后，每新增一条识别记录
    # 从加油包扣1次（最早到期的包先扣）。企业版无限额度不扣。失败不影响主流程
    try:
        _eid = data.get("enterprise_id")
        if _eid:
            from auth.db import (get_enterprise_by_id, get_enterprise_monthly_pdf_usage,
                                 consume_boost_pack)
            _ent = get_enterprise_by_id(db, _eid)
            if _ent and _ent.get("plan_type") not in ("enterprise", "enterprise_trial"):
                # 本条已计入当月用量，>3000 即本条属于超额部分
                if get_enterprise_monthly_pdf_usage(db, _eid) > 3000:
                    if consume_boost_pack(db, _eid):
                        logger.info("企业 %s 月额度已超，本条识别消费加油包1次", _eid)
    except Exception as e:
        logger.debug("加油包消费检查失败: %s", e)
    return record_id


def update_insurance_record(db, record_id: int, updates: dict):
    """
    更新指定 id 的保单识别记录。
    updates 中若包含 dict/list 类型的值，自动 json.dumps 序列化。
    updated_at 由 MySQL ON UPDATE CURRENT_TIMESTAMP 自动维护。
    """
    # 按企业配置剔除不需要系统自动识别的字段（如文十七的 业务员/出单渠道），
    # 已手动修改过的字段(manual_fields)不受影响，保留用户填写的值。
    if "parsed_fields" in updates or "mapped_fields" in updates or "display_fields" in updates:
        _conn0 = db.pool.connection()
        try:
            _c0 = _conn0.cursor(pymysql.cursors.DictCursor)
            _c0.execute("SELECT enterprise_id, manual_fields FROM insurance_records WHERE id = %s", (record_id,))
            _row0 = _c0.fetchone() or {}
        finally:
            _conn0.close()
        _eid1 = updates.get("enterprise_id", _row0.get("enterprise_id"))
        _manual_raw1 = _row0.get("manual_fields")
        if isinstance(_manual_raw1, str):
            try:
                _manual_fields1 = json.loads(_manual_raw1) if _manual_raw1 else []
            except (TypeError, json.JSONDecodeError):
                _manual_fields1 = []
        else:
            _manual_fields1 = _manual_raw1 or []
        # 若本次同时更新了 manual_fields，需并入一起判断，否则同批次新增的手动字段
        # （如应用AI值时把字段加入 manual_fields）会因 DB 旧值不含它而被误剔除。
        if "manual_fields" in updates:
            _incoming_mf1 = updates["manual_fields"]
            if isinstance(_incoming_mf1, str):
                try:
                    _incoming_mf1 = json.loads(_incoming_mf1)
                except (TypeError, json.JSONDecodeError):
                    _incoming_mf1 = []
            if isinstance(_incoming_mf1, list):
                _manual_fields1 = list(set(_manual_fields1) | set(_incoming_mf1))

        _disabled_pf1 = DISABLED_AUTO_PARSED_FIELDS_BY_ENTERPRISE.get(_eid1)
        _disabled_out1 = DISABLED_AUTO_OUTPUT_FIELDS_BY_ENTERPRISE.get(_eid1)
        if _disabled_pf1 or _disabled_out1:
            updates = dict(updates)
            if "parsed_fields" in updates and isinstance(updates["parsed_fields"], dict):
                updates["parsed_fields"] = _strip_disabled_fields(updates["parsed_fields"], _disabled_pf1, _manual_fields1)
            if "mapped_fields" in updates and isinstance(updates["mapped_fields"], dict):
                updates["mapped_fields"] = _strip_disabled_fields(updates["mapped_fields"], _disabled_out1, _manual_fields1)
            if "display_fields" in updates and isinstance(updates["display_fields"], dict):
                updates["display_fields"] = _strip_disabled_fields(updates["display_fields"], _disabled_out1, _manual_fields1)

    data = {}
    for k, v in updates.items():
        if isinstance(v, (dict, list)):
            data[k] = json.dumps(v, ensure_ascii=False)
        else:
            data[k] = v

    # 如果更新了 parsed_fields，同步更新 company_short 和 display_fields
    if "parsed_fields" in updates and "company_short" not in updates:
        cs = _extract_company_short(updates)
        if cs:
            data["company_short"] = cs
    if "parsed_fields" in updates and "display_fields" not in updates:
        # 仅在调用方未显式传入 display_fields 时才从 parsed_fields 重新生成
        pf_raw = updates["parsed_fields"]
        if isinstance(pf_raw, str):
            try:
                pf_raw = json.loads(pf_raw)
            except (TypeError, json.JSONDecodeError):
                pf_raw = {}
        if isinstance(pf_raw, dict) and pf_raw:
            cs = data.get("company_short") or _extract_company_short(updates)
            data["display_fields"] = json.dumps(
                apply_mapping(pf_raw, cs or ""), ensure_ascii=False
            )

    # MySQL 的 ON UPDATE CURRENT_TIMESTAMP 会自动更新 updated_at，无需手动传入
    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    values = list(data.values()) + [record_id]

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 如果更新了影响异常判断的字段，需先读取现有值补全上下文
        if any(k in updates for k in ("parsed_fields", "status", "doc_category", "abnormal_override_reason")):
            cursor.execute(
                "SELECT parsed_fields, status, doc_category, abnormal_override_reason, user_id FROM insurance_records WHERE id = %s",
                (record_id,)
            )
            existing = cursor.fetchone() or {}
            pf_raw = updates.get("parsed_fields", existing.get("parsed_fields", ""))
            if isinstance(pf_raw, str):
                try:
                    pf_raw = json.loads(pf_raw)
                except (TypeError, json.JSONDecodeError):
                    pf_raw = {}
            # 获取 override reason：优先取本次更新值，否则取现有值
            override_reason = updates.get("abnormal_override_reason",
                                          existing.get("abnormal_override_reason"))
            visible_keys = _get_visible_export_keys(db, existing.get("user_id"))
            abnormal, hint = _compute_abnormal(
                pf_raw if isinstance(pf_raw, dict) else {},
                updates.get("status") or existing.get("status", ""),
                updates.get("doc_category") or existing.get("doc_category", ""),
                abnormal_override_reason=override_reason,
                visible_export_keys=visible_keys,
            )
            data["is_abnormal"] = 1 if abnormal else 0
            data["hint"] = hint
            set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
            values = list(data.values()) + [record_id]

        if data.get("status") == "done":
            # COALESCE：仅当当前值为NULL(从未识别成功过)才写入，重新识别不会覆盖首次识别时间
            set_clause += ", first_recognized_at = COALESCE(first_recognized_at, %s)"
            values.insert(-1, datetime.now())

        cursor.execute(
            f"UPDATE insurance_records SET {set_clause} WHERE id = %s",
            values
        )

        # 同步关联表（parsed_fields 有更新时）
        _synced_plate_vin_ent = None
        if "parsed_fields" in updates:
            pf_sync = updates["parsed_fields"]
            if isinstance(pf_sync, str):
                try:
                    pf_sync = json.loads(pf_sync)
                except (TypeError, json.JSONDecodeError):
                    pf_sync = {}
            if isinstance(pf_sync, dict) and pf_sync:
                try:
                    _upsert_policy_fields(cursor, record_id, pf_sync)
                    _synced_plate_vin_ent = (
                        pf_sync.get("车牌号", ""), pf_sync.get("车架号VIN", ""),
                    )
                except Exception as e:
                    logger.warning("同步关联表失败(update), record_id=%d: %s", record_id, e)

        conn.commit()
        logger.debug("保单记录已更新, id=%d, 字段=%s", record_id, list(updates.keys()))

        # 实时同步续保任务快照（重新识别/手动编辑字段等改了保单期限时，不必等下一次定时扫描）。
        # 后台线程异步执行：update_insurance_record 是全部保单落库的必经之路(不只是"重新识别"，
        # 每条新收到的保单消息都会走)，同步内联调用会给这张已经高频读写的表每次都额外多绑
        # 几条查询/更新，加重锁竞争；放到独立线程里跑，不占用主事务/主请求的时间和锁持有窗口
        # （2026-07-14锁雪崩故障的放大因素之一）。本地导入避免循环依赖：main.py 顶层
        # import insurance.db，而 renewal_task_scan.py 顶层又 import main，模块级 import 会互相卡死。
        if _synced_plate_vin_ent is not None:
            _sync_ent_id = None
            _sync_user_id = None
            try:
                cursor.execute("SELECT enterprise_id, user_id FROM insurance_records WHERE id=%s", (record_id,))
                _ent_row = cursor.fetchone()
                _sync_ent_id = _ent_row["enterprise_id"] if _ent_row else None
                _sync_user_id = _ent_row["user_id"] if _ent_row else None
            except Exception as e:
                logger.warning("查询企业ID失败(续保任务同步前置), record_id=%d: %s", record_id, e)
            if _sync_ent_id:
                def _bg_sync_renewal_task(_plate, _vin, _ent_id, _uid, _rid):
                    try:
                        import renewal_task_scan
                        renewal_task_scan.sync_customer_task(db, _plate, _vin, _ent_id, _uid)
                    except Exception as e:
                        logger.warning("实时同步续保任务失败, record_id=%d: %s", _rid, e)
                import threading as _threading
                _threading.Thread(
                    target=_bg_sync_renewal_task,
                    args=(_synced_plate_vin_ent[0], _synced_plate_vin_ent[1], _sync_ent_id, _sync_user_id, record_id),
                    daemon=True,
                ).start()
    finally:
        conn.close()


def update_records_abnormal_flag(db, record_ids: list, is_abnormal: int) -> None:
    """批量更新 is_abnormal 字段，保持统计与列表一致。"""
    if not record_ids:
        return
    placeholders = ",".join(["%s"] * len(record_ids))
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE insurance_records SET is_abnormal=%s WHERE id IN ({placeholders})",
            [is_abnormal] + list(record_ids),
        )
        conn.commit()
    finally:
        conn.close()


def find_records_by_plate(db, plate: str, exclude_id: int = None, roomid: str = None) -> list:
    """
    查找同车牌的已完成保单记录，用于跨保单字段互补。
    返回 parsed_fields 非空且 status=done 的记录列表（按时间倒序）。

    roomid: 可选，传入时只匹配同一个群的记录（如群内文本回填场景），
    避免不同群里凑巧同车牌号的保单互相串数据。

    车牌匹配两侧均做归一化（去连字符/空格、转大写），兼容
    "粤E-R705I"/"粤ER705I"/手动修改成小写等写法。
    """
    if not plate:
        return []
    # 与 SQL 侧同样的归一化：繁转简、去半/全角连字符、半/全角空格、间隔点，转大写
    plate = normalize_plate_keyword(plate)
    if not plate:
        return []
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 库中"车牌号"可能是OCR识别或手动修改后的值（带-、带空格、小写等），逐层剥掉再比对
        plate_norm_sql = (
            "UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
            "JSON_UNQUOTE(JSON_EXTRACT(parsed_fields, '$.车牌号')),"
            "'-',''),'－',''),' ',''),'　',''),'·',''),'.',''))"
        )
        sql = (
            "SELECT id, parsed_fields, manual_fields FROM insurance_records "
            "WHERE status = 'done' AND parsed_fields IS NOT NULL "
            f"AND {plate_norm_sql} = %s"
        )
        params = [plate]
        if exclude_id:
            sql += " AND id != %s"
            params.append(exclude_id)
        if roomid:
            sql += " AND roomid = %s"
            params.append(roomid)
        sql += " ORDER BY id DESC LIMIT 10"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        for row in rows:
            if isinstance(row.get("parsed_fields"), str):
                try:
                    row["parsed_fields"] = json.loads(row["parsed_fields"])
                except (TypeError, json.JSONDecodeError):
                    row["parsed_fields"] = {}
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def find_records_by_vin(db, vin: str, exclude_id: int = None) -> list:
    """
    查找同车架号(VIN)的已完成保单记录，用于跨保单字段互补。
    车架号是车辆唯一标识，比车牌可靠（适用于"暂未上牌"等无车牌保单）。
    返回 parsed_fields 非空且 status=done 的记录列表（按时间倒序）。
    """
    # VIN 至少 10 位才可靠，避免空值/OCR 残片误匹配
    if not vin or len(vin.strip()) < 10:
        return []
    vin = vin.strip()
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sql = (
            "SELECT id, parsed_fields, manual_fields FROM insurance_records "
            "WHERE status = 'done' AND parsed_fields IS NOT NULL "
            "AND JSON_UNQUOTE(JSON_EXTRACT(parsed_fields, '$.车架号VIN')) = %s"
        )
        params = [vin]
        if exclude_id:
            sql += " AND id != %s"
            params.append(exclude_id)
        sql += " ORDER BY id DESC LIMIT 10"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        for row in rows:
            if isinstance(row.get("parsed_fields"), str):
                try:
                    row["parsed_fields"] = json.loads(row["parsed_fields"])
                except (TypeError, json.JSONDecodeError):
                    row["parsed_fields"] = {}
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def find_compulsory_end_dates(db, plates: list) -> dict:
    """
    批量查询车牌对应的交强险终保日期。
    返回 {车牌: 终保日期} 映射，取最新一条交强险记录的 end_date。
    """
    if not plates:
        return {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ",".join(["%s"] * len(plates))
        cursor.execute(
            f"SELECT pf.plate_no, pf.end_date "
            f"FROM insurance_policy_fields pf "
            f"JOIN insurance_records r ON r.id = pf.record_id "
            f"WHERE pf.plate_no IN ({placeholders}) "
            f"AND (pf.policy_type LIKE '%%交强%%' OR pf.policy_type LIKE '%%交通事故责任强制%%') "
            f"AND r.status = 'done' AND pf.end_date != '' "
            f"ORDER BY r.id DESC",
            plates,
        )
        result = {}
        for row in cursor.fetchall():
            plate = row["plate_no"]
            if plate not in result:
                result[plate] = row["end_date"]
        return result
    except Exception:
        return {}
    finally:
        conn.close()


def find_commercial_compulsory_start_dates(db, plates: list) -> dict:
    """
    批量查询车牌对应的商业险/交强险起保日期，供"交强与交商起保时间"字段使用。
    返回 {车牌: {"commercial": 商业险起保日期, "compulsory": 交强险起保日期}}，
    同一车牌每类险种只取最新一条(r.id DESC 先到先得)的 start_date。
    """
    if not plates:
        return {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ",".join(["%s"] * len(plates))
        cursor.execute(
            f"SELECT pf.plate_no, pf.start_date, pf.policy_type "
            f"FROM insurance_policy_fields pf "
            f"JOIN insurance_records r ON r.id = pf.record_id "
            f"WHERE pf.plate_no IN ({placeholders}) "
            f"AND r.status = 'done' AND pf.start_date != '' "
            f"ORDER BY r.id DESC",
            plates,
        )
        from insurance.policy_parser import get_policy_type_code
        result = {}
        for row in cursor.fetchall():
            plate = row["plate_no"]
            policy_type = row["policy_type"] or ""
            # 用跟合并导出一致的险种分类（商业险/交强险/驾意险/非车险），不能简单地拿
            # "不是交强险就当商业险"——驾意险/非车险(如交通出行意外险)会被错误当成商业险，
            # 导致"交强与交商起保时间"里商业险的那个日期其实是不相关险种的日期
            type_code, _ = get_policy_type_code(policy_type)
            if type_code not in ("compulsory", "commercial"):
                continue
            bucket = result.setdefault(plate, {})
            if type_code not in bucket:
                bucket[type_code] = row["start_date"]
        return result
    except Exception:
        return {}
    finally:
        conn.close()


def find_commercial_compulsory_end_dates(db, plates: list) -> dict:
    """
    批量查询车牌对应的商业险/交强险终保日期，供"交强与交商终保时间"字段使用。
    返回 {车牌: {"commercial": 商业险终保日期, "compulsory": 交强险终保日期}}，
    同一车牌每类险种只取最新一条(r.id DESC 先到先得)的 end_date。
    """
    if not plates:
        return {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ",".join(["%s"] * len(plates))
        cursor.execute(
            f"SELECT pf.plate_no, pf.end_date, pf.policy_type "
            f"FROM insurance_policy_fields pf "
            f"JOIN insurance_records r ON r.id = pf.record_id "
            f"WHERE pf.plate_no IN ({placeholders}) "
            f"AND r.status = 'done' AND pf.end_date != '' "
            f"ORDER BY r.id DESC",
            plates,
        )
        from insurance.policy_parser import get_policy_type_code
        result = {}
        for row in cursor.fetchall():
            plate = row["plate_no"]
            policy_type = row["policy_type"] or ""
            type_code, _ = get_policy_type_code(policy_type)
            if type_code not in ("compulsory", "commercial"):
                continue
            bucket = result.setdefault(plate, {})
            if type_code not in bucket:
                bucket[type_code] = row["end_date"]
        return result
    except Exception:
        return {}
    finally:
        conn.close()


def find_related_policies_batch(db, match_field: str, keys: list) -> dict:
    """
    比例规则「关联单条件」批量预取：按 车牌号/车架号/投保人 查询已完成记录的险种与日期。
    返回 {关联值: [{"rid": record_id, "ptype": 险种, "date": 日期字符串}]}。
    任何异常返回 {}（关联条件按不存在处理），不影响主流程。
    """
    _COLS = {"车牌号": "plate_no", "车架号": "vin", "投保人": "applicant"}
    col = _COLS.get(match_field)
    if not col:
        return {}
    clean_keys = [str(k).strip() for k in (keys or []) if k and str(k).strip()]
    if not clean_keys:
        return {}
    try:
        conn = db.pool.connection()
    except Exception:
        return {}
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ",".join(["%s"] * len(clean_keys))
        # applicant 为 TEXT 列（无索引），LIMIT 兜底防止极端情况下拖慢查询
        cursor.execute(
            f"SELECT pf.{col} AS match_key, pf.record_id, pf.policy_type, "
            f"pf.sign_date_iso, pf.start_date, r.created_at "
            f"FROM insurance_policy_fields pf "
            f"JOIN insurance_records r ON r.id = pf.record_id "
            f"WHERE pf.{col} IN ({placeholders}) AND r.status = 'done' "
            f"ORDER BY r.id DESC LIMIT 5000",
            clean_keys,
        )
        result = {}
        for row in cursor.fetchall():
            key = str(row.get("match_key") or "").strip()
            if not key:
                continue
            date_val = row.get("sign_date_iso") or row.get("start_date") or row.get("created_at") or ""
            result.setdefault(key, []).append({
                "rid": row.get("record_id"),
                "ptype": row.get("policy_type") or "",
                "date": str(date_val),
            })
        return result
    except Exception:
        return {}
    finally:
        conn.close()


def find_insurer_info_by_plates(db, plates: list) -> dict:
    """
    批量查询车牌对应的保司信息和人员信息。
    返回 {车牌: {"insurer_name", "insurer_address", "owner", "id_number"}}，取最新一条有值记录互补。
    """
    if not plates:
        return {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ",".join(["%s"] * len(plates))
        cursor.execute(
            f"SELECT pf.plate_no, pf.insurer_name, pf.insurer_address, "
            f"pf.owner, pf.id_number "
            f"FROM insurance_policy_fields pf "
            f"JOIN insurance_records r ON r.id = pf.record_id "
            f"WHERE pf.plate_no IN ({placeholders}) "
            f"AND r.status = 'done' "
            f"ORDER BY r.id DESC",
            plates,
        )
        result = {}
        for row in cursor.fetchall():
            plate = row["plate_no"]
            if plate not in result:
                result[plate] = {
                    "insurer_name": row["insurer_name"] or "",
                    "insurer_address": row["insurer_address"] or "",
                    "owner": row["owner"] or "",
                    "id_number": row["id_number"] or "",
                }
            else:
                for k, col in [("insurer_name", "insurer_name"), ("insurer_address", "insurer_address"),
                                ("owner", "owner"), ("id_number", "id_number")]:
                    if not result[plate][k] and row[col]:
                        result[plate][k] = row[col]
        return result
    except Exception:
        return {}
    finally:
        conn.close()


def find_sign_info_by_plates(db, plates: list) -> dict:
    """
    批量查询车牌对应的签单日期和业务员。
    返回 {车牌: {"sign_date": ..., "salesperson": ...}}，取最新有值记录。
    走 insurance_policy_fields 关联表（plate_no 有索引），
    替代原先对 insurance_records.display_fields 的 JSON_EXTRACT 全表扫描（单次近 1 秒）。
    """
    if not plates:
        return {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ",".join(["%s"] * len(plates))
        cursor.execute(
            f"SELECT pf.plate_no, pf.sign_date, pf.salesperson "
            f"FROM insurance_policy_fields pf "
            f"JOIN insurance_records r ON r.id = pf.record_id "
            f"WHERE pf.plate_no IN ({placeholders}) "
            f"AND r.status = 'done' "
            f"ORDER BY r.id DESC",
            plates,
        )
        result = {}
        for row in cursor.fetchall():
            plate = row["plate_no"]
            if plate not in result:
                result[plate] = {"sign_date": "", "salesperson": ""}
            p = result[plate]
            if not p["sign_date"] and row["sign_date"]:
                p["sign_date"] = row["sign_date"]
            if not p["salesperson"] and row["salesperson"]:
                p["salesperson"] = row["salesperson"]
        return result
    except Exception:
        return {}
    finally:
        conn.close()


def find_records_by_person(db, applicant: str = "", insured: str = "", owner: str = "",
                           exclude_id: int = None) -> list:
    """
    通过投保人/被保险人/车主匹配其他保单记录（用于无车牌时的字段互补）。
    任一人名字段匹配即可。返回 parsed_fields 非空且 status=done 的记录列表。
    优先返回车险（非"非车险/驾意险"），按 id DESC 排序。
    """
    names = [n for n in (applicant, insured, owner) if n]
    if not names:
        return []
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 用关联表按人名匹配
        or_conditions = []
        params = []
        for name in names:
            or_conditions.append("(pf.applicant = %s OR pf.insured = %s OR pf.owner = %s)")
            params.extend([name, name, name])
        where = " OR ".join(or_conditions)
        sql = (
            "SELECT r.id, r.parsed_fields, r.manual_fields FROM insurance_records r "
            "JOIN insurance_policy_fields pf ON pf.record_id = r.id "
            f"WHERE r.status = 'done' AND r.parsed_fields IS NOT NULL AND ({where})"
        )
        if exclude_id:
            sql += " AND r.id != %s"
            params.append(exclude_id)
        sql += " ORDER BY r.id DESC LIMIT 20"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        for row in rows:
            if isinstance(row.get("parsed_fields"), str):
                try:
                    row["parsed_fields"] = json.loads(row["parsed_fields"])
                except (TypeError, json.JSONDecodeError):
                    row["parsed_fields"] = {}
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def _policy_types_for_category(db, category: str) -> list:
    """返回属于指定大类（商业险/交强险/驾意险/非车险）的所有原始 policy_type 值。

    险种大类由 get_policy_type_code 按关键词归类（驾意险/非车险为兜底类，无法用 LIKE
    精确匹配），因此先取库中 distinct 的原始险种全称逐一归类，再用 IN 过滤，保证与列表
    展示的归类完全一致。distinct 险种基数很小，仅在启用险种筛选时查询。"""
    if not category:
        return []
    from insurance.policy_parser import get_policy_type_code
    # 大类中文 → type_code（get_policy_type_code 返回的 label 为「驾乘/意外险」，
    # 与前端「驾意险」用词不同，故统一用稳定的 type_code 比较）
    _cat_to_code = {
        "商业险": "commercial",
        "交强险": "compulsory",
        "驾意险": "accident",
        "非车险": "non_vehicle",
    }
    code = _cat_to_code.get(category)
    if not code:
        return []
    result = []
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT DISTINCT policy_type FROM insurance_policy_fields WHERE policy_type != ''"
        )
        for row in cursor.fetchall():
            pt = row.get("policy_type") or ""
            if pt and get_policy_type_code(pt)[0] == code:
                result.append(pt)
        return result
    except Exception:
        logger.debug("查询险种分类失败")
        return result
    finally:
        conn.close()


def query_insurance_records(
    db,
    page: int = 1,
    page_size: int = 20,
    roomid: str = "",
    status: str = "",
    keyword: str = "",
    source: str = "",
    source_type: str = "",
    sender: str = "",
    ocr_engine: str = "",
    doc_category: str = "",
    is_abnormal: str = "",
    company_short: str = "",
    policy_type: str = "",
    date_start: str = "",
    date_end: str = "",
    updated_at_date_start: str = "",
    updated_at_date_end: str = "",
    user_ids: list = None,
    enterprise_id: int = None,
    # 关联表模糊搜索
    search_company: str = "",
    search_policy_no: str = "",
    search_plate_no: str = "",
    search_applicant: str = "",
    search_insured: str = "",
    search_salesperson: str = "",
    # 关联表日期筛选
    sign_date_start: str = "",
    sign_date_end: str = "",
    start_date_start: str = "",
    start_date_end: str = "",
    end_date_start: str = "",
    end_date_end: str = "",
    dedup: bool = False,
    review_status: str = "",
    sort_by: str = "",
    sort_order: str = "desc",
    exclude_nonpolicy: bool = False,
    policy_sort: dict = None,
) -> dict:
    """
    分页查询保单识别记录，支持按群、状态、关键词、来源、识别方式、文档类型筛选。
    dedup: 按保单号去重，只保留每个保单号最新的一条记录。
    source_type: 'room' 仅群聊, 'user' 仅私聊（roomid 为空的记录）
    sender: 按发送人 ID 筛选
    ocr_engine: 按识别方式筛选（pdfplumber / baidu）
    doc_category: 按文档类型筛选（保单 / 电子标志 等）
    exclude_nonpolicy: 过滤非保单（排除 doc_category 非空且不等于"保单"的记录）
    search_*: 关联表字段模糊搜索（承保公司、保单号、车牌、投保人、被保人、业务员）
    sign_date_*/start_date_*/end_date_*: 关联表日期范围筛选
    返回: {"total": 总数, "pages": 总页数, "page": 当前页, "records": [...]}
    """
    conditions = []
    params = []
    # 「保单排序」：客户设置的排序(sort_by)为最高优先级(主排序，前端始终传入)；
    # 保单排序作为「次级排序」追加在主排序之后——同一主排序值(如同一签单日期)下，
    # 把同车牌的保单按险种大类排在一起(下方 ORDER BY 追加 车牌+险种)。因追加了车牌/险种列，需 JOIN。
    _policy_sort_on = bool(policy_sort and policy_sort.get("enabled"))
    # 动态判断是否需要 JOIN 关联表：
    # dedup 始终需要（按 pf.policy_no 分组）；有关联表搜索/日期/险种/排序字段时才 JOIN
    _JOIN_SORT_FIELDS = {"plate_no", "sign_date", "start_date", "end_date", "policy_no"}
    need_join = dedup or _policy_sort_on or bool(
        policy_type or
        search_company or search_policy_no or search_plate_no or
        search_applicant or search_insured or search_salesperson or
        sign_date_start or sign_date_end or
        start_date_start or start_date_end or
        end_date_start or end_date_end or
        sort_by in _JOIN_SORT_FIELDS
    )
    # 主表别名前缀
    col_prefix = "r."

    # 排除已软删除的记录
    conditions.append(f"{col_prefix}deleted_at IS NULL")

    # 统一排除：处理中/排队中（未出结果）和重复文件记录，
    # 使"全部"与 成功+需人工补充+非保单+失败 各分类之和一致
    conditions.append(f"{col_prefix}status NOT IN ('processing', 'pending', 'duplicate')")

    # 来源类型筛选
    if source_type == "room":
        conditions.append(f"{col_prefix}roomid IS NOT NULL AND {col_prefix}roomid != ''")
        if roomid:
            conditions.append(f"{col_prefix}roomid = %s")
            params.append(roomid)
    elif source_type == "user":
        conditions.append(f"({col_prefix}roomid IS NULL OR {col_prefix}roomid = '')")
        if sender:
            conditions.append(f"{col_prefix}sender = %s")
            params.append(sender)
    else:
        if roomid:
            conditions.append(f"{col_prefix}roomid LIKE %s")
            params.append(f"%{roomid}%")
    if status:
        # done 和 success 视为同一状态，与 stats 统计逻辑保持一致
        if status == "done":
            conditions.append(f"({col_prefix}status = 'done' OR {col_prefix}status = 'success')")
        else:
            conditions.append(f"{col_prefix}status = %s")
            params.append(status)
    if keyword:
        # 在文件名、群名、发送人名中模糊匹配
        conditions.append(
            f"({col_prefix}filename LIKE %s OR {col_prefix}room_name LIKE %s OR {col_prefix}sender_name LIKE %s)"
        )
        params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
    if source:
        conditions.append(f"{col_prefix}source = %s")
        params.append(source)
    if ocr_engine:
        conditions.append(f"{col_prefix}ocr_engine = %s")
        params.append(ocr_engine)
    if doc_category:
        if doc_category.startswith("!"):
            conditions.append(f"{col_prefix}doc_category != %s AND {col_prefix}doc_category != ''")
            params.append(doc_category[1:])
        else:
            conditions.append(f"{col_prefix}doc_category = %s")
            params.append(doc_category)
    # 过滤非保单：只排除"已识别出非保单类型"的记录，doc_category 为空（失败/未识别）的仍保留
    if exclude_nonpolicy:
        conditions.append(
            f"({col_prefix}doc_category = '保单' OR {col_prefix}doc_category IS NULL OR {col_prefix}doc_category = '')"
        )
    if is_abnormal == "1":
        conditions.append(f"{col_prefix}is_abnormal = 1")
    elif is_abnormal == "0":
        conditions.append(f"{col_prefix}is_abnormal = 0")
    # 核对状态：今日未核对 = 当天无任何内部管理备注；今日已核对 = 当天有备注（配合异常单每日核对）
    if review_status == "unreviewed":
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM insurance_internal_notes n "
            f"WHERE n.record_id = {col_prefix}id AND DATE(n.created_at) = CURDATE())"
        )
    elif review_status == "reviewed":
        conditions.append(
            f"EXISTS (SELECT 1 FROM insurance_internal_notes n "
            f"WHERE n.record_id = {col_prefix}id AND DATE(n.created_at) = CURDATE())"
        )
    if company_short:
        conditions.append(f"{col_prefix}company_short = %s")
        params.append(company_short)
    # 险种大类筛选：将大类映射为属于该类的原始险种全称，用 IN 过滤（与列表归类一致）
    if policy_type:
        _pt_values = _policy_types_for_category(db, policy_type)
        if _pt_values:
            _ph = ",".join(["%s"] * len(_pt_values))
            conditions.append(f"pf.policy_type IN ({_ph})")
            params.extend(_pt_values)
        else:
            conditions.append("0")  # 该大类无匹配险种，返回空
    if date_start:
        conditions.append(f"{col_prefix}created_at >= %s")
        params.append(date_start)
    if date_end:
        conditions.append(f"{col_prefix}created_at < %s")
        params.append(date_end)
    if updated_at_date_start:
        conditions.append(f"{col_prefix}updated_at >= %s")
        params.append(updated_at_date_start)
    if updated_at_date_end:
        conditions.append(f"{col_prefix}updated_at < %s")
        params.append(updated_at_date_end)
    # 按账号权限过滤
    if user_ids is not None:
        if user_ids:
            ph = ",".join(["%s"] * len(user_ids))
            conditions.append(f"{col_prefix}user_id IN ({ph})")
            params.extend(user_ids)
        else:
            conditions.append("0")  # 空列表不返回数据
    # 按企业过滤
    if enterprise_id is not None:
        conditions.append(f"{col_prefix}enterprise_id = %s")
        params.append(enterprise_id)

    # 关联表模糊搜索条件
    if need_join:
        if search_company:
            conditions.append("pf.company LIKE %s")
            params.append(f"%{search_company}%")
        if search_policy_no:
            conditions.append("pf.policy_no LIKE %s")
            params.append(f"%{search_policy_no}%")
        if search_plate_no:
            # 车牌搜索两侧归一化：关键词繁转简/去分隔符/大写，库侧去连字符/空格/大写，
            # 兼容客户输入"粵B0U9U0"（繁体）、"粤B-0U9U0"（带杠）等写法
            _plate_kw = normalize_plate_keyword(search_plate_no)
            if _plate_kw:
                conditions.append(f"{_PF_PLATE_NORM_SQL} LIKE %s")
                params.append(f"%{_plate_kw}%")
            else:
                conditions.append("pf.plate_no LIKE %s")
                params.append(f"%{search_plate_no}%")
        if search_applicant:
            conditions.append("pf.applicant LIKE %s")
            params.append(f"%{search_applicant}%")
        if search_insured:
            conditions.append("pf.insured LIKE %s")
            params.append(f"%{search_insured}%")
        if search_salesperson:
            conditions.append("pf.salesperson LIKE %s")
            params.append(f"%{search_salesperson}%")
        # 关联表日期范围筛选（使用 ISO 日期列，可命中索引）
        for iso_col, val_start, val_end in [
            ("pf.sign_date_iso", sign_date_start, sign_date_end),
            ("pf.start_date_iso", start_date_start, start_date_end),
            ("pf.end_date_iso", end_date_start, end_date_end),
        ]:
            if val_start:
                conditions.append(f"{iso_col} >= %s")
                params.append(_date_to_iso(val_start))
            if val_end:
                conditions.append(f"{iso_col} <= %s")
                params.append(_date_to_iso(val_end))

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # 构建 FROM 子句（仅在需要时 JOIN 关联表）
    _join = " LEFT JOIN insurance_policy_fields pf ON r.id = pf.record_id"
    from_clause = f"insurance_records r{_join if need_join else ''}"

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 设置查询超时 30 秒，避免慢查询长时间占用连接池
        cursor.execute("SET SESSION MAX_EXECUTION_TIME = 30000")

        # 去重模式：按保单号分组，取每组最新记录
        # 仅「保单」参与按保单号去重；非保单(电子标志/发票等)即便与保单同号也一律保留，
        # 避免电子标志与其交强保单同号被并成一条、还把真保单挤掉。
        if dedup:
            # 参与去重的：有保单号 且 文档类型=保单
            dedup_cond = f"pf.policy_no IS NOT NULL AND pf.policy_no != '' AND {col_prefix}doc_category = '保单'"
            full_where = f"{where_clause} AND {dedup_cond}" if where_clause else f"WHERE {dedup_cond}"
            count_sql = (
                f"SELECT COUNT(*) as cnt FROM ("
                f"  SELECT MAX({col_prefix}id) FROM {from_clause} {full_where} GROUP BY pf.policy_no, DATE({col_prefix}created_at)"
                f") t"
            )
            cursor.execute(count_sql, params)
            total_dedup = cursor.fetchone()["cnt"]
            # 其余记录(空保单号，或非保单文档)全部保留，不参与去重
            _keep_cond = f"(pf.policy_no IS NULL OR pf.policy_no = '' OR {col_prefix}doc_category IS NULL OR {col_prefix}doc_category != '保单')"
            empty_where = f"{where_clause} AND {_keep_cond}" if where_clause else f"WHERE {_keep_cond}"
            cursor.execute(f"SELECT COUNT(*) as cnt FROM {from_clause} {empty_where}", params)
            total_empty = cursor.fetchone()["cnt"]
            total = total_dedup + total_empty
        else:
            cursor.execute(
                f"SELECT COUNT(*) as cnt FROM {from_clause} {where_clause}",
                params
            )
            total = cursor.fetchone()["cnt"]
        pages = max(1, (total + page_size - 1) // page_size)

        # 构建排序子句
        _dir = "ASC" if sort_order.upper() == "ASC" else "DESC"
        # 排序字段白名单映射（id 查询阶段 / 最终数据查询阶段使用不同别名）
        _sort_map_id = {
            "plate_no": "COALESCE(pf.plate_no, '')",
            "created_at": "r.created_at",
            "updated_at": "r.updated_at",
            "sign_date": "COALESCE(pf.sign_date_iso, '')",
            "start_date": "COALESCE(pf.start_date_iso, '')",
            "end_date": "COALESCE(pf.end_date_iso, '')",
            "company_short": "COALESCE(r.company_short, '')",
            "policy_no": "COALESCE(pf.policy_no, '')",
        }
        _sort_map_id_dedup = {
            "plate_no": "COALESCE(pf2.plate_no, '')",
            "created_at": "t.id",
            "updated_at": "t.id",
            "sign_date": "COALESCE(pf2.sign_date_iso, '')",
            "start_date": "COALESCE(pf2.start_date_iso, '')",
            "end_date": "COALESCE(pf2.end_date_iso, '')",
            "company_short": "COALESCE(pf2.company, '')",
            "policy_no": "COALESCE(pf2.policy_no, '')",
        }
        _sort_map_final = {
            "plate_no": "COALESCE(pf3.plate_no, '')",
            "created_at": "r2.created_at",
            "updated_at": "r2.updated_at",
            "sign_date": "COALESCE(pf3.sign_date_iso, '')",
            "start_date": "COALESCE(pf3.start_date_iso, '')",
            "end_date": "COALESCE(pf3.end_date_iso, '')",
            "company_short": "COALESCE(r2.company_short, '')",
            "policy_no": "COALESCE(pf3.policy_no, '')",
        }
        _sort_key = sort_by if sort_by in _sort_map_id else "created_at"
        # 默认无 sort_by 时按创建时间降序
        if not sort_by:
            _dir = "DESC"
        id_order = f"{_sort_map_id[_sort_key]} {_dir}, r.id DESC"
        dedup_order = f"{_sort_map_id_dedup[_sort_key]} {_dir}, t.id DESC"
        final_order = f"{_sort_map_final[_sort_key]} {_dir}, r2.id DESC"

        # 「保单排序」启用：在主排序(客户设置的 sort_by，最高优先级)之后，追加 车牌+险种 作为次级排序。
        # 同一主排序值(如同一签单日期)下，同车牌的保单按险种大类排在一起。
        # 险种大类 rank 由前端传入的 order 决定；CASE 里的 % 需写成 %% 供 pymysql 参数化转义。
        if _policy_sort_on:
            _order = policy_sort.get("order") or ["交强险", "商业险", "非车险"]
            _rk = {c: i for i, c in enumerate(_order)}
            _jq, _sy, _fc = _rk.get("交强险", 0), _rk.get("商业险", 1), _rk.get("非车险", 2)

            # 险种大类与系统规范分类 get_policy_type_code 一致：交强→交强险；含"商业"等→商业险；
            # 其余(驾乘/意外/尊享/各类非标准产品名)兜底→非车险（不能默认商业，否则如"平安车主尊享保障"会误判）
            def _rank_case(alias):
                col = f"{alias}.policy_type"
                return (
                    f"CASE WHEN {col} LIKE '%%交强%%' OR {col} LIKE '%%交通事故责任强制%%' OR {col} LIKE '%%交通事故强制保险%%' THEN {_jq} "
                    f"WHEN {col} LIKE '%%商业%%' OR {col} LIKE '%%机动车辆保险%%' OR {col} LIKE '%%机动车辆综合险%%' THEN {_sy} "
                    f"ELSE {_fc} END"
                )
            # 锚点排序：同一车牌用该车主排序列的锚点值 MAX(...) OVER(PARTITION BY 车牌) 参与主排序，
            # 让同车所有险种保单绑在一起、不被主排序(如创建日期)按各自时间拆散；再按 车牌ASC + 险种rank 组内排。
            _anchor_id = f"MAX({_sort_map_id[_sort_key]}) OVER (PARTITION BY {_sort_map_id['plate_no']})"
            _anchor_dedup = f"MAX({_sort_map_id_dedup[_sort_key]}) OVER (PARTITION BY {_sort_map_id_dedup['plate_no']})"
            _anchor_final = f"MAX({_sort_map_final[_sort_key]}) OVER (PARTITION BY {_sort_map_final['plate_no']})"
            id_order = f"{_anchor_id} {_dir}, {_sort_map_id['plate_no']} ASC, {_rank_case('pf')} ASC, r.id DESC"
            dedup_order = f"{_anchor_dedup} {_dir}, {_sort_map_id_dedup['plate_no']} ASC, {_rank_case('pf2')} ASC, t.id DESC"
            final_order = f"{_anchor_final} {_dir}, {_sort_map_final['plate_no']} ASC, {_rank_case('pf3')} ASC, r2.id DESC"

        # 延迟关联：先查 id（轻量排序），再用 id 取完整数据
        offset = (page - 1) * page_size
        if dedup:
            # 去重：每个保单号取最新的 id；空保单号全部保留
            id_sql = (
                f"SELECT t.id FROM ("
                f"SELECT MAX({col_prefix}id) as id FROM {from_clause} {full_where} "
                f"GROUP BY pf.policy_no, DATE({col_prefix}created_at) "
                f"UNION ALL "
                f"SELECT {col_prefix}id FROM {from_clause} {empty_where}"
                f") t LEFT JOIN insurance_policy_fields pf2 ON t.id = pf2.record_id "
                f"ORDER BY {dedup_order} LIMIT %s OFFSET %s"
            )
            cursor.execute(id_sql, params + params + [page_size, offset])
        else:
            cursor.execute(
                f"SELECT r.id FROM {from_clause} {where_clause} "
                f"ORDER BY {id_order} LIMIT %s OFFSET %s",
                params + [page_size, offset]
            )
        id_rows = cursor.fetchall()
        if id_rows:
            ids = [r["id"] for r in id_rows]
            placeholders = ",".join(["%s"] * len(ids))
            # 群名以统一群信息表 wecom_groups 为准（群改名后历史记录自动显示新名），
            # 未同步到的用入库时固化的 room_name 兜底
            select_cols = (
                "r2.id, r2.roomid, "
                "COALESCE(NULLIF(g.name, ''), r2.room_name) AS room_name, "
                "r2.sender, r2.sender_name, "
                "r2.filename, r2.cos_url, r2.ocr_engine, r2.doc_category, r2.confidence, "
                "r2.dingtalk_synced, r2.status, r2.source, r2.created_at, r2.updated_at, "
                "r2.company_short, r2.is_abnormal, r2.hint, r2.display_fields, r2.manual_fields, "
                "r2.abnormal_override_reason, r2.user_id, r2.file_md5, r2.error_message, "
                "r2.enterprise_id, r2.first_recognized_at, "
                "r2.ai_check_status, r2.ai_checked_at, "
                # 是否已产出AI质检结果（有含完整提取的流水即算），前端据此把按钮改名「AI质检结果」
                "EXISTS(SELECT 1 FROM ai_check_log acl WHERE acl.record_id = r2.id AND acl.extracted_json IS NOT NULL) AS has_ai_check, "
                "pf3.owner AS _pf_owner"
            )
            cursor.execute(
                f"SELECT {select_cols} FROM insurance_records r2 "
                f"LEFT JOIN insurance_policy_fields pf3 ON r2.id = pf3.record_id "
                f"LEFT JOIN wecom_groups g ON g.roomid = r2.roomid "
                f"WHERE r2.id IN ({placeholders}) ORDER BY {final_order}",
                ids
            )
            records = list(cursor.fetchall())
        else:
            records = []

        return {"total": total, "pages": pages, "page": page, "records": records}
    finally:
        conn.close()


def get_insurance_record(db, record_id: int) -> dict:
    """获取单条保单识别记录，不存在时返回 None。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM insurance_records WHERE id = %s",
            (record_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_insurance_config(db, key: str, default=None):
    """
    获取配置项。
    config_value 若是合法 JSON 字符串则自动反序列化，否则直接返回字符串。
    key 不存在时返回 default。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT config_value FROM insurance_config WHERE config_key = %s",
            (key,)
        )
        row = cursor.fetchone()
        if row is None:
            return default
        raw = row["config_value"]
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw
    finally:
        conn.close()


# 群内补充·全局跳过关键词黑名单缓存（60s TTL；默认空 = 行为与无此功能时一致）
_skip_keywords_cache = None
_skip_keywords_expire = 0.0

FILL_SKIP_KEYWORDS_CONFIG_KEY = "group_fill_skip_keywords"


def get_fill_skip_keywords(db) -> list:
    """
    获取全局跳过关键词黑名单（内部管理员在设置页维护）。
    群消息文本包含任一关键词时，跳过全部关键词触发链路（报价/回填/台账导出）。
    60s 内存缓存，读取失败返回空列表（不影响消息主流程）。
    """
    global _skip_keywords_cache, _skip_keywords_expire
    import time as _time
    if _skip_keywords_cache is not None and _time.time() < _skip_keywords_expire:
        return _skip_keywords_cache
    try:
        val = get_insurance_config(db, FILL_SKIP_KEYWORDS_CONFIG_KEY, [])
        keywords = [str(k).strip() for k in val if str(k).strip()] if isinstance(val, list) else []
    except Exception:
        logger.debug("读取跳过关键词黑名单失败")
        keywords = []
    _skip_keywords_cache = keywords
    _skip_keywords_expire = _time.time() + 60
    return keywords


def invalidate_fill_skip_keywords_cache():
    """保存黑名单后调用，让本进程缓存立即失效。"""
    global _skip_keywords_cache
    _skip_keywords_cache = None


_TZ008_ENTERPRISE_IDS = None


def get_tz008_enterprise_ids(db) -> set:
    """返回企业编号为 TZ-008 的 enterprise_id 集合（进程内缓存，编号极少变动）。"""
    global _TZ008_ENTERPRISE_IDS
    if _TZ008_ENTERPRISE_IDS is not None:
        return _TZ008_ENTERPRISE_IDS
    ids = set()
    try:
        conn = db.pool.connection()
        try:
            cur = conn.cursor(pymysql.cursors.DictCursor)
            cur.execute("SELECT id, enterprise_no FROM enterprises")
            for r in cur.fetchall():
                eno = _re.sub(r"[^A-Za-z0-9]", "", (r.get("enterprise_no") or "")).upper()
                if eno == "TZ008":
                    ids.add(r["id"])
        finally:
            conn.close()
    except Exception:
        logger.debug("查询 TZ-008 企业失败")
        return set()
    _TZ008_ENTERPRISE_IDS = ids
    return ids


def apply_tz008_plate_fallback(db, fields: dict, enterprise_id) -> bool:
    """
    TZ-008(鹏杰华智能)专属：车牌号为空(或"暂未上牌")且有车架号(VIN)时，用 VIN 填入车牌号。
    在写入路径(识别/重新识别)调用以持久化，使列表/详情/导出/合并各处一致。返回是否改动。
    """
    if enterprise_id is None:
        return False
    plate = (fields.get("车牌号") or "").strip()
    if plate and plate != "暂未上牌":
        return False
    vin = (fields.get("车架号VIN") or "").strip()
    if len(vin) < 10:
        return False
    if enterprise_id not in get_tz008_enterprise_ids(db):
        return False
    fields["车牌号"] = vin
    return True


def set_insurance_config(db, key: str, value):
    """
    设置配置项（存在则更新，不存在则插入）。
    value 若为 dict/list/int/float/bool/None 等非字符串类型，自动 json.dumps 序列化。
    MySQL 使用 REPLACE INTO 实现 upsert。
    """
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # REPLACE INTO：config_key 为 UNIQUE，存在则替换，不存在则插入
        cursor.execute(
            "REPLACE INTO insurance_config (config_key, config_value) VALUES (%s, %s)",
            (key, value)
        )
        conn.commit()
        logger.debug("保单配置已更新, key=%s", key)
    finally:
        conn.close()


def get_all_insurance_config(db, max_value_len: int = 0) -> dict:
    """
    获取全部配置项，返回 {key: value} 字典。
    每个 value 自动尝试 JSON 反序列化。
    max_value_len: 大于 0 时截断 config_value 读取长度，避免大 LONGTEXT 拖慢查询
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if max_value_len > 0:
            cursor.execute(
                "SELECT config_key, LEFT(config_value, %s) as config_value FROM insurance_config",
                (max_value_len,)
            )
        else:
            cursor.execute("SELECT config_key, config_value FROM insurance_config")
        result = {}
        for row in cursor.fetchall():
            raw = row["config_value"]
            try:
                result[row["config_key"]] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                result[row["config_key"]] = raw
        return result
    finally:
        conn.close()


def upsert_insurance_record_by_policy(db, record: dict) -> tuple:
    """
    按保单号+用户+企业去重保存记录。
    同一用户同一企业下相同保单号的记录才更新，不同用户/企业始终创建新记录。
    返回: (record_id, is_update)
    """
    parsed_fields = record.get("parsed_fields", {})
    if isinstance(parsed_fields, str):
        try:
            parsed_fields = json.loads(parsed_fields)
        except (TypeError, json.JSONDecodeError):
            parsed_fields = {}

    policy_no = parsed_fields.get("保单号", "") or parsed_fields.get("保险单号", "")
    record_user_id = record.get("user_id")
    record_enterprise_id = record.get("enterprise_id")

    # 有保单号时尝试查找同一用户/企业下的已有记录
    if policy_no:
        conn = db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            conditions = [
                "(JSON_UNQUOTE(JSON_EXTRACT(parsed_fields, '$.保单号')) = %s "
                "OR JSON_UNQUOTE(JSON_EXTRACT(parsed_fields, '$.保险单号')) = %s)"
            ]
            params = [policy_no, policy_no]

            if record_user_id is not None:
                conditions.append("user_id = %s")
                params.append(record_user_id)
            else:
                conditions.append("user_id IS NULL")

            if record_enterprise_id is not None:
                conditions.append("enterprise_id = %s")
                params.append(record_enterprise_id)
            else:
                conditions.append("enterprise_id IS NULL")

            sql = f"SELECT id FROM insurance_records WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT 1"
            cursor.execute(sql, params)
            existing = cursor.fetchone()
        finally:
            conn.close()

        if existing:
            update_insurance_record(db, existing["id"], record)
            return existing["id"], True

    # 无保单号或未找到已有记录，插入新记录
    record_id = save_insurance_record(db, record)
    return record_id, False


def get_company_stats(db) -> list:
    """
    统计每个保险公司简称的识别记录数。
    使用 company_short 冗余列（有索引），避免 JSON_EXTRACT 全表扫描。
    仅统计：status=done/success、doc_category=保单、confidence>0 的记录。
    返回: [{"company": "人保PICC", "count": 42}, ...]，按数量降序排列。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            SELECT company_short AS company, COUNT(*) AS cnt
            FROM insurance_records
            WHERE status IN ('done', 'success')
              AND doc_category = '保单'
              AND confidence > 0
              AND company_short != ''
              AND deleted_at IS NULL
            GROUP BY company_short
            ORDER BY cnt DESC
        """)
        return [{"company": r["company"], "count": r["cnt"]} for r in cursor.fetchall()]
    finally:
        conn.close()


def get_company_detail_stats(db) -> list:
    """
    统计每个保司的明细数据：总量、本月量、险种分布（交强/商业/驾意/非车）、最近识别时间。
    口径与 get_company_stats 一致（status=done/success、doc_category=保单、confidence>0）。
    险种大类由 policy_parser.get_policy_type_code 按 policy_type 归类后在 Python 端聚合。
    返回: [{company, count, month_count, compulsory, commercial, accident, non_vehicle, last_time}]
    """
    from insurance.policy_parser import get_policy_type_code

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            SELECT company_short AS company,
                   JSON_UNQUOTE(JSON_EXTRACT(parsed_fields, '$."险种类型"')) AS ptype,
                   COUNT(*) AS cnt,
                   SUM(CASE WHEN created_at >= DATE_FORMAT(NOW(), '%%Y-%%m-01') THEN 1 ELSE 0 END) AS month_cnt,
                   MIN(created_at) AS first_time,
                   MAX(created_at) AS last_time
            FROM insurance_records
            WHERE status IN ('done', 'success')
              AND doc_category = '保单'
              AND confidence > 0
              AND company_short != ''
              AND deleted_at IS NULL
            GROUP BY company_short, ptype
        """)
        rows = cursor.fetchall()
    finally:
        conn.close()

    agg = {}
    for r in rows:
        comp = r["company"]
        a = agg.get(comp)
        if a is None:
            a = {"company": comp, "count": 0, "month_count": 0,
                 "compulsory": 0, "commercial": 0, "accident": 0, "non_vehicle": 0,
                 "first_time": None, "last_time": None}
            agg[comp] = a
        cnt = int(r["cnt"] or 0)
        a["count"] += cnt
        a["month_count"] += int(r["month_cnt"] or 0)
        code, _ = get_policy_type_code(r["ptype"] or "")
        a[code] = a.get(code, 0) + cnt
        lt = r["last_time"]
        if lt and (a["last_time"] is None or lt > a["last_time"]):
            a["last_time"] = lt
        ft = r["first_time"]
        if ft and (a["first_time"] is None or ft < a["first_time"]):
            a["first_time"] = ft

    for a in agg.values():
        a["first_time"] = a["first_time"].strftime("%Y-%m-%d %H:%M") if a["first_time"] else ""
        a["last_time"] = a["last_time"].strftime("%Y-%m-%d %H:%M") if a["last_time"] else ""
    return list(agg.values())


def get_record_company_list(db, user_ids: list = None, enterprise_id: int = None) -> list:
    """
    获取全库（权限范围内）去重后的保险公司简称列表，供列表「保司」筛选下拉使用。
    与 get_company_stats 不同：不限制 status/doc_category/confidence，
    只要 company_short 非空即纳入，确保下拉覆盖列表中可能出现的所有保司（不受分页影响）。
    权限过滤逻辑与 query_insurance_records 保持一致。
    返回: ["人民财产", "国寿财产", ...]，按简称升序排列。
    """
    conditions = ["company_short != ''", "deleted_at IS NULL"]
    params = []
    # 按账号权限过滤
    if user_ids is not None:
        if user_ids:
            ph = ",".join(["%s"] * len(user_ids))
            conditions.append(f"user_id IN ({ph})")
            params.extend(user_ids)
        else:
            return []  # 空列表 = 无权限，不返回任何保司
    # 按企业过滤
    if enterprise_id is not None:
        conditions.append("enterprise_id = %s")
        params.append(enterprise_id)

    where = " AND ".join(conditions)
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            f"""
            SELECT DISTINCT company_short
            FROM insurance_records
            WHERE {where}
            ORDER BY company_short
            """,
            params,
        )
        return [r["company_short"] for r in cursor.fetchall()]
    finally:
        conn.close()


def get_enterprise_report_data(db, enterprise_id: int, date_start: str, date_end: str) -> dict:
    """企业报告数据聚合：按企业 + 日期范围（created_at，含端点）返回
    汇总 / 每日趋势 / 保司分布 / 险种分布 / 上传人排名。"""
    import datetime as _dt
    base = ("enterprise_id = %s AND deleted_at IS NULL "
            "AND created_at >= %s AND created_at < DATE_ADD(%s, INTERVAL 1 DAY)")
    base_r = base.replace("enterprise_id", "r.enterprise_id") \
                 .replace("deleted_at", "r.deleted_at") \
                 .replace("created_at", "r.created_at")
    bp = [enterprise_id, date_start, date_end]
    done = "status = 'done'"
    conn = db.pool.connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # 1. 汇总（口径与 Tab 一致：total = 成功 + 需补充 + 非保单 + 失败）
        cur.execute(f"""
            SELECT
              SUM({done} AND is_abnormal=0 AND doc_category='保单') AS success,
              SUM({done} AND is_abnormal=1) AS abnormal,
              SUM({done} AND doc_category<>'保单' AND doc_category<>'') AS nonpolicy,
              SUM(status='failed') AS failed
            FROM insurance_records WHERE {base}
        """, bp)
        row = cur.fetchone() or {}
        success = int(row.get("success") or 0)
        abnormal = int(row.get("abnormal") or 0)
        nonpolicy = int(row.get("nonpolicy") or 0)
        # 真失败：排除同文件已有成功记录的（重传/重识别成功的不算最终失败），与失败明细口径一致
        cur.execute(f"""
            SELECT COUNT(*) AS c FROM insurance_records r
            WHERE {base_r} AND r.status='failed'
              AND NOT EXISTS (
                SELECT 1 FROM insurance_records r2
                WHERE r2.enterprise_id = r.enterprise_id AND r2.deleted_at IS NULL AND r2.status='done'
                  AND (r.file_md5 IS NOT NULL AND r.file_md5 <> '' AND r2.file_md5 = r.file_md5))
        """, bp)
        failed = int((cur.fetchone() or {}).get("c") or 0)
        total = success + abnormal + nonpolicy + failed
        summary = {
            "total": total, "success": success, "abnormal": abnormal,
            "nonpolicy": nonpolicy, "failed": failed,
            # 成功率口径：仅"失败"算失败，需人工补充/非保单均算成功 → (总量-失败)/总量
            "success_rate": round((total - failed) / total * 100, 1) if total else 0.0,
        }

        # 2. 每日趋势（按天），并补全无数据的日期为 0
        _FE = ("(error_message LIKE '%%未能提取到任何文字%%' OR error_message LIKE '%%下载失败%%' "
               "OR error_message LIKE '%%内容为空%%' OR error_message LIKE '%%无法加载%%' "
               "OR error_message LIKE '%%无法打开%%' OR error_message LIKE '%%损坏%%' "
               "OR error_message LIKE '%%读取失败%%' OR error_message LIKE '%%解析失败%%' "
               "OR error_message LIKE '%%PDF%%')")
        cur.execute(f"""
            SELECT DATE(created_at) AS day, COUNT(*) AS total,
              SUM({done} AND is_abnormal=0 AND doc_category='保单') AS success,
              SUM(status='failed') AS failed,
              SUM({done} AND doc_category<>'保单' AND doc_category<>'') AS nonpolicy,
              SUM(status='failed' AND {_FE}) AS file_error
            FROM insurance_records
            WHERE {base} AND status NOT IN ('processing','pending','duplicate')
            GROUP BY DATE(created_at)
        """, bp)
        dmap = {}
        for r in cur.fetchall():
            d = r["day"]
            ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
            dmap[ds] = (int(r["total"] or 0), int(r["success"] or 0), int(r["failed"] or 0),
                        int(r["nonpolicy"] or 0), int(r["file_error"] or 0))
        daily = []
        d0 = _dt.datetime.strptime(date_start, "%Y-%m-%d").date()
        d1 = _dt.datetime.strptime(date_end, "%Y-%m-%d").date()
        cd = d0
        while cd <= d1:
            ds = cd.strftime("%Y-%m-%d")
            t, s, f, np_, fe = dmap.get(ds, (0, 0, 0, 0, 0))
            _sr = round((t - f) / t * 100, 1) if t else 0.0
            _ssr = round((t - max(f - fe, 0)) / t * 100, 1) if t else 0.0
            daily.append({"day": ds, "total": t, "success": s, "failed": f,
                          "nonpolicy": np_, "file_error": fe,
                          "success_rate": _sr, "sys_success_rate": _ssr})
            cd += _dt.timedelta(days=1)

        # 3. 保司分布（仅成功保单，含保费合计）
        cur.execute(f"""
            SELECT COALESCE(NULLIF(r.company_short,''),'未知') AS name, COUNT(*) AS cnt,
              ROUND(SUM(CAST(REPLACE(REPLACE(IFNULL(pf.total_premium,''),',',''),'元','') AS DECIMAL(14,2))),2) AS premium
            FROM insurance_records r
            LEFT JOIN insurance_policy_fields pf ON pf.record_id = r.id
            WHERE {base_r} AND r.status='done' AND r.is_abnormal=0 AND r.doc_category='保单'
            GROUP BY name ORDER BY cnt DESC
        """, bp)
        companies = [{"name": r["name"], "count": int(r["cnt"]),
                      "premium": float(r["premium"] or 0)} for r in cur.fetchall()]

        # 4. 险种分布（仅成功保单）
        cur.execute(f"""
            SELECT COALESCE(NULLIF(pf.policy_type,''),'未知') AS name, COUNT(*) AS cnt
            FROM insurance_records r JOIN insurance_policy_fields pf ON pf.record_id = r.id
            WHERE {base_r} AND r.status='done' AND r.is_abnormal=0 AND r.doc_category='保单'
            GROUP BY name ORDER BY cnt DESC
        """, bp)
        policy_types = [{"name": r["name"], "count": int(r["cnt"])} for r in cur.fetchall()]

        # 5. 上传人排名
        cur.execute(f"""
            SELECT sender, MAX(sender_name) AS sender_name, COUNT(*) AS total,
              SUM({done} AND is_abnormal=0 AND doc_category='保单') AS success
            FROM insurance_records
            WHERE {base} AND status NOT IN ('processing','pending','duplicate')
            GROUP BY sender ORDER BY total DESC
        """, bp)
        uploaders = []
        for r in cur.fetchall():
            uploaders.append({
                "sender_name": r["sender_name"] or r["sender"] or "未知",
                "total": int(r["total"] or 0),
                "success": int(r["success"] or 0),
            })

        # 6. 失败记录明细（报告页展示"哪几份识别失败了"）
        cur.execute(f"""
            SELECT r.id, r.filename, r.created_at, r.error_message, r.room_name, r.sender_name, r.cos_url
            FROM insurance_records r
            WHERE {base_r} AND r.status='failed'
              AND NOT EXISTS (
                SELECT 1 FROM insurance_records r2
                WHERE r2.enterprise_id = r.enterprise_id AND r2.deleted_at IS NULL
                  AND r2.status='done'
                  AND (r.file_md5 IS NOT NULL AND r.file_md5 <> '' AND r2.file_md5 = r.file_md5)
              )
            ORDER BY r.created_at DESC LIMIT 200
        """, bp)
        failed_records = []
        _FILE_ERR = _re.compile(r'未能提取到任何文字|下载失败|内容为空|无法加载|无法打开|损坏|读取失败|解析失败|PDF')
        _file_err_cnt = 0
        for r in cur.fetchall():
            ct = r["created_at"]
            _em = r["error_message"] or ""
            _is_file_err = bool(_FILE_ERR.search(_em))
            _hint = "文件异常，无法打开" if _is_file_err else "识别失败，可重新识别"
            if _is_file_err:
                _file_err_cnt += 1
            failed_records.append({
                "id": r["id"],
                "filename": r["filename"] or "",
                "created_at": ct.strftime("%Y-%m-%d %H:%M") if hasattr(ct, "strftime") else str(ct),
                "error": _hint,
                "room": r["room_name"] or "",
                "sender": r["sender_name"] or "",
                "cos_url": r["cos_url"] or "",
            })

        # 非系统问题成功率：文件本身损坏/无法打开(非系统识别问题)不计入失败
        _sys_failed = max(failed - _file_err_cnt, 0)
        summary["file_error"] = _file_err_cnt
        summary["system_failed"] = _sys_failed
        summary["sys_success_rate"] = round((total - _sys_failed) / total * 100, 1) if total else 100.0

        return {"summary": summary, "daily": daily, "companies": companies,
                "policy_types": policy_types, "uploaders": uploaders,
                "failed_records": failed_records}
    finally:
        conn.close()


def get_insurance_stats(db, filters: dict = None) -> dict:
    """
    获取保单识别统计信息，支持筛选条件。
    filters 支持: roomid, source_type, source, sender, keyword, company_short,
                   ocr_engine, date_start, date_end, updated_at_date_start, updated_at_date_end,
                   search_company, search_policy_no, search_plate_no,
                   search_applicant, search_insured, search_salesperson,
                   sign_date_start, sign_date_end, start_date_start, start_date_end,
                   end_date_start, end_date_end
    """
    filters = filters or {}
    dedup = filters.get("dedup", False)
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 是否需要 JOIN 关联表
        _pf_keys = [
            "search_company", "search_policy_no", "search_plate_no",
            "search_applicant", "search_insured", "search_salesperson",
            "sign_date_start", "sign_date_end",
            "start_date_start", "start_date_end",
            "end_date_start", "end_date_end",
        ]
        need_join = dedup or bool(filters.get("policy_type")) or any(filters.get(k) for k in _pf_keys)
        col_prefix = "r." if need_join else ""

        # 构建 WHERE 条件
        where_parts = []
        params = []
        # 排除已软删除的记录
        where_parts.append(f"{col_prefix}deleted_at IS NULL")
        # 统一排除：处理中/排队中（未出结果）和重复文件记录，
        # 使 total 与 done+failed（各分类之和）一致
        where_parts.append(f"{col_prefix}status NOT IN ('processing', 'pending', 'duplicate')")
        if filters.get("roomid"):
            where_parts.append(f"{col_prefix}roomid = %s")
            params.append(filters["roomid"])
        if filters.get("source_type") == "user":
            where_parts.append(f"({col_prefix}roomid IS NULL OR {col_prefix}roomid = '')")
        elif filters.get("source_type") == "room":
            where_parts.append(f"{col_prefix}roomid IS NOT NULL AND {col_prefix}roomid != ''")
        if filters.get("source"):
            where_parts.append(f"{col_prefix}source = %s")
            params.append(filters["source"])
        if filters.get("sender"):
            where_parts.append(f"{col_prefix}sender = %s")
            params.append(filters["sender"])
        if filters.get("keyword"):
            kw = f"%{filters['keyword']}%"
            where_parts.append(f"({col_prefix}company_short LIKE %s OR {col_prefix}filename LIKE %s OR {col_prefix}sender_name LIKE %s)")
            params.extend([kw, kw, kw])
        if filters.get("company_short"):
            where_parts.append(f"{col_prefix}company_short = %s")
            params.append(filters["company_short"])
        # 险种大类筛选（与 query_insurance_records 逻辑一致）
        if filters.get("policy_type"):
            _pt_values = _policy_types_for_category(db, filters["policy_type"])
            if _pt_values:
                _ph = ",".join(["%s"] * len(_pt_values))
                where_parts.append(f"pf.policy_type IN ({_ph})")
                params.extend(_pt_values)
            else:
                where_parts.append("0")
        if filters.get("ocr_engine"):
            where_parts.append(f"{col_prefix}ocr_engine = %s")
            params.append(filters["ocr_engine"])
        if filters.get("date_start"):
            where_parts.append(f"{col_prefix}created_at >= %s")
            params.append(filters["date_start"])
        if filters.get("date_end"):
            where_parts.append(f"{col_prefix}created_at < %s")
            params.append(filters["date_end"])
        if filters.get("updated_at_date_start"):
            where_parts.append(f"{col_prefix}updated_at >= %s")
            params.append(filters["updated_at_date_start"])
        if filters.get("updated_at_date_end"):
            where_parts.append(f"{col_prefix}updated_at < %s")
            params.append(filters["updated_at_date_end"])
        # 按账号权限过滤
        if filters.get("user_ids") is not None:
            uid_list = filters["user_ids"]
            if uid_list:
                placeholders = ",".join(["%s"] * len(uid_list))
                where_parts.append(f"{col_prefix}user_id IN ({placeholders})")
                params.extend(uid_list)
            else:
                where_parts.append("0")
        # 按企业过滤
        if filters.get("enterprise_id") is not None:
            where_parts.append(f"{col_prefix}enterprise_id = %s")
            params.append(filters["enterprise_id"])
        # 关联表模糊搜索和日期筛选
        if need_join:
            if filters.get("search_company"):
                where_parts.append("pf.company LIKE %s")
                params.append(f"%{filters['search_company']}%")
            if filters.get("search_policy_no"):
                where_parts.append("pf.policy_no LIKE %s")
                params.append(f"%{filters['search_policy_no']}%")
            if filters.get("search_plate_no"):
                where_parts.append("pf.plate_no LIKE %s")
                params.append(f"%{filters['search_plate_no']}%")
            if filters.get("search_applicant"):
                where_parts.append("pf.applicant LIKE %s")
                params.append(f"%{filters['search_applicant']}%")
            if filters.get("search_insured"):
                where_parts.append("pf.insured LIKE %s")
                params.append(f"%{filters['search_insured']}%")
            if filters.get("search_salesperson"):
                where_parts.append("pf.salesperson LIKE %s")
                params.append(f"%{filters['search_salesperson']}%")
            for iso_col, key_start, key_end in [
                ("pf.sign_date_iso", "sign_date_start", "sign_date_end"),
                ("pf.start_date_iso", "start_date_start", "start_date_end"),
                ("pf.end_date_iso", "end_date_start", "end_date_end"),
            ]:
                if filters.get(key_start):
                    where_parts.append(f"{iso_col} >= %s")
                    params.append(_date_to_iso(filters[key_start]))
                if filters.get(key_end):
                    where_parts.append(f"{iso_col} <= %s")
                    params.append(_date_to_iso(filters[key_end]))

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        if need_join:
            from_sql = "insurance_records r LEFT JOIN insurance_policy_fields pf ON r.id = pf.record_id"
        else:
            from_sql = "insurance_records"

        # 复用与 query_insurance_records 完全一致的计数方式
        def _count(extra_where="", extra_params=None):
            """计算符合条件的记录数，去重模式与列表查询逻辑完全一致"""
            ep = list(extra_params or [])
            w = where_sql
            if extra_where:
                w = f"{w} AND {extra_where}" if w else f" WHERE {extra_where}"
            all_params = params + ep
            if dedup:
                # 仅「保单」参与按保单号去重；非保单(电子标志等)一律保留（与列表口径一致）
                dedup_cond = f"pf.policy_no IS NOT NULL AND pf.policy_no != '' AND {col_prefix}doc_category = '保单'"
                fw = f"{w} AND {dedup_cond}" if w else f" WHERE {dedup_cond}"
                _keep_cond = f"(pf.policy_no IS NULL OR pf.policy_no = '' OR {col_prefix}doc_category IS NULL OR {col_prefix}doc_category != '保单')"
                ew = f"{w} AND {_keep_cond}" if w else f" WHERE {_keep_cond}"
                cursor.execute(
                    f"SELECT COUNT(*) as cnt FROM ("
                    f"  SELECT MAX({col_prefix}id) FROM {from_sql}{fw} GROUP BY pf.policy_no, DATE({col_prefix}created_at)"
                    f") t", all_params)
                c1 = cursor.fetchone()["cnt"]
                cursor.execute(f"SELECT COUNT(*) as cnt FROM {from_sql}{ew}", all_params)
                c2 = cursor.fetchone()["cnt"]
                return c1 + c2
            else:
                cursor.execute(f"SELECT COUNT(*) as cnt FROM {from_sql}{w}", all_params)
                return cursor.fetchone()["cnt"]

        done_where = f"({col_prefix}status = 'done' OR {col_prefix}status = 'success')"
        if dedup:
            # 去重模式：各分项独立统计（每个 Tab 在自己子集内去重）
            success_cnt = _count(f"{done_where} AND {col_prefix}is_abnormal = 0 AND {col_prefix}doc_category = '保单'")
            abnormal_cnt = _count(f"{done_where} AND {col_prefix}is_abnormal = 1")
            nonpolicy_cnt = _count(f"{done_where} AND {col_prefix}doc_category != '保单' AND {col_prefix}doc_category != ''")
            failed_cnt = _count(f"{col_prefix}status = 'failed'")
            total = _count("")
        else:
            # 非去重：合并为 1 次条件聚合查询（原先 5 次 COUNT → 1 次）
            cursor.execute(
                f"SELECT COUNT(*) as total,"
                f" SUM(CASE WHEN {done_where} AND {col_prefix}is_abnormal = 0"
                f"   AND {col_prefix}doc_category = '保单' THEN 1 ELSE 0 END) as success_cnt,"
                f" SUM(CASE WHEN {done_where} AND {col_prefix}is_abnormal = 1"
                f"   THEN 1 ELSE 0 END) as abnormal_cnt,"
                f" SUM(CASE WHEN {done_where} AND {col_prefix}doc_category != '保单'"
                f"   AND {col_prefix}doc_category != '' THEN 1 ELSE 0 END) as nonpolicy_cnt,"
                f" SUM(CASE WHEN {col_prefix}status = 'failed'"
                f"   THEN 1 ELSE 0 END) as failed_cnt"
                f" FROM {from_sql}{where_sql}",
                params
            )
            row = cursor.fetchone()
            total = row["total"] or 0
            success_cnt = int(row["success_cnt"] or 0)
            abnormal_cnt = int(row["abnormal_cnt"] or 0)
            nonpolicy_cnt = int(row["nonpolicy_cnt"] or 0)
            failed_cnt = int(row["failed_cnt"] or 0)
        done_cnt = success_cnt + abnormal_cnt + nonpolicy_cnt
        summary = {
            "total": total, "done_cnt": done_cnt, "failed_cnt": failed_cnt,
            "pending_cnt": 0, "processing_cnt": 0,
            "abnormal_cnt": abnormal_cnt, "nonpolicy_cnt": nonpolicy_cnt,
        }

        status_stats = []
        for s, c in [("done", done_cnt), ("failed", failed_cnt)]:
            if c:
                status_stats.append({"status": s, "cnt": int(c)})

        # 按识别方式统计
        cursor.execute(
            f"SELECT {col_prefix}ocr_engine AS ocr_engine, COUNT(*) as cnt "
            f"FROM {from_sql}{where_sql} GROUP BY {col_prefix}ocr_engine ORDER BY cnt DESC",
            params
        )
        engine_stats = list(cursor.fetchall())

        # 按群统计（始终返回，按账号/企业权限过滤）
        room_where_parts = [
            "deleted_at IS NULL",
            "status NOT IN ('processing', 'pending', 'duplicate')",
        ]
        room_params = []
        if filters.get("user_ids") is not None:
            uid_list = filters["user_ids"]
            if uid_list:
                ph = ",".join(["%s"] * len(uid_list))
                room_where_parts.append(f"user_id IN ({ph})")
                room_params.extend(uid_list)
            else:
                room_where_parts.append("0")
        if filters.get("enterprise_id") is not None:
            room_where_parts.append("enterprise_id = %s")
            room_params.append(filters["enterprise_id"])
        room_where_sql = (" WHERE " + " AND ".join(room_where_parts)) if room_where_parts else ""
        cursor.execute(
            f"SELECT roomid, MAX(room_name) as room_name, COUNT(*) as cnt "
            f"FROM insurance_records{room_where_sql} "
            f"GROUP BY roomid ORDER BY cnt DESC LIMIT 50",
            room_params
        )
        room_stats = list(cursor.fetchall())

        return {
            "total": total,
            "status_stats": status_stats,
            "room_stats": room_stats,
            "engine_stats": engine_stats,
            "abnormal_cnt": int(summary["abnormal_cnt"] or 0),
            "nonpolicy_cnt": int(summary["nonpolicy_cnt"] or 0),
        }
    finally:
        conn.close()

