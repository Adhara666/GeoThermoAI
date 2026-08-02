<script setup>
import { ref, computed, nextTick } from 'vue'
import { useProjectStore } from '../stores/project'
import { useChatStore } from '../stores/chat'

defineProps({ open: Boolean })
const emit = defineEmits(['close'])

const project = useProjectStore()
const chat = useChatStore()

// ── 新建项目弹窗 ────────────────────────────────────────────
const showNewProject = ref(false)
const newProjectName = ref('')
const newProjectDir = ref('')

function openNewProject() {
  newProjectName.value = ''
  newProjectDir.value = '/home/studio_service/PROJECT/output'
  showNewProject.value = true
}

async function onNewProject() {
  if (!newProjectName.value.trim()) return
  await project.createProject(newProjectName.value, newProjectDir.value)
  showNewProject.value = false
}

// ── 新建对话弹窗（针对某个项目） ────────────────────────────
const showNewConv = ref(false)
const convTarget = ref('')
const newConvTitle = ref('')

function openNewConv(pid) {
  convTarget.value = pid
  newConvTitle.value = ''
  showNewConv.value = true
}

async function onNewConv() {
  await project.createConv(newConvTitle.value, convTarget.value)
  showNewConv.value = false
}

// ── 删除确认弹窗（页面内弹窗，替代浏览器原生 confirm） ─────────
const deleteProjectTarget = ref('')
const deleteConvTarget = ref(null) // {pid, cid, title}

function openDeleteConv(p, c) {
  deleteConvTarget.value = { pid: p.project, cid: c.id, title: c.title }
}

async function confirmDeleteProject() {
  const pid = deleteProjectTarget.value
  deleteProjectTarget.value = ''
  if (pid) await project.deleteProject(pid)
}

async function confirmDeleteConv() {
  const t = deleteConvTarget.value
  deleteConvTarget.value = null
  if (t) await project.deleteConv(t.cid, t.pid)
}

// ── Codex 式项目列表：展开/收起 ─────────────────────────────
const collapsed = ref({}) // pid -> true（默认展开）
const isExpanded = (pid) => !collapsed.value[pid]

function toggleExpand(pid) {
  collapsed.value[pid] = !collapsed.value[pid]
}

// ── 项目重命名：点击名称 → 行内编辑 ────────────────────────
const renamingPid = ref('')
const renameValue = ref('')
const renameInput = ref(null)

function startRename(p) {
  renamingPid.value = p.project
  renameValue.value = p.project
  nextTick(() => {
    if (renameInput.value) {
      renameInput.value.focus()
      renameInput.value.select()
    }
  })
}

async function onRenameConfirm() {
  const pid = renamingPid.value
  if (!pid) return
  renamingPid.value = ''
  if (!renameValue.value.trim() || renameValue.value.trim() === pid) return
  await project.renameProject(pid, renameValue.value.trim())
}

async function onSelectConv(p, c) {
  await project.selectProject(p.project)
  await project.selectConv(c.id)
  await chat.loadMessages(p.project, c.id)
  // 若该对话仍有正在运行的流，恢复订阅让气泡继续实时更新
  await chat.resumeIfStreaming(c.id)
  await chat.refreshWorkflow(c.id)
  emit('close')
}

const hasAny = computed(() => project.tree.length > 0)
</script>

