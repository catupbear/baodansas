#!/usr/bin/env python3
"""
仅启动保单识别服务（不启动微信 SDK / 回调 / 消息拉取）
本地开发用，端口 8878
"""
import logging
import yaml
from flask import Flask, redirect, render_template

from storage.db import Database
from insurance.db import init_insurance_tables
from insurance.monitor_config_db import init_monitor_config_table
from insurance.handler import InsuranceHandler
from insurance.api import insurance_bp, init_insurance_api
from auth.db import init_users_table
from auth.jwt_utils import init_jwt
from auth.decorators import init_auth_decorators
from auth.api import auth_bp, init_auth_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def main():
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    app = Flask(__name__, template_folder="web/templates")
    app.secret_key = "ocr-dev"

    db = Database(config["mysql"])

    # COS（可选）
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
            )
        except Exception as e:
            logging.warning("COS初始化失败: %s", e)

    # 初始化账号认证模块
    init_users_table(db)
    init_jwt(config.get("secret_key", "ocr-dev"))
    init_auth_decorators(db)
    init_auth_api(db)
    app.register_blueprint(auth_bp)

    # 初始化保单识别
    init_insurance_tables(db)
    init_monitor_config_table(db)
    handler = InsuranceHandler(
        db=db, cos_storage=cos_storage, contacts=None, app_config=config,
    )
    init_insurance_api(db, handler=handler)
    app.register_blueprint(insurance_bp)

    @app.route("/login")
    def login_page():
        return render_template("login.html")

    @app.route("/")
    def index():
        return render_template("insurance.html")

    @app.route("/insurance")
    def insurance_page():
        return redirect("/")

    @app.route("/monitor-config")
    def monitor_config_page():
        return render_template("monitor_config.html")

    @app.route("/admin/users")
    def admin_users_page():
        return render_template("admin_users.html")

    port = 8878
    logging.info("保单识别服务启动: http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)

if __name__ == "__main__":
    main()
