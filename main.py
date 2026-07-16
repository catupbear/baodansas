"""
企业微信会话内容存档 - 入口文件
启动 Flask 回调服务，接收企业微信回调后拉取并解密聊天记录
"""

import logging
import threading

import yaml
from flask import Flask, redirect, render_template, request

from callback.crypto import WXBizMsgCrypt
from callback.server import callback_bp, init_callback, create_callback_blueprint
from web.api import (api_bp, init_api, register_extra_sdk, register_extra_contacts,
                     register_enterprises, register_corpid_cursor)
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
from insurance.renewal_db import init_renewal_tables
from insurance.renewal_api import renewal_bp, init_renewal_api
from quote.handler import QuoteHandler
from auth.db import init_users_table, init_enterprises_table, init_sender_binding_table, init_sms_verification_table, init_referral_columns, init_is_sales_column, init_enterprise_plan_column, init_wallet_tables, init_activated_column, init_users_renewal_column, init_enterprise_follow_status_column, init_enterprise_remark_column
from auth.jwt_utils import init_jwt
from auth.decorators import init_auth_decorators
from auth.api import auth_bp, init_auth_api
from core.notify import init_notifier

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

    # 初始化钉钉错误通知
    notify_cfg = config.get("notify", {})
    init_notifier(
        webhook_url=notify_cfg.get("webhook_url", ""),
        secret=notify_cfg.get("secret", ""),
    )

    # 初始化数据库（MySQL 连接池，传入主企业 corpid 用于回填历史消息）
    db = Database(config["mysql"], main_corpid=config["wecom"]["corpid"])

    # 初始化通讯录模块（主企业：车物家，不加缓存前缀以兼容历史数据）
    contacts = ContactsManager(
        corpid=config["wecom"]["corpid"],
        secret=config["wecom"]["secret"],
        db=db,
        external_secret=config["wecom"].get("external_secret", ""),
        enterprise_name="车物家",
    )
    logger.info("[车物家] 通讯录模块初始化完成")

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
    init_enterprises_table(db)
    init_sender_binding_table(db)
    init_sms_verification_table(db)
    init_referral_columns(db)
    init_is_sales_column(db)
    init_activated_column(db)
    init_enterprise_plan_column(db)
    init_users_renewal_column(db)
    init_enterprise_follow_status_column(db)
    init_enterprise_remark_column(db)
    init_wallet_tables(db)

    # 初始化短信模块
    from auth.sms import init_sms
    init_sms(config.get("sms", {}))
    # JWT 密钥：优先 config.yaml，否则从数据库读取/自动生成持久化随机密钥
    jwt_secret = config.get("jwt_secret", "")
    if not jwt_secret:
        from insurance.db import get_insurance_config, set_insurance_config
        import secrets
        jwt_secret = get_insurance_config(db, "jwt_secret", "")
        if not jwt_secret:
            jwt_secret = secrets.token_hex(32)
            set_insurance_config(db, "jwt_secret", jwt_secret)
            logger.info("已自动生成 JWT 密钥并持久化到数据库")
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

    # 初始化续保任务模块
    init_renewal_tables(db)
    init_renewal_api(db, cos_storage=cos_storage)
    app.register_blueprint(renewal_bp)

    # 初始化保险报价模块
    quote_handler = QuoteHandler(
        cos_storage=cos_storage,
        app_config=config,
    )

    # 登录页（无需认证）
    @app.route("/login")
    def login_page():
        return render_template("login.html")

    # 首页 → 客户展示落地页
    @app.route("/")
    def index():
        return render_template("landing.html")

    # 兼容旧路径 /home
    @app.route("/home")
    def home_page():
        from flask import redirect
        return redirect("/", code=301)

    # 保单识别兼容旧路径
    @app.route("/insurance")
    def insurance_page():
        return render_template("insurance.html")

    # 邀请有礼介绍页（对外营销页）
    @app.route("/invite")
    def invite_page():
        # 邀请有礼功能暂时下线：重定向到首页（恢复时改回 render_template("invite.html")）
        return redirect("/insurance")
        # return render_template("invite.html")

    # 使用手册（左侧目录的文档风格页面，内部超管可编辑）
    @app.route("/manual")
    def manual_page():
        return render_template("manual.html")

    # 监控配置管理
    @app.route("/monitor-config")
    def monitor_config_page():
        return render_template("monitor_config.html")

    # 模板训练管理
    @app.route("/training")
    def training_page():
        return render_template("training.html")

    # 客户管理（销售跟进视图，超管专属，前端校验权限）
    @app.route("/admin/customers")
    def admin_customers_page():
        return render_template("customers.html")

    # 企业管理（超管专属，前端校验权限；现在是「客户管理」里每个客户下的详细管理入口）
    @app.route("/admin/enterprises")
    def admin_enterprises_page():
        return render_template("admin_enterprises.html")

    # 账号管理（超管专属，前端校验权限）
    @app.route("/admin/users")
    def admin_users_page():
        return render_template("admin_users.html")

    # 企业信息收集表（公开页面，凭专属 token 访问，无需登录）
    @app.route("/enterprise-survey/<token>")
    def enterprise_survey_page(token):
        return render_template("enterprise_survey.html")

    # 企业经营报告（网页版，超管在后台打开，前端凭 token 拉数据）
    @app.route("/enterprise-report-view")
    def enterprise_report_view_page():
        return render_template("enterprise_report.html")

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

        # 主企业（车物家）消息拉取器
        main_corpid = config["wecom"]["corpid"]
        fetcher = MessageFetcher(
            finance_sdk=finance_sdk,
            decryptor=decryptor,
            parser=parser,
            db=db,
            limit=config["fetch"]["limit"],
            proxy=sdk_config.get("proxy", ""),
            passwd=sdk_config.get("proxy_passwd", ""),
            timeout=sdk_config.get("timeout", 10),
            cursor_id=1,
            enterprise_name="车物家",
            corpid=main_corpid,
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
            corp_id=main_corpid,
        )

        # 初始化回调服务
        init_callback(crypto, on_message_callback=on_message_notify)
        app.register_blueprint(callback_bp)

        # 注册额外企业回调（独立 SDK + fetcher + contacts）
        extra_fetchers = []
        for i, cb_conf in enumerate(config.get("extra_callbacks", [])):
            cb_corpid = cb_conf.get("corpid", main_corpid)
            cb_secret = cb_conf.get("secret", "")
            cb_name = cb_conf.get("name", f"企业{i + 2}")
            cb_cursor_id = i + 2  # 游标 id: 2, 3, 4...

            # 回调加解密
            extra_crypto = WXBizMsgCrypt(
                token=cb_conf["token"],
                encoding_aes_key=cb_conf["encoding_aes_key"],
                corp_id=cb_corpid,
            )

            # 如果有独立的 secret，初始化独立 SDK + fetcher
            if cb_secret and cb_corpid != main_corpid:
                extra_sdk = FinanceSDK(sdk_config["lib_path"])
                ret2 = extra_sdk.init(cb_corpid, cb_secret)
                if ret2 != 0:
                    logger.error("[%s] SDK初始化失败, 错误码: %d, 跳过", cb_name, ret2)
                    continue

                extra_decryptor = MessageDecryptor(extra_sdk, sdk_config["private_key_path"])
                extra_fetcher = MessageFetcher(
                    finance_sdk=extra_sdk,
                    decryptor=extra_decryptor,
                    parser=parser,  # 解析器无状态，共用
                    db=db,
                    limit=config["fetch"]["limit"],
                    proxy=sdk_config.get("proxy", ""),
                    passwd=sdk_config.get("proxy_passwd", ""),
                    timeout=sdk_config.get("timeout", 10),
                    cursor_id=cb_cursor_id,
                    enterprise_name=cb_name,
                    corpid=cb_corpid,
                )
                # 绑定保单识别和报价（共享同一个 handler）
                extra_fetcher.insurance_handler = insurance_handler
                extra_fetcher.quote_handler = quote_handler

                # 初始化独立通讯录（用 corpid 前缀隔离缓存）
                extra_contacts = ContactsManager(
                    corpid=cb_corpid,
                    secret=cb_secret,
                    db=db,
                    external_secret=cb_conf.get("external_secret", ""),
                    enterprise_name=cb_name,
                )
                extra_contacts.set_cache_prefix(cb_corpid)
                extra_contacts.set_fallback_contacts(contacts)  # 跨企业群回退到主企业查询
                extra_contacts.start_auto_resolve(interval=300)
                # 绑定本企业专属通讯录到 fetcher（群消息触发的功能，如台账导出，
                # 需要按消息实际所属企业解析群名，不能只用主企业 contacts）
                extra_fetcher.contacts = extra_contacts

                def make_notify(f):
                    def notify():
                        thread = threading.Thread(target=f.fetch_new_messages, daemon=True)
                        thread.start()
                    return notify

                extra_fetchers.append({
                    "name": cb_name,
                    "sdk": extra_sdk,
                    "fetcher": extra_fetcher,
                    "contacts": extra_contacts,
                })

                bp = create_callback_blueprint(
                    path=cb_conf["path"],
                    crypto=extra_crypto,
                    on_message_callback=make_notify(extra_fetcher),
                    name_suffix=f"_{i + 2}",
                )
            else:
                # 同企业不同回调，共享 fetcher
                bp = create_callback_blueprint(
                    path=cb_conf["path"],
                    crypto=extra_crypto,
                    on_message_callback=on_message_notify,
                    name_suffix=f"_{i + 2}",
                )

            app.register_blueprint(bp)
            logger.info("[%s] 回调已注册: %s (corpid=%s)", cb_name, cb_conf["path"], cb_corpid)

        # 把 fetcher 和 SDK 传给 API
        init_api(db, fetcher, finance_sdk, sdk_config, contacts, cos_storage)

        # 注册企业 corpid → cursor_id 映射（主企业 cursor_id=1）
        register_corpid_cursor(main_corpid, 1)

        # 注册额外企业 SDK、通讯录、游标映射
        for ef in extra_fetchers:
            register_extra_sdk(ef["fetcher"].corpid, ef["sdk"])
            register_extra_contacts(ef["fetcher"].corpid, ef["contacts"])
            register_corpid_cursor(ef["fetcher"].corpid, ef["fetcher"].cursor_id)

        # 注册企业列表（前端切换用）
        enterprise_list = [
            {"corpid": main_corpid, "name": "车物家"},
        ]
        for cb_conf in config.get("extra_callbacks", []):
            if cb_conf.get("corpid") and cb_conf["corpid"] != main_corpid:
                enterprise_list.append({
                    "corpid": cb_conf["corpid"],
                    "name": cb_conf.get("name", cb_conf["corpid"]),
                })
        register_enterprises(enterprise_list)

        # 更新保单识别的 SDK 引用
        insurance_handler.finance_sdk = finance_sdk
        insurance_handler.sdk_config = sdk_config
        logger.info("主企业 SDK id=%s", id(finance_sdk))
        for ef in extra_fetchers:
            logger.info("额外企业 [%s] SDK id=%s", ef["name"], id(ef["sdk"]))

        # 将 insurance_handler 绑定到 fetcher（用于自动触发）
        fetcher.insurance_handler = insurance_handler
        # 绑定主企业通讯录到 fetcher（与 extra_fetcher.contacts 对称，群消息触发的
        # 功能统一从 self.contacts 取，不必区分主/额外企业）
        fetcher.contacts = contacts

        # 将 quote_handler 绑定到 fetcher（用于自动触发报价）
        fetcher.quote_handler = quote_handler
        quote_handler.finance_sdk = finance_sdk
        quote_handler.sdk_config = sdk_config

        # 保存引用，方便清理
        app.extensions["wxbot"] = {
            "db": db,
            "finance_sdk": finance_sdk,
            "fetcher": fetcher,
            "insurance_handler": insurance_handler,
            "quote_handler": quote_handler,
            "extra_fetchers": extra_fetchers,
        }

        # 启动时在后台补拉中断期间的消息 + 补扫漏掉的保单 PDF
        #
        # gunicorn 是多worker(-w 2)，每个worker进程都会各自完整跑一遍app初始化，包括这段启动
        # 补拉/补扫逻辑；如果不加锁，两个worker会同时各自补扫到"同一条漏处理的消息"、同时把它
        # 排进各自worker独立的内存队列处理，二者都还没来得及看到对方刚创建的记录就都通过了
        # MD5去重检查，最终各自落库一条'done'记录——这就是历史上大量重复保单记录(3500+组、
        # 5700+条)的根因。用 MySQL 命名锁保证同一时刻只有一个worker真正执行补拉/补扫，
        # 抢不到锁的worker直接跳过，从根上避免这个竞态（2026-07-15）。
        def _startup_catch_up():
            lock_conn = db.pool.connection()
            got_lock = False
            try:
                lock_cursor = lock_conn.cursor()
                lock_cursor.execute("SELECT GET_LOCK('wxbot_startup_catchup', 0)")
                got_lock = lock_cursor.fetchone()[0] == 1
                if not got_lock:
                    logger.info("启动补拉/补扫：其他worker进程正在执行，本进程跳过（避免多worker重复补扫产生重复记录）")
                    return
                try:
                    # 主企业补拉
                    logger.info("[车物家] 启动补拉：开始拉取中断期间的历史消息...")
                    fetcher.fetch_new_messages()
                    logger.info("[车物家] 启动补拉完成，开始补扫漏掉的保单 PDF...")
                    fetcher.rescan_missed_insurance(lookback_days=5)
                    logger.info("[车物家] 启动补扫完成")

                    # 额外企业补拉 + 补扫
                    for ef in extra_fetchers:
                        logger.info("[%s] 启动补拉：开始拉取中断期间的历史消息...", ef["name"])
                        ef["fetcher"].fetch_new_messages()
                        logger.info("[%s] 启动补拉完成，开始补扫漏掉的保单 PDF...", ef["name"])
                        ef["fetcher"].rescan_missed_insurance(lookback_days=5)
                        logger.info("[%s] 启动补扫完成", ef["name"])

                    # 预热报价绑定列表缓存
                    try:
                        bindings = quote_handler.binding_service.get_bindings()
                        logger.info("报价模块就绪：绑定列表 %d 条", len(bindings))
                    except Exception as e:
                        logger.warning("报价绑定列表预热失败: %s", e)
                except Exception as e:
                    logger.error("启动补拉/补扫失败: %s", e)
            finally:
                try:
                    if got_lock:
                        lock_cursor.execute("SELECT RELEASE_LOCK('wxbot_startup_catchup')")
                except Exception:
                    pass
                lock_conn.close()

        threading.Thread(target=_startup_catch_up, daemon=True).start()

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
