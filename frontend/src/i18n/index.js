// ── 界面语言（i18n）─────────────────────────────────────────────
// 全局响应式 lang：zh（默认）| en。切换后：
//  - 全部组件模板/脚本里的静态文本通过 t() 读取，自动重渲染
//  - 持久化到 localStorage（gtai_lang），刷新后保持
//  - 后端动态消息（如「项目已存在」「参数已保存」）经 trServer() 在英文态翻译
//  - LLM 生成的内容（气泡正文/思考链/审批 summary）不做翻译，取决于用户提问语言

import { ref } from 'vue'

const STORAGE_KEY = 'gtai_lang'

function loadLang() {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v === 'en' ? 'en' : 'zh'
  } catch (_) {
    return 'zh'
  }
}

export const lang = ref(loadLang())

export function setLang(l) {
  const next = l === 'en' ? 'en' : 'zh'
  lang.value = next
  try { localStorage.setItem(STORAGE_KEY, next) } catch (_) {}
  applyDocumentMeta(next)
}

function applyDocumentMeta(l) {
  try {
    document.documentElement.setAttribute('lang', l === 'en' ? 'en' : 'zh-CN')
    document.title = l === 'en'
      ? 'GeoThermoAI — High-Resolution LST Intelligent Reconstruction System'
      : 'GeoThermoAI — 高分辨率地表温度智能重建系统'
  } catch (_) {}
}

// 启动时同步一次 <html lang> 与标题
applyDocumentMeta(lang.value)

// ── 词典 ─────────────────────────────────────────────────────────

