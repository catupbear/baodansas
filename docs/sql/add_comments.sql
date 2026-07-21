-- ============================================================
-- 数据库表和字段注释（pdfocr 库）
-- 生成日期：2026-06-02
-- 说明：为所有表和字段添加中文注释
-- ============================================================

-- ------------------------------------------------------------
-- 1. contacts_cache — 通讯录缓存表
-- ------------------------------------------------------------
ALTER TABLE contacts_cache COMMENT = '通讯录缓存表（企业微信联系人/群聊名称缓存，24h TTL）';

ALTER TABLE contacts_cache
  MODIFY COLUMN id         VARCHAR(128) NOT NULL COMMENT '联系人/群聊ID（企业微信userid或roomid）',
  MODIFY COLUMN type       VARCHAR(32)  DEFAULT NULL COMMENT '类型：user=内部员工, external=外部联系人, room=群聊',
  MODIFY COLUMN name       VARCHAR(128) DEFAULT NULL COMMENT '显示名称',
  MODIFY COLUMN avatar     VARCHAR(512) DEFAULT ''   COMMENT '头像URL',
  MODIFY COLUMN extra      TEXT                      COMMENT '附加信息（JSON，如错误码等）',
  MODIFY COLUMN updated_at INT          DEFAULT NULL COMMENT '最后更新时间戳（Unix秒）';

-- ------------------------------------------------------------
-- 2. cursor_state — 消息拉取游标表
-- ------------------------------------------------------------
ALTER TABLE cursor_state COMMENT = '消息拉取游标表（记录增量拉取的seq位置，单行表id=1）';

ALTER TABLE cursor_state
  MODIFY COLUMN id         INT NOT NULL COMMENT '固定值1',
  MODIFY COLUMN seq        INT DEFAULT 0 COMMENT '当前已拉取的最大消息序号',
  MODIFY COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间';

-- ------------------------------------------------------------
-- 3. messages — 消息存储表
-- ------------------------------------------------------------
ALTER TABLE messages COMMENT = '企业微信会话存档消息表（解密后的消息记录）';

ALTER TABLE messages
  MODIFY COLUMN id             INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  MODIFY COLUMN seq            INT DEFAULT NULL COMMENT '消息序号（SDK返回的递增序列号）',
  MODIFY COLUMN corpid         VARCHAR(64) DEFAULT '' COMMENT '企业ID',
  MODIFY COLUMN msgid          VARCHAR(128) DEFAULT NULL COMMENT '消息ID（企业微信唯一标识）',
  MODIFY COLUMN action         VARCHAR(64) DEFAULT NULL COMMENT '消息动作：send=发送, recall=撤回, switch=切换企业',
  MODIFY COLUMN sender         VARCHAR(128) DEFAULT NULL COMMENT '发送人ID（企业微信userid）',
  MODIFY COLUMN tolist         TEXT COMMENT '接收人列表（JSON数组）',
  MODIFY COLUMN roomid         VARCHAR(128) DEFAULT NULL COMMENT '群聊ID（私聊为空）',
  MODIFY COLUMN msgtime        BIGINT DEFAULT NULL COMMENT '消息时间戳（毫秒）',
  MODIFY COLUMN msgtype        VARCHAR(64) DEFAULT NULL COMMENT '消息类型：text/image/voice/video/file/link等',
  MODIFY COLUMN summary        TEXT COMMENT '消息摘要（纯文本预览）',
  MODIFY COLUMN raw_data       LONGTEXT COMMENT '原始消息JSON（SDK解密后的完整数据）',
  MODIFY COLUMN parsed_content LONGTEXT COMMENT '解析后的结构化内容（JSON）',
  MODIFY COLUMN created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间';

-- ------------------------------------------------------------
-- 4. enterprises — 企业信息表
-- ------------------------------------------------------------
ALTER TABLE enterprises COMMENT = '企业信息表（多租户企业账号管理）';
-- 字段已有COMMENT，无需修改

-- ------------------------------------------------------------
-- 5. users — 用户账号表
-- ------------------------------------------------------------
ALTER TABLE users COMMENT = '用户账号表（企业管理员和员工登录账号）';
-- 字段已有COMMENT，无需修改

-- ------------------------------------------------------------
-- 6. user_sender_binding — 用户-发送人绑定表
-- ------------------------------------------------------------
ALTER TABLE user_sender_binding COMMENT = '用户-发送人绑定表（关联系统用户与企业微信发送人ID）';
-- 字段已有COMMENT，无需修改

-- ------------------------------------------------------------
-- 7. insurance_config — 保单识别配置表
-- ------------------------------------------------------------
ALTER TABLE insurance_config COMMENT = '保单识别配置表（前端可管理的系统配置，如监控群、OCR参数等）';

