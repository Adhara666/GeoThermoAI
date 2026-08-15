<script setup>
import { ref, computed, nextTick } from 'vue'
import { useProjectStore } from '../stores/project'
import { useChatStore } from '../stores/chat'
import { useAuthStore } from '../stores/auth'
import { t } from '../i18n'
import LangSwitch from './LangSwitch.vue'

defineProps({ open: Boolean })
const emit = defineEmits(['close'])

const project = useProjectStore()
const chat = useChatStore()
const auth = useAuthStore()

function onLogout() {
  auth.logout()
  // 全量刷新回登录页（同时清空各 store 的登录态数据）
  window.location.reload()
}

// ── 新建项目弹窗 ────────────────────────────────────────────
const showNewProject = ref(false)
const newProjectName = ref('')

function openNewProject() {
  newProjectName.value = ''
  showNewProject.value = true
}

async function onNewProject() {
  if (!newProjectName.value.trim()) return
  await project.createProject(newProjectName.value)
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
  // 切换项目时 selectProject 会自动加载第一个对话；
  // 已在同项目时直接 selectConv（内部会加载该对话消息/进度/恢复流）
  if (p.project !== project.currentProject) {
    await project.selectProject(p.project)
    if (project.currentConv !== c.id) await project.selectConv(c.id)
  } else if (c.id !== project.currentConv) {
    await project.selectConv(c.id)
  }
  emit('close')
}

const hasAny = computed(() => project.tree.length > 0)
</script>

<template>
  <aside class="sidebar" :class="{ 'sidebar--open': open }">
    <div class="sidebar__brand">
      <img src="/logo.png?v=2" alt="GeoThermoAI" onerror="this.style.display='none'" />
      <div class="sidebar__brand-name">GeoThermoAI</div>
      <span class="sidebar__lang"><LangSwitch /></span>
    </div>

    <div class="sidebar__body">
      <div class="sidebar__new">
        <button class="btn btn--primary btn--block" @click="openNewProject">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          {{ t('sidebar.newProject') }}
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
              :title="t('sidebar.expandCollapse')"
              @click="toggleExpand(p.project)"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
            <span
              v-if="renamingPid !== p.project"
              class="project-card__name"
              :title="t('sidebar.renameTitle') + p.project"
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
            <button class="project-card__btn" :title="t('sidebar.newConvTitle')" @click="openNewConv(p.project)">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            </button>
            <button class="project-card__btn project-card__btn--del" :title="t('sidebar.delProjectTitle')" @click="deleteProjectTarget = p.project">
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
              <button class="conv-item__del" :title="t('sidebar.delConvTitle')" @click.stop="openDeleteConv(p, c)">✕</button>
            </div>
            <div v-if="!p.conversations.length" class="project-card__empty">
              {{ t('sidebar.noConvs') }}
            </div>
          </div>
        </div>
      </div>
      <div v-else class="sidebar-empty">
        {{ t('sidebar.empty') }}
      </div>
    </div>

    <div class="sidebar__footer">
      <div class="sidebar__user" :title="auth.user?.username">
        <span class="sidebar__user-name">{{ auth.display }}</span>
        <button class="sidebar__logout" :title="t('sidebar.logoutTitle')" @click="onLogout">{{ t('sidebar.logout') }}</button>
      </div>
      <div class="sidebar__meta">GeoThermoAI</div>
    </div>

    <!-- 新建项目弹窗 -->
    <div v-if="showNewProject" class="modal-mask" @click.self="showNewProject = false">
      <div class="modal-card">
        <h3>{{ t('sidebar.modalNewProject') }}</h3>
        <div class="form-group">
          <label>{{ t('sidebar.projectName') }}</label>
          <input
            v-model="newProjectName"
            class="form-input"
            :placeholder="t('sidebar.projectNamePh')"
            @keyup.enter="onNewProject"
            autofocus
          />
        </div>
        <p class="form-hint" style="margin:2px 0 4px">{{ t('sidebar.autoSaveHint') }}</p>
        <div class="modal-actions">
          <button class="btn btn--confirm" @click="onNewProject">{{ t('sidebar.create') }}</button>
          <button class="btn btn--cancel" @click="showNewProject = false">{{ t('sidebar.cancel') }}</button>
        </div>
      </div>
    </div>

    <!-- 新建对话弹窗 -->
    <div v-if="showNewConv" class="modal-mask" @click.self="showNewConv = false">
      <div class="modal-card modal-card--sm">
        <h3>{{ t('sidebar.newConvIn', { name: convTarget }) }}</h3>
        <div class="form-group">
          <label>{{ t('sidebar.convName') }}</label>
          <input
            v-model="newConvTitle"
            class="form-input"
            :placeholder="t('sidebar.convNamePh')"
            @keyup.enter="onNewConv"
            autofocus
          />
        </div>
        <div class="modal-actions">
          <button class="btn btn--confirm" @click="onNewConv">{{ t('sidebar.create') }}</button>
          <button class="btn btn--cancel" @click="showNewConv = false">{{ t('sidebar.cancel') }}</button>
        </div>
      </div>
    </div>

    <!-- 删除项目确认弹窗（页面内弹窗，替代浏览器 confirm） -->
    <div v-if="deleteProjectTarget" class="modal-mask" @click.self="deleteProjectTarget = ''">
      <div class="modal-card modal-card--md">
        <h3>{{ t('sidebar.delProject') }}</h3>
        <p class="modal-text">{{ t('sidebar.delProjectBody', { name: deleteProjectTarget }) }}</p>
        <div class="modal-actions">
          <button class="btn btn--danger" @click="confirmDeleteProject">{{ t('sidebar.del') }}</button>
          <button class="btn btn--cancel" @click="deleteProjectTarget = ''">{{ t('sidebar.cancel') }}</button>
        </div>
      </div>
    </div>

    <!-- 删除对话确认弹窗（页面内弹窗，替代浏览器 confirm） -->
    <div v-if="deleteConvTarget" class="modal-mask" @click.self="deleteConvTarget = null">
      <div class="modal-card modal-card--sm">
        <h3>{{ t('sidebar.delConv', { title: deleteConvTarget?.title }) }}</h3>
        <p class="modal-text">{{ t('sidebar.delConvBody', { title: deleteConvTarget?.title }) }}</p>
        <div class="modal-actions">
          <button class="btn btn--danger" @click="confirmDeleteConv">{{ t('sidebar.del') }}</button>
          <button class="btn btn--cancel" @click="deleteConvTarget = null">{{ t('sidebar.cancel') }}</button>
        </div>
      </div>
    </div>
  </aside>
</template>
