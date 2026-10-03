import type { TaskDetail } from '../tasks/types'
import type { BusinessEvent } from './events'
export type ControlAction = 'start' | 'pause' | 'resume' | 'cancel'
export type ControlBody = { expected_state_version: number; contract_version: number; settings_version: number }
export type Operation = { operation_id: string; task_id: string; run_id: string; action: 'start'|'retry'|'pause'|'resume'|'cancel';
  status: 'PENDING'|'APPLIED'|'REJECTED'; requested_state_version: number; accepted_run_state_version: number;
  contract_version: number; settings_version: number; reason: string|null; created_at: string; completed_at: string|null;
  state: string|null; state_version: number|null }
export type Workspace = {
  task: { task_id: string; preparation_status: 'READY'|'NEEDS_INPUT'; state_version: number; current_contract_version: number|null; current_run_id: string|null }
  run: { run_id: string; task_id: string; contract_version: number; settings_version: number; state: string; state_version: number;
    blocked_reason: string|null; created_at: string; started_at: string|null; ended_at: string|null; handoff_deadline: string|null }|null
  is_current_run: boolean; criteria_contract_version: number|null
  criteria: { criterion_id: string; expected_rule: string; check_method: string; critical: boolean; verified: boolean }[]
  current_subgoal: string|null
  checkpoint: { checkpoint_id:string; verified_item_ids:string[]; pending_item_ids:string[]; action_sequence:number; saved_at:string }|null
  graph_progress: { progress_id: string; phase: string; state_version: number; business_event_id: string; iteration: number; diagnostic: string|null; occurred_at: string }[]
  observation: { snapshot_id: string; captured_at: string; valid: boolean; state_version: number; page_version: string;
    screenshot_evidence_id: string|null; evidence: { evidence_id: string; artifact_kind: string; availability: string|null;
      redaction_status: string|null; mime_type: string|null; captured_at: string }[] }|null
  budget: { run_id: string; initialized: boolean; exhausted: boolean; reason: string|null; actions_used?: number;
    content_pages_used?: number; observations_used?: number; screenshots_used?: number; model_calls_used?: number;
    active_ms?: number; ci_wait_ms?: number; remaining_actions?: number; remaining_content_pages?: number;
    remaining_active_ms?: number; remaining_ci_wait_ms?: number; handoff_deadline?: string|null }|null
  queue: { status: string; queue_class: string; reason: string|null; available_at: string; updated_at: string }|null
  controls: Operation[]; events: BusinessEvent[]; event_cursor: string; event_high_water: string; global_event_high_water: string
  has_earlier_events: boolean; as_of: string
}
export type Readiness = { version: number; ready: boolean }
export type WorkbenchInputs = { workspace: Workspace; detail: TaskDetail; readiness: Readiness }
export type PendingControl = { action: ControlAction; target: string; taskId: string; body: ControlBody; key: string; fingerprint: string }