const zh = {
  'lang.label': '语言',
  'lang.buttonTitle': '切换界面语言',
  'lang.zhOption': '中文',
  'lang.enOption': 'English',

  // 登录页
  'login.sub': '高分辨率地表温度智能重建系统',
  'login.tabLogin': '登录',
  'login.tabRegister': '注册',
  'login.username': '账号名',
  'login.usernamePh': '仅字母/数字/_/-，2-32 位',
  'login.nickname': '昵称（可选，用于展示）',
  'login.nicknamePh': '留空则使用账号名',
  'login.password': '密码',
  'login.pwdRegPh': '至少 6 位，支持大小写字母、数字与符号',
  'login.pwdPh': '输入密码',
  'login.pleaseWait': '请稍候…',
  'login.loginBtn': '登 录',
  'login.regBtn': '注册并登录',
  'login.noAccount': '还没有账号？',
  'login.hasAccount': '已有账号？',
  'login.goRegister': '立即注册',
  'login.goLogin': '去登录',
  'login.errEmptyUser': '请输入账号名',
  'login.errEmptyPwd': '请输入密码',
  'login.errPwdLen': '密码至少 6 位',
  'login.errFailed': '操作失败',
  'login.showPwd': '显示密码',
  'login.hidePwd': '隐藏密码',

  // 侧边栏
  'sidebar.newProject': '新建项目',
  'sidebar.expandCollapse': '展开/收起',
  'sidebar.renameTitle': '点击重命名：',
  'sidebar.newConvTitle': '在此项目新建对话',
  'sidebar.delProjectTitle': '删除项目',
  'sidebar.delConvTitle': '删除对话',
  'sidebar.noConvs': '暂无对话，点击 + 新建',
  'sidebar.empty': '还没有项目，点击上方「新建项目」开始',
  'sidebar.logoutTitle': '退出登录',
  'sidebar.logout': '退出',
  'sidebar.modalNewProject': '新建项目',
  'sidebar.projectName': '项目名称',
  'sidebar.projectNamePh': '例如：武汉_202407',
  'sidebar.autoSaveHint': '数据将自动保存到你的私有工作区（按账号隔离）',
  'sidebar.create': '创建',
  'sidebar.cancel': '取消',
  'sidebar.newConvIn': '在「{name}」中新建对话',
  'sidebar.convName': '对话名称',
  'sidebar.convNamePh': '留空则默认「新对话」',
  'sidebar.delProject': '删除项目',
  'sidebar.delProjectBody': '确定删除项目「{name}」？将删除其全部对话文件、清除该项目产生的实验记忆与历史经验，并彻底删除该项目的工作区数据文件夹（原始影像与全部处理产物），此操作不可撤销。',
  'sidebar.del': '删除',
  'sidebar.delConv': '删除对话「{title}」',
  'sidebar.delConvBody': '确定删除对话「{title}」？将清除所有消息、运行中的进程，以及该对话产生的实验记录。',

  // 主界面
  'app.noConv': '未选择对话',
  'app.noModel': '未配置模型',
  'app.workbench': '工作面板',
  'app.openWorkbench': '打开工作面板',

  // 聊天区
  'chat.emptyIntro': '基于跨尺度热响应一致性的高分辨率地表温度智能重建系统',
  'chat.emptyExample': '描述你的任务，例如：「对武汉市做地表温度降尺度全流程处理」',
  'chat.thinking': '思考过程',
  'chat.thinkingSeconds': '(用时{sec}秒)',
  'chat.thinkingLive': '思考中…',
  'chatInput.collapsePath': '点击收起路径',
  'chatInput.fullPath': '点击查看完整路径',
  'chatInput.streamingPh': '回复生成中…',
  'chatInput.chatModePh': 'Chat 模式：只读对话，仅回答问题',
  'chatInput.placeholder': '输入指令…（Enter 发送，Shift+Enter 换行）',
  'chatInput.send': '发送',
  'chatMode.workHint': '完整能力：可执行降尺度工作流等相关任务',
  'chatMode.chatHint': '只读对话：仅回答问题，不执行工作流、不改动文件',
  'execMode.approval': '由我批准',
  'execMode.approvalHint': '关键节点会停下来问你',
  'execMode.auto': '完全执行',
  'execMode.autoHint': '一次跑完，不打断',

  // 配对卡片
  'pair.title': '找到 {n} 组影像配对，请选择一组',
  'pair.allTried': '以上影像组合都已尝试过，请考虑调整其他条件或更换时间范围',
  'pair.recommended': '（推荐）',
  'pair.tried': '（已尝试）',
  'pair.text': 'Landsat {ls} {ld}（{lc} 景, 覆盖 {lco}%）＋ Sentinel {ss}（{sc} 景, 覆盖 {sco}%）',
  'pair.lstCloud': 'Landsat 平均云量',
  'pair.senCloud': 'Sentinel 平均云量',
  'pair.cloud': '云量 {c}',
  'pair.cloudUnknown': '云量 未知',
  'pair.confirm': '确认选择',

  // 审批卡片
  'approval.recommended': '（推荐）',
  'approval.confirm': '确认',
  'approval.errSelect': '请选择一项',
  'approval.errRange': '{label} 需在 {min} 到 {max} 之间',
  'approval.title.plan_confirm': '执行方案已就绪，请确认是否开始',
  'approval.title.no_pair': '没有找到合格的影像组合，请选择下一步',
  'approval.title.data_quality': '数据检查未通过，请选择下一步',
  'approval.title.tuning_decision': '模型训练完成，请选择下一步',
  'approval.title.tuning_round': '本轮调优完成，请选择下一步',
  'approval.title.final_report': '全流程已完成',
  'approval.title.postprocess': '结果后处理（可选）',
  'approval.opt.start': '开始执行',
  'approval.opt.edit_request': '我要改需求',
  'approval.opt.ai_tune': '让系统继续自动调优',
  'approval.opt.manual_tune': '我自己设置参数',
  'approval.opt.accept': '接受当前结果，继续下一步',
  'approval.opt.next_round': '继续下一轮',
  'approval.opt.stop_tuning': '停止调优，取目前最好的一轮',
  'approval.opt.reselect_pair': '重新选择影像组合',
  'approval.opt.replan': '换时间或地区，重新规划',
  'approval.opt.relax_cloud': '放宽云量要求',
  'approval.opt.widen_time': '扩大时间范围',
  'approval.opt.change_source': '换数据源',
  'approval.opt.stop': '先停下来',
  'approval.opt.done': '结束',
  'approval.opt.more_analysis': '做其他分析',
  'approval.opt.run_postprocess': '执行结果后处理（空洞填补）',
  'approval.opt.skip_postprocess': '不需要，结束流程',
  'approval.hint.plan_start': '按上面的方案依次执行各个步骤',
  'approval.hint.plan_edit': '回到对话，重新说明研究区、时间或产品要求',
  'approval.hint.relax': '允许云量更高的影像进入候选',
  'approval.hint.widen': '向前后各延长搜索窗口',
  'approval.hint.reselect': '回到影像组合选择，换一组云量更低的重跑',
  'approval.hint.accept': '忽略本次检查未通过的提示，直接进入模型训练',
  'approval.hint.ai_tune': '在硬性规则约束下最多再训练 {n} 轮，自动选取误差最小的一轮',
  'approval.hint.next_round': '按规则给出的方向再调一次参数并重训',
  'approval.hint.more': '回到对话，继续提出新的分析需求',
  'approval.hint.post': '填充因云像元扣除造成的空洞，得到无空洞的 10m 地表温度产品',
  'approval.hint.skip': '保留当前带空洞的原始 10m 地表温度产品',

  // 工作流进度
  'wf.data_acquisition': '数据获取',
  'wf.data_pipeline': '数据预处理',
  'wf.ttri_compute': 'TTRI 计算',
  'wf.rf_model': '模型训练',
  'wf.tcr_compute': 'TCR 计算',
  'wf.lst_export': 'LST 导出',
  'wf.accuracy_eval': '精度评估',
  'wf.postprocess': '结果后处理（可选）',
  'wf.preprocessing': '数据预处理（含完整30m约束层）',
  'wf.split_dataset': '数据集划分（空间块+guard buffer）',
  'wf.ttri_train': 'TTRI训练集计算（仅train拟合一次）',
  'wf.train_rf': 'RF模型训练',
  'wf.predict_test': '测试集预测',
  'wf.ttri_predict': 'TTRI预测集计算',
  'wf.tcr': 'TCR计算',
  'wf.lst_final': 'LST最终计算',
  'wf.export_geotiff': 'GeoTIFF导出',
  'wf.evaluate_closure': '粗尺度闭合评估',
  'wf.status.completed': '完成',
  'wf.status.running': '进行中',
  'wf.status.failed': '失败',
  'wf.status.skipped_upstream': '未执行（上游失败）',
  'wf.status.skipped': '未执行（可选）',
  'wf.status.pending': '等待',

  // 工作面板
  'wb.api': 'API设置',
  'wb.datasource': '数据源',
  'wb.test': '测试',
  'wb.studyarea': '研究区',
  'wb.params': '初始参数',
  'wb.download': '下载',
  'wb.map': '地图',
  'wb.workflow': '进度',
  'wb.log': '日志',
  'wb.accuracy': '最终精度',
  'wb.resizeTitle': '拖动调整面板宽度',
  'wb.collapseTitle': '收起工作面板',
  'wb.collapse': '收起面板',
  'wb.currentModel': '当前模型：{tag}',
  'wb.footer': 'GeoThermoAI · 地表温度降尺度分析',
  'wb.noModel': '未配置模型',

  // API 设置
  'api.fmt': 'API 格式',
  'api.fmtOpenai': 'OpenAI Chat Completions 格式',
  'api.fmtAnthropic': 'Anthropic Messages 格式',
  'api.baseUrl': '请求地址',
  'api.urlHintOpenai': '请填写兼容 OpenAI 的地址，/chat/completions 会自动补到末尾',
  'api.urlHintAnthropic': '请填写 Claude API 地址，/v1/messages 会自动补到末尾',
  'api.modelId': '模型 ID',
  'api.apiKey': 'API 密钥',
  'api.apiKeyPh': '输入 API 密钥（保存后生效）；已配置时显示为黑点',
  'api.advanced': '高级配置',
  'api.displayName': '模型展示名称',
  'api.displayNamePh': '留空则用模型 ID',
  'api.ctxIn': '上下文-输入',
  'api.ctxOut': '上下文-输出',
  'api.saveApply': '保存并应用',
  'api.savedHot': '已保存并热更新模型',

  // 数据源
  'ds.hintTop': '数据源：Microsoft Planetary Computer（Landsat 8/9 L2、Sentinel-2 L2A、Copernicus DEM）＋ Copernicus Data Space（Sentinel-2 L2A、DEM，优先使用，国内访问更快），通过 STAC API 自动搜索下载',
  'ds.hintAuto': '各数据自动从数据源检索下载，无需手动上传原始影像',
  'ds.sentinel': 'Sentinel-2 L2A（多光谱 + SCL）',
  'ds.cdsePref': 'Copernicus Data Space（优先）',
  'ds.fbPc': '失败回退 Microsoft Planetary Computer',
  'ds.landsat': 'Landsat 8/9 L2（LST + QA）',
  'ds.msPc': 'Microsoft Planetary Computer',
  'ds.dem': 'DEM（Copernicus GLO-30）',
  'ds.s3Need': '需配置 S3 密钥，否则用 Microsoft Planetary Computer',
  'ds.account': 'Copernicus Data Space 账号',
  'ds.accountPh': '注册邮箱（dataspace.copernicus.eu）',
  'ds.password': '密码',
  'ds.passwordPh': '哥白尼数据空间登录密码；已配置时显示为黑点',
  'ds.adv': '高级配置（可选，非必需）',
  'ds.clientIdPh': '无账号时用于搜索影像',
  'ds.clientSecretPh': '无账号时用于搜索影像；已配置时显示为黑点',
  'ds.s3KeyPh': 'DEM 走 CDSE 必需；无账号时亦用于下载',
  'ds.s3SecretPh': '与 S3 Access Key 配套；已配置时显示为黑点',
  'ds.saving': '保存中…',
  'ds.saveBtn': '保存数据源配置',
  'ds.saved': '已保存并应用',
  'ds.hintBottom': 'Sentinel-2 填写账号密码即可优先走 Copernicus Data Space（国内更快）；DEM 走 CDSE 需额外配置 S3 密钥；未配置或下载失败时自动回退 Microsoft Planetary Computer',

  // 初始参数
  'mp.method': '机器学习方法',
  'mp.ntrees': 'n_estimators（决策树数量）：{n}',
  'mp.maxdepth': 'max_depth（最大深度）：{n}',
  'mp.save': '保存参数',

  // 测试页
  'dsrc.empty': '（空结果）',
  'dsrc.testing': '测试中…',
  'dsrc.failed': '❌ 测试失败：{msg}',
  'dsrc.pc': '测试 Planetary Computer 连接',
  'dsrc.cdse': '测试 Copernicus Data Space 连接',
  'dsrc.gdal': '测试地理处理环境',

  // 研究区
  'sa.hint': '上传研究区文件（GeoJSON / Shapefile），Agent 执行数据获取/全流程前需要使用研究区范围',
  'sa.upload': '点击选择研究区文件',
  'sa.uploaded': '已上传研究区（点击行切换当前使用）',
  'sa.current': '当前使用',
  'sa.setCurrent': '设为当前',
  'sa.delTitle': '删除该研究区',
  'sa.delModal': '删除研究区',
  'sa.delShp': '确定删除研究区「{name}」？将同时删除该文件及其配套的 Shapefile（.dbf/.shx/.prj）文件，此操作不可撤销。',
  'sa.delGeo': '确定删除研究区「{name}」？删除后不可恢复，此操作不可撤销。',
  'sa.del': '删除',
  'sa.cancel': '取消',

  // 文件下载
  'fd.noProject': '❌ 请先在左侧边栏新建项目',
  'fd.dirNotExist': '目录不存在',
  'fd.count': '✅ 共 {n} 个文件（含子目录）',
  'fd.emptyDir': '⚠️ 目录为空',
  'fd.tip': '下载项目目录中的文件（含子目录，相对路径显示为「子目录/文件名」）；支持勾选多个文件打包下载',
  'fd.refresh': '刷新文件列表',
  'fd.select': '选择文件（可多选）',
  'fd.selectAll': '全选 / 取消全选',
  'fd.packing': '打包下载中… {pct}%',
  'fd.downloadSelected': '下载已选 {n} 个文件（{size}）',
  'fd.browserDirect': '浏览器直接下载',
  'fd.bottomHint': '点击主按钮后前端会显示实时下载进度，多文件将打包为 zip，下载完成自动保存到浏览器下载目录',
  'fd.alertFail': '下载失败 {status}: {msg}',
  'fd.alertFail2': '下载失败: {msg}',

  // 日志
  'log.title': '实时日志',
  'log.count': '{n} 行',
  'log.copied': '已复制',
  'log.copy': '复制',
  'log.clear': '清除',
  'log.usageTitle': '本软件实时占用（含所有项目数据）',
  'log.usage': '内存 {m}G · 磁盘 {d}G',

  // 精度
  'acc.refresh': '刷新精度',
  'acc.loading': '精度数据加载中…',
  'acc.none': '暂无精度数据，请先运行完整流程',
  'acc.loadFailed': '读取失败，请稍后重试',
  'acc.errPrefix': '读取失败：{msg}',
  'acc.testSet': '测试区精度',
  'acc.mb': '平均偏差 (K)',
  'acc.samples': '样本数',
  'acc.closure': '与 30m 温度对照',
  'acc.subClosure': '温度高低端差异（正值表示 10m 更高）',
  'acc.minDiff': '最低温度差',
  'acc.maxDiff': '最高温度差',
  'acc.range30': '30m 地表温度值域范围',
  'acc.range10': '10m 地表温度（有空洞）值域范围',
  'acc.range10f': '10m 地表温度（填补空洞后）值域范围',

  // 地图
  'map.street': '街道地图',
  'map.satellite': '卫星影像',
  'map.esri': 'Esri 卫星',
  'map.attrGaode': '© 高德地图',
  'map.refresh': '刷新',
  'map.hideLayers': '▸ 收起图层',
  'map.showLayers': '◂ 图层控制',
  'map.noLayers': '未发现可渲染的数据图层',
  'map.noLayersSub': '请选择已设置项目目录的对话，并完成数据下载 / LST 生成后刷新',
  'map.layers': '图层控制',
  'map.showTempTitle': '显示鼠标所在像元在各 LST 图层上的温度',
  'map.hideTempTitle': '关闭温度显示，恢复地图操作',
  'map.hideTemp': '关闭温度',
  'map.showTemp': '显示温度',
  'map.defaultGroup': '图层',
  'map.opacityTitle': '透明度 {pct}%',
  'map.pixelTemp': '像元温度',
  'map.locked': '已锁定',
  'map.noLst': '请勾选至少一个LST图层',

  // store 消息
  'auth.loginFailed': '登录失败',
  'auth.registerFailed': '注册失败',
  'project.enterName': '请输入项目名称',
  'project.enterNewName': '请输入新的项目名称',
  'project.selectFirst': '请先选择项目',
  'project.newConv': '新对话',
  'project.selectFile': '请选择文件',
  'project.uploadWarn': '上传完成：{n} 个研究区文件未通过验证，详见研究区面板',
  'project.uploadDone': '研究区上传完成',
  'project.noArea': '未指定研究区',
  'chat.busy': '上一条回复还在生成中，请稍候',
  'chat.sendFailed': '发送失败',
  'chat.sendFailedMsg': '发送失败：{msg}',
  'chat.execError': '执行出错',
  'chat.resumeFailed': '恢复失败：{msg}',
}

