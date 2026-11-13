<template>
  <section class="page" data-module="sandbox">
    <header class="page-head">
      <div>
        <h2>采集沙箱测试管理</h2>
        <p class="page-desc">维护采集沙箱的测试脚本、参数配置与结果比对，支持断点重跑：失败后从断点的下一步恢复，重跑完成自动生成与上一版的差异摘要。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记沙箱实例</button>
        <button class="btn" type="button" @click="exportRows">导出采集沙箱测试清单</button>
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
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-active': detail?.entry?.id === row.id }">
          <td v-for="column in columns" :key="column" :title="String(row[column] ?? '')">
            <span v-if="column === '实例状态'" class="badge" :data-tone="toneOf(row[column])">{{ row[column] ?? '—' }}</span>
            <template v-else>{{ preview(row[column]) }}</template>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">运行详情</button>
            <button
              v-for="action in lifecycleActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runLifecycle(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集沙箱测试数据，可先登记沙箱实例</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集沙箱测试记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
    </footer>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <div>
          <h3>{{ detail.entry['实例编号'] }} 运行详情</h3>
          <p class="page-desc">
            实例状态 {{ detail.entry['status'] }} · 测试结论 {{ detail.entry['实例状态'] }} ·
            累计运行 {{ detail.run_total }} 次（重跑 {{ detail.rerun_count }} 次）
            <template v-if="detail.lock_holder"> · 运行锁持有：{{ detail.lock_holder }}</template>
          </p>
        </div>
        <div class="page-actions">
          <button v-if="detail.entry['status'] === '草稿'" class="btn primary" type="button" @click="startTest">启动测试</button>
          <button v-if="detail.breakpoint" class="btn primary" type="button" @click="startRun('resume')">断点重跑（从第 {{ detail.breakpoint['恢复步骤'] }} 步恢复）</button>
          <button class="btn" type="button" @click="startRun('fresh')">从头重跑</button>
          <button v-if="detail.entry['status'] === '运行中'" class="btn" type="button" @click="runLifecycle('关闭实例', detail.entry)">关闭实例</button>
          <button v-if="detail.entry['status'] === '已完成'" class="btn" type="button" @click="runLifecycle('归档实例', detail.entry)">归档实例</button>
          <button class="btn ghost" type="button" @click="closeDetail">收起详情</button>
        </div>
      </header>

      <div v-if="detail.breakpoint" class="breakpoint-card">
        <h4>断点定位 · {{ detail.breakpoint['run_no'] }}（{{ detail.breakpoint['运行状态'] }}）</h4>
        <div class="breakpoint-grid">
          <span>断点步骤：第 {{ detail.breakpoint['断点步骤'] }} 步已完成，从第 {{ detail.breakpoint['恢复步骤'] }} 步恢复</span>
          <span>入参摘要：{{ detail.breakpoint['入参摘要'] }}</span>
          <span>上轮耗时：{{ detail.breakpoint['上轮耗时ms'] }}ms</span>
          <span v-if="detail.breakpoint['失败断言']" class="error-text">失败断言：{{ detail.breakpoint['失败断言'] }}</span>
          <span v-if="detail.breakpoint['错误信息']" class="error-text">错误信息：{{ detail.breakpoint['错误信息'] }}</span>
        </div>
        <ol class="script-lines">
          <li
            v-for="line in detail.breakpoint['脚本行']"
            :key="line.seq"
            :class="{ 'line-failed': line['是否失败'], 'line-breakpoint': line['是否断点'] }"
          >
            <span class="line-tag">{{ line['是否失败'] ? '失败' : line['是否断点'] ? '断点' : `第${line.seq}步` }}</span>
            <code>{{ line.text }}</code>
          </li>
        </ol>
      </div>

      <div v-if="currentRun" class="run-detail">
        <h4>
          运行 {{ currentRun['run_no'] }} 步骤明细
          <span class="badge" :data-tone="toneOf(currentRun['status'])">{{ currentRun['status'] }}</span>
          <span class="run-meta">第 {{ currentRun['attempt'] }} 次运行 · {{ currentRun['触发方式'] }} · 总耗时 {{ currentRun['elapsed_ms'] + currentRun['carried_ms'] }}ms（含上轮沿用 {{ currentRun['carried_ms'] }}ms）</span>
        </h4>
        <table class="data-table">
          <thead>
            <tr><th>步骤</th><th>名称</th><th>脚本</th><th>状态</th><th>耗时</th><th>实际值</th><th>来源</th><th>错误</th></tr>
          </thead>
          <tbody>
            <tr v-for="step in currentRun['steps']" :key="step.seq" :class="{ 'row-failed': step['状态'] !== '通过' && step['状态'] !== '未执行' }">
              <td>第 {{ step.seq }} 步</td>
              <td>{{ step['名称'] }}</td>
              <td><code class="cell-code">{{ step['脚本'] }}</code></td>
              <td><span class="badge" :data-tone="toneOf(step['状态'])">{{ step['状态'] }}</span></td>
              <td>{{ step['耗时ms'] }}ms</td>
              <td>{{ step['实际值'] }}</td>
              <td>{{ step['来源'] }}</td>
              <td class="error-text">{{ step['错误'] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>

        <div v-if="currentRun['差异摘要']" class="diff-card">
          <h4>与上一版（{{ currentRun['差异摘要']['对比运行'] }}）的差异摘要</h4>
          <p class="diff-summary">{{ currentRun['差异摘要']['文本摘要'] }}</p>
          <div class="breakpoint-grid">
            <span>结论变化：{{ currentRun['差异摘要']['结论变化'] }}</span>
            <span>总耗时：{{ currentRun['差异摘要']['耗时变化']['上轮ms'] }}ms → {{ currentRun['差异摘要']['耗时变化']['本轮ms'] }}ms（{{ currentRun['差异摘要']['耗时变化']['变化率'] }}）</span>
          </div>
          <ul v-if="currentRun['差异摘要']['步骤变化'].length" class="diff-steps">
            <li v-for="change in currentRun['差异摘要']['步骤变化']" :key="change.seq">
              第 {{ change.seq }} 步「{{ change['名称'] }}」：{{ change['上轮'] }} → {{ change['本轮'] }}（{{ change['变化'] }}）
            </li>
          </ul>
        </div>
      </div>

      <div class="run-history">
        <h4>运行历史（同一组测试脚本 + 目标站点 + 测试参数的运行链）</h4>
        <table class="data-table">
          <thead>
            <tr><th>运行编号</th><th>次数</th><th>触发方式</th><th>状态</th><th>断点步骤</th><th>总耗时</th><th>完成时间</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="run in runs" :key="run['run_no']" :class="{ 'row-active': currentRun?.['run_no'] === run['run_no'] }">
              <td>{{ run['run_no'] }}</td>
              <td>第 {{ run['attempt'] }} 次</td>
              <td>{{ run['触发方式'] }}</td>
              <td><span class="badge" :data-tone="toneOf(run['status'])">{{ run['status'] }}</span></td>
              <td>{{ run['breakpoint_step'] ? `第 ${run['breakpoint_step']} 步` : '—' }}</td>
              <td>{{ run['elapsed_ms'] + run['carried_ms'] }}ms</td>
              <td>{{ run['finished_at'] ?? '进行中' }}</td>
              <td><button class="link" type="button" @click="selectRun(run)">查看步骤</button></td>
            </tr>
            <tr v-if="!runs.length">
              <td colspan="8" class="empty-state">还没有运行记录，启动测试后这里会出现运行链</td>
            </tr>
          </tbody>
        </table>
        <div class="pager">
          <button class="btn" type="button" :disabled="runPage <= 1" @click="turnPage(runPage - 1)">上一页</button>
          <button class="btn" type="button" :disabled="runPage >= maxRunPage" @click="turnPage(runPage + 1)">下一页</button>
          <span>第 {{ runPage }} / {{ maxRunPage }} 页 · 共 {{ runTotal }} 次运行</span>
          <label class="pager-jump">
            跳至
            <input v-model="jumpPage" type="text" inputmode="numeric" placeholder="页码" @keyup.enter="jumpToPage" />
            页
          </label>
          <button class="btn" type="button" @click="jumpToPage">跳转</button>
          <select v-model.number="runSize" class="pager-size" @change="changeSize">
            <option :value="5">每页 5 条</option>
            <option :value="10">每页 10 条</option>
            <option :value="20">每页 20 条</option>
          </select>
          <button v-if="detail.breakpoint" class="btn primary" type="button" @click="locateBreakpoint">定位断点运行</button>
        </div>
      </div>
    </section>

    <div v-if="createVisible" class="modal-mask" @click.self="createVisible = false">
      <form class="modal-card" @submit.prevent="submitCreate">
        <h3>登记沙箱实例</h3>
        <label class="filter-item"><span>实例编号</span><input v-model="createForm['实例编号']" placeholder="如 SAND-1001" /></label>
        <label class="filter-item"><span>测试脚本（每行一步：步骤名 | assert 表达式）</span><textarea v-model="createForm['测试脚本']" rows="4" placeholder="抓取列表页 | assert status_code == 200"></textarea></label>
        <label class="filter-item"><span>目标站点</span><input v-model="createForm['目标站点']" placeholder="https://example.com/list" /></label>
        <label class="filter-item"><span>测试参数（JSON）</span><input v-model="createForm['测试参数']" placeholder='{"items_min": 50, "timeout": 5}' /></label>
        <label class="filter-item"><span>预期结果</span><input v-model="createForm['预期结果']" placeholder="全部步骤通过" /></label>
        <label class="filter-item"><span>覆盖节点（逗号分隔）</span><input v-model="createForm['覆盖节点']" placeholder="PIPE-0001,PIPE-0002" /></label>
        <div class="page-actions">
          <button class="btn primary" type="submit">提交登记</button>
          <button class="btn ghost" type="button" @click="createVisible = false">取消</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { request } from '@/api/client'

type Row = Record<string, any>

interface ScriptLine { seq: number; text: string; 是否断点: boolean; 是否失败: boolean }
interface Breakpoint {
  run_no: string
  运行状态: string
  断点步骤: number
  恢复步骤: number
  失败步骤: number | null
  脚本片段: string | null
  脚本行: ScriptLine[]
  入参摘要: string
  上轮耗时ms: number
  失败断言: string | null
  错误信息: string | null
  历史序号: number
}
interface RunStep { seq: number; 名称: string; 脚本: string; 状态: string; 耗时ms: number; 实际值: string; 来源: string; 错误: string | null }
interface Run {
  run_no: string
  attempt: number
  触发方式: string
  status: string
  steps: RunStep[]
  breakpoint_step: number
  elapsed_ms: number
  carried_ms: number
  finished_at: string | null
  差异摘要: {
    对比运行: string
    结论变化: string
    耗时变化: { 上轮ms: number; 本轮ms: number; 差值ms: number; 变化率: string }
    步骤变化: { seq: number; 名称: string; 上轮: string; 本轮: string; 变化: string }[]
    文本摘要: string
  } | null
}
interface Detail {
  entry: Row
  latest_run: Run | null
  breakpoint: Breakpoint | null
  run_total: number
  rerun_count: number
  lock_holder: string | null
}

const ENDPOINT = '/api/sandbox'
const STORAGE_KEY = 'sandbox.detail'
const columns = ['实例编号', '测试脚本', '目标站点', '测试参数', '预期结果', '实际结果', '测试耗时', '实例状态']
const stats = ref([
  { label: '沙箱实例总数', value: 0 },
  { label: '运行中实例', value: 0 },
  { label: '已完成实例', value: 0 },
])

const route = useRoute()
const router = useRouter()

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const detail = ref<Detail | null>(null)
const runs = ref<Run[]>([])
const runTotal = ref(0)
const runPage = ref(1)
const runSize = ref(5)
const jumpPage = ref('')
const selectedRunNo = ref<string | null>(null)
const selectedRun = ref<Run | null>(null)

const createVisible = ref(false)
const createForm = ref<Record<string, string>>({ 实例编号: '', 测试脚本: '', 目标站点: '', 测试参数: '', 预期结果: '', 覆盖节点: '' })

const maxRunPage = computed(() => Math.max(1, Math.ceil(runTotal.value / runSize.value)))
const currentRun = computed<Run | null>(() => selectedRun.value ?? detail.value?.latest_run ?? null)

function preview(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > 36 ? `${text.slice(0, 36)}…` : text
}

function toneOf(status: unknown): string {
  const text = String(status ?? '')
  if (['验证通过', '通过', '已完成'].includes(text)) return 'ok'
  if (['验证失败', '失败', '崩溃'].includes(text)) return 'bad'
  if (['未收口', '超时', '运行中'].includes(text)) return 'warn'
  return 'muted'
}

function lifecycleActions(row: Row): string[] {
  const status = String(row['status'] ?? '')
  if (status === '草稿') return ['启动测试']
  if (status === '运行中') return ['关闭实例']
  if (status === '已完成') return ['归档实例']
  return []
}

function syncQuery() {
  const query: Record<string, string> = {}
  if (detail.value) {
    query.instance = String(detail.value.entry['id'])
    query.run_page = String(runPage.value)
    query.run_size = String(runSize.value)
    if (selectedRunNo.value) query.run = selectedRunNo.value
  }
  void router.replace({ query })
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(query))
}

async function parsePayload(response: Response): Promise<any> {
  const payload = await response.json()
  if (!response.ok) throw new Error(payload?.detail ?? `接口返回 ${response.status}`)
  return payload
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const payload = await parsePayload(await request(`${ENDPOINT}?${query}`))
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await reloadStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集沙箱测试列表读取失败'
  }
}

