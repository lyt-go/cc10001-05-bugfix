<template>
  <section class="page" data-module="taskcrawl">
    <header class="page-head">
      <div>
        <h2>采集任务管理</h2>
        <p class="page-desc">定义单次或周期性采集任务，关联采集源与解析规则，记录增量水位与批次边界。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集任务</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务清单</button>
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
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>任务状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>任务状态</th>
          <th>规则状态</th>
          <th>规则验证结论</th>
          <th>任务准入</th>
          <th>受影响原因</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td><span class="status-badge">{{ row.status ?? '—' }}</span></td>
          <td><span class="status-badge" :class="statusClass(row)">{{ row['规则状态'] ?? '—' }}</span></td>
          <td :class="{ 'error-text': !row.rule_ready }">{{ row['规则验证结论'] ?? '—' }}</td>
          <td :class="{ 'error-text': !row['可启动'] }">{{ row['任务准入'] ?? '—' }}</td>
          <td class="reason-cell">
            <span>{{ row['受影响原因'] || '—' }}</span>
            <!-- 历史结论只展示当时落库的规则状态，不随规则后来失效而改写。 -->
            <span v-if="row['历史规则结论'] && row['历史规则结论']['规则状态']" class="history-note">
              历史结论：{{ row['历史规则结论']['规则状态'] }} / {{ row['历史规则结论']['规则验证结论'] }}
            </span>
          </td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in rowActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!rowActions(row).length" class="empty-note">仅可查看</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 7" class="empty-state">暂无采集任务数据，可先登记采集任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/taskcrawl'
const columns = ["任务编号", "采集源名称", "规则编号", "调度周期", "增量字段", "批次大小", "起始水位", "截止水位"]
const statuses = ["草稿", "待调度", "执行中", "已完成", "已终止"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)

const stats = computed(() => [
  { label: '采集任务总数', value: total.value },
  { label: '执行中任务', value: rows.value.filter((row) => row.status === '执行中').length },
  { label: '已完成任务', value: rows.value.filter((row) => row.status === '已完成').length },
  { label: '禁止启动', value: rows.value.filter((row) => row['可启动'] === false).length },
])

function statusClass(row: Row) {
  return `status-${row['规则状态'] ?? 'unknown'}`
}

/** 只展示后端按规则当前状态算出的可执行动作。 */
function rowActions(row: Row): string[] {
  const available = row['可执行动作']
  return Array.isArray(available) ? available.map(String) : []
}

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action, request_id: `${row['任务编号'] ?? row.id}:${action}` }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '采集任务动作未生效，请稍后重试')
    }
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务操作失败'
    // 失败后也刷新一次，让准入结论和受影响原因回到页面上，而不是残留可启动假象。
    await reload()
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams(filters.value as Record<string, string>)
  if (statusFilter.value) {
    params.set('status', statusFilter.value)
  }
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('采集任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务列表读取失败'
  }
}

onMounted(reload)
</script>