ALTER TABLE insurance_config
  MODIFY COLUMN id           INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  MODIFY COLUMN config_key   VARCHAR(128) DEFAULT NULL COMMENT '配置项标识（如：monitor_rooms, baidu_ocr, dingtalk_app）',
  MODIFY COLUMN config_value LONGTEXT COMMENT '配置值（JSON格式）',
  MODIFY COLUMN updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间';

-- ------------------------------------------------------------
-- 8. insurance_records — 保单识别记录表
-- ------------------------------------------------------------
ALTER TABLE insurance_records COMMENT = '保单识别记录表（每条记录对应一个PDF的OCR识别结果）';

ALTER TABLE insurance_records
  MODIFY COLUMN id                 INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  MODIFY COLUMN msg_seq            INT DEFAULT NULL COMMENT '关联消息的seq序号',
  MODIFY COLUMN roomid             VARCHAR(64) DEFAULT NULL COMMENT '来源群聊ID',
  MODIFY COLUMN room_name          VARCHAR(128) DEFAULT NULL COMMENT '来源群聊名称',
  MODIFY COLUMN sender             VARCHAR(64) DEFAULT NULL COMMENT '发送人ID',
  MODIFY COLUMN sender_name        VARCHAR(128) DEFAULT NULL COMMENT '发送人名称',
  MODIFY COLUMN filename           VARCHAR(256) DEFAULT NULL COMMENT 'PDF文件名',
  MODIFY COLUMN filesize           INT DEFAULT 0 COMMENT '文件大小（字节）',
  MODIFY COLUMN cos_url            TEXT COMMENT '腾讯云COS存储URL',
  MODIFY COLUMN ocr_engine         VARCHAR(32) DEFAULT NULL COMMENT 'OCR引擎：pdfplumber/baidu_ocr',
  MODIFY COLUMN doc_category       VARCHAR(64) DEFAULT NULL COMMENT '文档类别（如：交强险、商业险）',
  MODIFY COLUMN confidence         FLOAT DEFAULT 0 COMMENT '识别置信度（0-1）',
  MODIFY COLUMN parsed_fields      LONGTEXT COMMENT '解析后的字段（JSON）',
  MODIFY COLUMN mapped_fields      LONGTEXT COMMENT '映射后的导出字段（JSON）',
  MODIFY COLUMN manual_fields      LONGTEXT COMMENT '手动编辑的字段（JSON，覆盖自动识别结果）',
  MODIFY COLUMN display_fields     TEXT COMMENT '前端展示字段（JSON，合并自动+手动）',
  MODIFY COLUMN dingtalk_synced    TINYINT DEFAULT 0 COMMENT '是否已同步钉钉：0=未同步, 1=已同步',
  MODIFY COLUMN dingtalk_target_id VARCHAR(64) DEFAULT NULL COMMENT '钉钉多维表目标ID',
  MODIFY COLUMN dingtalk_record_id VARCHAR(128) DEFAULT NULL COMMENT '钉钉多维表记录ID',
  MODIFY COLUMN status             VARCHAR(32) DEFAULT 'pending' COMMENT '状态：pending/processing/done/failed',
  MODIFY COLUMN error_message      TEXT COMMENT '错误信息（失败时记录）',
  MODIFY COLUMN source             VARCHAR(32) DEFAULT 'auto' COMMENT '来源：auto=自动识别, manual=手动上传',
  MODIFY COLUMN raw_text           LONGTEXT COMMENT 'OCR原始文本（pdfplumber提取）',
  MODIFY COLUMN ocr_text           LONGTEXT COMMENT 'OCR识别文本（百度OCR提取）',
  MODIFY COLUMN company_short      VARCHAR(64) DEFAULT '' COMMENT '保险公司简称',
  MODIFY COLUMN is_abnormal        TINYINT DEFAULT 0 COMMENT '是否异常：0=正常, 1=异常',
  MODIFY COLUMN abnormal_override_reason VARCHAR(128) DEFAULT NULL COMMENT '人工标记正常/异常的原因',
  MODIFY COLUMN hint               VARCHAR(64) DEFAULT '' COMMENT '提示信息（如：疑似重复）',
  MODIFY COLUMN file_md5           VARCHAR(32) DEFAULT NULL COMMENT '文件MD5（用于去重）',
  MODIFY COLUMN user_id            INT DEFAULT NULL COMMENT '所属用户ID → users.id',
  MODIFY COLUMN enterprise_id      INT DEFAULT NULL COMMENT '所属企业ID → enterprises.id',
  MODIFY COLUMN policy_count       TINYINT DEFAULT 1 COMMENT '同一PDF中的保单总数',
  MODIFY COLUMN policy_index       TINYINT DEFAULT 1 COMMENT '当前保单在PDF中的序号（从1开始）',
  MODIFY COLUMN page_range         VARCHAR(32) DEFAULT '' COMMENT '保单对应的PDF页码范围（如"1-3"）',
  MODIFY COLUMN created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间';

