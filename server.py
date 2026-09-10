"""轻量服务入口。spawn 重导入本文件时不会载入 Web 后端、算法或记忆。"""


def __getattr__(name):
    # 保留 import server 后访问 app/AppBackend 的兼容接口。
    from core import web_app
    return getattr(web_app, name)


def main():
    from core.web_app import main as serve
    serve()


if __name__ == "__main__":
    main()
