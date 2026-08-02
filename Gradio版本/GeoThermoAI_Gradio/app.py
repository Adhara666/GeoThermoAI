"""
GeoThermoAI Gradio 应用入口
================================================
基于前端 demo（视觉设计/前端demo/app.py）的三栏布局，
集成旧版 PyWebView API 业务逻辑（ui/api.py GradioAPI）和真实地图渲染（core.visualization）。

运行：python app.py
默认：http://127.0.0.1:7860
"""
import base64
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import gradio as gr

from ui.api import (
    GradioAPI, SYSTEM_PROMPT, WORKFLOW_STEPS,
    format_bubble, strip_thinking, LayerVisualizer,
)

# ── CSS ──────────────────────────────────────────────────────

CSS = """
details { font-size: 0.85em; margin-bottom: 8px; }
details summary { cursor: pointer; color: #8a8f98; }
details[open] > *:not(summary) { color: #9aa0a8; }
/* 文件上传区域字体缩小 */
.svelte-8prmba .wrap { font-size: 0.7em; }
.svelte-8prmba .icon-wrap { display: grid !important; place-items: center !important; }
.svelte-8prmba .icon-wrap svg { width: 60%; height: 60%; }
/* logo 固定宽 + 隐藏分享/下载/全屏按钮 */
.image-container.svelte-12vrxzd { width: 70px !important; min-width: 70px !important; max-width: 70px !important; }
.image-container.svelte-12vrxzd button { width: 70px !important; justify-content: flex-start !important; }
.icon-button-wrapper { display: none !important; }
/* 对话输入行 */
.chat-input-row input { height: 40px !important; padding: 10px !important; overflow: hidden !important; }
.chat-input-row .block { height: 60px !important; display: grid !important; place-items: center !important; }
.chat-input-row button { height: 60px !important; min-width: 70px !important; max-width: 70px !important; }
/* header 行：logo 与标题紧贴 + 垂直居中 */
.header-row { gap: 0 !important; padding: 0 !important; align-items: center !important; }
.header-row > div { padding: 0 !important; margin: 0 !important; }
.header-row .image-container, .header-row .image-container > button, .header-row .image-container .image-frame { padding: 0 !important; margin: 0 !important; }
.header-title { margin: 0 !important; padding: 0 0 0 4px !important; }
.header-title h2 { margin: 0 !important; line-height: 1.2 !important; }
.header-title p { margin: 2px 0 0 0 !important; line-height: 1.2 !important; font-size: 14px; color: #888; }
/* 高级配置展开后最大高度 340px */
.advanced-accordion { max-height: 340px !important; overflow: auto !important; }
/* 影像配对选择卡片 */
.pair-select-box { border: 1px solid #e0e0e0 !important; border-radius: 8px !important; padding: 8px 12px !important; margin: 6px 0 !important; background: #fafbfc !important; }
.pair-select-box .radio { gap: 4px !important; }
/* 三栏自然高度 */
"""


# ── 头部 HTML（logo + 标题） ─────────────────────────────────

