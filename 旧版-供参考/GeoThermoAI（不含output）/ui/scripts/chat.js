/**
 * GeoThermoAI - 对话脚本
 * 消息发送、显示、快捷操作、对话管理
 */

/* ── 自定义确认弹窗（复用 .modal 样式）── */
function showConfirm(msg) {
    return new Promise((resolve) => {
        const dialog = document.getElementById('confirm-dialog');
        const msgEl = document.getElementById('confirm-dialog__msg');
        const okBtn = document.getElementById('confirm-dialog__ok');
        const cancelBtn = document.getElementById('confirm-dialog__cancel');
        msgEl.textContent = msg;
        dialog.classList.add('modal--open');
        function close(val) {
            dialog.classList.remove('modal--open');
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
            resolve(val);
        }
        function onOk() { close(true); }
        function onCancel() { close(false); }
        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
    });
}

const ChatManager = {
    messages: [],       // { role: 'user'|'ai', content: string, time: string }
    sendingConversations: new Set(), // 正在发送/执行流式输出的对话 ID
    conversationId: null,   // 当前对话 ID
    conversations: [],      // 对话列表缓存

    init() {
        this.bindEvents();
        // 加载对话列表（需要 pywebview）
        this.loadConversationList();
        // 初始化项目目录显示
        this.loadProjectDir();
    },

    async loadProjectDir() {
        try {
            const path = await window.pywebview.api.get_project_dir();
            const el = document.getElementById('project-dir-input');
            if (el && path) {
                el.value = path;
                el.style.borderColor = 'green';
            }
        } catch (e) {
            // pywebview 未就绪
        }
    },

    /** 检查项目目录中是否有 LST 结果，有则在结果可视化面板显示（不插入聊天消息） */
    async checkAndShowLSTResult(projectDir) {
        try {
            // 结果仅在结果可视化面板中显示，不作为聊天消息插入
            if (typeof PanelManager !== 'undefined' && PanelManager.loadPanelResult) {
                await PanelManager.loadPanelResult();
            }
        } catch (e) {
            // 静默失败
        }
    },

    bindEvents() {
        const input = document.getElementById('chat-input');
        const sendBtn = document.getElementById('chat-send-btn');

        // 发送按钮
        sendBtn.addEventListener('click', () => this.sendCurrentMessage());

        // Enter 发送，Shift+Enter 换行
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendCurrentMessage();
            }
        });

        // 自动调整输入框高度
        input.addEventListener('input', () => {
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 120) + 'px';
        });

        // 快捷操作按钮
        document.querySelectorAll('.chat-quick-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                const text = btn.dataset.message;
                if (text) {
                    input.value = text;
                    this.sendCurrentMessage();
                }
            });
        });

        // 新对话按钮
        const newChatBtn = document.getElementById('btn-new-chat');
        if (newChatBtn) {
            newChatBtn.addEventListener('click', () => this.newConversation());
        }

        // 项目目录
        const selectDirBtn = document.getElementById('btn-select-project-dir');
        const dirInput = document.getElementById('project-dir-input');
        if (selectDirBtn && dirInput) {
            selectDirBtn.addEventListener('click', async () => {
                try {
                    const path = await window.pywebview.api.select_project_dir_dialog();
                    if (!path) return;
                    const currentDir = dirInput.value.trim();
                    if (currentDir && currentDir !== path) {
                        if (!await showConfirm('更换项目目录可能导致已有流程结果不可用，确定要更改吗？')) {
                            return;
                        }
                    }
                    dirInput.value = path;
                    dirInput.style.borderColor = 'green';
                    // 立即保存项目目录到对话文件
                    if (this.conversationId) {
                        try {
                            await window.pywebview.api.save_project_dir(this.conversationId, path);
                        } catch (e) {}
                    }
                    // 更新面板结果和聊天中的 LST 显示
                    PanelManager.loadPanelResult();
                    this.checkAndShowLSTResult(path);
                } catch (e) {
                    console.error('选择项目目录失败:', e);
                }
            });

            // 手动输入路径后，失焦或按 Enter 时保存
            const saveManualPath = async () => {
                const val = dirInput.value.trim();
                if (val && this.conversationId) {
                    try {
                        await window.pywebview.api.save_project_dir(this.conversationId, val);
                        dirInput.style.borderColor = 'green';
                    } catch (e) {}
                }
            };
            dirInput.addEventListener('blur', saveManualPath);
            dirInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); saveManualPath(); }
            });
        }

        // 关闭软件时自动保存当前对话
        window.addEventListener('beforeunload', () => {
            if (this.conversationId && this.messages.length > 0) {
                try {
                    // pywebview 的 Python 调用在底层是同步执行的
                    window.pywebview.api.save_conversation(
                        this.conversationId,
                        this.messages.filter(m => !m.resultPath),
                        null
                    );
                } catch (e) {}
            }
        });
    },

    /* ── 对话管理 ──────────────────────────── */

    /** 立即渲染侧边栏骨架（不依赖 pywebview） */
    renderSidebarSkeleton() {
        const listEl = document.getElementById('conversation-list');
        if (!listEl) return;
        listEl.innerHTML = '';
        const starredLabel = document.createElement('div');
        starredLabel.className = 'chat-sidebar__section-label';
        starredLabel.textContent = '星标对话';
        listEl.appendChild(starredLabel);
        const empty1 = document.createElement('div');
        empty1.className = 'chat-sidebar__empty';
        empty1.textContent = '暂无星标对话';
        listEl.appendChild(empty1);
        const historyLabel = document.createElement('div');
        historyLabel.className = 'chat-sidebar__section-label';
        historyLabel.textContent = '历史对话';
        listEl.appendChild(historyLabel);
        const empty2 = document.createElement('div');
        empty2.className = 'chat-sidebar__empty';
        empty2.textContent = '暂无历史对话';
        listEl.appendChild(empty2);
    },

    async loadConversationList() {
        try {
            this.conversations = await window.pywebview.api.list_conversations();
            this.renderConversationList();
        } catch (e) {
            // 静默失败
        }
    },

    renderConversationList() {
        const listEl = document.getElementById('conversation-list');
        if (!listEl) return;

        listEl.innerHTML = '';

        // 分星标区和普通区（始终显示两个区域）
        const starred = this.conversations.filter(c => c.starred);
        const regular = this.conversations.filter(c => !c.starred);

        // 星标对话区域
        const starredLabel = document.createElement('div');
        starredLabel.className = 'chat-sidebar__section-label';
        starredLabel.textContent = '星标对话';
        listEl.appendChild(starredLabel);

        if (starred.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'chat-sidebar__empty';
            empty.textContent = '暂无星标对话';
            listEl.appendChild(empty);
        } else {
            starred.forEach((conv) => {
                listEl.appendChild(this.createConversationItem(conv));
            });
        }

        // 历史对话区域
        const historyLabel = document.createElement('div');
        historyLabel.className = 'chat-sidebar__section-label';
        historyLabel.textContent = '历史对话';
        listEl.appendChild(historyLabel);

        if (regular.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'chat-sidebar__empty';
            empty.textContent = '暂无历史对话';
            listEl.appendChild(empty);
        } else {
            regular.forEach((conv) => {
                listEl.appendChild(this.createConversationItem(conv));
            });
        }
    },

    createConversationItem(conv) {
        const el = document.createElement('div');
        el.className = 'chat-sidebar__item' + (conv.id === this.conversationId ? ' chat-sidebar__item--active' : '');
        el.dataset.convId = conv.id;

        // 日期简写
        let dateStr = '';
        if (conv.updated_at) {
            const d = new Date(conv.updated_at.replace(' ', 'T'));
            dateStr = d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate() + ' ' +
                String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
        }

        const starIcon = conv.starred ? '&#9733;' : '&#9734;';
        const starClass = conv.starred ? ' chat-sidebar__item-star--active' : '';

        el.innerHTML = `
            <span class="chat-sidebar__item-title">${escapeHtml(conv.title)}</span>
            <span class="chat-sidebar__item-meta">${dateStr}</span>
            <button class="chat-sidebar__item-star${starClass}" title="星标">${starIcon}</button>
            <button class="chat-sidebar__item-rename" title="重命名">&#9998;</button>
            <button class="chat-sidebar__item-delete" title="删除对话">&times;</button>
        `;

        // 点击切换对话
        el.addEventListener('click', (e) => {
            if (e.target.closest('.chat-sidebar__item-delete') ||
                e.target.closest('.chat-sidebar__item-star') ||
                e.target.closest('.chat-sidebar__item-rename')) return;
            this.switchConversation(conv.id);
        });

        // 星标按钮
        const starBtn = el.querySelector('.chat-sidebar__item-star');
        starBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            try {
                await window.pywebview.api.toggle_star(conv.id);
                this.loadConversationList();
            } catch (err) {}
        });

        // 重命名按钮 - inline 编辑
        const renameBtn = el.querySelector('.chat-sidebar__item-rename');
        renameBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.startInlineRename(el, conv.id, conv.title);
        });

        // 删除按钮
        const deleteBtn = el.querySelector('.chat-sidebar__item-delete');
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.deleteConversation(conv.id);
        });

        return el;
    },

    startInlineRename(itemEl, convId, currentTitle) {
        const titleSpan = itemEl.querySelector('.chat-sidebar__item-title');
        const metaSpan = itemEl.querySelector('.chat-sidebar__item-meta');
        const renameBtn = itemEl.querySelector('.chat-sidebar__item-rename');

        // 替换为输入框
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'chat-sidebar__item-title-input';
        input.value = currentTitle;
        titleSpan.replaceWith(input);
        if (metaSpan) metaSpan.style.display = 'none';
        if (renameBtn) renameBtn.style.display = 'none';

        input.focus();
        input.select();

        const finishRename = async () => {
            const newTitle = input.value.trim();
            if (newTitle && newTitle !== currentTitle) {
                try {
                    await window.pywebview.api.rename_conversation(convId, newTitle);
                } catch (err) {}
            }
            this.loadConversationList();
        };

        input.addEventListener('blur', finishRename);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                input.blur();
            } else if (e.key === 'Escape') {
                input.value = currentTitle;
                input.blur();
            }
        });
    },

    async newConversation() {
        // 保存当前对话（如果有消息）
        await this.saveCurrentConversation();

        try {
            const result = await window.pywebview.api.create_conversation();
            this.conversationId = result.id;
            this.messages = [];
            this.clearChatUI();
            this.renderWelcomeMessage();
            this.loadConversationList();

            // 新对话：清空项目目录输入框和面板图片
            const dirEl = document.getElementById('project-dir-input');
            if (dirEl) {
                dirEl.value = '';
                dirEl.style.borderColor = '';
            }
            const imgEl = document.getElementById('result-image');
            const phEl = document.getElementById('result-placeholder');
            if (imgEl) { imgEl.src = ''; imgEl.style.display = 'none'; }
            if (phEl) phEl.style.display = 'flex';
        } catch (e) {
            // 静默失败
        }
    },

    async switchConversation(convId) {
        if (convId === this.conversationId) return;

        // 先保存当前对话（避免丢失未保存的消息和项目路径）
        await this.saveCurrentConversation();

        // 立即清除结果可视化面板的旧图片（同步操作，不等异步）
        const _imgEl = document.getElementById('result-image');
        const _phEl = document.getElementById('result-placeholder');
        if (_imgEl) { _imgEl.src = ''; _imgEl.style.display = 'none'; }
        if (_phEl) _phEl.style.display = 'flex';

        try {
            const result = await window.pywebview.api.load_conversation(convId);
            if (!result.success) return;

            this.conversationId = convId;
            // 过滤掉之前版本插入的 LST 结果消息（带 resultPath 字段），不再在聊天中显示
            this.messages = (result.messages || []).filter(m => !m.resultPath);
            this.clearChatUI();
            this.renderWelcomeMessage();

            // 渲染对话消息（欢迎区域已在 clearChatUI 中渲染）
            this.messages.forEach((msg, i) => this.renderMessage(msg, i));
            if (this.messages.length > 0) {
                this.scrollToBottom();
                typesetMathJax(document.getElementById('chat-messages'));
            }

            this.renderConversationList();

            // 更新发送按钮状态：如果该对话正在执行则禁用
            this.updateSendButton(this.sendingConversations.has(convId));

            // 恢复项目目录
            const savedDir = result.project_dir || '';
            const dirEl = document.getElementById('project-dir-input');
            if (dirEl) {
                dirEl.value = savedDir;
                dirEl.style.borderColor = savedDir ? 'green' : '';
            }

            // 加载新对话的 LST 结果到面板（有则显示，无则保持已清除状态）
            if (typeof PanelManager !== 'undefined' && PanelManager.loadPanelResult) {
                PanelManager.loadPanelResult();
            }

            // 恢复该对话的活跃流式输出（如果有）
            this.restoreConversationStream(convId);
        } catch (e) {
            // 静默失败
        }
    },

    /** 切换回某对话时，恢复其正在执行或已完成的流式输出 */
    async restoreConversationStream(convId) {
        try {
            const state = await window.pywebview.api.chat_stream_poll(convId);
            if (!state || state.id === null) return;

            const msgId = 'stream-active-' + convId;
            const existingEl = document.getElementById(msgId);

            // 流已完成：把最终内容追加为正式 AI 消息（如果还没有的话）
            if (state.done && state.content && !state.error) {
                const alreadyExists = this.messages.some(m => m.role === 'ai' && m.content === state.content);
                if (!alreadyExists) {
                    const finalMsg = { role: 'ai', content: state.content, time: formatTime() };
                    this.messages.push(finalMsg);
                    this.renderMessage(finalMsg);
                    this.scrollToBottom();
                    typesetMathJax(document.getElementById('chat-messages'));
                    await this.saveCurrentConversation();
                    this.loadConversationList();
                }
                return;
            }

            // 流仍在进行或等待输入：创建/恢复流式消息气泡并继续轮询
            if (!state.done) {
                this.sendingConversations.add(convId);
                if (this.conversationId === convId) this.updateSendButton(true);
                let msgEl = existingEl;
                let bubbleEl;
                if (!msgEl) {
                    msgEl = this.createStreamingMessage(msgId);
                    bubbleEl = msgEl.querySelector('.chat-msg__bubble');
                    const contentEl = bubbleEl.querySelector('.chat-msg__bubble-content') || bubbleEl;
                    const content = state.content || '';
                    if (content) {
                        contentEl.innerHTML = renderMarkdown(content) + '<span class="chat-streaming-cursor">▊</span>';
                    }
                } else {
                    bubbleEl = msgEl.querySelector('.chat-msg__bubble');
                }

                // 如果处于等待输入状态，渲染配对选择器
                const waiting = state.waiting_for_input;
                if (waiting && waiting.type === 'select_pair' && waiting.pairs && !bubbleEl.querySelector('.pair-selector')) {
                    const cursor = bubbleEl.querySelector('.chat-streaming-cursor');
                    if (cursor) cursor.remove();
                    bubbleEl.appendChild(this.createPairSelector(waiting.pairs, msgId, bubbleEl, convId));
                    this.scrollToBottom();
                }

                // 继续轮询（不阻塞）
                this.pollStream(msgId, bubbleEl, convId).then(async (fullContent) => {
                    this.sendingConversations.delete(convId);
                    // 如果轮询期间再次切换离开，不保存部分结果
                    if (this.conversationId !== convId) return;
                    this.updateSendButton(false);
                    msgEl.classList.remove('chat-msg--streaming');
                    msgEl.querySelector('.chat-streaming-cursor')?.remove();
                    // 避免重复追加（如多次恢复同一流）
                    const alreadyExists = this.messages.some(m => m.role === 'ai' && m.content === fullContent);
                    if (alreadyExists) return;
                    const finalMsg = { role: 'ai', content: fullContent, time: formatTime() };
                    this.messages.push(finalMsg);
                    const newIndex = this.messages.length - 1;
                    msgEl.dataset.msgIndex = newIndex;
                    const timeEl = msgEl.querySelector('.chat-msg__time');
                    if (timeEl) timeEl.textContent = finalMsg.time;
                    typesetMathJax(msgEl);
                    await this.saveCurrentConversation();
                    this.loadConversationList();
                }).catch(async (e) => {
                    this.sendingConversations.delete(convId);
                    if (this.conversationId !== convId) return;
                    this.updateSendButton(false);
                    msgEl.classList.remove('chat-msg--streaming');
                    msgEl.querySelector('.chat-streaming-cursor')?.remove();
                    this.addAIMessage('抱歉，发生了错误：' + (e.message || e));
                });
            }
        } catch (e) {
            // 静默失败
        }
    },

    async deleteConversation(convId) {
        try {
            await window.pywebview.api.delete_conversation(convId);

            // 如果删除的是当前对话，清空聊天
            if (convId === this.conversationId) {
                this.conversationId = null;
                this.messages = [];
                this.clearChatUI();
                this.renderWelcomeMessage();
            }

            this.loadConversationList();
        } catch (e) {
            // 静默失败
        }
    },

    async saveCurrentConversation() {
        if (!this.conversationId || this.messages.length === 0) return;

        // 自动用第一条用户消息作为标题
        let title = null;
        const firstUserMsg = this.messages.find(m => m.role === 'user');
        if (firstUserMsg) {
            title = firstUserMsg.content.slice(0, 30) + (firstUserMsg.content.length > 30 ? '...' : '');
        }

        // 保存时过滤掉 LST 结果消息和欢迎消息，不在聊天中持久化
        const messagesToSave = this.messages.filter(m => !m.resultPath && !m.isWelcome);

        try {
            await window.pywebview.api.save_conversation(
                this.conversationId,
                messagesToSave,
                title
            );
        } catch (e) {
            // 静默失败
        }
    },

    /* ── UI 辅助 ──────────────────────────── */

    renderWelcomeMessage() {
        // 仅渲染欢迎消息到 UI，不存入对话消息列表
        const modelName = (typeof PanelManager !== 'undefined' && PanelManager.currentModelName) || 'RF';
        const msg = {
            role: 'ai',
            content: '你好！我是 **GeoThermoAI** 智能助手，专注于地表温度降尺度处理。\n' +
                '我可以帮你：\n' +
                '- 自动获取遥感数据（Landsat / Sentinel / DEM）\n' +
                '- 运行 TTRI-TCR-' + modelName + ' 降尺度流水线\n' +
                '- 智能调参、诊断精度、生成报告\n' +
                '试试点击上方快捷按钮，或直接输入指令开始吧！',
            time: formatTime(),
            isWelcome: true,
        };
        this.renderMessage(msg);
    },

    clearChatUI() {
        const container = document.getElementById('chat-messages');
        if (container) container.innerHTML = '';
        this.renderWelcomeHeader();
    },

    /** 在消息列表顶部渲染欢迎区域（始终存在） */
    renderWelcomeHeader() {
        const container = document.getElementById('chat-messages');
        if (!container) return;
        // 避免重复渲染
        if (document.getElementById('chat-welcome')) return;

        const header = document.createElement('div');
        header.className = 'chat-welcome';
        header.id = 'chat-welcome';
        header.innerHTML = `
            <img src="assets/logo.png" alt="GeoThermoAI" class="chat-welcome__logo"
                 onerror="this.style.display='none'">
            <h1 class="chat-welcome__title">GeoThermoAI 智能助手</h1>
            <p class="chat-welcome__desc">
                你好！我是 GeoThermoAI 智能助手，可以帮你自动获取遥感数据、训练模型、生成 LST 产品。
                输入指令或点击下方快捷按钮开始吧。
            </p>
        `;
        container.insertBefore(header, container.firstChild);

        // 快捷操作按钮
        if (!document.getElementById('chat-quick-actions')) {
            const qa = document.createElement('div');
            qa.className = 'chat-quick-actions';
            qa.id = 'chat-quick-actions';
            qa.innerHTML = `
                <button class="chat-quick-btn" data-message="帮我处理武汉市2024年7月的数据">处理武汉市2024年7月数据</button>
                <button class="chat-quick-btn" id="btn-recommend-params" data-message="">推荐随机森林训练参数</button>
                <button class="chat-quick-btn" data-message="TTRI是什么？原理是什么？">TTRI原理</button>
                <button class="chat-quick-btn" id="btn-diag-accuracy">诊断精度</button>
            `;
            container.insertBefore(qa, header.nextSibling);
            // 绑定快捷按钮事件
            qa.querySelectorAll('.chat-quick-btn').forEach((btn) => {
                btn.addEventListener('click', async () => {
                    // 推荐随机森林训练参数按钮：先读取 CSV 形状，再把信息交给 AI 推荐
                    if (btn.id === 'btn-recommend-params') {
                        try {
                            const convId = this.conversationId;
                            const ok = await window.pywebview.api.check_preprocessed_data(convId);
                            if (!ok) {
                                // 没有预处理数据，不发送消息
                                return;
                            }
                            const shape = await window.pywebview.api.get_csv_shape(convId);
                            if (shape && shape.n_samples > 0 && shape.n_features > 0) {
                                const msg = `基于当前 train.csv 的数据规模（样本数：${shape.n_samples}，特征数：${shape.n_features}），请你在随机森林回归的调参经验范围内，只推荐以下 5 个超参数的合适数值：\n1. n_estimators（决策树数量）\n2. max_depth（最大深度）\n3. min_samples_split（最小分裂样本数）\n4. min_samples_leaf（叶节点最小样本数）\n5. max_features（最大特征比例）\n\n请综合考虑样本量与特征维度，防止过拟合或欠拟合，以列表形式直接给出推荐值，不需要执行操作。`;
                                document.getElementById('chat-input').value = msg;
                                this.sendCurrentMessage();
                            }
                        } catch (e) {
                            // 出错也不发送
                        }
                        return;
                    }
                    // 诊断精度按钮：动态填充评估结果
                    if (btn.id === 'btn-diag-accuracy') {
                        try {
                            const acc = await window.pywebview.api.get_accuracy_summary();
                            if (acc && acc.rmse !== undefined) {
                                const msg = `请分析以下模型精度并给出改进建议：\n1. 测试集指标：R²=${acc.r2.toFixed(4)}，MAE=${acc.test_mae.toFixed(2)}K，RMSE=${acc.test_rmse.toFixed(2)}K\n2. 空间一致性（10m聚合vs30m测试集，${acc.n_matched}个匹配像元）：MAE=${acc.mae.toFixed(2)}K，RMSE=${acc.rmse.toFixed(2)}K\n3. 全量值域偏差（10m全量vs30m全量）：${acc.max_abs_dev.toFixed(2)}K`;
                                document.getElementById('chat-input').value = msg;
                                this.sendCurrentMessage();
                                return;
                            }
                        } catch (e) {}
                    }
                    const text = btn.dataset.message;
                    if (text) {
                        document.getElementById('chat-input').value = text;
                        this.sendCurrentMessage();
                    }
                });
            });
        }
    },

    /* ── 发送消息 ──────────────────────────── */

    async sendCurrentMessage() {
        const input = document.getElementById('chat-input');
        const message = input.value.trim();
        if (!message || this.sendingConversations.has(this.conversationId)) return;

        // 如果没有当前对话，先创建一个
        if (!this.conversationId) {
            try {
                const result = await window.pywebview.api.create_conversation();
                this.conversationId = result.id;
                this.loadConversationList();
            } catch (e) {
                // 即使创建失败也继续发送
            }
        }

        if (!this.conversationId) {
            this.addAIMessage('抱歉，无法创建新对话，请重试。');
            return;
        }

        const currentConvId = this.conversationId;

        // 清空输入
        input.value = '';
        input.style.height = 'auto';

        // 添加用户消息
        this.addUserMessage(message);
        this.sendingConversations.add(currentConvId);
        this.updateSendButton(true);

        try {
            // 构建历史消息
            const historyForAPI = this.messages.slice(0, -1).map(m => ({
                role: m.role,
                content: m.content,
            }));

            // 创建流式消息占位（使用稳定 ID，便于切换对话后恢复）
            const msgId = 'stream-active-' + currentConvId;
            const msgEl = this.createStreamingMessage(msgId);
            const bubbleEl = msgEl.querySelector('.chat-msg__bubble');

            // 开始流式调用
            await window.pywebview.api.chat_stream_start(currentConvId, message, historyForAPI);

            // 轮询流式结果
            const fullContent = await this.pollStream(msgId, bubbleEl, currentConvId);

            // 如果轮询期间切换到了其他对话，不要在此处保存部分结果；
            // 流会继续在后台执行，由 restoreConversationStream 在返回时恢复
            if (this.conversationId !== currentConvId) {
                return;
            }

            // 移除流式标记
            msgEl.classList.remove('chat-msg--streaming');
            msgEl.querySelector('.chat-streaming-cursor')?.remove();

            // 存入消息列表
            const finalMsg = {
                role: 'ai',
                content: fullContent,
                time: formatTime(),
            };
            this.messages.push(finalMsg);
            if (msgEl) msgEl.dataset.msgIndex = this.messages.length - 1;

            // 更新时间为真实时间
            const timeEl = msgEl.querySelector('.chat-msg__time');
            if (timeEl) timeEl.textContent = finalMsg.time;

            // 触发 MathJax 渲染
            typesetMathJax(msgEl);

            // 自动保存对话
            await this.saveCurrentConversation();
            this.loadConversationList();
        } catch (e) {
            if (this.conversationId === currentConvId) {
                this.addAIMessage('抱歉，发生了错误：' + (e.message || e));
            }
        } finally {
            this.sendingConversations.delete(currentConvId);
            if (this.conversationId === currentConvId) {
                this.updateSendButton(false);
            }
        }
    },

    /** 创建流式消息 DOM 元素 */
    createStreamingMessage(msgId) {
        const container = document.getElementById('chat-messages');
        const el = document.createElement('div');
        el.id = msgId;
        el.className = 'chat-msg chat-msg--ai chat-msg--streaming';
        el.dataset.msgIndex = -1; // 流式消息尚未加入 messages 列表
        el.innerHTML = `
            <div class="chat-msg__avatar chat-msg__avatar--ai">
                <img class="chat-msg__avatar-img" src="assets/logo.png" alt="GeoThermoAI">
            </div>
            <div class="chat-msg__content">
                <div class="chat-msg__bubble">
                    <div class="chat-msg__bubble-content"><span class="chat-streaming-cursor">▊</span></div>
                </div>
                <div class="chat-msg__footer">
                    <span class="chat-msg__time">思考中...</span>
                    <button class="chat-msg__delete-btn" title="删除此消息">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
                    </button>
                </div>
            </div>
        `;

        // 绑定流式消息删除按钮
        const deleteBtn = el.querySelector('.chat-msg__delete-btn');
        if (deleteBtn) {
            deleteBtn.addEventListener('click', async (e) => {
                e.stopPropagation();

                // 如果流消息已完成（已加入 this.messages），走标准删除流程
                if (msgId && msgId.startsWith('stream-active-')) {
                    const convId = msgId.replace('stream-active-', '');
                    const msgIndex = parseInt(el.dataset.msgIndex, 10);
                    if (msgIndex >= 0 && msgIndex < this.messages.length) {
                        await this.deleteMessage(msgIndex, el);
                        return;
                    }
                }

                // 未完成的流消息：直接销毁，用弹窗确认
                const confirmed = await showConfirm('确定要删除这条正在生成的AI消息吗？删除后不可恢复。');
                if (!confirmed) return;
                el.remove();
            });
        }
        container.appendChild(el);
        this.scrollToBottom();
        return el;
    },

    /** 轮询流式结果，逐步更新气泡内容（按对话隔离） */
    pollStream(msgId, bubbleEl, convId) {
        return new Promise((resolve, reject) => {
            let lastContent = '';
            const timer = setInterval(async () => {
                try {
                    // 如果已经切换到其他对话，停止当前轮询，让目标对话自己的轮询接管
                    if (this.conversationId !== convId) {
                        clearInterval(timer);
                        resolve(lastContent);
                        return;
                    }

                    const state = await window.pywebview.api.chat_stream_poll(convId);

                    // 检测是否需要用户输入（如选择影像配对）
                    const waiting = state.waiting_for_input;
                    if (waiting && waiting.type === 'select_pair' && waiting.pairs && !bubbleEl.querySelector('.pair-selector')) {
                        // 移除光标
                        const cursor = bubbleEl.querySelector('.chat-streaming-cursor');
                        if (cursor) cursor.remove();
                        // 添加配对选择器（只添加一次）
                        bubbleEl.appendChild(this.createPairSelector(waiting.pairs, msgId, bubbleEl, convId));
                        this.scrollToBottom();
                        // 不返回，继续轮询
                    }

                    const content = state.content || '';
                    if (content !== lastContent) {
                        lastContent = content;
                        // 移除光标占位符再更新内容
                        const contentEl = bubbleEl.querySelector('.chat-msg__bubble-content') || bubbleEl;
                        const cursor = contentEl.querySelector('.chat-streaming-cursor');
                        if (cursor) cursor.remove();
                        contentEl.innerHTML = renderMarkdown(content) + '<span class="chat-streaming-cursor">▊</span>';
                        this.scrollToBottom();
                    }
                    if (state.done) {
                        clearInterval(timer);
                        if (state.error) {
                            reject(new Error(state.error));
                        } else {
                            resolve(content);
                        }
                    }
                } catch (e) {
                    clearInterval(timer);
                    reject(e);
                }
            }, 100);
        });
    },

    /** 创建影像配对选择器 */
    createPairSelector(pairs, msgId, bubbleEl, convId) {
        const container = document.createElement('div');
        container.className = 'pair-selector';
        container.innerHTML = '<div style="font-weight:600;margin-bottom:8px;">请选择影像配对：</div>';

        pairs.forEach((pair) => {
            const card = document.createElement('div');
            card.className = 'pair-card';
            const lsat_scenes_html = (pair.landsat_scenes || []).map(s =>
                `<span style="font-size:11px;color:var(--text-muted);">${s.date} ${s.satellite} ☁${s.cloud_cover}%</span>`
            ).join('<br>') || '';
            const s2_scenes_html = (pair.sentinel_scenes || []).map(s =>
                `<span style="font-size:11px;color:var(--text-muted);">${s.date} S2 ☁${s.cloud_cover}%</span>`
            ).join('<br>') || '';

            card.innerHTML = `
                <div class="pair-card__header">
                    <span class="pair-card__label">🛰️ Landsat ${pair.landsat_satellite || ''}</span>
                    <span class="pair-card__value">${pair.landsat_date}</span>
                </div>
                <div class="pair-card__scenes">${lsat_scenes_html}</div>
                <div class="pair-card__meta">
                    <span>${pair.landsat_count}景拼合 ☁️ ${pair.landsat_cloud}%</span>
                    <span>覆盖度 ${pair.landsat_coverage}%</span>
                </div>
                <div class="pair-card__header" style="margin-top:6px;">
                    <span class="pair-card__label">🛸 Sentinel</span>
                    <span class="pair-card__value">${pair.sentinel_date}</span>
                </div>
                <div class="pair-card__scenes">${s2_scenes_html}</div>
                <div class="pair-card__meta">
                    <span>${pair.sentinel_count}景拼合 ☁️ ${pair.sentinel_cloud}%</span>
                    <span>覆盖度 ${pair.sentinel_coverage}%</span>
                </div>
            `;
            card.addEventListener('click', async () => {
                container.querySelectorAll('.pair-card').forEach(c => c.style.pointerEvents = 'none');
                card.classList.add('pair-card--selected');

                const result = await window.pywebview.api.agent_select_pair(convId, pair.index);
                if (result.success) {
                    // 追加选择提示
                    const statusMsg = document.createElement('div');
                    statusMsg.style.color = 'var(--color-success)';
                    statusMsg.style.fontWeight = '600';
                    statusMsg.style.marginTop = '8px';
                    statusMsg.textContent = `✅ 已选择第 ${pair.index + 1} 对，继续执行...`;
                    container.appendChild(statusMsg);
                    this.scrollToBottom();
                }
            });
            container.appendChild(card);
        });

        return container;
    },

    /* ── 添加用户消息 ──────────────────────── */
    addUserMessage(content) {
        const msg = {
            role: 'user',
            content: content,
            time: formatTime(),
        };
        this.messages.push(msg);
        this.renderMessage(msg);
        this.scrollToBottom();
    },

    /* ── 添加 AI 消息 ──────────────────────── */
    addAIMessage(content) {
        const msg = {
            role: 'ai',
            content: content,
            time: formatTime(),
        };
        this.messages.push(msg);
        this.renderMessage(msg);
        this.scrollToBottom();
    },

    /* ── 渲染单条消息 ──────────────────────── */
    renderMessage(msg, index = -1) {
        const container = document.getElementById('chat-messages');
        const el = document.createElement('div');
        el.className = `chat-msg chat-msg--${msg.role}`;
        // 优先用传入的 index，否则从数组中查找
        const msgIndex = index >= 0 ? index : this.messages.indexOf(msg);
        el.dataset.msgIndex = msgIndex;
        // 额外存储消息内容用于备用查找（防止 index 失效）
        el.dataset.msgContent = (msg.content || '').slice(0, 100);

        const avatarHtml = msg.role === 'ai'
            ? '<img class="chat-msg__avatar-img" src="assets/logo.png" alt="GeoThermoAI">'
            : '<span class="chat-msg__avatar-text">User</span>';
        const bubbleHtml = msg.role === 'ai' ? renderMarkdown(msg.content) : escapeHtml(msg.content).replace(/\n/g, '<br>');

        el.innerHTML = `
            <div class="chat-msg__avatar chat-msg__avatar--${msg.role}">${avatarHtml}</div>
            <div class="chat-msg__content">
                <div class="chat-msg__bubble">
                    <div class="chat-msg__bubble-content">${bubbleHtml}</div>
                </div>
                ${msg.isWelcome ? '' : `
                <div class="chat-msg__footer">
                    <span class="chat-msg__time">${msg.time}</span>
                    <button class="chat-msg__delete-btn" title="删除此消息">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
                    </button>
                </div>
                `}
            </div>
        `;

        // 绑定删除按钮事件（欢迎消息没有删除按钮）
        const deleteBtn = el.querySelector('.chat-msg__delete-btn');
        if (deleteBtn && !msg.isWelcome) {
            deleteBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                // 先用 dataset 中的 index
                let idx = parseInt(el.dataset.msgIndex, 10);
                // 如果 index 失效，通过内容前缀在数组中查找
                if (idx < 0 || idx >= this.messages.length || this.messages[idx].content !== el.dataset.msgContent) {
                    const prefix = el.dataset.msgContent;
                    idx = this.messages.findIndex(m => (m.content || '').slice(0, 100) === prefix);
                }
                if (idx >= 0 && idx < this.messages.length) {
                    await this.deleteMessage(idx, el);
                }
            });
        }

        // 如果有缩略图，插入到 bubble-content 前面
        if (msg.thumbnail) {
            const contentEl = el.querySelector('.chat-msg__bubble-content');
            const img = document.createElement('img');
            img.src = msg.thumbnail;
            img.alt = 'LST Result';
            img.style.cssText = 'max-width:100%;height:auto;border-radius:8px;border:1px solid var(--border-color);margin-bottom:12px;display:block;';
            contentEl.insertBefore(img, contentEl.firstChild);
        }

        container.appendChild(el);

        // 触发 MathJax 渲染公式
        typesetMathJax(el);
    },

    /* ── 删除单条消息 ────────────────────────── */
    async deleteMessage(index, msgEl) {
        if (index < 0 || index >= this.messages.length) return;
        const msg = this.messages[index];

        // 使用软件风格弹窗确认
        const confirmed = await showConfirm(`确定要删除这条${msg.role === 'user' ? '用户' : 'AI'}消息吗？删除后不可恢复。`);
        if (!confirmed) return;

        // 从消息列表中移除
        this.messages.splice(index, 1);

        // 从 DOM 中移除卡片
        if (msgEl && msgEl.parentNode) {
            msgEl.remove();
        } else {
            // 如果 DOM 元素未传入，重新渲染整个对话
            this.clearChatUI();
            this.messages.forEach((m, i) => this.renderMessage(m, i));
        }

        // 如果删除后消息列表为空，渲染欢迎消息
        if (this.messages.length === 0) {
            this.renderWelcomeMessage();
        }

        // 持久化保存
        try {
            await this.saveCurrentConversation();
            this.loadConversationList();
        } catch (e) {
            // 静默失败
        }
    },

    /* ── 输入指示器 ────────────────────────── */
    showTypingIndicator() {
        const container = document.getElementById('chat-messages');
        const id = 'typing-' + Date.now();
        const el = document.createElement('div');
        el.id = id;
        el.className = 'chat-msg chat-msg--ai';
        el.innerHTML = `
            <div class="chat-msg__avatar chat-msg__avatar--ai"><img class="chat-msg__avatar-img" src="assets/logo.png" alt="GeoThermoAI"></div>
            <div class="chat-msg__content">
                <div class="chat-msg__bubble">
                    <div class="chat-typing">
                        <span class="chat-typing__dot"></span>
                        <span class="chat-typing__dot"></span>
                        <span class="chat-typing__dot"></span>
                    </div>
                </div>
            </div>
        `;
        container.appendChild(el);
        this.scrollToBottom();
        return id;
    },

    removeTypingIndicator(id) {
        const el = document.getElementById(id);
        if (el) el.remove();
    },

    /* ── 滚动到底部 ────────────────────────── */
    scrollToBottom() {
        const container = document.getElementById('chat-messages');
        requestAnimationFrame(() => {
            container.scrollTop = container.scrollHeight;
        });
    },

    /* ── 发送按钮状态 ──────────────────────── */
    updateSendButton(disabled) {
        const btn = document.getElementById('chat-send-btn');
        btn.disabled = disabled;
    },
};