async function reloadStats() {
  const [running, done] = await Promise.all([
    parsePayload(await request(`${ENDPOINT}?status=${encodeURIComponent('运行中')}&size=1`)),
    parsePayload(await request(`${ENDPOINT}?status=${encodeURIComponent('已完成')}&size=1`)),
  ])
  stats.value = [
    { label: '沙箱实例总数', value: total.value },
    { label: '运行中实例', value: running.total ?? 0 },
    { label: '已完成实例', value: done.total ?? 0 },
  ]
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createVisible.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  const values: Record<string, unknown> = { ...createForm.value }
  values['覆盖节点'] = String(createForm.value['覆盖节点'] ?? '')
    .split(/[,，]/).map((item) => item.trim()).filter(Boolean)
  try {
    const payload = await parsePayload(await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values }) }))
    if (!payload.ok) throw new Error(payload.message)
    createVisible.value = false
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '沙箱实例登记失败'
  }
}

async function runLifecycle(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const payload = await parsePayload(await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    }))
    if (!payload.ok) throw new Error(payload.message)
    noticeMessage.value = payload.message
    await reload()
    if (detail.value && detail.value.entry['id'] === row.id) await loadDetail(Number(row.id), false)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : `采集沙箱测试动作「${action}」未生效`
  }
}