const en = {
  'lang.label': 'Language',
  'lang.buttonTitle': 'Switch interface language',
  'lang.zhOption': '中文',
  'lang.enOption': 'English',

  'login.sub': 'Intelligent High-Resolution Land Surface Temperature Reconstruction System',
  'login.tabLogin': 'Login',
  'login.tabRegister': 'Register',
  'login.username': 'Username',
  'login.usernamePh': 'Letters/numbers/_/-, 2-32 chars',
  'login.nickname': 'Nickname (optional, for display)',
  'login.nicknamePh': 'Leave blank to use the username',
  'login.password': 'Password',
  'login.pwdRegPh': 'At least 6 chars; letters, numbers and symbols',
  'login.pwdPh': 'Enter password',
  'login.pleaseWait': 'Please wait…',
  'login.loginBtn': 'Log In',
  'login.regBtn': 'Register & Log In',
  'login.noAccount': 'No account yet?',
  'login.hasAccount': 'Already have an account?',
  'login.goRegister': 'Register now',
  'login.goLogin': 'Log in',
  'login.errEmptyUser': 'Please enter a username',
  'login.errEmptyPwd': 'Please enter a password',
  'login.errPwdLen': 'Password must be at least 6 characters',
  'login.errFailed': 'Operation failed',
  'login.showPwd': 'Show password',
  'login.hidePwd': 'Hide password',

  'sidebar.newProject': 'New Project',
  'sidebar.expandCollapse': 'Expand / Collapse',
  'sidebar.renameTitle': 'Click to rename: ',
  'sidebar.newConvTitle': 'New conversation in this project',
  'sidebar.delProjectTitle': 'Delete project',
  'sidebar.delConvTitle': 'Delete conversation',
  'sidebar.noConvs': 'No conversations yet, click + to create',
  'sidebar.empty': 'No projects yet. Click "New Project" above to get started',
  'sidebar.logoutTitle': 'Log out',
  'sidebar.logout': 'Logout',
  'sidebar.modalNewProject': 'New Project',
  'sidebar.projectName': 'Project name',
  'sidebar.projectNamePh': 'e.g. Wuhan_202407',
  'sidebar.autoSaveHint': 'Data is auto-saved to your private workspace (isolated per account)',
  'sidebar.create': 'Create',
  'sidebar.cancel': 'Cancel',
  'sidebar.newConvIn': 'New conversation in "{name}"',
  'sidebar.convName': 'Conversation name',
  'sidebar.convNamePh': 'Leave blank for default "New conversation"',
  'sidebar.delProject': 'Delete Project',
  'sidebar.delProjectBody': 'Delete project "{name}"? All conversation files, experiment memory and history will be removed, and the project workspace folder (raw imagery and all outputs) will be permanently deleted. This action cannot be undone.',
  'sidebar.del': 'Delete',
  'sidebar.delConv': 'Delete conversation "{title}"',
  'sidebar.delConvBody': 'Delete conversation "{title}"? All messages, running processes, and experiment records will be removed.',

  'app.noConv': 'No conversation selected',
  'app.noModel': 'Model not configured',
  'app.workbench': 'Workbench',
  'app.openWorkbench': 'Open workbench',

  'chat.emptyIntro': 'Intelligent high-resolution land surface temperature reconstruction system based on cross-scale thermal response consistency',
  'chat.emptyExample': 'Describe your task, e.g. "Run the full LST downscaling workflow for Wuhan City"',
  'chat.thinking': 'Thinking process',
  'chat.thinkingSeconds': '({sec}s)',
  'chat.thinkingLive': 'Thinking…',
  'chatInput.collapsePath': 'Click to collapse path',
  'chatInput.fullPath': 'Click to view full path',
  'chatInput.streamingPh': 'Generating reply…',
  'chatInput.chatModePh': 'Chat mode: read-only, only answers questions',
  'chatInput.placeholder': 'Type a command… (Enter to send, Shift+Enter for newline)',
  'chatInput.send': 'Send',
  'chatMode.workHint': 'Full capability: executes downscaling workflows and related tasks',
  'chatMode.chatHint': 'Read-only chat: answers questions only, no workflows, no file changes',
  'execMode.approval': 'Ask me',
  'execMode.approvalHint': 'Pauses at key nodes to ask you',
  'execMode.auto': 'Full execution',
  'execMode.autoHint': 'Runs through without interruption',

  'pair.title': 'Found {n} imagery pairs, please select one',
  'pair.allTried': 'All above combinations have been tried. Consider adjusting other conditions or changing the time range',
  'pair.recommended': ' (Recommended)',
  'pair.tried': ' (Tried)',
  'pair.text': 'Landsat {ls} {ld} ({lc} scenes, {lco}% coverage) + Sentinel {ss} ({sc} scenes, {sco}% coverage)',
  'pair.lstCloud': 'Landsat avg cloud',
  'pair.senCloud': 'Sentinel avg cloud',
  'pair.cloud': 'Cloud {c}',
  'pair.cloudUnknown': 'Cloud: unknown',
  'pair.confirm': 'Confirm Selection',

  'approval.recommended': ' (Recommended)',
  'approval.confirm': 'Confirm',
  'approval.errSelect': 'Please select an option',
  'approval.errRange': '{label} must be between {min} and {max}',
  'approval.title.plan_confirm': 'The execution plan is ready. Please confirm to start',
  'approval.title.no_pair': 'No qualified imagery pairs found. Please choose the next step',
  'approval.title.data_quality': 'Data check failed. Please choose the next step',
  'approval.title.tuning_decision': 'Model training complete. Please choose the next step',
  'approval.title.tuning_round': 'This round of tuning is complete. Please choose the next step',
  'approval.title.final_report': 'Full workflow completed',
  'approval.title.postprocess': 'Result post-processing (optional)',
  'approval.opt.start': 'Start execution',
  'approval.opt.edit_request': 'I want to change the request',
  'approval.opt.ai_tune': 'Let the system continue auto-tuning',
  'approval.opt.manual_tune': 'I will set the parameters myself',
  'approval.opt.accept': 'Accept current result and continue',
  'approval.opt.next_round': 'Continue to the next round',
  'approval.opt.stop_tuning': 'Stop tuning, keep the best round so far',
  'approval.opt.reselect_pair': 'Reselect imagery pairs',
  'approval.opt.replan': 'Change time/area and re-plan',
  'approval.opt.relax_cloud': 'Relax cloud cover requirement',
  'approval.opt.widen_time': 'Widen the time range',
  'approval.opt.change_source': 'Change data source',
  'approval.opt.stop': 'Stop here',
  'approval.opt.done': 'Finish',
  'approval.opt.more_analysis': 'Do other analysis',
  'approval.opt.run_postprocess': 'Run post-processing (gap filling)',
  'approval.opt.skip_postprocess': 'Not needed, end the workflow',
  'approval.hint.plan_start': 'Execute the steps in sequence as planned above',
  'approval.hint.plan_edit': 'Back to chat to restate study area, time or product requirements',
  'approval.hint.relax': 'Allow imagery with higher cloud cover into candidates',
  'approval.hint.widen': 'Extend the search window backward and forward',
  'approval.hint.reselect': 'Back to pair selection to retry with lower cloud cover',
  'approval.hint.accept': 'Ignore the failed check and proceed directly to model training',
  'approval.hint.ai_tune': 'Train up to {n} more rounds under hard rules, auto-picking the round with the lowest error',
  'approval.hint.next_round': 'Tune parameters once more per the rules and retrain',
  'approval.hint.more': 'Back to chat to continue with new analysis requests',
  'approval.hint.post': 'Fill holes caused by cloud-pixel removal to produce a gap-free 10m LST product',
  'approval.hint.skip': 'Keep the current 10m LST product with holes',

  'wf.data_acquisition': 'Data Acquisition',
  'wf.data_pipeline': 'Data Preprocessing',
  'wf.ttri_compute': 'TTRI Computation',
  'wf.rf_model': 'Model Training',
  'wf.tcr_compute': 'TCR Computation',
  'wf.lst_export': 'LST Export',
  'wf.accuracy_eval': 'Accuracy Evaluation',
  'wf.postprocess': 'Post-processing (optional)',
  'wf.preprocessing': 'Data preprocessing (incl. full 30m constraint layer)',
  'wf.split_dataset': 'Dataset split (spatial blocks + guard buffer)',
  'wf.ttri_train': 'TTRI training set computation (fit once)',
  'wf.train_rf': 'RF model training',
  'wf.predict_test': 'Test set prediction',
  'wf.ttri_predict': 'TTRI prediction set computation',
  'wf.tcr': 'TCR computation',
  'wf.lst_final': 'Final LST computation',
  'wf.export_geotiff': 'GeoTIFF export',
  'wf.evaluate_closure': 'Coarse-scale closure evaluation',
  'wf.status.completed': 'Done',
  'wf.status.running': 'Running',
  'wf.status.failed': 'Failed',
  'wf.status.skipped_upstream': 'Skipped (upstream failed)',
  'wf.status.skipped': 'Not executed (optional)',
  'wf.status.pending': 'Pending',

  'wb.api': 'API Settings',
  'wb.datasource': 'Data Sources',
  'wb.test': 'Test',
  'wb.studyarea': 'Study Area',
  'wb.params': 'Initial Params',
  'wb.download': 'Download',
  'wb.map': 'Map',
  'wb.workflow': 'Progress',
  'wb.log': 'Logs',
  'wb.accuracy': 'Final Accuracy',
  'wb.resizeTitle': 'Drag to resize the panel',
  'wb.collapseTitle': 'Collapse the workbench',
  'wb.collapse': 'Collapse',
  'wb.currentModel': 'Current model: {tag}',
  'wb.footer': 'GeoThermoAI · LST Downscaling Analysis',
  'wb.noModel': 'Model not configured',

  'api.fmt': 'API Format',
  'api.fmtOpenai': 'OpenAI Chat Completions Format',
  'api.fmtAnthropic': 'Anthropic Messages Format',
  'api.baseUrl': 'Base URL',
  'api.urlHintOpenai': 'Enter an OpenAI-compatible URL; /chat/completions is appended automatically',
  'api.urlHintAnthropic': 'Enter the Claude API URL; /v1/messages is appended automatically',
  'api.modelId': 'Model ID',
  'api.apiKey': 'API Key',
  'api.apiKeyPh': 'Enter the API key (takes effect after saving); shown as dots when configured',
  'api.advanced': 'Advanced',
  'api.displayName': 'Display name',
  'api.displayNamePh': 'Leave blank to use the model ID',
  'api.ctxIn': 'Context - Input',
  'api.ctxOut': 'Context - Output',
  'api.saveApply': 'Save & Apply',
  'api.savedHot': 'Saved; model hot-reloaded',

  'ds.hintTop': 'Data sources: Microsoft Planetary Computer (Landsat 8/9 L2, Sentinel-2 L2A, Copernicus DEM) + Copernicus Data Space (Sentinel-2 L2A, DEM, preferred, faster in China), searched & downloaded automatically via STAC API',
  'ds.hintAuto': 'All data is retrieved from the data sources automatically; no manual raw imagery upload needed',
  'ds.sentinel': 'Sentinel-2 L2A (multispectral + SCL)',
  'ds.cdsePref': 'Copernicus Data Space (preferred)',
  'ds.fbPc': 'Falls back to Microsoft Planetary Computer',
  'ds.landsat': 'Landsat 8/9 L2 (LST + QA)',
  'ds.msPc': 'Microsoft Planetary Computer',
  'ds.dem': 'DEM (Copernicus GLO-30)',
  'ds.s3Need': 'Requires S3 keys, otherwise uses Microsoft Planetary Computer',
  'ds.account': 'Copernicus Data Space Account',
  'ds.accountPh': 'Registration email (dataspace.copernicus.eu)',
  'ds.password': 'Password',
  'ds.passwordPh': 'Copernicus Data Space login password; shown as dots when configured',
  'ds.adv': 'Advanced (optional)',
  'ds.clientIdPh': 'Used for imagery search without an account',
  'ds.clientSecretPh': 'Used for imagery search without an account; shown as dots when configured',
  'ds.s3KeyPh': 'Required for DEM via CDSE; also used for downloads without an account',
  'ds.s3SecretPh': 'Pairs with the S3 Access Key; shown as dots when configured',
  'ds.saving': 'Saving…',
  'ds.saveBtn': 'Save Data Source Config',
  'ds.saved': 'Saved and applied',
  'ds.hintBottom': 'For Sentinel-2, entering the account and password gives priority to Copernicus Data Space (faster in China); DEM via CDSE requires additional S3 keys; if unconfigured or downloads fail, it automatically falls back to Microsoft Planetary Computer',

  'mp.method': 'ML Method',
  'mp.ntrees': 'n_estimators (number of trees): {n}',
  'mp.maxdepth': 'max_depth (max depth): {n}',
  'mp.save': 'Save Params',

  'dsrc.empty': '(Empty result)',
  'dsrc.testing': 'Testing…',
  'dsrc.failed': '❌ Test failed: {msg}',
  'dsrc.pc': 'Test Planetary Computer connection',
  'dsrc.cdse': 'Test Copernicus Data Space connection',
  'dsrc.gdal': 'Test geospatial processing environment',

  'sa.hint': 'Upload study area files (GeoJSON / Shapefile). The Agent needs the study area boundary before data acquisition / full workflow',
  'sa.upload': 'Click to select study area files',
  'sa.uploaded': 'Uploaded study areas (click a row to switch)',
  'sa.current': 'In use',
  'sa.setCurrent': 'Set current',
  'sa.delTitle': 'Delete this study area',
  'sa.delModal': 'Delete Study Area',
  'sa.delShp': 'Delete study area "{name}"? Its companion Shapefile files (.dbf/.shx/.prj) will also be removed. This action cannot be undone.',
  'sa.delGeo': 'Delete study area "{name}"? It cannot be recovered. This action cannot be undone.',
  'sa.del': 'Delete',
  'sa.cancel': 'Cancel',

  'fd.noProject': '❌ Please create a project in the left sidebar first',
  'fd.dirNotExist': 'Directory does not exist',
  'fd.count': '✅ {n} files in total (incl. subdirectories)',
  'fd.emptyDir': '⚠️ The directory is empty',
  'fd.tip': 'Download files from the project directory (incl. subdirectories; relative paths shown as "subdir/file"); multiple files can be selected and packed into a zip',
  'fd.refresh': 'Refresh file list',
  'fd.select': 'Select files (multi-select)',
  'fd.selectAll': 'Select all / Deselect all',
  'fd.packing': 'Packing & downloading… {pct}%',
  'fd.downloadSelected': 'Download {n} selected file(s) ({size})',
  'fd.browserDirect': 'Direct browser download',
  'fd.bottomHint': 'Clicking the main button shows real-time progress; multiple files are packed into a zip and auto-saved to the browser download folder',
  'fd.alertFail': 'Download failed {status}: {msg}',
  'fd.alertFail2': 'Download failed: {msg}',

  'log.title': 'Live Logs',
  'log.count': '{n} lines',
  'log.copied': 'Copied',
  'log.copy': 'Copy',
  'log.clear': 'Clear',
  'log.usageTitle': 'Real-time usage of this software (incl. all project data)',
  'log.usage': 'Mem {m}G · Disk {d}G',

  'acc.refresh': 'Refresh accuracy',
  'acc.loading': 'Loading accuracy data…',
  'acc.none': 'No accuracy data yet. Please run the full workflow first',
  'acc.loadFailed': 'Failed to load; please retry later',
  'acc.errPrefix': 'Load failed: {msg}',
  'acc.testSet': 'Test Set Accuracy',
  'acc.mb': 'Mean Bias (K)',
  'acc.samples': 'Samples',
  'acc.closure': 'Comparison with 30m LST',
  'acc.subClosure': 'High/low-end temperature differences (positive means 10m is higher)',
  'acc.minDiff': 'Min temp difference',
  'acc.maxDiff': 'Max temp difference',
  'acc.range30': '30m LST value range',
  'acc.range10': '10m LST (with holes) value range',
  'acc.range10f': '10m LST (after gap-filling) value range',

  'map.street': 'Street Map',
  'map.satellite': 'Satellite',
  'map.esri': 'Esri Satellite',
  'map.attrGaode': '© AutoNavi',
  'map.refresh': 'Refresh',
  'map.hideLayers': '▸ Hide layers',
  'map.showLayers': '◂ Layers',
  'map.noLayers': 'No renderable data layers found',
  'map.noLayersSub': 'Select a conversation with a project directory set, then refresh after data download / LST generation',
  'map.layers': 'Layers',
  'map.showTempTitle': 'Show the temperature of the pixel under the cursor on each LST layer',
  'map.hideTempTitle': 'Turn off temperature display and restore map interaction',
  'map.hideTemp': 'Hide temp',
  'map.showTemp': 'Show temp',
  'map.defaultGroup': 'Layers',
  'map.opacityTitle': 'Opacity {pct}%',
  'map.pixelTemp': 'Pixel Temperature',
  'map.locked': 'Locked',
  'map.noLst': 'Check at least one LST layer',

  'auth.loginFailed': 'Login failed',
  'auth.registerFailed': 'Registration failed',
  'project.enterName': 'Please enter a project name',
  'project.enterNewName': 'Please enter a new project name',
  'project.selectFirst': 'Please select a project first',
  'project.newConv': 'New conversation',
  'project.selectFile': 'Please select a file',
  'project.uploadWarn': 'Upload complete: {n} study area file(s) failed validation. See the Study Area panel',
  'project.uploadDone': 'Study area upload complete',
  'project.noArea': 'No study area specified',
  'chat.busy': 'The previous reply is still generating; please wait',
  'chat.sendFailed': 'Send failed',
  'chat.sendFailedMsg': 'Send failed: {msg}',
  'chat.execError': 'Execution error',
  'chat.resumeFailed': 'Resume failed: {msg}',
}

