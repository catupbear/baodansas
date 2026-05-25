# quote/handler.py
"""
保险报价处理器
监听图片和文本消息，通过百度OCR识别行驶证/身份证，配对后提交预报价
"""

import gc
import logging
import threading
import time
from queue import Queue, Full

from quote.binding import BindingService
from quote.ocr import QuoteOCR
from quote.matcher import QuoteMatcher

logger = logging.getLogger(__name__)


class QuoteHandler:
    """保险报价处理器"""

    def __init__(self, cos_storage=None, app_config: dict = None):
        self._queue = Queue(maxsize=200)
        self.cos_storage = cos_storage
        self.finance_sdk = None   # 由 main.py 注入
        self.sdk_config = None    # 由 main.py 注入

        # 初始化子组件
        self.binding_service = BindingService()
        self.matcher = QuoteMatcher(timeout_seconds=60, check_interval=10)

        # 初始化百度 OCR（复用 config.yaml 中的 baidu_ocr 配置）
        self._ocr = None
        if app_config:
            baidu_cfg = app_config.get("baidu_ocr", {})
            api_key = baidu_cfg.get("api_key", "")
            secret_key = baidu_cfg.get("secret_key", "")
            if api_key and secret_key:
                self._ocr = QuoteOCR(api_key, secret_key)
                logger.info("报价OCR初始化完成")
            else:
                logger.warning("报价OCR未配置百度OCR密钥，证件识别不可用")

        # 启动消费线程
        self._consumer_thread = threading.Thread(target=self._consume, daemon=True)
        self._consumer_thread.start()
        logger.info("保险报价处理器已启动")

    def check_and_enqueue(self, msg_data: dict):
        """
        检查消息是否需要处理，是则入队。
        由 fetcher._check_quote_trigger 调用。
        """
        roomid = msg_data.get("roomid", "")
        sender = msg_data.get("sender", "")

        if not roomid:
            return

        binding = self.binding_service.find_binding(roomid, sender)
        if not binding:
            logger.info("报价：未匹配绑定 roomid=%s, sender=%s", roomid, sender)
            return
        logger.info("报价：匹配绑定成功 roomid=%s, sender=%s, binding_id=%s",
                    roomid, sender, binding.get("id"))

        msg_data["binding"] = binding
        try:
            self._queue.put_nowait(msg_data)
        except Full:
            logger.warning("报价队列已满，丢弃消息 seq=%s", msg_data.get("seq"))

    def _consume(self):
        """消费线程主循环"""
        while True:
            try:
                msg = self._queue.get(block=True, timeout=1)
            except Exception:
                continue
            try:
                self._process(msg)
            except Exception as e:
                logger.error("报价处理异常: %s", e, exc_info=True)

    def _process(self, msg: dict):
        """处理单条消息"""
        msgtype = msg.get("msgtype", "")
        if msgtype == "image":
            self._process_image(msg)
        elif msgtype == "text":
            self._process_text(msg)

    def _process_image(self, msg: dict):
        """处理图片消息：下载→COS上传→OCR识别→配对"""
        roomid = msg["roomid"]
        sender = msg["sender"]
        binding = msg["binding"]
        content = msg.get("content", {})
        sdkfileid = content.get("sdkfileid", "")
        seq = msg.get("seq", 0)

        if not sdkfileid:
            logger.warning("报价：图片消息缺少 sdkfileid, seq=%d", seq)
            return

        if not self._ocr:
            logger.warning("报价：OCR 未初始化，跳过 seq=%d", seq)
            return

        # 1. 通过 SDK 下载图片到内存（优先使用消息携带的 SDK，支持多企业）
        image_bytes = None
        msg_sdk = msg.get("finance_sdk") or self.finance_sdk
        msg_sdk_config = msg.get("sdk_config") or self.sdk_config
        sdk_source = "消息携带" if msg.get("finance_sdk") else "默认"
        logger.info("报价：下载图片使用 %s SDK (id=%s), seq=%d", sdk_source, id(msg_sdk) if msg_sdk else "None", seq)
        try:
            if msg_sdk and msg_sdk_config:
                ret, data = msg_sdk.get_media_data_bytes(
                    sdkfileid,
                    proxy=msg_sdk_config.get("proxy", ""),
                    passwd=msg_sdk_config.get("proxy_passwd", ""),
                    timeout=msg_sdk_config.get("timeout", 10),
                )
                if ret != 0:
                    logger.error("报价：下载图片失败 seq=%d, 错误码=%d", seq, ret)
                    return
                image_bytes = data
        except Exception as e:
            logger.error("报价：下载图片失败 seq=%d: %s", seq, e)
            return

        if not image_bytes:
            logger.error("报价：下载图片为空 seq=%d", seq)
            return

        try:
            # 2. 上传 COS
            cos_url = ""
            if self.cos_storage:
                timestamp = int(time.time() * 1000)
                cos_key = f"quote/{roomid}/{sender}/{timestamp}.jpg"
                cos_url = self.cos_storage.upload_bytes(image_bytes, cos_key)

            # 3. 同时尝试行驶证和身份证识别（优先用COS压缩URL，节省带宽并避免图片过大报错）
            if cos_url:
                compressed_url = cos_url + "?imageMogr2/thumbnail/!500x500r/quality/80"
                vehicle_result = self._ocr.recognize_vehicle_license(image_url=compressed_url)
                idcard_result = self._ocr.recognize_idcard(image_url=compressed_url)
            else:
                vehicle_result = self._ocr.recognize_vehicle_license(image_bytes)
                idcard_result = self._ocr.recognize_idcard(image_bytes)

            vehicle_ok = vehicle_result.get("success", False)
            idcard_ok = idcard_result.get("success", False)

            if vehicle_ok:
                logger.info("报价：行驶证识别成功 seq=%d, 车牌=%s",
                           seq, vehicle_result["data"].get("plate_number"))
                self.matcher.add_vehicle_license(
                    roomid, sender, vehicle_result["data"], cos_url, binding)
            elif idcard_ok:
                side = idcard_result.get("side", "front")
                logger.info("报价：身份证识别成功 seq=%d, side=%s", seq, side)
                if side == "front":
                    self.matcher.add_idcard_front(
                        roomid, sender, idcard_result["data"], cos_url, binding)
                else:
                    self.matcher.add_idcard_back(
                        roomid, sender, idcard_result["data"], cos_url, binding)
            else:
                # 两个都识别不出来，打印图片 COS 地址
                logger.warning("报价：图片识别失败 seq=%d, cos_url=%s, "
                             "行驶证错误=%s, 身份证错误=%s",
                             seq, cos_url,
                             vehicle_result.get("error", ""),
                             idcard_result.get("error", ""))
        finally:
            del image_bytes
            gc.collect()

    def _process_text(self, msg: dict):
        """处理文本消息：暂存等待配对，或识别续保关键词直接提交续保报价"""
        roomid = msg["roomid"]
        sender = msg["sender"]
        binding = msg["binding"]
        content = msg.get("content", {})
        text = content.get("content", "")

        if not text or not text.strip():
            return

        logger.info("报价：收到文本消息 room=%s, sender=%s, text=%s",
                    roomid, sender, text[:50])

        if "续保" in text:
            logger.info("报价：检测到续保关键词，转续保报价流程 room=%s sender=%s", roomid, sender)
            self.matcher.check_and_submit_renewal(roomid, sender, text, binding)
            return

        self.matcher.add_text(roomid, sender, text, binding)