def build_header_html() -> str:
    """生成 logo + 标题的合并 HTML，绕过 Gradio 组件间距"""
    # 优先使用 demo 目录下的 logo，其次旧版 ui/assets/logo.png
    candidates = [
        _ROOT / "logo.png",
        _ROOT.parent / "视觉设计" / "前端demo" / "logo.png",
        _ROOT.parent / "旧版-供参考" / "GeoThermoAI（不含output）" / "ui" / "assets" / "logo.png",
    ]
    logo_path = None
    for p in candidates:
        if p.exists():
            logo_path = p
            break
    if logo_path:
        with open(logo_path, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode()
        return (
            f'<div style="display:flex;align-items:center;gap:6px;">'
            f'<img src="data:image/png;base64,{logo_b64}" style="height:65px;width:70px;object-fit:contain;flex-shrink:0;"/>'
            f'<div style="flex:1;">'
            f'<h2 style="margin:0;line-height:1.2;">GeoThermoAI</h2>'
            f'<p style="margin:2px 0 0 0;line-height:1.2;font-size:14px;color:#888;">'
            f'基于跨尺度热响应一致性的高分辨率地表温度智能重建系统</p>'
            f'</div></div>'
        )
    return "<h2>GeoThermoAI</h2>"


# ── 工作流步骤标签 ───────────────────────────────────────────

WORKFLOW_LABELS = {
    "data_acquisition": "1. 数据获取",
    "data_pipeline": "2. 数据预处理",
    "ttri_compute": "3. TTRI 计算",
    "rf_model": "4. RF 模型训练",
    "tcr_compute": "5. TCR 计算",
    "lst_export": "6. LST 导出",
    "accuracy_eval": "7. 精度评估",
}


def build_workflow_rows():
    """生成工作流步骤的初始 dataframe 数据"""
    return [[WORKFLOW_LABELS.get(s, s), "⏳"] for s in WORKFLOW_STEPS]


def build_accuracy_rows():
    """精度面板初始数据"""
    return [["R²", "—"], ["RMSE", "—"], ["MAE", "—"], ["样本数", "—"]]


# ── UI 构建 ──────────────────────────────────────────────────

def build_ui(api: GradioAPI):
    # 启动时从磁盘加载对话
    initial_convs = api.load_conversations_from_disk()
    # 启动时加载 API 配置
    initial_settings = api.get_initial_settings()
    initial_api_form = api.get_initial_api_form_values()
    # 启动时加载 Data Space 配置
    initial_ds_cfg = api._load_settings().get("data_space", {})

    with gr.Blocks(title="GeoThermoAI", css=CSS) as demo:
        # ── State ───────────────────────────────────────────
        convs_state = gr.State(initial_convs)
        current_state = gr.State({"project": None, "conv": None})
        settings_state = gr.State(initial_settings)

        # ── Header ─────────────────────────────────────────
        gr.HTML(build_header_html())

        with gr.Row(elem_classes="main-row"):
            # ── 左栏 25%：项目管理 + 对话 + 路径 + 研究区 ──
            with gr.Column(scale=25, min_width=240):
                gr.Markdown("### 📁 项目")
                with gr.Row():
                    proj_name = gr.Textbox(placeholder="项目名称", max_lines=1, show_label=False, scale=4)
                    create_proj_btn = gr.Button("创建", size="sm", variant="primary", scale=1)
                # 启动时自动选中第一个项目，并预填其对话列表
                auto_project = next(iter(initial_convs), None)
                auto_conv_choices = []
                if auto_project:
                    auto_conv_choices = [
                        (v["title"], k) for k, v in initial_convs[auto_project].items()
                        if not k.startswith("__")
                    ]
                proj_select = gr.Dropdown(
                    label="选择项目",
                    choices=list(initial_convs.keys()),
                    value=auto_project,
                    interactive=True,
                )

                gr.Markdown("### 💬 对话")
                with gr.Row():
                    conv_title = gr.Textbox(placeholder="对话标题", max_lines=1, show_label=False, scale=4)
                    create_conv_btn = gr.Button("创建", size="sm", variant="primary", scale=1)
                conv_select = gr.Radio(label="对话列表", choices=auto_conv_choices, interactive=True)
                delete_conv_btn = gr.Button("🗑️ 删除当前对话", size="sm")

                gr.Markdown("---")
                gr.Markdown("### 📂 项目保存路径")
                proj_dir = gr.Textbox(
                    label="路径",
                    placeholder="D:/GeoThermoAI/output/wuhan_202407",
                    max_lines=1,
                    value=(initial_convs.get(auto_project, {}).get("__dir__", "") if auto_project else ""),
                    info="手动输入或粘贴文件夹路径",
                )
                save_dir_btn = gr.Button("💾 保存路径", size="sm")

                gr.Markdown("---")
                gr.Markdown("### 📎 研究区域")
                study_area_file = gr.File(
                    label="上传研究区",
                    file_count="multiple",
                    file_types=[".geojson", ".json", ".shp", ".dbf", ".shx", ".prj", ".zip"],
                    height=90,
                )
                upload_area_btn = gr.Button("📤 上传", size="sm")
                study_area_status = gr.Markdown(api.list_study_areas())

            # ── 中栏 45%：对话 ──────────────────────────────
            with gr.Column(scale=45, min_width=360):
                cur_label = gr.Markdown("📄 未选择对话")
                model_label = gr.Markdown(
                    f"🟢 当前模型：**{initial_api_form['display_name'] or initial_api_form['model_id']}**"
                    if initial_api_form["model_id"] else "⚪ 未配置模型（请在右侧「🔑 API 设置」配置）"
                )
                chatbot = gr.Chatbot(value=[], height=705, show_label=False)
                # 影像配对选择卡片（默认隐藏，agent pause 时显示）
                # 注：不用 gr.Column(visible=False)，Gradio 6.8.0 有 bug（6.20.0 才修复），
                # 多次 yield 的可见性更新不会生效，改用 gr.Group 绕过
                with gr.Group(visible=False, elem_classes="pair-select-box") as pair_select_box:
                    gr.Markdown("### 📋 影像配对选择")
                    pair_radio = gr.Radio(label="请选择一组配对", choices=[], interactive=True)
                    pair_confirm_btn = gr.Button("✅ 确认选择", variant="primary", size="sm")
                with gr.Row(elem_classes="chat-input-row"):
                    msg_input = gr.Textbox(
                        placeholder="输入消息后按 Enter 或点击发送",
                        max_lines=1,
                        show_label=False,
                        scale=20,
                    )
                    send_btn = gr.Button("发送", variant="primary", min_width=70)
                    clear_btn = gr.Button("清空", min_width=70)

            # ── 右栏 30%：工作面板 ─────────────────────────
            with gr.Column(scale=30, min_width=310):
                gr.Markdown("### 📊 工作面板")
                with gr.Tabs():
                    with gr.Tab("🔑 API 设置"):
                        api_fmt = gr.Dropdown(
                            ["OpenAI Chat Completions 格式", "Anthropic Messages 格式"],
                            value=initial_api_form["fmt"],
                            label="API 格式",
                        )
                        url_note = gr.Markdown(_fmt_note_text(initial_api_form["fmt"]))
                        base_url = gr.Textbox(
                            label="请求地址",
                            placeholder="e.g. https://api.openai.com/v1",
                            value=initial_api_form["base_url"],
                        )
                        model_id = gr.Textbox(
                            label="模型 ID",
                            value=initial_api_form["model_id"],
                        )
                        api_key = gr.Textbox(
                            label="API 密钥",
                            type="password",
                            value=initial_api_form["api_key"],
                        )
                        with gr.Accordion("高级", open=True, elem_classes="advanced-accordion"):
                            disp_name = gr.Textbox(
                                label="展示名称",
                                placeholder="留空则用模型 ID",
                                max_lines=1,
                                value=initial_api_form["display_name"],
                            )
                            with gr.Row():
                                ctx_in = gr.Number(
                                    label="上下文-输入",
                                    value=initial_api_form["context_input"],
                                    precision=0,
                                )
                                ctx_out = gr.Number(
                                    label="上下文-输出",
                                    value=initial_api_form["context_output"],
                                    precision=0,
                                )

                    with gr.Tab("⚙️ 参数"):
                        # 模型超参（写入 config/settings.json 的 model 分组）
                        model_method = gr.Dropdown(
                            ["Random Forest"], value="Random Forest", label="机器学习方法",
                        )
                        n_estimators = gr.Slider(50, 1000, value=200, step=50, label="n_estimators")
                        max_depth = gr.Slider(5, 50, value=25, step=1, label="max_depth")
                        min_samples_split = gr.Slider(2, 50, value=16, step=1, label="min_samples_split")
                        min_samples_leaf = gr.Slider(1, 20, value=8, step=1, label="min_samples_leaf")
                        save_params_btn = gr.Button("💾 保存参数", size="sm")
                        params_status = gr.Markdown("")

                    with gr.Tab("🛰️ 数据源"):
                        gr.Markdown(
                            "**Sentinel-2** 可切换到 Copernicus Data Space（国内下载更快）。\n\n"
                            "在 [dataspace.copernicus.eu](https://dataspace.copernicus.eu/) 免费注册后，"
                            "进入 *Dashboard → My Account → OAuth2 Clients* 创建应用，即可获得 Client ID 和 Client Secret。\n\n"
                            "填写保存后生效；留空则 Sentinel-2 继续使用 Planetary Computer。"
                        )
                        ds_client_id = gr.Textbox(label="Data Space Client ID", value=initial_ds_cfg.get("client_id", ""))
                        ds_client_secret = gr.Textbox(
                            label="Data Space Client Secret", type="password",
                            value=initial_ds_cfg.get("client_secret", ""),
                        )
                        save_ds_btn = gr.Button("💾 保存数据源配置", size="sm")
                        ds_status = gr.Markdown("")

                    with gr.Tab("🌍 地图"):
                        map_html = gr.HTML(LayerVisualizer.build_empty_map())
                        refresh_map_btn = gr.Button("🔄 刷新地图", size="sm")

                    with gr.Tab("📊 精度"):
                        accuracy_df = gr.Dataframe(
                            value=build_accuracy_rows(),
                            headers=["指标", "值"],
                            interactive=False,
                            wrap=True,
                        )
                        refresh_acc_btn = gr.Button("🔄 刷新精度", size="sm")

                    with gr.Tab("📋 进度"):
                        workflow_df = gr.Dataframe(
                            value=build_workflow_rows(),
                            headers=["步骤", "状态"],
                            interactive=False,
                            wrap=True,
                        )
                        refresh_wf_btn = gr.Button("🔄 刷新进度", size="sm")

                # 保存按钮在 Tabs 外（与 demo 一致）
                save_api_btn = gr.Button("保存并应用", variant="primary")
                api_status = gr.Markdown("")

        gr.Markdown("<div style='color:#888;font-size:11px'>数据源: Microsoft Planetary Computer</div>")

        # ── 事件绑定 ────────────────────────────────────────

        # 创建项目
        create_proj_btn.click(
            api.create_project, [proj_name, convs_state],
            [proj_select, convs_state, proj_name],
        )
        # 选择项目
        proj_select.change(
            api.select_project, [proj_select, convs_state],
            [conv_select, proj_dir],
        )
        # 创建对话
        create_conv_btn.click(
            api.create_conversation, [proj_select, conv_title, convs_state],
            [conv_select, convs_state, conv_title],
        )
        # 选择对话
        conv_select.change(
            api.select_conversation, [conv_select, proj_select, convs_state, current_state],
            [chatbot, current_state, cur_label],
        )
        # 删除对话
        delete_conv_btn.click(
            api.delete_conversation, [conv_select, proj_select, convs_state, current_state],
            [convs_state, current_state, chatbot, cur_label, conv_select],
        )

        # 保存项目目录
        save_dir_btn.click(
            api.save_project_dir, [proj_select, proj_dir, convs_state],
            [convs_state, proj_dir],
        )

        # 研究区上传
        upload_area_btn.click(
            api.upload_study_area, [study_area_file], [study_area_status],
        )

        # API 格式切换：更新 url_note 和 base_url placeholder
        api_fmt.change(
            on_fmt_change, [api_fmt], [url_note, base_url],
        )
        # 保存 API 设置
        save_api_btn.click(
            api.save_api_settings,
            [api_fmt, base_url, api_key, model_id, disp_name, ctx_in, ctx_out, settings_state],
            [settings_state, model_label, api_status],
        )

        # 保存模型参数
        save_params_btn.click(
            on_save_model_params,
            [n_estimators, max_depth, min_samples_split, min_samples_leaf],
            [params_status],
        )

        # 保存 Data Space 数据源配置
        save_ds_btn.click(
            on_save_dataspace,
            [ds_client_id, ds_client_secret],
            [ds_status],
        )

        # 刷新地图/精度/进度
        refresh_map_btn.click(
            api.build_map_html, [current_state], [map_html],
        )
        refresh_acc_btn.click(
            api.get_accuracy_summary, [current_state], [accuracy_df],
        )
        refresh_wf_btn.click(
            api.get_workflow_status, [current_state], [workflow_df],
        )

        # 对话发送：先 user_send，再 bot_respond
        send_btn.click(
            api.user_send, [msg_input, chatbot], [msg_input, chatbot],
        ).then(
            api.bot_respond,
            [chatbot, settings_state, convs_state, current_state],
            [chatbot, convs_state, workflow_df, accuracy_df, pair_select_box, pair_radio],
        )
        msg_input.submit(
            api.user_send, [msg_input, chatbot], [msg_input, chatbot],
        ).then(
            api.bot_respond,
            [chatbot, settings_state, convs_state, current_state],
            [chatbot, convs_state, workflow_df, accuracy_df, pair_select_box, pair_radio],
        )
        # 影像配对选择确认：恢复 agent 执行
        pair_confirm_btn.click(
            api.resume_pair_select,
            [chatbot, convs_state, current_state, pair_radio],
            [chatbot, convs_state, workflow_df, accuracy_df, pair_select_box, pair_radio],
        )
        # 清空对话（仅清空 UI，不删除磁盘文件）
        clear_btn.click(lambda: ([], gr.update(visible=False), gr.update()), None, [chatbot, pair_select_box, pair_radio])

        # 页面加载/刷新：从磁盘重新加载对话，保证删除/创建与磁盘一致
        demo.load(
            api.reload_conversations,
            None,
            [convs_state, proj_select, conv_select, proj_dir],
        )

    return demo


# ── 事件回调（独立函数，需访问 api 实例时通过闭包） ───────────

def on_fmt_change(fmt_label: str):
    """API 格式切换：更新提示文案和 placeholder"""
    import gradio as gr
    if "Anthropic" in (fmt_label or ""):
        return (
            "请填写 Claude API 地址，`/v1/messages` 会自动补到末尾。",
            gr.update(placeholder="e.g. https://api.anthropic.com"),
        )
    return (
        "请填写 OpenAI API 地址，`/chat/completions` 会自动补到末尾。",
        gr.update(placeholder="e.g. https://api.openai.com/v1"),
    )


def _fmt_note_text(fmt_label: str) -> str:
    if "Anthropic" in (fmt_label or ""):
        return "请填写 Claude API 地址，`/v1/messages` 会自动补到末尾。"
    return "请填写 OpenAI API 地址，`/chat/completions` 会自动补到末尾。"


def on_save_model_params(n_est, max_d, min_ss, min_sl):
    """保存模型超参到 config/settings.json 的 model 分组"""
    import json
    settings_path = _ROOT / "config" / "settings.json"
    try:
        if settings_path.exists():
            with open(settings_path, "r", encoding="utf-8") as f:
                s = json.load(f)
        else:
            s = {}
        s.setdefault("model", {})
        s["model"].update({
            "n_estimators": int(n_est),
            "max_depth": int(max_d),
            "min_samples_split": int(min_ss),
            "min_samples_leaf": int(min_sl),
        })
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        return "✅ 参数已保存到 config/settings.json"
    except Exception as e:
        return f"❌ 保存失败: {e}"


def on_save_dataspace(client_id: str, client_secret: str):
    """保存 Copernicus Data Space 配置到 config/settings.json 的 data_space 分组"""
    import json
    settings_path = _ROOT / "config" / "settings.json"
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    try:
        if settings_path.exists():
            with open(settings_path, "r", encoding="utf-8") as f:
                s = json.load(f)
        else:
            s = {}
        s["data_space"] = {
            "client_id": client_id,
            "client_secret": client_secret,
        }
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        if client_id and client_secret:
            return "✅ Data Space 配置已保存，Sentinel-2 将使用 Data Space 下载"
        return "⚠️ 已清空 Data Space 配置，Sentinel-2 回退 Planetary Computer"
    except Exception as e:
        return f"❌ 保存失败: {e}"


# ── 入口 ─────────────────────────────────────────────────────

def main():
    api = GradioAPI()
    demo = build_ui(api)
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        theme=gr.themes.Soft(),
    )


if __name__ == "__main__":
    main()
