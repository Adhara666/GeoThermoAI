"""
GeoThermoAI 前端 Demo — Gradio 6.x 正确 API 版
================================================
修复要点：
1. 不用 gr.update() — 直接返回值
2. theme/css 传给 launch() 不传 Blocks()
3. Chatbot 不用 type/bubble_full_width 参数
4. 事件绑定的 inputs/outputs 数量与函数参数/返回值严格匹配
运行：双击 启动前端demo.bat
"""
import json
import os
import re
import time

import numpy as np
import requests
import gradio as gr

try:
    import folium
    from folium.raster_layers import ImageOverlay
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ══════════════════════════════════════════
SYSTEM_PROMPT = "你是GeoThermoAI智能助手，专注地表温度降尺度。核心算法：TTRI、TCR。请用简洁专业的中文回答。"
WUHAN_CENTER = [30.59, 114.30]
WUHAN_BOUNDS = [[29.97, 113.75], [31.22, 114.85]]

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
.header-title p { margin: 2px 0 0 0 !important; line-height: 1.2 !important; }
/* 高级配置展开后最大高度 335px */
.advanced-accordion { max-height: 340px !important; overflow: auto !important; }
/* 三栏自然高度，不强制对齐 */
"""

# ══════════════════════════════════════════
# LLM API
# ══════════════════════════════════════════

def _call_openai(base_url, api_key, model_id, messages):
    """流式调用 OpenAI 格式，yield (thinking, content)"""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model_id, "messages": messages, "stream": True}
    thinking, content = "", ""
    with requests.post(url, headers=headers, json=payload, stream=True, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            t = line.decode("utf-8").strip()
            if not t.startswith("data: "):
                continue
            d = t[6:]
            if d == "[DONE]":
                break
            try:
                ch = json.loads(d)
                delta = ch.get("choices", [{}])[0].get("delta", {})
                rc = delta.get("reasoning_content") or ""
                c = delta.get("content") or ""
                if rc: thinking += rc
                if c: content += c
                if rc or c:
                    yield thinking, content
            except json.JSONDecodeError:
                pass
    yield thinking, content


def _call_anthropic(base_url, api_key, model_id, messages):
    """流式调用 Anthropic 格式，yield (thinking, content)"""
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {"x-api-key": api_key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    system, chat = "", []
    for m in messages:
        if m["role"] == "system":
            system += m["content"] + "\n"
        else:
            chat.append({"role": m["role"], "content": m["content"]})
    payload = {"model": model_id, "messages": chat, "max_tokens": 4096, "stream": True}
    if system.strip():
        payload["system"] = system.strip()
    content = ""
    with requests.post(url, headers=headers, json=payload, stream=True, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            t = line.decode("utf-8").strip()
            if not t.startswith("data: "):
                continue
            try:
                ev = json.loads(t[6:])
                if ev.get("type") == "content_block_delta":
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        content += d.get("text", "")
                        yield "", content
            except json.JSONDecodeError:
                pass
    yield "", content


def format_bubble(thinking, content, streaming=False, elapsed=0):
    parts = []
    thinking = (thinking or "").strip()
    content = content or ""
    if thinking:
        label = "思考中…" if (streaming and not content) else f"已深度思考（{elapsed:.1f}s）"
        o = " open" if (streaming and not content) else ""
        parts.append(f"<details{o}><summary>💭 {label}</summary>\n\n{thinking}\n\n</details>")
    if content:
        parts.append(content)
    elif streaming:
        parts.append("▍")
    return "\n\n".join(parts)


def strip_thinking(text):
    return re.sub(r"<details[^>]*>.*?</details>", "", text or "", flags=re.DOTALL).strip()

# ══════════════════════════════════════════
# 地图 & 图表
# ══════════════════════════════════════════

def build_map():
    if not HAS_FOLIUM:
        return "<p>folium 未安装</p>"
    m = folium.Map(location=WUHAN_CENTER, zoom_start=10, control_scale=True)
    rng = np.random.default_rng(42)
    h, w = 256, 256
    y, x = np.mgrid[0:h, 0:w]
    lst = 296.0 + 4.0 * (y / h)
    r = np.sqrt((x - w*0.48)**2 + (y - h*0.52)**2)
    lst += 7.0 * np.exp(-r**2 / (2*(w*0.22)**2))
    lst = (lst + rng.normal(0, 0.4, (h, w))).astype(np.float32)
    from matplotlib import cm
    v = lst[~np.isnan(lst)]
    n = np.clip((lst - v.min()) / (v.max() - v.min() + 1e-10), 0, 1)
    rgba = (cm.RdYlBu_r(n) * 255).astype(np.uint8)
    rgba[..., 3] = 200
    ImageOverlay(image=rgba, bounds=WUHAN_BOUNDS, opacity=0.7, name="LST 10m").add_to(m)
    folium.TileLayer("OpenStreetMap", name="街道").add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    return m._repr_html_()


def build_chart():
    if not HAS_MPL:
        return None
    fig, ax = plt.subplots(figsize=(6, 3))
    models = ["RF", "XGBoost", "LightGBM", "CatBoost", "ExtraTrees"]
    r2 = [0.8745, 0.8912, 0.8856, 0.8823, 0.8634]
    ax.bar(models, r2, color=["#4C72B0","#55A868","#C44E52","#8172B2","#CCB974"])
    ax.set_ylabel("R²"); ax.set_ylim(0.80, 0.92)
    ax.set_title("Benchmark 对比", fontsize=10)
    plt.tight_layout()
    return fig

# ══════════════════════════════════════════
# 事件处理 — 返回值数量严格匹配 outputs
# ══════════════════════════════════════════

# --- 项目管理 ---
# outputs: [proj_select(Dropdown), convs_state(State), proj_name(Textbox)]
# 返回3个值
def on_create_project(name, convs):
    name = (name or "").strip()
    if not name:
        gr.Warning("请输入项目名称")
        return gr.update(), convs, gr.update()
    if name in convs:
        gr.Warning("项目已存在")
        return gr.update(), convs, gr.update()
    convs = dict(convs)
    convs[name] = {}
    gr.Info(f"项目「{name}」创建成功")
    return gr.update(choices=list(convs.keys()), value=name), convs, ""

# outputs: [conv_select(Radio), proj_dir(Textbox)]
# 返回2个值
def on_select_project(pid, convs):
    if not pid or pid not in convs:
        return gr.update(choices=[], value=None), ""
    conv = convs[pid]
    choices = [(v["title"], k) for k, v in conv.items() if not k.startswith("__")]
    return gr.update(choices=choices, value=None), conv.get("__dir__", "")

# outputs: [conv_select(Radio), convs_state(State), conv_title(Textbox)]
# 返回3个值
def on_create_conv(pid, title, convs):
    title = (title or "").strip()
    if not pid or pid not in convs:
        gr.Warning("请先选择项目")
        return gr.update(), convs, gr.update()
    if not title:
        gr.Warning("请输入对话标题")
        return gr.update(), convs, gr.update()
    cid = f"conv_{len([k for k in convs[pid] if not k.startswith('__')])+1:03d}"
    convs = dict(convs)
    convs[pid] = dict(convs[pid])
    convs[pid][cid] = {"title": title, "messages": []}
    choices = [(v["title"], k) for k, v in convs[pid].items() if not k.startswith("__")]
    gr.Info(f"对话「{title}」创建成功")
    return gr.update(choices=choices, value=cid), convs, ""

# outputs: [chatbot(Chatbot), current_state(State), cur_label(Markdown)]
# 返回3个值
def on_select_conv(cid, pid, convs, current):
    if not pid or not cid or pid not in convs or cid not in convs[pid]:
        return [], current, "未选择对话"
    current = {"project": pid, "conv": cid}
    msgs = convs[pid][cid]["messages"]
    title = convs[pid][cid]["title"]
    return msgs, current, f"📄 {pid} / {title}"

# --- 项目目录 ---
# outputs: [convs_state(State)]
# 返回1个值
def on_save_dir(pid, path, convs):
    if not pid or pid not in convs:
        gr.Warning("请先选择项目")
        return convs
    convs = dict(convs)
    convs[pid] = dict(convs[pid])
    convs[pid]["__dir__"] = (path or "").strip()
    gr.Info("目录已保存")
    return convs

# --- API 设置 ---
# outputs: [settings_state(State), model_label(Markdown), api_status(Markdown)]
# 返回3个值
def on_save_api(fmt, url, key, mid, dname, cin, cout, settings):
    mid = (mid or "").strip()
    dname = (dname or "").strip() or mid
    settings = dict(settings)
    settings.update({
        "api_format": "anthropic" if "Anthropic" in fmt else "openai",
        "base_url": (url or "").strip(),
        "api_key": (key or "").strip(),
        "model_id": mid,
        "display_name": dname,
        "context_input": cin,
        "context_output": cout,
    })
    label = f"🟢 当前模型：**{dname}**" if mid else "⚪ 未配置模型"
    return settings, label, "✅ API 设置已保存并应用"

# outputs: [url_note(Markdown), base_url(Textbox)]
# 返回2个值
def on_fmt_change(fmt):
    if "Anthropic" in fmt:
        return "请填写 Claude API 地址，`/v1/messages` 会自动补到末尾。", gr.update(placeholder="e.g. https://api.anthropic.com")
    return "请填写 OpenAI API 地址，`/chat/completions` 会自动补到末尾。", gr.update(placeholder="e.g. https://api.openai.com/v1")

# --- 对话 ---
# outputs: [msg_input(Textbox), chatbot(Chatbot)]
# 返回2个值
def on_user_send(msg, history):
    if not msg or not msg.strip():
        return gr.update(), history
    return "", history + [{"role": "user", "content": msg}]

# outputs: [chatbot(Chatbot), convs_state(State)]
# 返回2个值（generator）
def on_bot_respond(history, settings, convs, current):
    history = history + [{"role": "assistant", "content": "▍"}]
    yield history, convs

    if not settings.get("api_key") or not settings.get("base_url") or not settings.get("model_id"):
        history[-1]["content"] = "⚠️ 请先在右侧「🔑 API 设置」配置模型。"
        _save(convs, current, history)
        yield history, convs
        return

    api_msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history[:-1]:
        # Gradio 6 可能将 content 存为 list，需转为 str
        raw = m["content"]
        if isinstance(raw, list):
            raw = "\n".join(str(x) if not isinstance(x, str) else x for x in raw)
        c = strip_thinking(raw)
        if c:
            api_msgs.append({"role": m["role"], "content": c})

    user_q = api_msgs[-1]["content"] if api_msgs else ""
    start = time.time()
    thinking, content = "", ""

    try:
        gen = _call_anthropic if settings["api_format"] == "anthropic" else _call_openai
        for thinking, content in gen(settings["base_url"], settings["api_key"], settings["model_id"], api_msgs):
            history[-1]["content"] = format_bubble(thinking, content, streaming=True)
            _save(convs, current, history)
            yield history, convs

        elapsed = time.time() - start
        if not thinking.strip():
            thinking = f"1. 解析用户意图\n2. 检索领域知识\n3. 生成回复\n\n*（模拟，{elapsed:.1f}s）*"
        history[-1]["content"] = format_bubble(thinking, content, streaming=False, elapsed=elapsed)
        _save(convs, current, history)
        yield history, convs
    except Exception as e:
        history[-1]["content"] = f"⚠️ API 调用失败：{e}\n\n请检查 Base URL、API Key、模型 ID。"
        _save(convs, current, history)
        yield history, convs


def _save(convs, current, history):
    pid, cid = current.get("project"), current.get("conv")
    if pid and cid and pid in convs and cid in convs[pid]:
        convs[pid][cid]["messages"] = history

# ══════════════════════════════════════════
# UI
# ══════════════════════════════════════════

def build_ui():
    logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")

    with gr.Blocks(title="GeoThermoAI") as demo:
        # gr.State 必须在 Blocks 上下文内创建（Gradio 6 要求）
        convs_state = gr.State({})
        current_state = gr.State({"project": None, "conv": None})
        settings_state = gr.State({
            "api_format": "openai", "base_url": "", "api_key": "",
            "model_id": "", "display_name": "", "context_input": None, "context_output": None,
        })
        # Header（logo+标题合并到单个 HTML，完全绕过 Gradio 组件间距）
        if os.path.exists(logo):
            import base64
            with open(logo, "rb") as f:
                logo_b64 = base64.b64encode(f.read()).decode()
            header_html = (
                f'<div style="display:flex;align-items:center;gap:6px;">'
                f'<img src="data:image/png;base64,{logo_b64}" style="height:65px;width:70px;object-fit:contain;flex-shrink:0;"/>'
                f'<div style="flex:1;">'
                f'<h2 style="margin:0;line-height:1.2;">GeoThermoAI</h2>'
                f'<p style="margin:2px 0 0 0;line-height:1.2;font-size:14px;color:#888;">基于跨尺度热响应一致性的高分辨率地表温度智能重建系统</p>'
                f'</div></div>'
            )
        else:
            header_html = "<h2>GeoThermoAI</h2>"
        gr.HTML(header_html)

        with gr.Row(elem_classes="main-row"):
            with gr.Column(scale=25, min_width=240):
                gr.Markdown("### 📁 项目")
                with gr.Row():
                    proj_name = gr.Textbox(placeholder="项目名称", max_lines=1, show_label=False, scale=4)
                    create_proj_btn = gr.Button("创建", size="sm", variant="primary", scale=1)
                proj_select = gr.Dropdown(label="选择项目", choices=[], interactive=True)

                gr.Markdown("### 💬 对话")
                with gr.Row():
                    conv_title = gr.Textbox(placeholder="对话标题", max_lines=1, show_label=False, scale=4)
                    create_conv_btn = gr.Button("创建", size="sm", variant="primary", scale=1)
                conv_select = gr.Radio(label="对话列表", choices=[], interactive=True)

                gr.Markdown("---")
                gr.Markdown("### 📂 项目保存路径")
                proj_dir = gr.Textbox(label="路径", placeholder="D:/output", max_lines=1, info="手动输入或粘贴文件夹路径")
                save_dir_btn = gr.Button("保存路径", size="sm")

                gr.Markdown("---")
                gr.Markdown("### 📎 研究区域")
                gr.File(label="上传", file_types=[".geojson", ".json", ".shp", ".zip"], height=90)

            # ── 中：对话（45%）──
            with gr.Column(scale=45, min_width=360):
                cur_label = gr.Markdown("📄 未选择对话")
                model_label = gr.Markdown("⚪ 未配置模型（请在右侧「🔑 API 设置」配置）")
                chatbot = gr.Chatbot(value=[], height=705, show_label=False)
                with gr.Row(elem_classes="chat-input-row"):
                    msg_input = gr.Textbox(placeholder="输入消息后按 Enter 或点击发送", max_lines=1, show_label=False, scale=20)
                    send_btn = gr.Button("发送", variant="primary", min_width=70)
                    clear_btn = gr.Button("清空", min_width=70)

            # ── 右：工作面板（30%）──
            with gr.Column(scale=30, min_width=310):
                gr.Markdown("### 📊 工作面板")
                with gr.Tabs():
                    with gr.Tab("🔑 API 设置"):
                        api_fmt = gr.Dropdown(["OpenAI Chat Completions 格式", "Anthropic Messages 格式"], value="OpenAI Chat Completions 格式", label="API 格式")
                        url_note = gr.Markdown("请填写 OpenAI API 地址，`/chat/completions` 会自动补到末尾。")
                        base_url = gr.Textbox(label="请求地址", placeholder="e.g. https://api.openai.com/v1")
                        model_id = gr.Textbox(label="模型 ID")
                        api_key = gr.Textbox(label="API 密钥", type="password")
                        with gr.Accordion("高级", open=True, elem_classes="advanced-accordion"):
                            disp_name = gr.Textbox(label="展示名称", placeholder="留空则用模型 ID", max_lines=1)
                            with gr.Row():
                                ctx_in = gr.Number(label="上下文-输入", value=184000, precision=0)
                                ctx_out = gr.Number(label="上下文-输出", value=16000, precision=0)

                    with gr.Tab("⚙️ 参数"):
                        gr.Dropdown(["Random Forest","XGBoost","LightGBM","CatBoost","Extra Trees"], value="Random Forest", label="机器学习方法")
                        gr.Slider(50, 1000, value=200, step=50, label="n_estimators")
                        gr.Slider(5, 50, value=25, step=1, label="max_depth")
                        gr.Slider(2, 50, value=10, step=1, label="min_samples_split")
                        gr.Slider(1, 20, value=5, step=1, label="min_samples_leaf")

                    with gr.Tab("🌍 地图"):
                        map_html = gr.HTML(build_map())

                    with gr.Tab("📊 精度"):
                        gr.Dataframe(value=[["R²","0.8745"],["RMSE","1.2345"],["MAE","0.9876"]], headers=["指标","值"], interactive=False)

                    with gr.Tab("📈 对比"):
                        gr.Plot(build_chart())

                    with gr.Tab("📋 进度"):
                        gr.Dataframe(value=[["1.数据获取","✅"],["2.预处理","✅"],["3.TTRI","🔄"],["4.模型","⏳"],["5.TCR","⏳"],["6.导出","⏳"],["7.评估","⏳"]], headers=["步骤","状态"], interactive=False)

                # 保存按钮移到 Tabs 外面，直接作为 Column 子元素，margin-top:auto 才能生效
                save_api_btn = gr.Button("保存并应用", variant="primary")
                api_status = gr.Markdown("")

        gr.Markdown("<div style='color:#888;font-size:11px'>数据源: Microsoft Planetary Computer</div>")

        # ── 事件绑定（inputs/outputs 严格匹配函数签名）──
        # 创建项目: 2 inputs → 3 outputs
        create_proj_btn.click(on_create_project, [proj_name, convs_state], [proj_select, convs_state, proj_name])
        # 选择项目: 2 inputs → 2 outputs
        proj_select.change(on_select_project, [proj_select, convs_state], [conv_select, proj_dir])
        # 创建对话: 3 inputs → 3 outputs
        create_conv_btn.click(on_create_conv, [proj_select, conv_title, convs_state], [conv_select, convs_state, conv_title])
        # 选择对话: 4 inputs → 3 outputs
        conv_select.change(on_select_conv, [conv_select, proj_select, convs_state, current_state], [chatbot, current_state, cur_label])
        # 保存目录: 3 inputs → 1 output
        save_dir_btn.click(on_save_dir, [proj_select, proj_dir, convs_state], [convs_state])
        # API 格式切换: 1 input → 2 outputs
        api_fmt.change(on_fmt_change, [api_fmt], [url_note, base_url])
        # 保存API: 8 inputs → 3 outputs
        save_api_btn.click(on_save_api, [api_fmt, base_url, api_key, model_id, disp_name, ctx_in, ctx_out, settings_state], [settings_state, model_label, api_status])

        # 对话发送
        send_btn.click(on_user_send, [msg_input, chatbot], [msg_input, chatbot]).then(
            on_bot_respond, [chatbot, settings_state, convs_state, current_state], [chatbot, convs_state])
        msg_input.submit(on_user_send, [msg_input, chatbot], [msg_input, chatbot]).then(
            on_bot_respond, [chatbot, settings_state, convs_state, current_state], [chatbot, convs_state])
        clear_btn.click(lambda: [], None, chatbot)

    return demo


if __name__ == "__main__":
    demo = build_ui()
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True, theme=gr.themes.Soft(), css=CSS)
