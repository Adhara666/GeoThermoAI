"""
GeoThermoAI 程序入口

启动 PyWebView 桌面应用，加载前端界面并暴露 Python API。
"""

import sys
import os

# 将项目根目录添加到 sys.path，确保跨目录 import 正常工作
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import webview

from ui.api import GeoThermoAPI


def _setup_app_user_model_id():
    """设置 Windows AppUserModelID，帮助任务栏正确识别应用图标。"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'GeoThermoAI.App'
        )
    except Exception:
        pass


def _cleanup_study_areas():
    """每次启动时清空研究区文件，确保不残留上一次会话的数据"""
    import shutil
    study_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'study_areas')
    if os.path.isdir(study_dir):
        shutil.rmtree(study_dir)
    os.makedirs(study_dir, exist_ok=True)


def start_ui():
    """启动UI"""
    _setup_app_user_model_id()
    _cleanup_study_areas()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(base_dir, 'ui', 'assets', 'logo.ico')

    api = GeoThermoAPI()
    window = webview.create_window(
        'GeoThermoAI',
        url=os.path.join(base_dir, 'ui', 'index.html'),
        js_api=api,
        width=1200,
        height=800,
        resizable=True,
        background_color='#f0f2f5',
        text_select=True,
    )
    webview.start(icon=icon_path)


if __name__ == "__main__":
    start_ui()
