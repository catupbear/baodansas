"""
消息拉取模块
收到回调后，基于 seq 游标分页拉取聊天记录
"""

import json
import logging
import threading
import time

from core.decryptor import MessageDecryptor
from core.notify import notify_error
from core.parser import MessageParser
from sdk.finance_sdk import FinanceSDK
from storage.db import Database

logger = logging.getLogger(__name__)


class MessageFetcher:
    """消息拉取器"""

    def __init__(self, finance_sdk: FinanceSDK, decryptor: MessageDecryptor,
                 parser: MessageParser, db: Database,
                 limit: int = 1000, proxy: str = "", passwd: str = "", timeout: int = 10,
                 cursor_id: int = 1, enterprise_name: str = "", corpid: str = ""):
        self.finance_sdk = finance_sdk
        self.decryptor = decryptor
        self.parser = parser
        self.db = db
        self.limit = limit
        self.proxy = proxy
        self.passwd = passwd
        self.timeout = timeout
        self.cursor_id = cursor_id
        self.enterprise_name = enterprise_name
        self.corpid = corpid
        self._lock = threading.Lock()
        self._log_prefix = f"[{enterprise_name}] " if enterprise_name else ""

    def fetch_new_messages(self):
        """
        拉取新消息（回调触发时调用）
        从上次的 seq 开始，循环拉取直到没有新数据
        """
        with self._lock:
            self._do_fetch()

    def _do_fetch(self):
        """执行拉取逻辑"""
        seq = self.db.get_seq(self.cursor_id)
        total_count = 0

        while True:
            logger.info("%s拉取消息, seq=%d, limit=%d", self._log_prefix, seq, self.limit)

            # SDK 拉取，偶发网络错误（如 10001）退避重试，仍失败才告警
            max_attempts = 3
            ret, data = 1, {}
            for attempt in range(1, max_attempts + 1):
                ret, data = self.finance_sdk.get_chat_data(
                    seq, self.limit, self.proxy, self.passwd, self.timeout
                )
                if ret == 0:
                    break
                logger.warning("%s拉取消息失败, 错误码: %d (第 %d/%d 次)",
                               self._log_prefix, ret, attempt, max_attempts)
                if attempt < max_attempts:
                    time.sleep(attempt)  # 1s, 2s 递增退避
            if ret != 0:
                logger.error("%s拉取消息失败, 错误码: %d (已重试 %d 次)",
                             self._log_prefix, ret, max_attempts)
                notify_error("消息拉取", "_do_fetch", f"SDK拉取消息失败, 错误码: {ret} (已重试{max_attempts}次)", f"企业: {self.enterprise_name}, seq={seq}")
                break

            chat_list = data.get("chatdata", [])
            if not chat_list:
                logger.info("%s没有新消息", self._log_prefix)
                break

            for item in chat_list:
                self._process_item(item)
                # 更新 seq 为当前消息的 seq
                seq = item.get("seq", seq)

            total_count += len(chat_list)

            # 更新游标
            self.db.update_seq(seq, self.cursor_id)

            # 如果拉取数量小于 limit，说明没有更多数据
            if len(chat_list) < self.limit:
                break

        logger.info("%s本次共拉取 %d 条消息", self._log_prefix, total_count)

    def rescan_missed_insurance(self, lookback_days: int = 5,
                                config_ids: list = None,
                                room_ids: list = None,
                                force: bool = False):
        """
        补扫历史消息中漏掉的保单 PDF。
        查找最近 lookback_days 天内，在监控群/用户中的 PDF 文件消息，
        且未在 insurance_records 表中生成记录的，重新入队识别。

        参数:
            lookback_days: 回溯天数
            config_ids: 指定监控配置 ID 列表，为空则使用全部启用配置
            room_ids: 指定群 ID 列表（优先级高于 config_ids）
            force: 是否强制重新识别已有记录的 PDF
        返回:
            int: 入队数量
        """
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            logger.info("补扫跳过：insurance_handler 未绑定")
            return 0

        # 确定监控范围
        if room_ids:
            # 直接指定群 ID
            watch_rooms = room_ids
            watch_users = []
        elif config_ids:
            # 指定配置 ID，从中提取 rooms/users
            from insurance.monitor_config_db import get_enabled_monitor_configs
            configs = get_enabled_monitor_configs(self.db)
            watch_rooms = []
            watch_users = []
            for cfg in configs:
                if cfg.get("id") not in config_ids:
                    continue
                for r in (cfg.get("rooms") or []):
                    rid = r["id"] if isinstance(r, dict) else r
                    if rid:
                        watch_rooms.append(rid)
                for u in (cfg.get("users") or []):
                    uid = u["id"] if isinstance(u, dict) else u
                    if uid:
                        watch_users.append(uid)
        else:
            # 使用全部启用配置
            watch = handler.get_watch_config()
            watch_rooms = watch.get("rooms", [])
            watch_users = watch.get("users", [])

        if not watch_rooms and not watch_users:
            logger.info("补扫跳过：没有配置监控群/用户")
            return 0

        import time
        cutoff_ms = int((time.time() - lookback_days * 86400) * 1000)

        conn = self.db.pool.connection()
        try:
            import pymysql.cursors
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # 查找监控范围内的 PDF 文件消息
            conditions = []
            params = [cutoff_ms]

            if watch_rooms:
                placeholders = ",".join(["%s"] * len(watch_rooms))
                conditions.append(f"m.roomid IN ({placeholders})")
                params.extend(watch_rooms)

            if watch_users:
                placeholders = ",".join(["%s"] * len(watch_users))
                conditions.append(f"(m.roomid = '' AND m.sender IN ({placeholders}))")
                params.extend(watch_users)

            where_source = " OR ".join(conditions)

            # 按 corpid 过滤，每个企业只扫自己的消息
            corpid_filter = ""
            if self.corpid:
                corpid_filter = " AND m.corpid = %s"
                params.append(self.corpid)

            if force:
                # 强制模式：不排除已有记录
                sql = f"""
                    SELECT m.seq, m.roomid, m.sender, m.parsed_content
                    FROM messages m
                    WHERE m.msgtime >= %s
                      AND m.msgtype = 'file'
                      AND ({where_source})
                      {corpid_filter}
                    ORDER BY m.seq ASC
                """
            else:
                # 默认模式：排除已成功/重复的记录，保留失败的可重试
                sql = f"""
                    SELECT m.seq, m.roomid, m.sender, m.parsed_content
                    FROM messages m
                    LEFT JOIN insurance_records ir ON ir.msg_seq = m.seq
                    WHERE m.msgtime >= %s
                      AND m.msgtype = 'file'
                      AND ({where_source})
                      AND ir.id IS NULL
                      {corpid_filter}
                    ORDER BY m.seq ASC
                """
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            logger.info("补扫完成：没有遗漏的保单 PDF")
            return 0

        import json
        enqueued = 0
        for row in rows:
            try:
                content = json.loads(row.get("parsed_content", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue

            filename = content.get("filename", "")
            if not filename.lower().endswith(".pdf"):
                continue

            logger.info("补扫入队: seq=%d, room=%s, file=%s",
                        row["seq"], row["roomid"], filename)
            handler.enqueue({
                "seq": row["seq"],
                "roomid": row["roomid"],
                "sender": row["sender"],
                "filename": filename,
                "sdkfileid": content.get("sdkfileid", ""),
                "filesize": content.get("filesize", 0),
                "finance_sdk": self.finance_sdk,
                "sdk_config": {
                    "proxy": self.proxy,
                    "proxy_passwd": self.passwd,
                    "timeout": self.timeout,
                },
            })
            enqueued += 1

        logger.info("补扫完成：共 %d 条遗漏 PDF 已入队", enqueued)
        return enqueued

    def rescan_quote_history(self, lookback_days: int = 5, room_ids: list = None):
        """
        补扫历史消息中的报价相关消息（图片/文本）。
        查找最近 lookback_days 天内，符合报价绑定规则的群消息，重新入队。

        参数:
            lookback_days: 回溯天数
            room_ids: 可选，指定群 ID 列表，为空则使用全部绑定的群
        返回:
            int: 入队数量
        """
        handler = getattr(self, "quote_handler", None)
        if not handler:
            logger.info("报价补扫跳过：quote_handler 未绑定")
            return 0

        # 获取所有绑定关系，提取关联的群 ID
        bindings = handler.binding_service.get_bindings()
        if not bindings:
            logger.info("报价补扫跳过：没有绑定关系")
            return 0

        # 确定要扫描的群 ID
        if room_ids:
            target_rooms = set(room_ids)
        else:
            target_rooms = set()
            for b in bindings:
                gid = (b.get("group_id") or "").strip()
                if gid:
                    target_rooms.add(gid)

        if not target_rooms:
            logger.info("报价补扫跳过：没有绑定的群")
            return 0

        import time
        cutoff_ms = int((time.time() - lookback_days * 86400) * 1000)

        conn = self.db.pool.connection()
        try:
            import pymysql.cursors
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            placeholders = ",".join(["%s"] * len(target_rooms))
            params = [cutoff_ms] + list(target_rooms)

            # 查找绑定群中的图片和文本消息
            # 按 corpid 过滤，每个企业只扫自己的消息
            corpid_filter = ""
            if self.corpid:
                corpid_filter = " AND m.corpid = %s"
                params.append(self.corpid)

            sql = f"""
                SELECT m.seq, m.roomid, m.sender, m.msgtype, m.parsed_content
                FROM messages m
                WHERE m.msgtime >= %s
                  AND m.msgtype IN ('image', 'text')
                  AND m.roomid IN ({placeholders})
                  {corpid_filter}
                ORDER BY m.seq ASC
            """
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            logger.info("报价补扫完成：没有符合条件的历史消息")
            return 0

        enqueued = 0
        for row in rows:
            roomid = row.get("roomid", "")
            sender = row.get("sender", "")

            # 检查是否匹配绑定规则
            binding = handler.binding_service.find_binding(roomid, sender)
            if not binding:
                continue

            try:
                content = json.loads(row.get("parsed_content", "{}"))
            except (json.JSONDecodeError, TypeError):
                content = {}

            logger.info("报价补扫入队: seq=%d, room=%s, type=%s",
                        row["seq"], roomid, row["msgtype"])
            handler.check_and_enqueue({
                "msgtype": row["msgtype"],
                "roomid": roomid,
                "sender": sender,
                "content": content,
                "seq": row["seq"],
                "finance_sdk": self.finance_sdk,
                "sdk_config": {
                    "proxy": self.proxy,
                    "proxy_passwd": self.passwd,
                    "timeout": self.timeout,
                },
            })
            enqueued += 1

        logger.info("报价补扫完成：共 %d 条消息已入队", enqueued)
        return enqueued

    def _process_item(self, item: dict):
        """处理单条加密消息"""
        msg_seq = item.get("seq", 0)
        encrypt_random_key = item.get("encrypt_random_key", "")
        encrypt_chat_msg = item.get("encrypt_chat_msg", "")
        publickey_ver = item.get("publickey_ver", 1)

        # 解密消息
        ret, decrypted_json = self.decryptor.decrypt_message(encrypt_random_key, encrypt_chat_msg)
        if ret != 0:
            logger.error("%s解密消息失败, seq=%d", self._log_prefix, msg_seq)
            notify_error("消息解密", "_process_item", f"解密消息失败, seq={msg_seq}", f"企业: {self.enterprise_name}")
            return

        # 解析消息
        try:
            msg_data = json.loads(decrypted_json)
            parsed = self.parser.parse(msg_data)
            logger.info("%s消息[seq=%d]: %s", self._log_prefix, msg_seq, parsed.get("summary", ""))

            # 存储消息
            self.db.save_message(msg_seq, msg_data, parsed, corpid=self.corpid)

            # 检查是否需要触发保单识别
            self._check_insurance_trigger(msg_seq, msg_data, parsed)

            # "群公告"文本（机器人自己发的入群欢迎语/系统公告）只正常存档，
            # 不识别、不触发任何关键词类功能（保险报价/回填/台账导出等），避免误命中
            _notice_text = (parsed.get("content") or {}).get("text") or "" if parsed.get("msgtype") == "text" else ""
            if "群公告" in _notice_text:
                return

            # 全局跳过关键词黑名单（内部管理员维护，默认空）：命中则跳过
            # 全部关键词触发链路，仅正常存档。在「群公告」硬编码过滤之后叠加
            if _notice_text and getattr(self, "insurance_handler", None):
                try:
                    from insurance.db import get_fill_skip_keywords
                    _skips = get_fill_skip_keywords(self.insurance_handler.db)
                    _hit = next((k for k in _skips if k in _notice_text), None)
                    if _hit:
                        logger.info("命中跳过关键词「%s」，忽略关键词触发, seq=%d", _hit, msg_seq)
                        return
                except Exception:
                    logger.debug("跳过关键词黑名单检查异常, seq=%d", msg_seq, exc_info=True)

            # 检查是否需要触发保险报价
            self._check_quote_trigger(msg_seq, msg_data, parsed)

            # 检查是否为"政策报价"文本（车牌+商业/交强/驾意），按车牌回填政策字段
            self._check_policy_fill_trigger(msg_seq, parsed)

            # 检查是否为"发送台账XX"命令文本，命中则导出台账Excel并发回群里@发送人
            self._check_ledger_export_trigger(msg_seq, parsed)

            # 未开通AI机器人识别的群里发补充信息/保单PDF时，群内提醒并@发送人+@技术客服
            self._check_unmonitored_group_notice(msg_seq, parsed)
        except json.JSONDecodeError as e:
            logger.error("%s消息JSON解析失败, seq=%d: %s", self._log_prefix, msg_seq, e)
            notify_error("消息解析", "_process_item", f"JSON解析失败, seq={msg_seq}", str(e))

    def _check_policy_fill_trigger(self, seq: int, parsed: dict):
        """群内"政策报价"文本（车牌 + 商业X/交强X/驾意X）按车牌回填政策字段。

        仅对监控群的文本消息生效；普通聊天文本（无车牌或无政策项）自动忽略。
        企业定制：填充的「政策商业险/政策强险/政策非车险/电话」是自定义字段，
        未配置这些列的企业看不到、无副作用。
        """
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            return
        if parsed.get("msgtype", "") != "text":
            return
        roomid = parsed.get("roomid", "")
        if not roomid:
            return
        # 仅监控群
        watch = handler.get_watch_config()
        if roomid not in watch.get("rooms", []):
            return
        # 文本消息内容在 content["text"]（parser 解析为 {"text": ...}）；兼容旧 content 字段
        _content = parsed.get("content", {}) or {}
        text = _content.get("text") or _content.get("content") or ""
        if not text:
            return
        try:
            from insurance.policy_quote_fill import apply_policy_quote
            # 群所属企业：配置过关键词规则的企业走可配置引擎，否则走原写死逻辑
            enterprise_id = None
            try:
                for m in handler._get_matched_monitors(roomid, parsed.get("sender", "")):
                    if m.get("enterprise_id"):
                        enterprise_id = m["enterprise_id"]
                        break
            except Exception:
                logger.debug("解析群所属企业失败 room=%s", roomid, exc_info=True)
            cnt, info = apply_policy_quote(handler.db, text, roomid=roomid,
                                           enterprise_id=enterprise_id)
            if info:
                logger.info("群内回填: seq=%d room=%s 命中%d条 %s", seq, roomid, cnt, info)
                if cnt == 0:
                    # 命中0条：补充信息未录入，群内通知 @发送人 和 @技术客服
                    self._notify_policy_fill_miss(
                        seq, roomid, parsed.get("from", ""), info.get("plate", ""))
                else:
                    # 录入成功但企业从未配置过群内补充模板（走的默认写死规则）：
                    # 提醒 @技术客服 尽快为该企业配置专属模板
                    from insurance.policy_quote_fill import get_compiled_rules
                    if get_compiled_rules(handler.db, enterprise_id) is None:
                        self._notify_policy_fill_no_template(
                            seq, roomid, info.get("plate", ""))
        except Exception as e:
            logger.error("群内回填异常 seq=%d: %s", seq, e, exc_info=True)

    # 群内补充命中0条时的默认通知文案（{plate} 动态替换为解析到的车牌）
    _POLICY_FILL_MISS_TEMPLATE = (
        "⚠️ 补充信息未录入\n"
        "车牌：{plate}\n"
        "系统暂未找到该车牌的保单识别记录，本次补充的信息（业务经理、业务专员、保险公司经办人等）未能生效。\n"
        "请检查：\n"
        "1️⃣ 该车牌的保单是否已发到本群并识别成功\n"
        "2️⃣ 车牌号是否输入有误\n"
        "确认保单已识别后，请重新发送一次补充信息即可。\n"
        "若车牌无误但仍提示未录入，请联系客服处理（已@客服跟进）。"
    )

    def _resolve_sender_at_name(self, sender: str) -> str:
        """解析发送人名称用于群内@（用本 fetcher 所属企业的通讯录凭证）。

        只用解析出的真实名称；get_name 解析失败回退成原始 userid 时
        返回空串 = 不@发送人（FlowBot 按名字匹配，userid @不上），消息照发。
        """
        contacts = getattr(self, "contacts", None)
        if not sender or not contacts:
            return ""
        try:
            name = contacts.get_name(sender) or ""
        except Exception:
            return ""
        return "" if name == sender else name

    def _notify_policy_fill_miss(self, seq: int, roomid: str, sender: str, plate: str):
        """群内补充文本按车牌命中0条识别记录时，FlowBot 群通知 @发送人 + @技术客服。

        配置键 quote_fill_miss_notify（insurance_config 表）：
          enabled           开关（默认关闭）
          robot_id          FlowBot 机器人 ID，缺省复用 flowbot_fail_notify 的机器人
          tech_support_name 客服微信名，缺省"客户客服（午虎保单台账系统）"
          template          自定义文案，{plate} 占位符替换车牌
        旁路通知：任何异常只记日志，绝不影响消息处理主流程。
        """
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            return
        try:
            from insurance.db import get_insurance_config
            cfg = get_insurance_config(handler.db, "quote_fill_miss_notify", {}) or {}
            if not cfg.get("enabled"):
                return
            robot_id = cfg.get("robot_id")
            if not robot_id:
                fail_cfg = get_insurance_config(handler.db, "flowbot_fail_notify", {}) or {}
                robot_id = fail_cfg.get("robot_id")
            if not robot_id:
                logger.info("补充未录入通知跳过：未配置 robot_id seq=%d room=%s", seq, roomid)
                return

            sender_name = self._resolve_sender_at_name(sender)
            room_name = ""
            if getattr(self, "contacts", None):
                try:
                    # 用本 fetcher 所属企业的通讯录凭证解析（勿用 handler.contacts=主企业）
                    room_name = self.contacts.get_room_name(roomid) or ""
                    if room_name == roomid:
                        room_name = ""  # 解析失败时会回退返回roomid，视为无群名
                except Exception:
                    pass
            if not room_name:
                logger.info("补充未录入通知跳过：无群名 seq=%d room=%s", seq, roomid)
                return

            tech_name = cfg.get("tech_support_name") or "客户客服（午虎保单台账系统）"
            template = cfg.get("template") or self._POLICY_FILL_MISS_TEMPLATE
            msg = template.replace("{plate}", plate or "未知")
            # 去重：发送人本身就是技术客服账号时只@一次
            at_list = list(dict.fromkeys(n for n in (sender_name, tech_name) if n))

            from insurance.flowbot_notify import send_flowbot_group_message
            send_flowbot_group_message(room_name, at_list, msg, robot_id)
        except Exception as e:
            logger.warning("补充未录入通知发送失败（不影响主流程）seq=%d: %s", seq, e)

    # 企业未配置群内补充模板时的默认提醒文案（{plate} 动态替换车牌）
    _POLICY_FILL_NO_TEMPLATE_TEMPLATE = (
        "⚠️ 群内补充提醒：车牌 {plate} 的补充信息已收到，"
        "但贵司暂未配置补充信息模板，部分内容可能未生效。\n"
        "技术客服将尽快为贵司完成配置，请留意群内通知。"
    )

    # 未配置模板提醒的群级节流：{roomid: "YYYY-MM-DD"}，同一群每天最多提醒一次
    _no_template_notified: dict = {}

    def _notify_policy_fill_no_template(self, seq: int, roomid: str, plate: str):
        """企业未配置群内补充模板但客户已在发补充文本时，FlowBot 群通知 @技术客服。

        配置键 quote_fill_template_notify（insurance_config 表）：
          enabled           开关（默认关闭）
          robot_id          FlowBot 机器人 ID，缺省复用 flowbot_fail_notify 的机器人
          tech_support_name 客服微信名，缺省"客户客服（午虎保单台账系统）"
          template          自定义文案，{plate} 占位符替换车牌
        同一群每天最多提醒一次（内存节流，进程重启后重置）。
        旁路通知：任何异常只记日志，绝不影响消息处理主流程。
        """
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            return
        try:
            today = time.strftime("%Y-%m-%d")
            if self._no_template_notified.get(roomid) == today:
                return

            from insurance.db import get_insurance_config
            cfg = get_insurance_config(handler.db, "quote_fill_template_notify", {}) or {}
            if not cfg.get("enabled"):
                return
            robot_id = cfg.get("robot_id")
            if not robot_id:
                fail_cfg = get_insurance_config(handler.db, "flowbot_fail_notify", {}) or {}
                robot_id = fail_cfg.get("robot_id")
            if not robot_id:
                logger.info("未配置模板提醒跳过：未配置 robot_id seq=%d room=%s", seq, roomid)
                return

            room_name = ""
            if getattr(self, "contacts", None):
                try:
                    # 用本 fetcher 所属企业的通讯录凭证解析（勿用 handler.contacts=主企业）
                    room_name = self.contacts.get_room_name(roomid) or ""
                    if room_name == roomid:
                        room_name = ""  # 解析失败时会回退返回roomid，视为无群名
                except Exception:
                    pass
            if not room_name:
                logger.info("未配置模板提醒跳过：无群名 seq=%d room=%s", seq, roomid)
                return

            tech_name = cfg.get("tech_support_name") or "客户客服（午虎保单台账系统）"
            template = cfg.get("template") or self._POLICY_FILL_NO_TEMPLATE_TEMPLATE
            msg = template.replace("{plate}", plate or "未知")

            from insurance.flowbot_notify import send_flowbot_group_message
            if send_flowbot_group_message(room_name, tech_name, msg, robot_id):
                self._no_template_notified[roomid] = today
        except Exception as e:
            logger.warning("未配置模板提醒发送失败（不影响主流程）seq=%d: %s", seq, e)

    # 未开通群提醒的默认文案（{plate}/{filename} 动态替换）
    _UNMONITORED_TEXT_TEMPLATE = (
        "⚠️ 本群暂未开通AI机器人识别，补充信息（车牌：{plate}）未被记录。\n"
        "技术客服将尽快开通，开通后重发一次即可。"
    )
    _UNMONITORED_PDF_TEMPLATE = (
        "⚠️ 本群暂未开通AI机器人识别，保单文件（{filename}）未被识别。\n"
        "技术客服将尽快开通，开通后重发一次即可。"
    )
    # 保单类 PDF 文件名特征：含车牌号或保险相关关键词才提醒，避免无关 PDF 误触发
    _POLICY_PDF_KEYWORDS = ("保单", "保险", "投保", "批单", "交强", "商业", "驾意", "电子保单")

    # 未开通群提醒的群级节流：{roomid: "YYYY-MM-DD"}，同一群每天最多提醒一次（文本/PDF共用）
    _unmonitored_notified: dict = {}

    def _check_unmonitored_group_notice(self, seq: int, parsed: dict):
        """未开通AI机器人识别的群里发补充信息文本/保单PDF时，群内提醒。

        @发送人 + @技术客服；群名解析不到或FlowBot发送失败时，走钉钉告警兜底。
        配置键 unmonitored_group_notify（insurance_config 表）：
          enabled           开关（默认关闭）
          robot_id          FlowBot 机器人 ID，缺省复用 flowbot_fail_notify
          tech_support_name 技术客服微信名
          text_template     补充文本提醒文案（{plate} 占位）
          pdf_template      保单PDF提醒文案（{filename} 占位）
        同一群每天最多提醒一次。旁路通知：任何异常只记日志，不影响主流程。
        """
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            return
        try:
            roomid = parsed.get("roomid", "")
            msgtype = parsed.get("msgtype", "")
            if not roomid or msgtype not in ("text", "file"):
                return
            # 已开通（监控中）的群走正常识别/回填链路，这里只管未开通的群
            watch = handler.get_watch_config()
            if roomid in watch.get("rooms", []):
                return

            import time as _time
            today = _time.strftime("%Y-%m-%d")
            if self._unmonitored_notified.get(roomid) == today:
                return

            from insurance.db import get_insurance_config
            cfg = get_insurance_config(handler.db, "unmonitored_group_notify", {}) or {}
            if not cfg.get("enabled"):
                return

            content = parsed.get("content", {}) or {}
            if msgtype == "text":
                text = content.get("text") or content.get("content") or ""
                if not text:
                    return
                from insurance.policy_quote_fill import parse_policy_quote_text
                info = parse_policy_quote_text(text)
                if not info:
                    return
                template = cfg.get("text_template") or self._UNMONITORED_TEXT_TEMPLATE
                msg = template.replace("{plate}", info.get("plate", "未知"))
                content_desc = f"车牌 {info.get('plate', '未知')} 的补充信息"
            else:
                filename = content.get("filename", "") or ""
                if not filename.lower().endswith(".pdf"):
                    return
                from insurance.policy_quote_fill import _PLATE_RE
                if not (_PLATE_RE.search(filename)
                        or any(k in filename for k in self._POLICY_PDF_KEYWORDS)):
                    return
                template = cfg.get("pdf_template") or self._UNMONITORED_PDF_TEMPLATE
                msg = template.replace("{filename}", filename)
                content_desc = f"保单文件 {filename}"

            robot_id = cfg.get("robot_id")
            if not robot_id:
                fail_cfg = get_insurance_config(handler.db, "flowbot_fail_notify", {}) or {}
                robot_id = fail_cfg.get("robot_id")

            sender = parsed.get("from", "")
            sender_name = self._resolve_sender_at_name(sender)
            room_name = ""
            if getattr(self, "contacts", None):
                try:
                    # 用本 fetcher 所属企业的通讯录凭证解析（勿用 handler.contacts=主企业）
                    room_name = self.contacts.get_room_name(roomid) or ""
                    if room_name == roomid:
                        room_name = ""  # 解析失败时会回退返回roomid，视为无群名
                except Exception:
                    pass

            tech_name = cfg.get("tech_support_name") or "客户客服（午虎保单台账系统）"
            sent = False
            if room_name and robot_id:
                at_list = list(dict.fromkeys(n for n in (sender_name, tech_name) if n))
                from insurance.flowbot_notify import send_flowbot_group_message
                sent = send_flowbot_group_message(room_name, at_list, msg, robot_id)
                logger.info("未开通群提醒: seq=%d room=%s(%s) %s 发送%s",
                            seq, roomid, room_name, content_desc, "成功" if sent else "失败")
            else:
                logger.info("未开通群提醒: seq=%d room=%s 群名或robot_id缺失，走钉钉兜底", seq, roomid)

            if not sent:
                # 群内发不出去（群名解析不到/机器人不在群/接口失败）→ 钉钉告警兜底
                from core.notify import notify_unmonitored_group
                notify_unmonitored_group(
                    f"{room_name or roomid}", sender_name or sender, content_desc)
            # 无论走哪个通道，当天该群不再重复提醒
            self._unmonitored_notified[roomid] = today
        except Exception as e:
            logger.warning("未开通群提醒失败（不影响主流程）seq=%d: %s", seq, e)

    def _check_ledger_export_trigger(self, seq: int, parsed: dict):
        """群内"发送台账+今日/本周/本月"文本 -> 生成保单台账Excel，
        上传COS后经FlowBot发到该群并@发送人。

        仅对监控群生效；发送人需能解析出所属账号——优先用其个人绑定
        （user_sender_binding），否则退回监控配置/该企业自己的 enterprise 账号
        （企业管理员一般不建个人绑定，这层兜底让企业管理员本人也能直接触发）。
        解析不出账号时区分提示：群监控配置未关联企业 → "该群未配置，请联系客服配置"；
        能定位企业但无可用账号 → "您当前账号无权限，请联系客服开通"。
        任何环节异常都只记日志，绝不影响消息处理主流程。

        ⚠️ 已知风险（2026-07-17 明确评估过、按用户要求恢复此兜底）：这层兜底
        对"发消息的人是谁"完全不校验，只要是监控群里的任意消息（含机器人自己
        被存档SDK回收的输出消息、群内任何外部联系人）都会被当成该企业管理员
        触发导出。当天曾因"群公告"广播消息触发这条链路，真实导出并发到群里
        过熠辉/宏鑫的保单数据——那个直接诱因已通过跳过含"群公告"文本的关键词
        触发单独修复（见 _process_item 里的过滤），但这层兜底本身的权限漏洞
        并未消除，仍需注意。
        """
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            return
        if parsed.get("msgtype", "") != "text":
            return
        roomid = parsed.get("roomid", "")
        if not roomid:
            return
        watch = handler.get_watch_config()
        if roomid not in watch.get("rooms", []):
            return
        _content = parsed.get("content", {}) or {}
        text = _content.get("text") or _content.get("content") or ""
        if not text:
            return

        from insurance.ledger_export import match_ledger_trigger
        period = match_ledger_trigger(text)
        if not period:
            return

        sender = parsed.get("from", "")
        try:
            sender_name = self._resolve_sender_at_name(sender)
            room_name = ""
            if getattr(self, "contacts", None):
                try:
                    # 用本 fetcher 所属企业的通讯录凭证解析（勿用 handler.contacts=主企业）
                    room_name = self.contacts.get_room_name(roomid) or ""
                    if room_name == roomid:
                        room_name = ""  # 解析失败时会回退返回roomid，视为无群名
                except Exception:
                    pass

            from insurance.db import get_insurance_config
            cfg = get_insurance_config(handler.db, "flowbot_fail_notify", {}) or {}
            robot_id = cfg.get("robot_id")
            if not robot_id or not room_name:
                logger.info("台账导出命令：缺少 robot_id 或群名，跳过 seq=%d room=%s", seq, roomid)
                return

            # 解析绑定账号：与保单识别共用的"监控配置 + sender 绑定"解析链路
            matched_monitors = handler._get_matched_monitors(roomid, sender)
            config_user_id = None
            config_enterprise_id = None
            for m in matched_monitors:
                if m.get("user_id"):
                    config_user_id = m["user_id"]
                if m.get("enterprise_id"):
                    config_enterprise_id = m["enterprise_id"]
                if config_user_id:
                    break
            from auth.db import get_binding_by_sender
            bound = get_binding_by_sender(handler.db, sender, config_enterprise_id)
            if bound:
                config_user_id = bound["user_id"]
                if not config_enterprise_id and bound.get("enterprise_id"):
                    config_enterprise_id = bound["enterprise_id"]
            # 兜底：企业管理员一般不建 sender 绑定（绑定主要给员工用），
            # 监控配置也常只挂了 enterprise_id、没挂具体 user_id。
            # 此时只要能确定是哪家企业的群，就用该企业自己的 enterprise 账号身份导出。
            if not config_user_id and config_enterprise_id:
                from auth.db import list_users
                ent_users = [u for u in list_users(handler.db, role="enterprise", parent_id=config_enterprise_id)
                             if u.get("enabled")]
                if ent_users:
                    config_user_id = ent_users[0]["id"]
            if not config_user_id:
                from insurance.flowbot_notify import send_flowbot_group_message
                if not config_enterprise_id:
                    # 群的监控配置没有关联企业 → 是配置问题，不是发送人权限问题
                    logger.info("台账导出命令：room=%s 监控配置未关联企业，提示群未配置 seq=%d", roomid, seq)
                    send_flowbot_group_message(room_name, sender_name, "该群未配置，请联系客服配置", robot_id)
                else:
                    # 能定位到企业但解析不出可用账号（无个人绑定且企业账号不可用）
                    logger.info("台账导出命令：sender=%s 未绑定账号且企业(%s)下无可用账号，提示无权限 seq=%d",
                                sender, config_enterprise_id, seq)
                    send_flowbot_group_message(room_name, sender_name, "您当前账号无权限，请联系客服开通", robot_id)
                return

            from insurance.ledger_export import export_ledger_via_binding
            from insurance.flowbot_notify import send_flowbot_group_message

            label, date_start, date_end = period
            excel_bytes, filename, count = export_ledger_via_binding(
                handler.db, config_user_id, config_enterprise_id, label, date_start, date_end)

            if not excel_bytes:
                send_flowbot_group_message(room_name, sender_name, f"【保单台账】{label}暂无保单数据", robot_id)
                return

            if not handler.cos_storage:
                logger.warning("台账导出：cos_storage 未配置，无法上传文件 seq=%d", seq)
                return
            cos_key_suffix = f"ledger_exports/{int(time.time())}_{sender}.xlsx"
            # 下载文件名与推送文案里的文件名保持一致（通过 Content-Disposition 指定）
            url = handler.cos_storage.upload_bytes(excel_bytes, cos_key_suffix,
                                                   download_filename=filename)
            if not url:
                logger.warning("台账导出：上传COS失败 seq=%d", seq)
                return

            msg = f"【保单台账导出】{filename}\n共 {count} 条\n下载链接（请尽快下载）：\n{url}"
            send_flowbot_group_message(room_name, sender_name, msg, robot_id)
            logger.info("台账导出命令处理完成: seq=%d room=%s sender=%s period=%s count=%d",
                        seq, roomid, sender, period, count)
        except Exception as e:
            logger.error("台账导出命令处理异常 seq=%d: %s", seq, e, exc_info=True)

    def _check_insurance_trigger(self, seq: int, msg_data: dict, parsed: dict):
        """检查是否需要触发保单识别（支持群聊 + 私聊）"""
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            return

        msgtype = parsed.get("msgtype", "")
        roomid = parsed.get("roomid", "")
        sender = parsed.get("from", "")
        content = parsed.get("content", {})

        # 条件1：文件类型 + PDF后缀
        if msgtype != "file":
            return

        filename = content.get("filename", "")
        if not filename.lower().endswith(".pdf"):
            return

        # 条件2：在监控列表中（群聊匹配 roomid，私聊匹配 sender）
        watch = handler.get_watch_config()
        logger.info(
            "保单触发检查: seq=%d, roomid=%r, sender=%r, watch_rooms=%s, watch_users=%s",
            seq, roomid, sender, watch.get("rooms", []), watch.get("users", []),
        )
        is_watched = False
        if roomid and roomid in watch.get("rooms", []):
            is_watched = True
        elif not roomid and sender and sender in watch.get("users", []):
            is_watched = True

        if not is_watched:
            logger.info("保单触发检查: seq=%d 未匹配任何监控配置，跳过", seq)
            return

        source_desc = f"room={roomid}" if roomid else f"user={sender}"
        logger.info("触发保单识别: seq=%d, %s, file=%s", seq, source_desc, filename)
        handler.enqueue({
            "seq": seq,
            "roomid": roomid,
            "sender": sender,
            "filename": filename,
            "sdkfileid": content.get("sdkfileid", ""),
            "filesize": content.get("filesize", 0),
            "finance_sdk": self.finance_sdk,
            "sdk_config": {
                "proxy": self.proxy,
                "proxy_passwd": self.passwd,
                "timeout": self.timeout,
            },
        })

    def _check_quote_trigger(self, seq: int, msg_data: dict, parsed: dict):
        """检查是否需要触发保险报价（图片/文本消息）"""
        handler = getattr(self, "quote_handler", None)
        if not handler:
            return

        msgtype = parsed.get("msgtype", "")
        roomid = parsed.get("roomid", "")
        sender = parsed.get("from", "")
        content = parsed.get("content", {})

        # 仅处理图片和文本消息
        if msgtype not in ("image", "text"):
            return

        # 仅处理群聊消息
        if not roomid:
            return

        handler.check_and_enqueue({
            "msgtype": msgtype,
            "roomid": roomid,
            "sender": sender,
            "content": content,
            "seq": seq,
            "finance_sdk": self.finance_sdk,
            "sdk_config": {
                "proxy": self.proxy,
                "proxy_passwd": self.passwd,
                "timeout": self.timeout,
            },
        })