async function startTest() {
  if (!detail.value) return
  await runLifecycle('启动测试', detail.value.entry)
}

async function startRun(mode: 'fresh' | 'resume') {
  if (!detail.value) return
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const payload = await parsePayload(await request(`${ENDPOINT}/${detail.value.entry['id']}/runs`, {
      method: 'POST',
      body: JSON.stringify({ values: { mode } }),
    }))
    if (!payload.ok) throw new Error(payload.message)
    noticeMessage.value = payload.message
    selectedRunNo.value = payload.entry?.['run_no'] ?? null
    runPage.value = 1
    await loadDetail(detail.value.entry['id'], false)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行启动失败'
  }
}

async function openDetail(row: Row) {
  selectedRunNo.value = null
  selectedRun.value = null
  runPage.value = 1
  await loadDetail(Number(row.id))
}

function closeDetail() {
  detail.value = null
  runs.value = []
  selectedRun.value = null
  selectedRunNo.value = null
  syncQuery()
}

async function loadDetail(id: number, restorePage = true) {
  errorMessage.value = ''
  try {
    detail.value = await parsePayload(await request(`${ENDPOINT}/${id}/detail`))
    if (!restorePage) runPage.value = Math.min(runPage.value, maxRunPage.value)
    await loadRuns()
    if (selectedRunNo.value) {
      selectedRun.value = runs.value.find((run) => run['run_no'] === selectedRunNo.value) ?? detail.value?.latest_run ?? null
    } else {
      selectedRun.value = null
    }
    syncQuery()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '沙箱实例详情读取失败'
  }
}

