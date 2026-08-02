/**
 * GeoThermoAI - 面板脚本
 * 配置管理、日志显示、结果可视化
 */

const PanelManager = {
    config: null,
    modelSkills: [],
    currentModelName: '',
    hyperparamValues: {},

    init() {
        // 配置面板 Tab 切换
        document.querySelectorAll('.config-tab').forEach((tab) => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.config-tab').forEach((t) => t.classList.remove('config-tab--active'));
                document.querySelectorAll('.config-panel').forEach((p) => p.classList.remove('config-panel--active'));
                tab.classList.add('config-tab--active');
                const target = document.getElementById('panel-' + tab.dataset.tab);
                if (target) target.classList.add('config-panel--active');
            });
        });

        // API 格式切换 - 动态更新提示文字
        const apiFormatEl = document.getElementById('api-format');
        if (apiFormatEl) {
            apiFormatEl.addEventListener('change', () => this.updateApiHint());
        }

        // 数据源连接测试
        const testDsBtn = document.getElementById('btn-test-gee');
        if (testDsBtn) testDsBtn.addEventListener('click', () => this.testDataSource());

        // 研究区域上传
        const uploadStudyBtn = document.getElementById('btn-upload-study-area');
        const studyInput = document.getElementById('study-area-input');
        if (uploadStudyBtn && studyInput) {
            uploadStudyBtn.addEventListener('click', () => studyInput.click());
            studyInput.addEventListener('change', () => this.uploadStudyArea());
        }

        // 模型选择切换
        const modelSelectorEl = document.getElementById('model-selector');
        if (modelSelectorEl) {
            modelSelectorEl.addEventListener('change', () => this.onModelChange());
        }

        // 保存按钮
        const saveApiBtn = document.getElementById('btn-save-api');
        if (saveApiBtn) saveApiBtn.addEventListener('click', () => this.saveApiConfig());

        // 数据设置自动保存（失焦或回车时）
        const cloudThreshold = document.getElementById('cloud-threshold');
        if (cloudThreshold) {
            const saveData = () => {
                const val = cloudThreshold.value.trim();
                if (val !== '') {
                    window.pywebview.api.update_config('cloud_threshold', parseInt(val));

                } else {
                    window.pywebview.api.update_config('cloud_threshold', null);

                }
            };
            cloudThreshold.addEventListener('blur', saveData);
            cloudThreshold.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveData(); });
        }

    },

    /* ── API 格式提示文字切换 ──────────────── */
    updateApiHint() {
        const apiFormat = document.getElementById('api-format').value;
        const hintEl = document.getElementById('api-base-url-hint');
        if (!hintEl) return;
        if (apiFormat === 'anthropic') {
            hintEl.innerHTML = '请填写兼容 Claude API 的服务端点地址，不要以斜杠结尾。<code>/v1/messages</code> 将会被补充到你填写的地址末尾。';
        } else {
            hintEl.innerHTML = '请填写兼容 OpenAI API 的服务端点地址，不要以斜杠结尾。<code>/chat/completions</code> 将会被补充到你填写的地址末尾。';
        }
    },

    /* ── 加载配置 ──────────────────────────── */
    async loadConfig() {
        try {
            this.config = await window.pywebview.api.get_config();
            this.renderConfig(this.config);
        } catch (e) {

        }
    },

    /* ── 渲染配置到表单 ───────────────────── */
    renderConfig(config) {
        // API 设置
        const apiFormatEl = document.getElementById('api-format');
        const apiBaseUrlEl = document.getElementById('api-base-url');
        const modelIdEl = document.getElementById('model-id');
        const apiKeyEl = document.getElementById('api-key');
        const displayNameEl = document.getElementById('api-display-name');
        const contextInputEl = document.getElementById('context-input');
        const contextOutputEl = document.getElementById('context-output');

        if (apiFormatEl) apiFormatEl.value = config.api_format || 'openai';
        if (apiBaseUrlEl) apiBaseUrlEl.value = config.api_base_url || '';
        if (modelIdEl) modelIdEl.value = config.model_id || '';
        if (apiKeyEl) apiKeyEl.value = config.api_key || '';
        if (displayNameEl) displayNameEl.value = config.display_name || '';
        if (contextInputEl) contextInputEl.value = config.context_input || '';
        if (contextOutputEl) contextOutputEl.value = config.context_output || '';

        // 根据 API 格式更新提示文字
        this.updateApiHint();

        // 数据设置
        const cloudThreshold = document.getElementById('cloud-threshold');
        if (cloudThreshold) cloudThreshold.value = (config.data || {}).cloud_threshold || '';

        // 加载模型技能列表
        this.loadModelSkills();
    },

    /* ── 加载模型技能列表 ─────────────────── */
    async loadModelSkills() {
        try {
            const skills = await window.pywebview.api.get_model_skills();
            this.modelSkills = skills || [];
            this.renderModelSelector();
        } catch (e) {

        }
    },

    /* ── 渲染模型选择器 ───────────────────── */
    renderModelSelector() {
        const selectorEl = document.getElementById('model-selector');
        if (!selectorEl) return;

        selectorEl.innerHTML = '';

        if (this.modelSkills.length === 0) {
            selectorEl.innerHTML = '<option value="">暂无可用模型</option>';
            return;
        }

        this.modelSkills.forEach((skill, index) => {
            const option = document.createElement('option');
            option.value = skill.name;
            // 下拉框只显示模型名称（如 Random Forest），完整描述在下方显示
            // 特殊处理：rf_model → Random Forest
            let displayName = skill.name;
            if (skill.name === 'rf_model') {
                displayName = 'Random Forest';
            } else {
                // 其他模型：将下划线转为空格，首字母大写
                displayName = skill.name.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
            }
            option.textContent = displayName;
            if (index === 0) option.selected = true;
            selectorEl.appendChild(option);
        });

        // 默认选中第一个模型
        if (this.modelSkills.length > 0) {
            this.currentModelName = this.modelSkills[0].name;
            this.renderHyperparameters(this.modelSkills[0]);

            // 初始化时通知后端当前模型
            let displayName = this.modelSkills[0].name;
            if (displayName === 'rf_model') {
                displayName = 'Random Forest';
            } else {
                displayName = displayName.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
            }
            window.pywebview.api.set_current_model(displayName);
        }
    },

    /* ── 模型选择变更 ─────────────────────── */
    async onModelChange() {
        const selectorEl = document.getElementById('model-selector');
        if (!selectorEl) return;

        const modelName = selectorEl.value;
        const skill = this.modelSkills.find(s => s.name === modelName);

        if (skill) {
            this.currentModelName = modelName;
            this.renderHyperparameters(skill);

            // 通知后端更新当前模型名称，让 AI 回答时基于该模型
            try {
                // 获取显示名称
                let displayName = modelName;
                if (modelName === 'rf_model') {
                    displayName = 'Random Forest';
                } else {
                    displayName = modelName.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
                }
                await window.pywebview.api.set_current_model(displayName);
            } catch (e) {
                // 静默失败
            }
        }
    },

    /* ── 渲染超参数表单 ───────────────────── */
    renderHyperparameters(skill) {
        const containerEl = document.getElementById('hyperparams-container');
        const descEl = document.getElementById('model-description');

        if (!containerEl) return;

        // 更新模型描述
        if (descEl) {
            descEl.textContent = skill.description;
        }

        containerEl.innerHTML = '';

        if (!skill.hyperparameters || skill.hyperparameters.length === 0) {
            containerEl.innerHTML = '<div class="hyperparams-empty">该模型无需配置超参数</div>';
            return;
        }

        skill.hyperparameters.forEach((hp) => {
            const groupEl = document.createElement('div');
            groupEl.className = 'form-group';

            const labelEl = document.createElement('label');
            labelEl.className = 'form-group__label';
            labelEl.textContent = hp.label;
            if (hp.description) {
                labelEl.title = hp.description;
            }
            groupEl.appendChild(labelEl);

            let inputEl;

            if (hp.type === 'select') {
                inputEl = document.createElement('select');
                inputEl.className = 'form-select';
                inputEl.id = `hp-${hp.name}`;

                hp.options.forEach((opt) => {
                    const optionEl = document.createElement('option');
                    optionEl.value = opt;
                    optionEl.textContent = opt;
                    if (opt === hp.default) optionEl.selected = true;
                    inputEl.appendChild(optionEl);
                });
            } else if (hp.type === 'boolean') {
                inputEl = document.createElement('input');
                inputEl.type = 'checkbox';
                inputEl.className = 'form-checkbox';
                inputEl.id = `hp-${hp.name}`;
                inputEl.checked = hp.default || false;
            } else {
                // number 类型
                inputEl = document.createElement('input');
                inputEl.type = 'number';
                inputEl.className = 'form-input';
                inputEl.id = `hp-${hp.name}`;
                inputEl.value = hp.default || '';
                if (hp.min !== null && hp.min !== undefined) inputEl.min = hp.min;
                if (hp.max !== null && hp.max !== undefined) inputEl.max = hp.max;
                if (hp.step !== null && hp.step !== undefined) inputEl.step = hp.step;
            }

            groupEl.appendChild(inputEl);
            containerEl.appendChild(groupEl);
        });
    },

    /* ── 保存 API 配置 ─────────────────────── */
    async saveApiConfig() {
        const apiFormat = document.getElementById('api-format').value;
        const apiBaseUrl = document.getElementById('api-base-url').value.trim();
        const modelId = document.getElementById('model-id').value.trim();
        const apiKey = document.getElementById('api-key').value.trim();
        const displayName = document.getElementById('api-display-name').value.trim();
        const contextInput = document.getElementById('context-input').value.trim();
        const contextOutput = document.getElementById('context-output').value.trim();
        const cloudThreshold = document.getElementById('cloud-threshold').value.trim();

        try {
            await window.pywebview.api.update_config('api_format', apiFormat);
            await window.pywebview.api.update_config('api_base_url', apiBaseUrl);
            await window.pywebview.api.update_config('model_id', modelId);
            await window.pywebview.api.update_config('api_key', apiKey);
            await window.pywebview.api.update_config('display_name', displayName);
            if (contextInput) await window.pywebview.api.update_config('context_input', parseInt(contextInput));
            if (contextOutput) await window.pywebview.api.update_config('context_output', parseInt(contextOutput));
            // 保存 data 设置
            if (cloudThreshold !== '') {
                await window.pywebview.api.update_config('cloud_threshold', parseInt(cloudThreshold));
            } else {
                await window.pywebview.api.update_config('cloud_threshold', null);
            }
            // 保存模型超参数
            const hpNames = ['n_estimators', 'max_depth', 'min_samples_split', 'min_samples_leaf', 'max_features'];
            for (const hpName of hpNames) {
                const el = document.getElementById(`hp-${hpName}`);
                if (el && el.value !== '') {
                    await window.pywebview.api.update_config(hpName, parseFloat(el.value));
                }
            }
            showToast('配置已保存', 'success');

        } catch (e) {
            showToast('保存失败: ' + e, 'error');

        }
    },

    /* ── 测试数据源连接 ─────────────────────── */
    async testDataSource() {
        const btn = document.getElementById('btn-test-gee');
        const statusEl = document.getElementById('gee-connection-status');

        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> 测试中...';
        if (statusEl) {
            statusEl.className = 'connection-status';
            statusEl.textContent = '';
            statusEl.style.display = 'none';
        }



        try {
            const result = await window.pywebview.api.test_data_source_connection();

            if (result.success) {
                if (statusEl) {
                    statusEl.className = 'connection-status connection-status--success';
                    statusEl.textContent = '\u2713 ' + result.message;
                    statusEl.style.display = 'flex';
                }

                showToast(result.message, 'success');
            } else {
                if (statusEl) {
                    statusEl.className = 'connection-status connection-status--error';
                    statusEl.textContent = '\u2717 ' + result.message;
                    statusEl.style.display = 'flex';
                }

                showToast(result.message, 'error');
            }
        } catch (e) {
            if (statusEl) {
                statusEl.className = 'connection-status connection-status--error';
                statusEl.textContent = '\u2717 测试异常: ' + e;
                statusEl.style.display = 'flex';
            }

        } finally {
            btn.disabled = false;
            btn.textContent = '测试连接';
        }
    },

    /* ── 研究区域上传 ─────────────────── */
    async uploadStudyArea() {
        const fileInput = document.getElementById('study-area-input');
        const filenameEl = document.getElementById('study-area-filename');
        const statusEl = document.getElementById('study-area-status');

        if (!fileInput || !fileInput.files || fileInput.files.length === 0) return;

        const files = Array.from(fileInput.files);
        const mainFile = files[0];

        // 显示文件名
        if (filenameEl) {
            filenameEl.textContent = files.map(f => f.name).join(', ');
        }

        // 显示状态
        if (statusEl) {
            statusEl.className = 'connection-status';
            statusEl.textContent = '正在上传...';
            statusEl.style.display = 'flex';
        }

        try {
            // 读取主文件为 Base64
            const mainContent = await this._readFileAsBase64(mainFile);

            // 读取伴随文件（.dbf, .shx, .prj 等）
            const extraFiles = {};
            for (let i = 1; i < files.length; i++) {
                const f = files[i];
                extraFiles[f.name] = await this._readFileAsBase64(f);
            }

            const result = await window.pywebview.api.upload_study_area(
                mainFile.name, mainContent,
                Object.keys(extraFiles).length > 0 ? extraFiles : null
            );

            if (result.success) {
                if (statusEl) {
                    statusEl.className = 'connection-status connection-status--success';
                    statusEl.textContent = '✓ 上传成功，已保存为 GeoJSON: ' + result.path.split('\\').pop();
                    statusEl.style.display = 'flex';
                }

                showToast('研究区域上传成功', 'success');
            } else {
                if (statusEl) {
                    statusEl.className = 'connection-status connection-status--error';
                    statusEl.textContent = '✗ ' + result.message;
                    statusEl.style.display = 'flex';
                }

                showToast(result.message, 'error');
            }
        } catch (e) {
            if (statusEl) {
                statusEl.className = 'connection-status connection-status--error';
                statusEl.textContent = '✗ 上传失败: ' + e;
                statusEl.style.display = 'flex';
            }

        } finally {
            fileInput.value = '';
        }
    },

    _readFileAsBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                const base64 = reader.result.split(',')[1];
                resolve(base64);
            };
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    },

    /* ── 结果图片展示 ──────────────────────── */
    async showResultImage(artifactPath) {
        const placeholder = document.getElementById('result-placeholder');
        const imgEl = document.getElementById('result-image');

        try {
            const base64 = await window.pywebview.api.get_result_image(artifactPath);
            if (base64 && imgEl) {
                imgEl.src = base64;
                imgEl.style.display = 'block';
                if (placeholder) placeholder.style.display = 'none';

            }
        } catch (e) {

        }
    },

    /** 从当前项目目录加载 LST 结果到可视化面板 */
    async loadPanelResult() {
        const placeholder = document.getElementById('result-placeholder');
        const imgEl = document.getElementById('result-image');
        if (!imgEl) return;
        try {
            const convId = (typeof ChatManager !== 'undefined' && ChatManager.conversationId) ? ChatManager.conversationId : null;
            const lst = await window.pywebview.api.get_lst_result(convId);
            if (lst && lst.found && lst.thumbnail) {
                imgEl.src = lst.thumbnail;
                imgEl.style.display = 'block';
                if (placeholder) placeholder.style.display = 'none';
            } else {
                // 清空 src 确保旧图片彻底消失
                imgEl.src = '';
                imgEl.style.display = 'none';
                if (placeholder) placeholder.style.display = 'flex';
            }
        } catch (e) {
            imgEl.src = '';
            imgEl.style.display = 'none';
            if (placeholder) placeholder.style.display = 'flex';
        }
    },

    /* ── Skill 列表展示 ────────────────────── */
    async loadSkillList() {
        try {
            const skills = await window.pywebview.api.get_config();

        } catch (e) {

        }
    },
};
