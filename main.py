"""
企业微信会话内容存档 - 入口文件
启动 Flask 回调服务，接收企业微信回调后拉取并解密聊天记录
"""

import logging
import threading

import yaml
from flask import Flask, redirect, render_template, request

from callback.crypto import WXBizMsgCrypt
from callback.server import callback_bp, init_callback
from web.api import api_bp, init_api
from core.contacts import ContactsManager
from core.decryptor import MessageDecryptor
from core.fetcher import MessageFetcher
from core.parser import MessageParser
from sdk.finance_sdk import FinanceSDK
from storage.db import Database
from insurance.db import init_insurance_tables
from insurance.monitor_config_db import init_monitor_config_table, migrate_from_watch_list
from insurance.handler import InsuranceHandler
from insurance.api import insurance_bp, init_insurance_api
from auth.db import init_users_table
from auth.jwt_utils import init_jwt
from auth.decorators import init_auth_decorators
from auth.api import auth_bp, init_auth_api

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
    app.secret_key = config.get("secret_key", "wxbot-monitor-secret-key")

    # 初始化数据库（MySQL 连接池）
    db = Database(config["mysql"])

    # 初始化通讯录模块
    contacts = ContactsManager(
        corpid=config["wecom"]["corpid"],
        secret=config["wecom"]["secret"],
        db=db,
        external_secret=config["wecom"].get("external_secret", ""),
    )
    logger.info("通讯录模块初始化完成")

    # 启动通讯录自动解析（每5分钟检查一次未解析的联系人/群名）
    contacts.start_auto_resolve(interval=300)

    # 初始化COS存储（可选）
    cos_storage = None
    cos_config = config.get("cos")
    if cos_config and cos_config.get("secret_id"):
        try:
            from core.cos_storage import CosStorage
            cos_storage = CosStorage(
                secret_id=cos_config["secret_id"],
                secret_key=cos_config["secret_key"],
                region=cos_config["region"],
                bucket=cos_config["bucket"],
                prefix=cos_config.get("prefix", "wxbot/media/"),
                cdn_domain=cos_config.get("cdn_domain", ""),
            )
        except Exception as e:
            logger.warning("COS初始化失败（%s），媒体文件将保存到本地", e)

    # 初始化账号认证模块
    init_users_table(db)
    jwt_secret = config.get("secret_key", "wxbot-monitor-secret-key")
    init_jwt(jwt_secret)
    init_auth_decorators(db)
    init_auth_api(db)
    app.register_blueprint(auth_bp)

    # 注册前端 API 蓝图
    init_api(db, contacts=contacts, cos_storage=cos_storage)
    app.register_blueprint(api_bp)

    # 初始化保单识别模块
    init_insurance_tables(db)
    init_monitor_config_table(db)
    migrate_from_watch_list(db)
    insurance_handler = InsuranceHandler(
        db=db, cos_storage=cos_storage, contacts=contacts,
        app_config=config,
    )
    init_insurance_api(db, handler=insurance_handler, contacts=contacts)
    app.register_blueprint(insurance_bp)

    # 登录页（无需认证）
    @app.route("/login")
    def login_page():
        return render_template("login.html")

    # 首页 → 保单识别
    @app.route("/")
    def index():
        return render_template("insurance.html")

    # 保单识别兼容旧路径
    @app.route("/insurance")
    def insurance_page():
        return redirect("/")

    # 监控配置管理
    @app.route("/monitor-config")
    def monitor_config_page():
        return render_template("monitor_config.html")

    # 模板训练管理
    @app.route("/training")
    def training_page():
        return render_template("training.html")

    # 账号管理（超管专属，前端校验权限）
    @app.route("/admin/users")
    def admin_users_page():
        return render_template("admin_users.html")

    # 消息监控（需要密码）
    @app.route("/monitor")
    def monitor_page():
        return render_template("monitor_login.html")

    @app.route("/monitor/chat")
    def monitor_chat():
        from flask import session
        if not session.get("monitor_auth"):
            return redirect("/monitor")
        return render_template("index.html")

    @app.route("/monitor/login", methods=["POST"])
    def monitor_login():
        from flask import session
        password = request.form.get("password", "")
        if password == config.get("monitor", {}).get("password", "wuhu2025"):
            session["monitor_auth"] = True
            return redirect("/monitor/chat")
        return render_template("monitor_login.html", error="密码错误")

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

        # 把 fetcher 和 SDK 传给 API
        init_api(db, fetcher, finance_sdk, sdk_config, contacts, cos_storage)

        # 更新保单识别的 SDK 引用
        insurance_handler.finance_sdk = finance_sdk
        insurance_handler.sdk_config = sdk_config

        # 将 insurance_handler 绑定到 fetcher（用于自动触发）
        fetcher.insurance_handler = insurance_handler

        # 保存引用，方便清理
        app.extensions["wxbot"] = {
            "db": db,
            "finance_sdk": finance_sdk,
            "fetcher": fetcher,
            "insurance_handler": insurance_handler,
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
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)


if __name__ == "__main__":
    main()
