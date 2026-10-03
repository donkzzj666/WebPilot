import type { JsonValue, Scenario } from './types'
export const SCENARIOS: Record<Scenario, string> = { finance: '财务信息', operations: '运维与代码', research: '科研检索', monitoring: '来源监控' }
export const OPERATIONS = {
  edit_file: '编辑指定文件', create_branch: '创建工作分支', commit: '提交修改', create_pr: '创建拉取请求', update_pr: '更新拉取请求',
} as const
export const FIELD_LABELS: Record<string, string> = {
  scenario: '任务场景', sources: '允许访问的来源', start_urls: '起始页面', action_policy: '写入范围与允许动作',
  identity_ref: '执行账号', time_scope: '时间窗口', 'parameters.operation_kind': '运维任务类型',
  'parameters.entity_id': '企业对象标识', 'parameters.report_version': '报告年份或版本',
  'parameters.period_type': '报告口径', 'parameters.metrics': '需要读取的指标', 'parameters.currency': '币种',
  'parameters.repository': '仓库（所有者/名称）', 'parameters.base_sha': '基线提交 SHA',
  'parameters.branch': '工作分支', 'parameters.failure_run_id': '失败流水线标识',
  'parameters.required_checks': '必须通过的检查', 'parameters.independent_rules_ref': '独立检查规则引用',
  'parameters.dashboard_id': '仪表盘标识', 'parameters.panel_ids': '面板标识',
  'parameters.variables': '仪表盘变量', 'parameters.timezone': '仪表盘时区',
  'parameters.queries': '检索词', 'parameters.topic_criteria': '主题筛选条件',
  'parameters.cutoff_at': '检索截止时间（UTC）', 'parameters.max_items': '结果数量上限',
  'parameters.source_id': '监控来源标识', 'parameters.source_kind': '监控来源类型',
  'parameters.baseline': '是否建立首次基线', 'parameters.scheduled_at': '计划时间（UTC）',
  'parameters.max_list_items': '列表条目上限', 'parameters.max_details': '详情条目上限',
  'parameters.confirmed_boundary': '已确认边界标识',
}
export const STATE_LABELS: Record<string, string> = {
  NEEDS_INPUT: '等待补充信息', READY: '契约已就绪', QUEUED: '等待执行', RUNNING: '运行中',
  RECOVERY: '恢复中', PAUSED: '已暂停', BLOCKED: '运行受阻', HANDOFF: '等待人工处理',
  SUCCEEDED: '验证成功', PARTIAL: '部分完成', FAILED: '运行失败', CANCELLED: '已取消',
  AWAITING_IDENTITY: '等待身份核对', AWAITING_USER: '等待人工处理', WAITING_LOGIN: '等待登录',
  VERIFYING: '运行验证中', WAITING_CI: '等待检查', WAITING_SITE: '等待站点', WAITING_HANDOFF: '等待人工处理', RECONCILING: '查证运行状态',
}
export const fieldLabel = (field: string) => FIELD_LABELS[field] ?? '待补充字段'
export const formatValue = (value: JsonValue): string => value === null ? '未指定'
  : typeof value === 'boolean' ? value ? '是' : '否'
    : Array.isArray(value) ? value.map(formatValue).join('、')
      : typeof value === 'object' ? Object.entries(value).map(([key, item]) => `${key} = ${formatValue(item)}`).join('；')
        : String(value)
export const dateLabel = (value: string) => {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '时间暂不可读' : parsed.toLocaleString('zh-CN', { hour12: false })
}
export const splitLines = (value: string) => value.split(/[\n,，]+/).map((line) => line.trim()).filter(Boolean)
export function utcInput(value: string): string {
  if (!/(Z|\+00:00)$/.test(value.trim()) || Number.isNaN(Date.parse(value))) throw new Error('时间请使用明确的 UTC 格式，例如 2026-10-02T00:00:00Z。')
  return new Date(value).toISOString()
}
