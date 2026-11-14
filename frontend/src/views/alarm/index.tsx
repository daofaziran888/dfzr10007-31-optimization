import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/alarm'
const columns = [
  "规则编号",
  "监控指标",
  "告警条件",
  "告警级别",
  "通知渠道",
  "静默周期",
  "生效范围",
  "增长阈值",
  "规则状态"
]
const detailFields = [
  "规则版本",
  "触发记录数",
  "触发记录号",
  "核对编号",
  "告警触发结果"
]
const actions = [
  "启用规则",
  "暂停规则",
  "关闭规则"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "告警规则总数",
    "value": 0
  },
  {
    "label": "已启用规则",
    "value": 0
  },
  {
    "label": "已暂停规则",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

export default function AlarmView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('告警规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析告警规则列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('分析告警规则动作未生效，请稍后重试')
      }
      const payload = await response.json()
      // 后端拒绝进入生效状态时 ok=false，把原因直接显示在页脚，避免静默失败。
      if (payload.ok === false) {
        throw new Error(payload.message || '分析告警规则动作未生效')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析告警规则操作失败')
    }
  }

  async function recalculate() {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/trigger-windows/recalculate`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      if (!response.ok) {
        throw new Error('触发窗口重算失败')
      }
      const payload = await response.json()
      setErrorMessage(`${payload.message}（共 ${payload.total} 条触发记录）`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '触发窗口重算失败')
    }
  }

  return (
    <section className="page" data-module="alarm">
      <header className="page-head">
        <div>
          <h2>分析告警规则管理</h2>
          <p className="page-desc">维护分析告警的监控指标、触发条件与通知策略，为分析告警规则提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('告警规则登记入口尚未接入审批流')}>
            登记告警规则
          </button>
          <button className="btn" type="button" onClick={() => void recalculate()}>
            重算触发窗口
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/trigger-records/export`, '_blank')}>
            导出触发记录
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析告警规则清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析告警规则数据，可先登记告警规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <table className="data-table">
        <thead>
          <tr>
            <th>规则编号</th>
            {detailFields.map((field) => <th key={field}>{field}</th>)}
            <th>保留最近触发规则</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`detail-${String(row.id)}`}>
              <td>{row["规则编号"]}</td>
              {detailFields.map((field) => <td key={field}>{row[field] ?? '—'}</td>)}
              <td>{row["保留最近触发规则"] || '—'}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={detailFields.length + 2}>暂无触发依据，可先重算触发窗口</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条分析告警规则记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
