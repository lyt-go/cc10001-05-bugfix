<template>
  <section class="page" data-module="parserule">
    <header class="page-head">
      <div>
        <h2>解析规则管理</h2>
        <p class="page-desc">定义网页内容的解析提取规则，包括选择器表达式、容错处理和字段映射，为准确抽取提供结构化配置。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记解析规则</button>
        <button class="btn" type="button" @click="exportRows">导出解析规则清单</button>
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
        <span>规则状态</span>
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
          <th>规则状态</th>
          <th>验证结论</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>
            <span class="status-badge" :class="statusClass(row)">{{ row['规则状态'] ?? row.status ?? '—' }}</span>
          </td>
          <td :class="{ 'error-text': !row.validation_passed }">{{ row['验证结论'] ?? '—' }}</td>
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
            <span v-if="!rowActions(row).length" class="empty-note">已废弃，无可用动作</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无解析规则数据，可先登记解析规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条解析规则记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | unknown>

const ENDPOINT = '/api/parserule'
const columns = ["规则编号", "页面选择器", "内容选择器", "属性提取器", "列表容错", "空值占位符", "字符清洗正则", "输出字段映射"]
const statuses = ["草稿", "规则验证中", "已启用", "已废弃"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)

const stats = computed(() => [
  { label: '解析规则总数', value: total.value },
  { label: '已启用规则', value: rows.value.filter((row) => row['规则状态'] === '已启用').length },
  { label: '验证中规则', value: rows.value.filter((row) => row['规则状态'] === '规则验证中').length },
  { label: '已废弃规则', value: rows.value.filter((row) => row['规则状态'] === '已废弃').length },
])

function statusClass(row: Row) {
  return `status-${row['规则状态'] ?? row.status ?? 'unknown'}`
}

/** 动作只认后端落库后的可执行清单，避免在页面上把跨级启用当成可点。 */
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
  errorMessage.value = '解析规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    if (action === '新建下一代规则') {
      const response = await request(`${ENDPOINT}/${row.id}/next-generation`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '解析规则新建下一代失败')
      }
      noticeMessage.value = payload.message
      await reload()
      return
    }
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action, request_id: buildRequestId(row, action) }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '解析规则动作未生效，请稍后重试')
    }
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '解析规则操作失败'
  }
}

/** 同一动作的重复点击复用同一回执编号，后端只认首个回执并返回“已处理”。 */
function buildRequestId(row: Row, action: string): string {
  return `${row['规则编号'] ?? row.id}:${action}`
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
      throw new Error('解析规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '解析规则列表读取失败'
  }
}

onMounted(reload)
</script>