-- ------------------------------------------------------------
-- 9. insurance_policy_fields — 保单字段平铺表
-- ------------------------------------------------------------
ALTER TABLE insurance_policy_fields COMMENT = '保单字段平铺表（将识别结果按字段拆分存储，便于索引查询）';
-- 字段已有COMMENT，无需修改

-- ------------------------------------------------------------
-- 10. monitor_configs — 监控配置表
-- ------------------------------------------------------------
ALTER TABLE monitor_configs COMMENT = '监控配置表（保单自动识别的群聊/私聊监控规则及钉钉同步目标）';

ALTER TABLE monitor_configs
  MODIFY COLUMN id               INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  MODIFY COLUMN name             VARCHAR(128) NOT NULL DEFAULT '' COMMENT '配置名称',
  MODIFY COLUMN enabled          TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用：0=禁用, 1=启用',
  MODIFY COLUMN rooms            LONGTEXT COMMENT '监控群聊列表（JSON数组：[{"id":"wr...","name":"群名"}]）',
  MODIFY COLUMN users            LONGTEXT COMMENT '监控私聊列表（JSON数组：[{"id":"wm...","name":"人名"}]）',
  MODIFY COLUMN dingtalk_doc_url VARCHAR(512) DEFAULT '' COMMENT '钉钉多维表文档URL',
  MODIFY COLUMN dingtalk_base_id VARCHAR(128) DEFAULT '' COMMENT '钉钉多维表base_id',
  MODIFY COLUMN dingtalk_sheet_id VARCHAR(128) DEFAULT '' COMMENT '钉钉多维表sheet_id',
  MODIFY COLUMN field_mapping    LONGTEXT COMMENT '字段映射配置（JSON：{"OCR字段名":"钉钉列名"}）',
  MODIFY COLUMN user_id          INT DEFAULT NULL COMMENT '绑定用户ID → users.id',
  MODIFY COLUMN enterprise_id    INT DEFAULT NULL COMMENT '绑定企业ID → enterprises.id',
  MODIFY COLUMN created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间';

-- ------------------------------------------------------------
-- 11. user_field_config — 用户字段配置表
-- ------------------------------------------------------------
-- 表和字段已有COMMENT，无需修改

-- ------------------------------------------------------------
-- 12. user_active_template — 用户启用模板表
-- ------------------------------------------------------------
-- 表和字段已有COMMENT，无需修改

-- ------------------------------------------------------------
-- 13. template_alert_records — 模板告警记录表
-- ------------------------------------------------------------
ALTER TABLE template_alert_records COMMENT = '模板告警记录表（保单识别模板异常告警记录）';

-- ------------------------------------------------------------
-- 14. template_train_records — 模板训练记录表
-- ------------------------------------------------------------
ALTER TABLE template_train_records COMMENT = '模板训练记录表（保单识别模板训练/学习记录）';

ALTER TABLE template_train_records
  MODIFY COLUMN id                INT NOT NULL AUTO_INCREMENT COMMENT '主键',
  MODIFY COLUMN company_id        VARCHAR(64) DEFAULT NULL COMMENT '保险公司标识',
  MODIFY COLUMN policy_type       VARCHAR(128) DEFAULT NULL COMMENT '险种类型',
  MODIFY COLUMN version           VARCHAR(32) DEFAULT NULL COMMENT '模板版本号',
  MODIFY COLUMN sample_count      INT DEFAULT NULL COMMENT '训练样本数量',
  MODIFY COLUMN discovered_fields TEXT COMMENT '发现的字段列表（JSON）',
  MODIFY COLUMN overall_hit_rate  FLOAT DEFAULT NULL COMMENT '整体命中率',
  MODIFY COLUMN passed            TINYINT DEFAULT NULL COMMENT '是否通过验证：0=未通过, 1=通过',
  MODIFY COLUMN unstable_fields   TEXT COMMENT '不稳定字段列表（JSON）',
  MODIFY COLUMN registered        TINYINT DEFAULT NULL COMMENT '是否已注册为正式模板：0=否, 1=是',
  MODIFY COLUMN trigger_source    VARCHAR(64) DEFAULT NULL COMMENT '触发来源（如：auto/manual）',
  MODIFY COLUMN created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ------------------------------------------------------------
-- 15. user_sender_bindng — 历史遗留表（表名拼写错误）
-- ------------------------------------------------------------
-- 注：截图中存在 user_sender_bindng（少了字母i），可能是早期创建的表
-- 建议确认是否仍在使用，如已废弃可执行：DROP TABLE IF EXISTS user_sender_bindng;