<template>
  <aside class="sidebar" :class="{ 'sidebar--open': open }">
    <div class="sidebar__brand">
      <img src="/logo.png?v=2" alt="GeoThermoAI" onerror="this.style.display='none'" />
      <div>
        <div class="sidebar__brand-name">GeoThermoAI</div>
        <div class="sidebar__brand-sub">高分辨率地表温度智能重建系统</div>
      </div>
    </div>

    <div class="sidebar__body">
      <div class="sidebar__new">
        <button class="btn btn--primary btn--block" @click="openNewProject">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          新建项目
        </button>
      </div>

      <!-- Codex 式项目列表 -->
      <div v-if="hasAny" class="project-list">
        <div
          v-for="p in project.tree"
          :key="p.project"
          class="project-card"
          :class="{ 'project-card--active': p.project === project.currentProject }"
        >
          <div class="project-card__head">
            <button
              class="project-card__arrow"
              :class="{ 'project-card__arrow--open': isExpanded(p.project) }"
              title="展开/收起"
              @click="toggleExpand(p.project)"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
            <span
              v-if="renamingPid !== p.project"
              class="project-card__name"
              :title="'点击重命名：' + p.project"
              @click="startRename(p)"
            >{{ p.project }}</span>
            <input
              v-else
              ref="renameInput"
              v-model="renameValue"
              class="form-input project-card__rename"
              @keyup.enter="onRenameConfirm"
              @keyup.esc="renamingPid = ''"
              @blur="onRenameConfirm"
              @click.stop
            />
            <span class="project-card__count">{{ p.conversations.length }}</span>
            <button class="project-card__btn" title="在此项目新建对话" @click="openNewConv(p.project)">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            </button>
            <button class="project-card__btn project-card__btn--del" title="删除项目" @click="deleteProjectTarget = p.project">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>

          <div v-if="isExpanded(p.project)" class="project-card__convs">
            <div
              v-for="c in p.conversations"
              :key="c.id"
              class="conv-item"
              :class="{ 'conv-item--active': c.id === project.currentConv && p.project === project.currentProject }"
              @click="onSelectConv(p, c)"
            >
              <span class="conv-item__title" :title="c.title">{{ c.title }}</span>
              <button class="conv-item__del" title="删除对话" @click.stop="openDeleteConv(p, c)">✕</button>
            </div>
            <div v-if="!p.conversations.length" class="project-card__empty">
              暂无对话，点击 + 新建
            </div>
          </div>
        </div>
      </div>
      <div v-else class="sidebar-empty">
        还没有项目，点击上方「新建项目」开始
      </div>
    </div>

    <div class="sidebar__footer">
      <div>GeoThermoAI</div>
    </div>

    <!-- 新建项目弹窗 -->
    <div v-if="showNewProject" class="modal-mask" @click.self="showNewProject = false">
      <div class="modal-card">
        <h3>新建项目</h3>
        <div class="form-group">
          <label>项目名称</label>
          <input
            v-model="newProjectName"
            class="form-input"
            placeholder="例如：武汉_202407"
            @keyup.enter="onNewProject"
            autofocus
          />
        </div>
        <div class="form-group">
          <label>项目保存路径</label>
          <input
            v-model="newProjectDir"
            class="form-input"
            placeholder="/home/studio_service/PROJECT/output"
            @keyup.enter="onNewProject"
          />
        </div>
        <div class="modal-actions">
          <button class="btn btn--confirm" @click="onNewProject">创建</button>
          <button class="btn btn--cancel" @click="showNewProject = false">取消</button>
        </div>
      </div>
    </div>

    <!-- 新建对话弹窗 -->
    <div v-if="showNewConv" class="modal-mask" @click.self="showNewConv = false">
      <div class="modal-card">
        <h3>在「{{ convTarget }}」中新建对话</h3>
        <div class="form-group">
          <label>对话名称</label>
          <input
            v-model="newConvTitle"
            class="form-input"
            placeholder="留空则默认「新对话」"
            @keyup.enter="onNewConv"
            autofocus
          />
        </div>
        <div class="modal-actions">
          <button class="btn btn--confirm" @click="onNewConv">创建</button>
          <button class="btn btn--cancel" @click="showNewConv = false">取消</button>
        </div>
      </div>
    </div>

    <!-- 删除项目确认弹窗（页面内弹窗，替代浏览器 confirm） -->
    <div v-if="deleteProjectTarget" class="modal-mask" @click.self="deleteProjectTarget = ''">
      <div class="modal-card modal-card--sm">
        <h3>删除项目</h3>
        <p class="modal-text">确定删除项目「{{ deleteProjectTarget }}」？将删除其全部对话文件，此操作不可撤销。</p>
        <div class="modal-actions">
          <button class="btn btn--danger" @click="confirmDeleteProject">删除</button>
          <button class="btn btn--cancel" @click="deleteProjectTarget = ''">取消</button>
        </div>
      </div>
    </div>

    <!-- 删除对话确认弹窗（页面内弹窗，替代浏览器 confirm） -->
    <div v-if="deleteConvTarget" class="modal-mask" @click.self="deleteConvTarget = null">
      <div class="modal-card modal-card--sm">
        <h3>删除对话</h3>
        <p class="modal-text">确定删除对话「{{ deleteConvTarget?.title }}」？将清除所有消息与运行中的进程。</p>
        <div class="modal-actions">
          <button class="btn btn--danger" @click="confirmDeleteConv">删除</button>
          <button class="btn btn--cancel" @click="deleteConvTarget = null">取消</button>
        </div>
      </div>
    </div>
  </aside>
</template>
