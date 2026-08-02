/**
 * GeoThermoAI - 主脚本
 * 页面初始化、视图切换、事件绑定、工具函数
 */

/* ── 全局状态 ──────────────────────────────── */
const App = {
    currentView: 'chat',        // 'chat' | 'panel'
    workflowPollingTimer: null,
    pywebviewReady: false,
};

/* ── 等待 pywebview 就绪 ──────────────────── */
function waitForPywebview() {
    return new Promise((resolve) => {
        if (window.pywebview && window.pywebview.api) {
            resolve();
            return;
        }
        window.addEventListener('pywebviewready', () => {
            resolve();
        });
    });
}

/* ── Toast 通知 ────────────────────────────── */
function showToast(message, type = 'info', duration = 3000) {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast toast--${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = '0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

/* ─ 视图切换 ──────────────────────────────── */
function switchView(viewName) {
    App.currentView = viewName;

    // 更新视图可见性
    document.querySelectorAll('.view').forEach((el) => {
        el.classList.toggle('view--active', el.dataset.view === viewName);
    });

    // 更新导航按钮激活态
    document.querySelectorAll('[data-nav]').forEach((btn) => {
        btn.classList.toggle('btn--primary', btn.dataset.nav === viewName);
        btn.classList.toggle('btn--outline', btn.dataset.nav !== viewName);
    });

    // 切换到面板时加载配置、结果和启动轮询
    if (viewName === 'panel') {
        PanelManager.loadConfig();
        PanelManager.loadPanelResult();
        fetchWorkflowLabels();
        startWorkflowPolling();
    } else {
        stopWorkflowPolling();
    }
}

/* ── 工作流状态轮询 ────────────────────────── */
function startWorkflowPolling() {
    stopWorkflowPolling();
    App.workflowPollingTimer = setInterval(async () => {
        try {
            const convId = (typeof ChatManager !== 'undefined' && ChatManager.conversationId) ? ChatManager.conversationId : null;
            const status = await window.pywebview.api.get_workflow_status(convId);
            updateWorkflowBar(status);
        } catch (e) {
            // 静默失败
        }
    }, 1500);
}

function stopWorkflowPolling() {
    if (App.workflowPollingTimer) {
        clearInterval(App.workflowPollingTimer);
        App.workflowPollingTimer = null;
    }
}

/* ── 更新工作流状态条 ──────────────────────── */
const STEP_LABELS = {
    data_acquisition: '数据获取',
    data_pipeline: '预处理',
    ttri_compute: 'TTRI计算',
    rf_model: '模型训练',
    tcr_compute: 'TCR修正',
    lst_export: 'LST导出',
    accuracy_eval: '精度评估',
};

// 动态工作流标签（根据加载的 Skill 更新）
let workflowLabels = {};

async function fetchWorkflowLabels() {
    try {
        workflowLabels = await window.pywebview.api.get_workflow_step_labels();
    } catch (e) {
        workflowLabels = {};
    }
}

function updateWorkflowBar(status) {
    const bar = document.getElementById('workflow-bar');
    if (!bar) return;

    const steps = status.steps || [];
    if (steps.length === 0) {
        bar.innerHTML = '<span class="workflow-bar__step workflow-bar__step--pending">等待执行...</span>';
        return;
    }

    let html = '';
    steps.forEach((step, i) => {
        // 优先使用动态标签，否则用默认标签
        const label = workflowLabels[step.name] || STEP_LABELS[step.name] || step.name;
        const statusClass = `workflow-bar__step--${step.status}`;
        const icon = step.status === 'completed' ? '&#10003;' :
                     step.status === 'running' ? '&#9679;' :
                     step.status === 'failed' ? '&#10007;' : '&#9675;';
        html += `<span class="workflow-bar__step ${statusClass}">${icon} ${label}</span>`;
        if (i < steps.length - 1) {
            html += '<span class="workflow-bar__arrow">&#8594;</span>';
        }
    });

    bar.innerHTML = html;
}

/* ── 时间格式化 ───────────────────────────── */
function formatTime(date) {
    const d = date || new Date();
    return d.toTimeString().slice(0, 8);
}

/* ── 简易 Markdown 转 HTML ─────────────────── */
function renderMarkdown(text) {
    if (!text) return '';

    // ── 第一步：提取 LaTeX 公式，防止被 Markdown 正则破坏 ──
    const latexBlocks = [];
    let html = text;

    // 提取 $$...$$ 块级公式
    html = html.replace(/\$\$([\s\S]*?)\$\$/g, (_, tex) => {
        const idx = latexBlocks.length;
        latexBlocks.push('$$' + tex + '$$');
        return '\x00LATEX_' + idx + '\x00';
    });

    // 提取 $...$ 行内公式
    html = html.replace(/\$([^\n$]+?)\$/g, (_, tex) => {
        const idx = latexBlocks.length;
        latexBlocks.push('$' + tex + '$');
        return '\x00LATEX_' + idx + '\x00';
    });

    // ── 第二步：按段落分割处理 ──
    // 1. 连续 3+ 个换行 降级为 2 个换行（消除多余空行）
    html = html.replace(/\n{3,}/g, '\n\n');
    // 2. 清除标题前后可能产生的多余空行，确保标题紧跟内容或正常分段
    html = html.replace(/\n{1,}(#{1,4}\s)/g, '\n$1');
    html = html.replace(/(#{1,4}\s[^\n]+)\n{1,}/g, '$1\n');

    // 按 \n 分割为行
    const allLines = html.split('\n');

    // 收集最终 HTML 行
    const outputLines = [];

    for (let i = 0; i < allLines.length; i++) {
        const line = allLines[i];

        // 段落分隔标记：直接输出 spacer
        if (line.trim() === '\x01') {
            outputLines.push('\x01');
            continue;
        }

        // 空行跳过
        if (line.trim() === '') continue;

        // 代码块 ```...```
        if (line.trim().startsWith('```')) {
            // 收集代码块内容直到结束
            let codeContent = '';
            const lang = line.trim().replace(/^```/, '');
            i++;
            while (i < allLines.length && !allLines[i].trim().startsWith('```')) {
                codeContent += (codeContent ? '\n' : '') + allLines[i];
                i++;
            }
            outputLines.push('<pre><code class="lang-' + lang + '">' + escapeHtml(codeContent.trim()) + '</code></pre>');
            continue;
        }

        // 标题 ### / ## / #
        let m;
        if ((m = line.match(/^### (.+)$/))) {
            outputLines.push('<h4>' + m[1] + '</h4>');
            continue;
        }
        if ((m = line.match(/^## (.+)$/))) {
            outputLines.push('<h3>' + m[1] + '</h3>');
            continue;
        }
        if ((m = line.match(/^# (.+)$/))) {
            outputLines.push('<h2>' + m[1] + '</h2>');
            continue;
        }

        // 分割线 --- 或 *** 或 ___
        if (/^[-*_]{3,}\s*$/.test(line.trim())) {
            outputLines.push('<hr>');
            continue;
        }

        // 表格行：收集连续的 |...| 行
        if (/^\|(.+)\|$/.test(line.trim())) {
            const tableRows = [];
            while (i < allLines.length && /^\|(.+)\|$/.test(allLines[i].trim())) {
                const trimmed = allLines[i].trim();
                if (!/^\|[\s\-:|]+\|$/.test(trimmed)) {
                    tableRows.push(trimmed);
                }
                i++;
            }
            i--; // 回退一行
            outputLines.push(renderTable(tableRows));
            continue;
        }

        // 无序列表：收集连续的 - / * 开头行
        if (/^[-*] (.+)$/.test(line.trim())) {
            const listItems = [];
            while (i < allLines.length && /^[-*] (.+)$/.test(allLines[i].trim())) {
                listItems.push(applyInlineFormat(allLines[i].trim().replace(/^[-*] /, '')));
                i++;
            }
            i--; // 回退一行
            const ulHtml = '<ul style="margin:4px 0;padding-left:28px;">' +
                listItems.map(t => '<li style="margin:2px 0;padding-left:4px;list-style-type:disc;list-style-position:outside;line-height:1.6;">' + t + '</li>').join('') +
                '</ul>';
            outputLines.push(ulHtml);
            continue;
        }

        // 有序列表：收集连续的 1. 2. 3. 开头行
        if (/^\d+\. (.+)$/.test(line.trim())) {
            const listItems = [];
            while (i < allLines.length && /^\d+\. (.+)$/.test(allLines[i].trim())) {
                listItems.push(applyInlineFormat(allLines[i].trim().replace(/^\d+\. /, '')));
                i++;
            }
            i--; // 回退一行
            const olHtml = '<ol style="margin:4px 0;padding-left:28px;">' +
                listItems.map(t => '<li style="margin:2px 0;padding-left:4px;line-height:1.6;">' + t + '</li>').join('') +
                '</ol>';
            outputLines.push(olHtml);
            continue;
        }

        // 普通行：应用行内格式
        outputLines.push(applyInlineFormat(line));
    }

    // 把 Markdown 转义的换行符（两个空格+换行）或普通换行符保留
    // 而块级元素（标题、列表、表格等）由于有闭合标签，内部换行无意义，这里把所有 \n 转为 <br>
    for (let i = 0; i < outputLines.length; i++) {
        if (!outputLines[i].startsWith('<h') && 
            !outputLines[i].startsWith('<ul') && 
            !outputLines[i].startsWith('<ol') && 
            !outputLines[i].startsWith('<li') && 
            !outputLines[i].startsWith('<table') && 
            !outputLines[i].startsWith('<tr') && 
            !outputLines[i].startsWith('<pre') && 
            !outputLines[i].startsWith('<hr')) {
            outputLines[i] = outputLines[i].replace(/\n/g, '<br>');
        }
    }

    // 使用 <br> 连接各段落和块级元素，确保在浏览器中换行
    // 并且清理掉多余的连续 <br>（超过2个的降级为2个）
    html = outputLines.join('<br>\n');
    html = html.replace(/(<br>\s*){3,}/g, '<br><br>');

    // 移除首尾 <br>
    html = html.replace(/^<br>/, '').replace(/<br>$/, '');

    // 移除块级元素前后的多余 <br>（<ul>/<ol>/<pre>/<table>/<h2>/<h3>/<h4>/<hr> 前后不需要 <br>）
    html = html.replace(/<br>\s*(<ul|<ol|<pre|<table|<h[2-4]|<hr)/g, '$1');
    html = html.replace(/(<\/ul>|<\/ol>|<\/pre>|<\/table>|<\/h[2-4]>|<hr>)\s*<br>/g, '$1');
    // 同时清理首尾 <br> 被移除后可能残留的连续换行
    html = html.replace(/(<br>\s*){3,}/g, '<br><br>');

    // ── 第四步：恢复 LaTeX 公式 ──
    html = html.replace(/\x00LATEX_(\d+)\x00/g, (_, idx) => {
        return latexBlocks[parseInt(idx)];
    });

    return html;
}

/* ── 行内格式化辅助 ──────────────────────── */
function applyInlineFormat(text) {
    let s = text;
    // 行内代码 `...`
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    // 粗体 **...**
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // 斜体 *...*（LaTeX 已提取，不会误匹配）
    s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');
    return s;
}

/* ── 表格渲染辅助 ────────────────────────── */
function renderTable(rows) {
    if (rows.length === 0) return '';

    // 解析表头
    const headerCells = rows[0].replace(/^\||\|$/g, '').split('|').map(c => c.trim());

    let tableHtml = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
    headerCells.forEach(cell => {
        tableHtml += '<th>' + applyInlineFormat(cell) + '</th>';
    });
    tableHtml += '</tr></thead><tbody>';

    // 解析数据行（跳过分隔行）
    for (let i = 1; i < rows.length; i++) {
        const cells = rows[i].replace(/^\||\|$/g, '').split('|').map(c => c.trim());
        tableHtml += '<tr>';
        cells.forEach(cell => {
            tableHtml += '<td>' + applyInlineFormat(cell) + '</td>';
        });
        tableHtml += '</tr>';
    }

    tableHtml += '</tbody></table></div>';
    return tableHtml;
}

function escapeHtml(str) {
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, (m) => map[m]);
}

/* ── MathJax 重新渲染 ──────────────────────── */
function typesetMathJax(element) {
    if (window.MathJax && window.MathJax.typesetPromise) {
        window.MathJax.typesetPromise([element]).catch(() => {});
    }
}

/* ── 加载第三方 Skill ──────────────────────── */
async function loadSkillFromFile() {
    const fileInput = document.getElementById('skill-file-input');
    const resultEl = document.getElementById('skill-load-result');
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        showToast('请先选择 Skill 文件', 'warning');
        return;
    }

    const file = fileInput.files[0];
    const btn = document.getElementById('btn-confirm-load-skill');
    btn.disabled = true;
    btn.textContent = '加载中...';

    try {
        // 读取文件为 base64
        const base64 = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                // 去掉 data:application/zip;base64, 前缀
                const result = reader.result.split(',')[1];
                resolve(result);
            };
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });

        const result = await window.pywebview.api.load_third_party_skill(base64);

        if (resultEl) {
            resultEl.style.display = 'block';
            if (result.success) {
                resultEl.className = 'skill-load-result skill-load-result--success';
                resultEl.innerHTML = '<strong>' + escapeHtml(result.message) + '</strong><br>' +
                    (result.details || []).map(d => escapeHtml(d)).join('<br>');
                showToast(result.message, 'success');
                // 加载成功后刷新工作流标签
                fetchWorkflowLabels();
            } else {
                resultEl.className = 'skill-load-result skill-load-result--error';
                resultEl.innerHTML = '<strong>' + escapeHtml(result.message) + '</strong><br>' +
                    (result.details || []).map(d => escapeHtml(d)).join('<br>');
                showToast(result.message, 'error');
            }
        }
    } catch (e) {
        if (resultEl) {
            resultEl.style.display = 'block';
            resultEl.className = 'skill-load-result skill-load-result--error';
            resultEl.textContent = '加载失败: ' + e;
        }
        showToast('加载失败: ' + e, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '加载';
    }
}

/* ── 模态框控制 ───────────────────────────── */
function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.add('modal--open');
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.remove('modal--open');
}

function closeAllModals() {
    document.querySelectorAll('.modal--open').forEach(m => m.classList.remove('modal--open'));
}

/* ── 页面初始化 ────────────────────────────── */
async function initApp() {
    // ── 立即执行：不依赖 pywebview 的所有操作 ──
    switchView('chat');
    ChatManager.clearChatUI();  // 渲染欢迎区域 + 快捷按钮
    ChatManager.renderWelcomeMessage();  // 渲染欢迎 AI 消息
    ChatManager.renderSidebarSkeleton();

    // 绑定导航按钮
    document.querySelectorAll('[data-nav]').forEach((btn) => {
        btn.addEventListener('click', () => switchView(btn.dataset.nav));
    });

    // 模态框：关闭按钮 & 遮罩层点击关闭
    document.querySelectorAll('[data-close-modal]').forEach((btn) => {
        btn.addEventListener('click', () => closeAllModals());
    });
    document.querySelectorAll('.modal__overlay').forEach((overlay) => {
        overlay.addEventListener('click', () => closeAllModals());
    });

    // Skill 开发指南按钮
    const skillGuideBtn = document.getElementById('btn-skill-guide');
    if (skillGuideBtn) {
        skillGuideBtn.addEventListener('click', () => openModal('modal-skill-guide'));
    }

    // 加载第三方 Skill 按钮
    const loadSkillBtn = document.getElementById('btn-load-skill');
    if (loadSkillBtn) {
        loadSkillBtn.addEventListener('click', () => {
            openModal('modal-load-skill');
            const fileInput = document.getElementById('skill-file-input');
            if (fileInput) fileInput.value = '';
            const filenameEl = document.getElementById('skill-filename');
            if (filenameEl) filenameEl.textContent = '未选择文件';
            const resultEl = document.getElementById('skill-load-result');
            if (resultEl) {
                resultEl.style.display = 'none';
                resultEl.className = 'skill-load-result';
            }
        });
    }

    // 绑定 Skill 文件选择按钮
    const selectSkillFileBtn = document.getElementById('btn-select-skill-file');
    const skillFileInput = document.getElementById('skill-file-input');
    const skillFilenameEl = document.getElementById('skill-filename');
    if (selectSkillFileBtn && skillFileInput) {
        selectSkillFileBtn.addEventListener('click', () => skillFileInput.click());
        skillFileInput.addEventListener('change', () => {
            if (skillFileInput.files && skillFileInput.files.length > 0) {
                if (skillFilenameEl) skillFilenameEl.textContent = skillFileInput.files[0].name;
            } else {
                if (skillFilenameEl) skillFilenameEl.textContent = '未选择文件';
            }
        });
    }

    // 确认加载 Skill
    const confirmLoadBtn = document.getElementById('btn-confirm-load-skill');
    if (confirmLoadBtn) {
        confirmLoadBtn.addEventListener('click', () => loadSkillFromFile());
    }

    // 初始化子模块的 DOM 事件绑定（不需要 pywebview）
    ChatManager.bindEvents();
    PanelManager.init();

    // ── 异步等待 pywebview 就绪，加载后端数据 ──
    waitForPywebview().then(() => {
        App.pywebviewReady = true;
        ChatManager.loadConversationList();
    });
}

/* ─ 启动 ──────────────────────────────────── */
document.addEventListener('DOMContentLoaded', initApp);