async function loadRuns() {
  if (!detail.value) return
  const payload = await parsePayload(await request(
    `${ENDPOINT}/${detail.value.entry['id']}/runs?page=${runPage.value}&size=${runSize.value}`,
  ))
  runs.value = payload.items ?? []
  runTotal.value = payload.total ?? 0
}

function selectRun(run: Run) {
  selectedRunNo.value = run['run_no']
  selectedRun.value = run
  syncQuery()
}

async function turnPage(page: number) {
  if (page < 1 || page > maxRunPage.value) return
  runPage.value = page
  await loadRuns()
  syncQuery()
}

async function jumpToPage() {
  const page = Number(jumpPage.value)
  if (!Number.isInteger(page) || page < 1 || page > maxRunPage.value) {
    errorMessage.value = `页码需在 1 到 ${maxRunPage.value} 之间`
    return
  }
  errorMessage.value = ''
  jumpPage.value = ''
  await turnPage(page)
}

async function changeSize() {
  runPage.value = 1
  await loadRuns()
  syncQuery()
}

async function locateBreakpoint() {
  const breakpoint = detail.value?.breakpoint
  if (!breakpoint) return
  selectedRunNo.value = breakpoint['run_no']
  const page = Math.max(1, Math.ceil(breakpoint['历史序号'] / runSize.value))
  if (page !== runPage.value) {
    runPage.value = page
    await loadRuns()
  }
  selectedRun.value = runs.value.find((run) => run['run_no'] === breakpoint['run_no']) ?? detail.value?.latest_run ?? null
  syncQuery()
}

onMounted(async () => {
  await reload()
  // 返回后仍定位到同一断点：优先读路由查询，其次读会话快照
  const fromRoute = route.query.instance ? route.query : null
  let saved: Record<string, string> = {}
  try {
    saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '{}') as Record<string, string>
  } catch {
    saved = {}
  }
  const source = (fromRoute ?? saved) as Record<string, unknown>
  const instanceId = Number(source.instance)
  if (instanceId) {
    runPage.value = Math.max(1, Number(source.run_page) || 1)
    runSize.value = [5, 10, 20].includes(Number(source.run_size)) ? Number(source.run_size) : 5
    selectedRunNo.value = typeof source.run === 'string' ? source.run : null
    await loadDetail(instanceId)
  }
})
</script>
