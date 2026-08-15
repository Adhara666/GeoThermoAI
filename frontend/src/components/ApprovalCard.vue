<script setup>
// 通用审批卡片：渲染 title/summary + 单选选项列表，
// 选中项带 fields 时展开数值输入框；确认后调用 chat.resumeApproval。
// 字段声明（label/min/max/step/default）全部来自后端载荷，前端不硬编码任何超参。
import { computed, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import { useToast } from '../composables/useToast'
import { t, translateApprovalPayload } from '../i18n'

const chat = useChatStore()
const toast = useToast()

const payload = computed(() => translateApprovalPayload(chat.approval || {}))
const options = computed(() => payload.value.options || [])

const selected = ref('')
const values = ref({})
const submitting = ref(false)

function defaultsFor(option) {
  const out = {}
  for (const f of option?.fields || []) out[f.name] = f.default
  return out
}

function resetFromPayload(p) {
  const opts = p?.options || []
  const preferred = p?.default_option || (opts.find((o) => o.recommended) || opts[0] || {}).id
  selected.value = preferred || ''
  const chosen = opts.find((o) => o.id === selected.value)
  values.value = defaultsFor(chosen)
}

watch(payload, (p) => resetFromPayload(p), { immediate: true, deep: false })

watch(selected, (id) => {
  const chosen = options.value.find((o) => o.id === id)
  values.value = defaultsFor(chosen)
})

const activeFields = computed(() => {
  const chosen = options.value.find((o) => o.id === selected.value)
  return chosen?.fields || []
})

function outOfRange(field) {
  const v = Number(values.value[field.name])
  if (!Number.isFinite(v)) return true
  if (field.min !== undefined && v < Number(field.min)) return true
  if (field.max !== undefined && v > Number(field.max)) return true
  return false
}

const invalidField = computed(() => activeFields.value.find((f) => outOfRange(f)))

async function confirm() {
  if (submitting.value) return
  if (!selected.value) { toast.error(t('approval.errSelect')); return }
  if (invalidField.value) {
    const f = invalidField.value
    toast.error(t('approval.errRange', { label: f.label, min: f.min, max: f.max }))
    return
  }
  submitting.value = true
  try {
    await chat.resumeApproval(selected.value, { ...values.value })
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="approval-card">
    <div class="approval-card__title">{{ payload.title }}</div>
    <p v-if="payload.summary" class="approval-card__summary">{{ payload.summary }}</p>

    <div class="approval-options">
      <label
        v-for="o in options"
        :key="o.id"
        class="approval-option"
        :class="{ 'approval-option--selected': selected === o.id }"
      >
        <input type="radio" :value="o.id" v-model="selected" />
        <span class="approval-option__text">
          {{ o.label }}<span v-if="o.recommended" class="approval-option__badge">{{ t('approval.recommended') }}</span>
        </span>
        <span v-if="o.hint" class="approval-option__hint">{{ o.hint }}</span>

        <div v-if="selected === o.id && (o.fields || []).length" class="approval-fields">
          <div v-for="f in o.fields" :key="f.name" class="approval-field">
            <label class="approval-field__label" :title="f.description || ''">{{ f.label }}</label>
            <input
              class="approval-field__input"
              type="number"
              :min="f.min"
              :max="f.max"
              :step="f.step"
              v-model.number="values[f.name]"
            />
            <span class="approval-field__range" v-if="f.min !== undefined && f.max !== undefined">
              {{ f.min }} ~ {{ f.max }}
            </span>
          </div>
        </div>
      </label>
    </div>

    <div class="approval-card__actions">
      <button class="btn btn--primary" :disabled="submitting" @click="confirm">{{ t('approval.confirm') }}</button>
    </div>
  </div>
</template>