// ── 翻译函数 ─────────────────────────────────────────────────────
// lang 是响应式 ref，t() 在渲染期间读取它 → 切换语言时所有组件自动重渲染

export function t(key, params) {
  const d = lang.value === 'en' ? en : zh
  let s = d[key]
  if (s === undefined) s = zh[key]
  if (s === undefined) return key
  if (params) {
    for (const k in params) {
      s = s.split(`{${k}}`).join(String(params[k]))
    }
  }
  return s
}

export function useI18n() {
  return { lang, t, setLang }
}

// ── 后端动态消息翻译（英文态）───────────────────────────────────
// 只翻译已知的固定/半固定中文模板；未匹配的原文透传（LLM 生成内容不在此列）。

const SERVER_MSG_PATTERNS = [
  // 账号
  [/^失败次数过多，请 1 分钟后再试$/, 'Too many failed attempts, please try again in 1 minute'],
  [/^账号或密码错误$/, 'Incorrect username or password'],
  [/^用户不存在$/, 'User does not exist'],
  [/^账号名仅允许 2-32 位字母\/数字\/_\/-$/, 'Username must be 2-32 chars of letters/numbers/_/-'],
  [/^密码至少 (\d+) 位，仅允许英文字母\/数字\/符号$/, 'Password must be at least {1} chars of letters/numbers/symbols'],
  [/^账号名已存在$/, 'The username already exists'],
  // 项目
  [/^请输入项目名称$/, 'Please enter a project name'],
  [/^项目已存在$/, 'The project already exists'],
  [/^无法创建项目目录：(.+)（(.+)）$/, 'Cannot create project directory: {1} ({2})'],
  [/^项目「(.+?)」创建成功$/, 'Project "{1}" created'],
  [/^请输入新的项目名称$/, 'Please enter a new project name'],
  [/^项目不存在$/, 'The project does not exist'],
  [/^名称未变化$/, 'Name unchanged'],
  [/^项目已重命名为「(.+?)」$/, 'Project renamed to "{1}"'],
  [/^项目「(.+?)」已删除$/, 'Project "{1}" deleted'],
  [/^项目不存在，请先创建项目$/, 'The project does not exist, please create it first'],
  [/^对话「(.+?)」创建成功$/, 'Conversation "{1}" created'],
  [/^对话不存在$/, 'The conversation does not exist'],
  [/^对话「(.+?)」已彻底删除$/, 'Conversation "{1}" deleted'],
  [/^请先选择项目$/, 'Please select a project first'],
  [/^项目目录已保存$/, 'Project directory saved'],
  // 设置
  [/^API 设置已保存并应用$/, 'API settings saved and applied'],
  [/^参数已保存$/, 'Parameters saved'],
  // 对话
  [/^消息不能为空$/, 'Message cannot be empty'],
  [/^该对话已有任务在执行中，请等待当前任务完成$/, 'This conversation already has a task running, please wait for it to finish'],
  [/^对话不存在，请先选择对话$/, 'The conversation does not exist, please select one first'],
  [/^没有待恢复的流$/, 'No stream to resume'],
  [/^没有待处理的选择，请重新发送指令$/, 'No pending selection, please resend your instruction'],
  [/^没有待选配对，请重新发送指令$/, 'No pending pairs, please resend your instruction'],
  // 文件 / 下载
  [/^未选择文件$/, 'No file selected'],
  [/^上传失败 (\d+)$/, 'Upload failed {1}'],
  [/^无权访问该目录$/, 'No permission to access this directory'],
  [/^目录不存在或未设置$/, 'Directory does not exist or is not set'],
  [/^非法路径$/, 'Invalid path'],
  [/^该文件为中间过程产物，不提供下载$/, 'This file is an intermediate product and is not available for download'],
  [/^该文件正在生成中，请稍后再试$/, 'This file is being generated, please try again later'],
  [/^文件不存在$/, 'The file does not exist'],
  // 研究区
  [/^未指定研究区文件名$/, 'No study area file name specified'],
  [/^研究区文件不存在：(.+)$/, 'Study area file does not exist: {1}'],
  [/^研究区文件不存在$/, 'Study area file does not exist'],
  [/^已删除 (.+)$/, 'Deleted {1}'],
  [/^删除失败：(.+)$/, 'Delete failed: {1}'],
  [/^已切换当前研究区为 (.+)$/, 'Current study area switched to {1}'],
]

