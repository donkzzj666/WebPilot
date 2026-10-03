import type { Results } from './types'
export const now = '2026-10-02T08:00:00Z'
export function sample(): Results {
  const run = {run_id:'run-one',task_id:'task-one',contract_version:1,parent_run_id:null,state:'SUCCEEDED',state_version:4,assistance_count:0,created_at:now,started_at:now,ended_at:now,has_result:true}
  return {task_id:'task-one',current_run_id:'run-one',runs:[run],next_cursor:null,selected_run:{...run},result_status:'AVAILABLE',
    result:{task_id:'task-one',run_id:'run-one',contract_version:1,scenario:'finance',outcome:'SUCCEEDED',assistance_count:0,items:{scenario:'finance',values:[{normalized_value:'1.23'}]},
      checks:[{criterion_id:'finance-value',expected_rule:'Compare disclosed value',actual:'1.23',verdict:'PASS',evidence_ids:['evidence-one'],checked_at:now,checker_version:'rules-v1'}],
      coverage:{searched_sources:['source-one'],queries:[],cutoff_at:null,content_pages:1,unread_candidates:[],gaps:[],complete:true},evidence_ids:['evidence-one'],unresolved:[],side_effects:[],generated_by:'business_aggregator'},
    field_checks:[{result_path:'/values/0/normalized_value',verdict:'PASS',evidence_ids:['evidence-one'],actual:'1.23'}],verification_id:'verification-one',assistance:'autonomous',
    evidence:[{evidence_id:'evidence-one',run_id:'run-one',artifact_kind:'text',source_url:'https://example.test/disclosure',captured_at:now,object_id:'object-one',locator_or_page:'line-one',sha256:'0'.repeat(64),availability:'AVAILABLE',original_evidence_id:null,snapshot_id:null,display_evidence_id:'display-one',display_sha256:'0'.repeat(64),display_size_bytes:4,display_mime_type:'text/plain; charset=utf-8',display_status:'AVAILABLE',displayable:true}],
    write_intents:[],pending_write_count:0,write_intents_truncated:false,display_complete_success:true,display_blockers:[],as_of:now}
}
