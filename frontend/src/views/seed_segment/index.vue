<template>
  <section class="page" data-module="seed_segment">
    <header class="page-head">
      <div>
        <h2>种子页段清单</h2>
        <p class="page-desc">解析种子分页配置生成页段边界，与增量游标起点同步落库，支撑断点续采与版本冲突处理。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记种子页段</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出种子页段清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <form class="filter-bar" @submit.prevent="resolveConflict('沿用旧游标')">
      <label class="filter-item">
        <span>版本冲突处理</span>
        <input v-model="conflictSeedId" data-testid="conflict-seed-id" placeholder="种子ID，如 1" />
      </label>
      <button class="btn" data-testid="conflict-keep-old" type="button" @click="resolveConflict('沿用旧游标')">沿用旧游标</button>
      <button class="btn" data-testid="conflict-new-checkpoint" type="button" @click="resolveConflict('新检查点')">新检查点</button>
      <button class="btn ghost" data-testid="resume-unfinished" type="button" @click="resumeUnfinished">中断恢复</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>剩余页</th>
          <th>可写</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row._剩余页 ?? '—' }}</td>
          <td>{{ row._可写 ? '可写' : '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无种子页段数据，可先在种子管理对含多页参数的种子执行「生成页段」</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记种子页段</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条种子页段记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/seed_segment'
const columns = ["段编号", "关联种子", "分页参数", "页段范围", "游标起点", "当前游标", "已完成页数", "清单版本"]
const actions = ["开始采集", "标记完成", "归档页段", "恢复续采"]
const statuses = ["待采集", "采集中", "已完成", "已归档"]
const metricNames = ["页段总数", "采集中页段", "已归档页段"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const conflictSeedId = ref('')
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'seed_segment.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '种子页段动作未生效')
    noticeMessage.value = payload.message || `页段已${action}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '种子页段操作失败'
  }
}

async function resolveConflict(strategy: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  if (!conflictSeedId.value.trim()) {
    errorMessage.value = '请先填写种子ID'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/resolve_conflict`, {
      method: 'POST',
      body: JSON.stringify({ seed_id: Number(conflictSeedId.value), strategy }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.error || payload.message || '冲突处理失败')
    noticeMessage.value = payload.message || `冲突已按「${strategy}」处理`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '冲突处理失败'
  }
}

async function resumeUnfinished() {
  errorMessage.value = ''
  noticeMessage.value = ''
  if (!conflictSeedId.value.trim()) {
    errorMessage.value = '请先填写种子ID'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/resume`, {
      method: 'POST',
      body: JSON.stringify({ seed_id: Number(conflictSeedId.value) }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.error || payload.message || '中断恢复失败')
    noticeMessage.value = payload.message || '中断恢复完成'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '中断恢复失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('种子页段列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '种子页段列表读取失败'
  }
}

onMounted(reload)
</script>