export function trServer(msg) {
  if (!msg || lang.value !== 'en') return msg
  const s = String(msg)
  for (const [re, enTpl] of SERVER_MSG_PATTERNS) {
    const m = re.exec(s)
    if (m) {
      let out = enTpl
      for (let i = 1; i < m.length; i++) out = out.split(`{${i}}`).join(m[i])
      return out
    }
  }
  return s
}

// ── 审批载荷翻译（英文态）：按 node / option id 映射静态文案 ─────
const APPROVAL_NODE_TITLE = (node) => t(`approval.title.${node}`)
const APPROVAL_OPTION_LABEL = (optId) => t(`approval.opt.${optId}`)

// ai_tune 的 hint 带动态轮数：从原文提取数字填入英文模板
function aiTuneHint(origHint) {
  const m = /最多再训练 (\d+) 轮/.exec(String(origHint || ''))
  return t('approval.hint.ai_tune', { n: m ? m[1] : '5' })
}

export function translateApprovalPayload(payload) {
  if (!payload || lang.value !== 'en') return payload
  const node = payload.node
  const out = { ...payload }
  if (node && t(`approval.title.${node}`) !== `approval.title.${node}`) {
    out.title = APPROVAL_NODE_TITLE(node)
  }
  const options = (payload.options || []).map((o) => {
    const item = { ...o }
    const labelKey = `approval.opt.${o.id}`
    const translated = t(labelKey)
    if (translated !== labelKey) item.label = translated
    if (o.hint) {
      let hintEn = null
      if (o.id === 'ai_tune') hintEn = aiTuneHint(o.hint)
      else {
        const hintKey = {
          start: 'approval.hint.plan_start',
          edit_request: 'approval.hint.plan_edit',
          relax_cloud: 'approval.hint.relax',
          widen_time: 'approval.hint.widen',
          reselect_pair: 'approval.hint.reselect',
          accept: 'approval.hint.accept',
          next_round: 'approval.hint.next_round',
          more_analysis: 'approval.hint.more',
          run_postprocess: 'approval.hint.post',
          skip_postprocess: 'approval.hint.skip',
        }[o.id]
        if (hintKey) hintEn = t(hintKey)
      }
      if (hintEn) item.hint = hintEn
    }
    // 手动调参表单字段：按超参名映射英文标签
    if (item.fields) {
      const HP_EN = {
        n_estimators: 'Number of trees',
        max_depth: 'Max depth',
        min_samples_split: 'Min samples per split',
        min_samples_leaf: 'Min samples per leaf',
        max_features: 'Max feature ratio',
      }
      item.fields = item.fields.map((f) => ({
        ...f,
        label: HP_EN[f.name] || f.label,
      }))
    }
    return item
  })
  out.options = options
  return out
}

