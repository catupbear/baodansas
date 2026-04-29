"""
备份来源: insurance/handler.py
备份日期: 2026-04-29
备份原因: 旧版钉钉同步方法，已被 _sync_to_dingtalk_v2() 和 _get_matched_monitors() 替代
恢复方式: 见同目录 README.md
"""


def _get_bound_targets(self, roomid: str, sender: str) -> list:
    """
    根据 roomid / sender 查找绑定的钉钉目标 ID 列表。
    watch_list 中每个监控项有 target_ids 字段。
    """
    watch_list = get_insurance_config(self.db, "watch_list", [])
    if not isinstance(watch_list, list):
        return []

    target_ids = set()
    for w in watch_list:
        if not isinstance(w, dict):
            continue
        wid = w.get("id", "")
        wtype = w.get("type", "")
        # 群聊匹配 roomid，私聊匹配 sender
        matched = False
        if wtype == "room" and roomid and wid == roomid:
            matched = True
        elif wtype == "user" and not roomid and sender and wid == sender:
            matched = True
        if matched:
            for tid in w.get("target_ids", []):
                target_ids.add(tid)

    return list(target_ids)

def _sync_to_dingtalk(
    self, record_id: int, mapped_fields: dict, parsed_fields: dict,
    sender_name: str = "", roomid: str = "", sender: str = "",
    doc_category: str = "", target_ids: list = None,
):
    """
    将保单字段同步到钉钉多维表。

    根据 roomid/sender 查找绑定的目标文档，只同步到绑定的目标。
    同步时自动带上「发送人」字段。

    Args:
        record_id:     数据库记录 ID
        mapped_fields: apply_mapping 后的导出列字段
        parsed_fields: OCR 原始解析字段
        sender_name:   发送人姓名
        roomid:        来源群 ID
        sender:        发送人 ID
        target_ids:    指定目标 ID 列表（为 None 时根据绑定关系自动查找）
    """
    from insurance.dingtalk_sync import DingTalkTableService

    try:
        # 读取应用凭证（优先 config.yaml，其次数据库）
        app_cfg = self.app_config.get("dingtalk", {})
        if not app_cfg or not app_cfg.get("app_key"):
            app_cfg = get_insurance_config(self.db, "dingtalk_app", {})
        if not isinstance(app_cfg, dict):
            logger.warning("钉钉应用配置格式不正确")
            return

        app_key = app_cfg.get("app_key", "")
        app_secret = app_cfg.get("app_secret", "")
        operator_id = app_cfg.get("operator_id", "")

        if not (app_key and app_secret and operator_id):
            logger.warning("钉钉应用凭证未完整配置（app_key/app_secret/operator_id）")
            return

        # 确定要同步的目标
        if target_ids is None:
            target_ids = self._get_bound_targets(roomid, sender)

        if not target_ids:
            logger.debug("record_id=%d 无绑定的钉钉目标，跳过同步", record_id)
            return

        # 读取所有目标配置
        all_targets = get_insurance_config(self.db, "dingtalk_targets", [])
        if not isinstance(all_targets, list):
            return
        target_map = {t["id"]: t for t in all_targets if isinstance(t, dict) and t.get("id")}

        # 构建同步数据（带上发送人、文档类型字段）
        invoice_fields = {k: v for k, v in mapped_fields.items() if v}
        if sender_name:
            invoice_fields["发送人"] = sender_name
        if doc_category:
            invoice_fields["文档类型"] = doc_category
        field_names = list(invoice_fields.keys())
        invoice = {"fields": invoice_fields}

        all_success = True
        synced_target_ids = []

        for tid in target_ids:
            target = target_map.get(tid)
            if not target:
                logger.warning("钉钉目标 %s 不存在，跳过", tid)
                continue

            base_id = target.get("base_id", "")
            sheet_id = target.get("sheet_id", "")
            if not (base_id and sheet_id):
                continue

            try:
                svc = DingTalkTableService(
                    app_key, app_secret, base_id, sheet_id, operator_id
                )
                result = svc.append_invoices(field_names, [invoice])
                synced_target_ids.append(tid)
                logger.info(
                    "同步钉钉成功, record_id=%d target=%s inserted=%s",
                    record_id, target.get("name", tid),
                    result.get("inserted_count", 0),
                )
            except Exception as e:
                all_success = False
                logger.error(
                    "同步钉钉失败, record_id=%d target=%s: %s",
                    record_id, target.get("name", tid), e, exc_info=True,
                )

        if synced_target_ids:
            update_insurance_record(self.db, record_id, {
                "dingtalk_synced": 1 if all_success else 0,
                "dingtalk_target_id": ",".join(synced_target_ids),
            })

    except Exception as e:
        logger.error("_sync_to_dingtalk 异常, record_id=%d: %s", record_id, e, exc_info=True)
