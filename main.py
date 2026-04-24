"""
企业微信会话内容存档 - 入口文件
启动 Flask 回调服务，接收企业微信回调后拉取并解密聊天记录
"""

import logging
import threading

import yaml
from flask import Flask, render_template

from callback.crypto import WXBizMsgCrypt
from callback.server import callback_bp, init_callback
from web.api import api_bp, init_api
from core.decryptor import MessageDecryptor
from core.fetcher import MessageFetcher
from core.parser import MessageParser
from sdk.finance_sdk import FinanceSDK
from storage.db import Database

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    """加载配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def create_app(config: dict) -> Flask:
    """创建并配置 Flask 应用"""
    app = Flask(__name__, template_folder="web/templates")

    # 初始化数据库
    db = Database(config["storage"]["db_path"])

    # 注册前端 API 蓝图（fetcher 在SDK初始化后补充传入）
    init_api(db)
    app.register_blueprint(api_bp)

    # 前端首页
    @app.route("/")
    def index():
        return render_template("index.html")

    # 尝试初始化 SDK 和回调服务（SDK不可用时仅前端可用）
    sdk_config = config["sdk"]
    try:
        finance_sdk = FinanceSDK(sdk_config["lib_path"])
        ret = finance_sdk.init(config["wecom"]["corpid"], config["wecom"]["secret"])
        if ret != 0:
            raise RuntimeError(f"SDK初始化失败, 错误码: {ret}")

        # 初始化解密器
        decryptor = MessageDecryptor(finance_sdk, sdk_config["private_key_path"])

        # 初始化解析器
        parser = MessageParser()

        # 初始化消息拉取器
        fetcher = MessageFetcher(
            finance_sdk=finance_sdk,
            decryptor=decryptor,
            parser=parser,
            db=db,
            limit=config["fetch"]["limit"],
            proxy=sdk_config.get("proxy", ""),
            passwd=sdk_config.get("proxy_passwd", ""),
            timeout=sdk_config.get("timeout", 10),
        )

        # 回调触发函数：在新线程中拉取消息，避免阻塞回调响应
        def on_message_notify():
            thread = threading.Thread(target=fetcher.fetch_new_messages, daemon=True)
            thread.start()

        # 初始化回调加解密
        callback_config = config["callback"]
        crypto = WXBizMsgCrypt(
            token=callback_config["token"],
            encoding_aes_key=callback_config["encoding_aes_key"],
            corp_id=config["wecom"]["corpid"],
        )

        # 初始化回调服务
        init_callback(crypto, on_message_callback=on_message_notify)
        app.register_blueprint(callback_bp)

        # 把 fetcher 和 SDK 传给 API，支持手动拉取和媒体文件下载
        init_api(db, fetcher, finance_sdk, sdk_config)

        # 保存引用，方便清理
        app.extensions["wxbot"] = {
            "db": db,
            "finance_sdk": finance_sdk,
            "fetcher": fetcher,
        }

        logger.info("应用初始化完成，回调地址: /callback，前端地址: /")

    except Exception as e:
        logger.warning("SDK初始化跳过（%s），仅前端可用。配置好SDK后重启即可启用回调。", e)
        app.extensions["wxbot"] = {"db": db}

    return app


def main():
    config = load_config()
    app = create_app(config)
    port = config["callback"].get("port", 5000)
    logger.info("启动回调服务, 端口: %d", port)
    app.run(host="0.0.0.0", port=port, debug=True)


if __name__ == "__main__":
    main()