// ── 工作流步骤标签（英文态）：按 step id 映射 ──────────────────
export function wfStepLabel(id, fallback) {
  if (lang.value === 'en') {
    const k = `wf.${id}`
    const v = t(k)
    if (v !== k) return v
  }
  return fallback
}

// ── 地图图层标签（英文态）：替换已知中文片段 ────────────────────
const LAYER_LABEL_MAP = [
  [/30m LST（无空洞）/g, '30m LST (gap-filled)'],
  [/10m LST（无空洞）/g, '10m LST (gap-filled)'],
  [/（无空洞）/g, ' (gap-filled)'],
  [/图层/g, 'Layers'],
  [/10m LST/g, '10m LST'],
  [/30m LST/g, '30m LST'],
  [/Sentinel-2 RGB/g, 'Sentinel-2 RGB'],
  [/DEM/g, 'DEM'],
]

export function mapLayerLabel(label) {
  if (lang.value !== 'en' || !label) return label
  let out = String(label)
  for (const [re, to] of LAYER_LABEL_MAP) out = out.replace(re, to)
  // 中文全角括号 → 英文半角（如 "30m LST（2024-07）" → "30m LST (2024-07)"）
  out = out.replace(/（/g, ' (').replace(/）\s*/g, ')')
  return out
}
