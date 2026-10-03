export const stateLabel = (state: string) => ({ QUEUED:'排队中', RUNNING:'执行中', VERIFYING:'验证中', WAITING_CI:'等待检查', WAITING_SITE:'等待网站', WAITING_HANDOFF:'等待人工处理', PAUSED:'已暂停', RECONCILING:'恢复核对中', SUCCEEDED:'已成功', PARTIAL:'部分完成', FAILED:'已失败', CANCELLED:'已取消' }[state] ?? '状态待核对')
export const actionLabel = (action: string) => ({ start:'启动', pause:'暂停', resume:'继续', cancel:'取消', retry:'重试' }[action] ?? '操作')
export const phaseLabel = (phase: string) => ({ reconcile:'核对持久状态', observe:'观察页面', decide:'选择结构化动作', dispatch:'执行动作', confirm:'重新观察', verify:'独立验证', aggregate:'汇总验证', wait:'登记等待', stopped:'安全停止', recover:'恢复核对' }[phase] ?? '等待最新进度')
export const reasonLabel = (reason: string|null) => reason === null ? '无' : ({
  resource_conflict:'等待共享资源', context_capacity:'浏览器上下文已满', worker_restarted:'进程重启后待核对', lease_expired:'执行租约已到期',
  unknown_write:'外部写入结果未知，需查证', human_control:'浏览器正由人工控制', budget_exhausted:'预算已耗尽', budget_exceeded:'预算已耗尽',
  active_time:'执行时间预算已耗尽', ci_wait:'检查等待预算已耗尽', handoff:'人工处理截止已到期', action_limit:'动作预算已耗尽',
  content_page_limit:'页面预算已耗尽', recovery_limit:'恢复次数已达上限', site_wait_exceeds_budget:'网站等待超过剩余预算',
  configuration_required:'需要模型配置', identity_recheck_required:'需要重新核对账号', evidence_required:'需要页面证据', input_required:'需要补充输入',
  verification_incomplete:'验证尚未完整通过', invalid_model_output:'模型输出校验未通过', model_failed:'模型调用未完成', page_changed:'页面已变化，待重新核对',
  recovery_required:'需要恢复核对', run_finished:'运行已结束', write_adapter_unavailable:'写入查证不可用', graph_preparation_failed:'执行准备未完成',
  rate_limit:'网站请求受限', site_blocked:'网站访问受阻', deadline_exceeded:'截止时间已到', checkpoint_mismatch:'恢复检查点不一致',
  version_conflict:'版本已变化', state_conflict:'状态已变化', terminal_run:'运行已结束', pause_unavailable:'当前状态不能暂停', resume_unavailable:'当前状态不能继续',
  cancelled:'已取消', finished:'已完成队列处理', reconciled:'恢复核对完成', executor_interrupted:'执行进程中断', waiting:'等待安全恢复',
}[reason] ?? '存在限制，需重新核对')
export const eventLabel = (type: string) => ({ state_changed:'运行状态更新', action_recorded:'动作记录已持久化', wait_registered:'等待已登记', result_ready:'运行结果已持久化', operation_requested:'控制请求已受理', operation_completed:'控制请求已处理' }[type] ?? '进度更新')
export const duration = (ms: number|undefined) => ms === undefined ? '待初始化' : `${(ms / 1000).toFixed(1)} 秒`
/** Accepted requests stop claiming processing as soon as the durable receipt changes. */
export function operationNotice(operation: Operation|undefined): string {
  if (!operation) return ''
  if (operation.status === 'PENDING') return `${actionLabel(operation.action)}请求已受理，正在等待安全边界处理；受理不代表操作完成。`
  if (operation.status === 'APPLIED') return `${actionLabel(operation.action)}请求已应用；运行状态以最新 API 快照为准。`
  return `${actionLabel(operation.action)}请求未应用：${reasonLabel(operation.reason)}。请核对最新状态。`
}
import type { Operation } from './types'
