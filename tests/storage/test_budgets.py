"""Budget boundaries, immutable reservations and conservative elapsed time."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from webagent.budgets.models import ObstacleType
from webagent.budgets.store import BudgetStore
from webagent.db import connect, transaction
from webagent.db.repository import create_task, add_contract, create_run, utc_text
from webagent.errors import BusinessError
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore


class Clock:
    domain = 'budget-storage-test-boot'

    def __init__(self):
        self.wall = datetime(2026,9,30,tzinfo=timezone.utc)
        self.ns = 0

    def utcnow(self):
        return self.wall

    def monotonic_ns(self):
        return self.ns

    def advance(self, seconds, wall=None):
        self.ns += round(seconds * 1_000_000_000)
        self.wall += timedelta(seconds=seconds if wall is None else wall)


def add_run(path, name='run', *, profile=None, queue_class='ordinary', source_kind=None, parent=None):
    task = 'task-'+name
    content = {'schema_version':'m0-contract-v1','task_id':task,'contract_version':1,
               'scenario':'monitoring' if source_kind else 'research','objective':'Synthetic budget test',
               'parameters':{'source_kind':source_kind} if source_kind else {'query':'fixture'},
               'sources':['local-fixture'],'action_policy':{'mode':'read_only'}}
    if profile is not None:
        content['budget_profile'] = profile
    with connect(path) as db,transaction(db):
        create_task(db,task_id=task,instruction='Synthetic budget test',requested_fields=['contract'])
        add_contract(db,content)
        create_run(db,run_id=name,task_id=task,contract_version=1,graph_version='test',
                   graph_state_schema_version='test',model_config_sha256='a'*64,runtime_config_sha256='b'*64,
                   parent_run_id=parent)
    return content


def setup(path, *, profile=None, site='local-fixture', realm='public'):
    clock = Clock()
    budgets = BudgetStore(path,clock=clock)
    scheduler = SchedulerStore(path,clock=clock.utcnow,budgets=budgets)
    add_run(path,profile=profile)
    scheduler.enqueue('run',[Resource.site_identity(site,'identity',realm=realm),Resource.browser_context('run')],
                      expected_state_version=0)
    generation = scheduler.start_worker('worker')
    token = scheduler.claim('worker',generation)
    return clock,budgets,scheduler,generation,token


def assert_error(call,code='BUDGET_EXCEEDED',field=None):
    with pytest.raises(BusinessError) as captured:
        call()
    assert captured.value.code==code
    if field is not None:
        assert captured.value.field==field


def test_150th_action_allowed_151st_commits_stop_without_refund(database):
    _,budgets,_,_,token = setup(database)
    for index in range(150):
        result = budgets.consume(token,kind='action',attempt_id=f'action-{index}')
        assert result['dispatch_allowed'] and result['actions_used']==index+1
    assert not result['exhausted'] and result['remaining_actions']==0
    assert_error(lambda:budgets.consume(token,kind='action',attempt_id='action-151'),field='action_limit')
    status = budgets.status('run')
    assert status['actions_used']==150 and status['reason']=='action_limit' and status['exhausted']
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM budget_attempts').fetchone()[0]==150
    assert_error(lambda:budgets.consume(token,kind='observation',attempt_id='late-observation'),field='action_limit')


def test_25_content_opens_include_reopening_and_26th_stops(database):
    _,budgets,_,_,token = setup(database)
    for index in range(25):
        # Each reopen has a fresh attempt ID; no URL-based deduplication.
        budgets.consume(token,kind='action',attempt_id=f'reopen-{index}',content_page=True)
    assert_error(lambda:budgets.consume(token,kind='action',attempt_id='reopen-26',content_page=True),field='content_page_limit')
    result = budgets.status('run')
    assert (result['actions_used'],result['content_pages_used'])==(25,25)
    assert result['reason']=='content_page_limit'


def test_failed_dispatch_retains_action_and_new_retry_spends_another(database):
    _,budgets,_,_,token = setup(database)
    first = budgets.consume(token,kind='action',attempt_id='network-failed')
    assert first['actions_used']==1  # There is deliberately no refund API.
    second = budgets.consume(token,kind='action',attempt_id='network-retry')
    assert second['actions_used']==2


@pytest.mark.parametrize('kind',['action','observation','screenshot','ci_poll','recovery'])
def test_duplicate_attempt_cannot_reauthorize_dispatch_or_double_charge(database,kind):
    _,budgets,_,_,token = setup(database)
    extra = {'site_id':'local-fixture','subgoal':'find-record','obstacle_type':'locator_changed'} if kind=='recovery' else {}
    first = budgets.consume(token,kind=kind,attempt_id='same',**extra)
    second = budgets.consume(token,kind=kind,attempt_id='same',**extra)
    assert first['dispatch_allowed'] and first['charged'] and not first['duplicate']
    assert not second['dispatch_allowed'] and not second['charged'] and second['duplicate']
    assert (first['actions_used'],first['observations_used'],first['screenshots_used'])==(second['actions_used'],second['observations_used'],second['screenshots_used'])


def test_attempt_id_binds_exact_payload_and_does_not_alias_a_second_action(database):
    _,budgets,_,_,token = setup(database)
    budgets.consume(token,kind='action',attempt_id='bound')
    assert_error(lambda:budgets.consume(token,kind='action',attempt_id='bound',content_page=True),'STATE_CONFLICT')
    assert budgets.status('run')['actions_used']==1


@pytest.mark.parametrize('obstacle',list(ObstacleType))
def test_recovery_is_fixed_taxonomy_and_fourth_of_same_group_is_stopped(database,obstacle):
    _,budgets,_,_,token = setup(database)
    for index in range(3):
        budgets.consume(token,kind='recovery',attempt_id=f'recover-{index}',site_id='local-fixture',
                        subgoal='stable-subgoal',obstacle_type=obstacle)
    assert_error(lambda:budgets.consume(token,kind='recovery',attempt_id='recover-4',site_id='local-fixture',
                        subgoal='stable-subgoal',obstacle_type=obstacle),field='recovery_limit')
    status = budgets.status('run')
    assert status['actions_used']==3 and list(status['recovery_counts'].values())==[3]
    assert 'stable-subgoal' not in json.dumps(status['recovery_counts'])


def test_recovery_groups_use_canonical_site_subgoal_and_type(database):
    _,budgets,_,_,token = setup(database,site='github.com')
    for i,(site,subgoal,kind) in enumerate([('github.dev','read-one','locator_changed'),
          ('github.com','read-one','locator_changed'),('github','read-two','locator_changed'),
          ('github','read-one','network_error')]):
        budgets.consume(token,kind='recovery',attempt_id=str(i),site_id=site,subgoal=subgoal,obstacle_type=kind)
    assert sorted(budgets.status('run')['recovery_counts'].values())==[1,1,2]


@pytest.mark.parametrize('extra',[
    {'kind':'arbitrary'}, {'kind':'action','content_page':1}, {'kind':'action','navigation':1},
    {'kind':'observation','navigation':True}, {'kind':'ci_poll','content_page':True},
    {'kind':'recovery','site_id':'local-fixture','subgoal':'x','obstacle_type':'changed description'},
    {'kind':'recovery','site_id':'local-fixture','subgoal':'','obstacle_type':'locator_changed'},
    {'kind':'action','subgoal':'x'}, {'kind':'action','obstacle_type':'locator_changed'},
    {'kind':'action','attempt_id':''}, {'kind':'action','attempt_id':'bad\nid'},
])
def test_invalid_consumption_never_spends_or_stops(database,extra):
    _,budgets,_,_,token = setup(database)
    options = {'attempt_id':'invalid',**extra}
    assert_error(lambda:budgets.consume(token,**options),'INVALID_PARAMETER')
    result = budgets.status('run')
    assert not result['exhausted'] and result['actions_used']==0


@pytest.mark.parametrize('site',['elsewhere','public:elsewhere','webarena:local-fixture'])
def test_site_outside_qualification_is_rejected(database,site):
    _,budgets,_,_,token = setup(database)
    assert_error(lambda:budgets.consume(token,kind='action',attempt_id='scope',site_id=site,navigation=True),'RESOURCE_CONFLICT')
    assert budgets.status('run')['actions_used']==0


def test_read_and_screenshot_report_separately_from_actions(database):
    _,budgets,_,_,token = setup(database)
    budgets.consume(token,kind='observation',attempt_id='read')
    result = budgets.consume(token,kind='screenshot',attempt_id='shot')
    assert (result['actions_used'],result['observations_used'],result['screenshots_used'])==(0,1,1)


def test_stale_epoch_rejects_duplicate_and_new_consumption_without_writes(database):
    _,budgets,scheduler,_,token = setup(database)
    budgets.consume(token,kind='action',attempt_id='first')
    scheduler.abandon(token)
    for attempt in ('first','new'):
        assert_error(lambda:budgets.consume(token,kind='action',attempt_id=attempt),'RESOURCE_CONFLICT')
    assert budgets.status('run')['actions_used']==1


def test_navigation_pacing_covers_github_aliases_and_cross_account(database):
    clock,budgets,scheduler,generation,token = setup(database,site='github.com')
    budgets.consume(token,kind='action',attempt_id='nav1',site_id='github.com',navigation=True)
    add_run(database,'other')
    scheduler.enqueue('other',[Resource.site_identity('github.dev','other-account'),Resource.browser_context('other')],expected_state_version=0)
    other = scheduler.claim('worker',generation)
    clock.advance(2.999)
    assert_error(lambda:budgets.consume(other,kind='action',attempt_id='nav2',site_id='github.dev',navigation=True),'SITE_THROTTLED')
    assert budgets.status('other')['actions_used']==0
    clock.advance(.001)
    assert budgets.consume(other,kind='action',attempt_id='nav2',site_id='github',navigation=True)['actions_used']==1


def test_stricter_site_interval_is_preserved_across_accounts(database):
    clock,budgets,scheduler,generation,token = setup(database,profile={'min_site_interval_seconds':5})
    budgets.consume(token,kind='action',attempt_id='nav1',site_id='local-fixture',navigation=True)
    add_run(database,'other')
    scheduler.enqueue('other',[Resource.site_identity('local-fixture','other'),Resource.browser_context('other')],expected_state_version=0)
    other = scheduler.claim('worker',generation)
    clock.advance(3)
    assert_error(lambda:budgets.consume(other,kind='action',attempt_id='nav2',site_id='local-fixture',navigation=True),'SITE_THROTTLED')
    clock.advance(2)
    assert budgets.consume(other,kind='action',attempt_id='nav2',site_id='local-fixture',navigation=True)['charged']


def test_ci_poll_at_least_30_seconds_also_counts_as_action_and_observation(database):
    clock,budgets,scheduler,_,token = setup(database)
    budgets.consume(token,kind='ci_poll',attempt_id='poll1')
    clock.advance(29.999)
    assert_error(lambda:budgets.consume(token,kind='ci_poll',attempt_id='poll2'),'SITE_THROTTLED')
    scheduler.heartbeat(token)
    clock.advance(.001)
    result = budgets.consume(token,kind='ci_poll',attempt_id='poll2')
    assert (result['actions_used'],result['observations_used'])==(2,2)


def test_navigation_clock_rollback_on_new_domain_remains_throttled(database):
    clock,budgets,_,_,token = setup(database)
    budgets.consume(token,kind='action',attempt_id='nav1',site_id='local-fixture',navigation=True)
    clock.advance(1,wall=-1)
    clock.domain='new-boot'
    # ACTIVE timer conservatively exhausts first; pacing never gifts time either.
    assert_error(lambda:budgets.consume(token,kind='action',attempt_id='nav2',site_id='local-fixture',navigation=True),field='active_time')
    assert budgets.status('run')['actions_used']==1


def test_site_gate_is_shared_and_untouched_by_budget_failure(database):
    clock,budgets,_,_,token = setup(database)
    with connect(database) as db,transaction(db):
        db.execute("INSERT INTO site_gates(site_id,state,next_eligible_at,updated_at) VALUES('public:local-fixture','COOLDOWN',?,?)",
                   (utc_text(clock.wall+timedelta(seconds=10)),utc_text(clock.wall)))
    assert_error(lambda:budgets.consume(token,kind='action',attempt_id='blocked',site_id='local-fixture'),'SITE_THROTTLED')
    assert budgets.status('run')['actions_used']==0
    clock.advance(10)
    assert budgets.consume(token,kind='action',attempt_id='blocked',site_id='local-fixture')['charged']
    with connect(database) as db:
        assert db.execute("SELECT state FROM site_gates WHERE site_id='public:local-fixture'").fetchone()[0]=='COOLDOWN'


def test_normal_active_clock_is_monotonic_despite_wall_jump(database):
    clock,budgets,scheduler,_,token = setup(database)
    clock.advance(10,wall=-3600)
    scheduler.heartbeat(token)
    assert budgets.status('run')['active_ms']==10_000
    clock.advance(5,wall=7200)
    with connect(database) as db,transaction(db):
        result = budgets.flush_in_transaction(db,'run')
    assert result['active_ms']==15_000 and not result['exhausted']


def test_submillisecond_heartbeats_accumulate_without_losing_rounding(database):
    clock,budgets,_,_,token = setup(database)
    for _ in range(20):
        clock.advance(.0001)
        budgets.flush(token)
    assert budgets.status('run')['active_ms']==2


@pytest.mark.parametrize('state',['PAUSED','WAITING_HANDOFF'])
def test_pause_handoff_waits_are_excluded_from_active_time(database,state):
    clock,budgets,scheduler,_,token = setup(database)
    clock.advance(2)
    scheduler.defer(token,state)
    clock.advance(20)
    result = budgets.status('run')
    assert result['active_ms']==2_000 and result['mode']=='inactive'


def test_site_cooldown_continues_active_time_without_execution_slot(database):
    clock,budgets,scheduler,_,token = setup(database)
    clock.advance(2)
    scheduler.defer(token,'WAITING_SITE',next_eligible_at=clock.wall+timedelta(seconds=15))
    clock.advance(10)
    budgets.sweep_due()
    assert budgets.status('run')['active_ms']==12_000
    assert not any(row['resource_type']=='active_slot' for row in scheduler.snapshot()['leases'])


def test_ci_time_cumulative_across_waits_and_observation_uses_active_time(database):
    clock,budgets,scheduler,generation,token = setup(database)
    clock.advance(2)
    wait = scheduler.defer(token,'WAITING_CI')
    clock.advance(10)
    scheduler.resume('run',wait['run_state_version'])
    token = scheduler.claim('worker',generation)
    assert budgets.status('run')['ci_wait_ms']==10_000
    clock.advance(3)
    scheduler.reconcile('run',token.state_version)
    # Qualification changes after reconciliation; acquire its updated token.
    from dataclasses import replace
    with connect(database) as db:
        version = db.execute("SELECT state_version FROM runs WHERE run_id='run'").fetchone()[0]
    token = replace(token,state_version=version)
    wait = scheduler.defer(token,'WAITING_CI')
    clock.advance(7)
    result = budgets.status('run')
    assert result['ci_wait_ms']==17_000 and result['active_ms']==5_000


@pytest.mark.parametrize('recover',[False,True])
def test_same_domain_recovery_never_returns_unclosed_monotonic_time(database,recover):
    clock,budgets,_,_,_ = setup(database)
    clock.advance(7,wall=-5)
    with connect(database) as db,transaction(db):
        result = budgets.flush_in_transaction(db,'run',recover=recover)
    assert result['active_ms']==7_000


def test_recovery_charges_max_wall_or_monotonic_gap(database):
    clock,budgets,_,_,_ = setup(database)
    clock.advance(3,wall=11)
    with connect(database) as db,transaction(db):
        result = budgets.flush_in_transaction(db,'run',recover=True)
    assert result['active_ms']==11_000


def test_unknown_clock_domain_rollback_conservatively_exhausts(database):
    clock,budgets,_,_,token = setup(database,profile={'max_active_seconds':10})
    clock.advance(2)
    budgets.flush(token)
    clock.ns=0; clock.domain='new-boot'; clock.wall-=timedelta(minutes=1)
    with connect(database) as db,transaction(db):
        result = budgets.flush_in_transaction(db,'run',recover=True)
    assert result['active_ms']==10_000 and result['exhausted'] and result['reason']=='active_time'


def test_20_minute_active_boundary_is_persisted_and_action_is_denied(database):
    clock,budgets,_,_,token = setup(database)
    clock.advance(1200)
    # A long hung operation has stale leases, so the independent sweep is used.
    assert budgets.sweep_due()==[{'run_id':'run','reason':'active_time'}]
    with connect(database) as db:
        assert db.execute("SELECT active_ms FROM run_budgets WHERE run_id='run'").fetchone()[0]==1_200_000
        assert db.execute("SELECT stop_reason FROM budget_timers WHERE run_id='run'").fetchone()[0]=='active_time'
    assert budgets.sweep_due()==[{'run_id':'run','reason':'active_time'}]


def test_ci_wait_budget_is_independent_from_active_budget(database):
    clock,budgets,scheduler,_,token = setup(database,profile={'max_ci_wait_seconds':10})
    clock.advance(3)
    scheduler.defer(token,'WAITING_CI')
    clock.advance(10)
    assert budgets.sweep_due()==[{'run_id':'run','reason':'ci_wait'}]
    result = budgets.status('run')
    assert result['active_ms']==3_000 and result['ci_wait_ms']==10_000


def test_first_handoff_deadline_cannot_be_extended_or_cleared(database):
    clock,budgets,scheduler,_,token = setup(database)
    scheduler.defer(token,'WAITING_HANDOFF')
    with connect(database) as db,transaction(db):
        first = budgets.handoff_deadline(db,'run')
        clock.advance(60)
        second = budgets.handoff_deadline(db,'run',clock.wall+timedelta(hours=24))
        assert second==first
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE budget_timers SET handoff_deadline=NULL WHERE run_id='run'")
    clock.wall=first
    assert budgets.sweep_due()==[{'run_id':'run','reason':'handoff'}]


def test_requested_earlier_handoff_deadline_is_kept(database):
    clock,budgets,_,_,_ = setup(database)
    deadline=clock.wall+timedelta(hours=1)
    with connect(database) as db,transaction(db):
        assert budgets.handoff_deadline(db,'run',deadline)==deadline
    assert budgets.status('run')['handoff_deadline']==utc_text(deadline)


@pytest.mark.parametrize('profile',[
    {'max_actions':151},{'max_content_pages':26},{'max_active_seconds':1201},
    {'max_recoveries_per_obstacle':4},{'min_site_interval_seconds':2},
    {'max_ci_wait_seconds':1201},{'min_ci_poll_seconds':29},{'max_handoff_seconds':86401},
    {'action_timeout_seconds':31},{'max_model_format_repairs':3},{'max_actions':True},
    {'unknown_ceiling':1},
])
def test_frozen_contract_cannot_raise_product_limits(database,profile):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    add_run(database,profile=profile)
    with connect(database) as db,transaction(db):
        assert_error(lambda:budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'}),'INVALID_PARAMETER')
        assert db.execute('SELECT count(*) FROM budget_limits').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==0


def test_tighter_limits_apply_and_immutable_schema_prevents_upward_edit(database):
    _,budgets,_,_,token = setup(database,profile={'max_actions':2,'max_content_pages':1})
    budgets.consume(token,kind='action',attempt_id='a1')
    budgets.consume(token,kind='action',attempt_id='a2')
    assert_error(lambda:budgets.consume(token,kind='action',attempt_id='a3'),field='action_limit')
    with connect(database) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE budget_limits SET max_actions=150 WHERE run_id='run'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM budget_limits WHERE run_id='run'")


def test_budget_attempt_immutable_under_update_delete_and_replace(database):
    _,budgets,_,_,token=setup(database)
    budgets.consume(token,kind='action',attempt_id='one')
    with connect(database) as db:
        row=dict(db.execute('SELECT * FROM budget_attempts').fetchone())
        for statement in ("UPDATE budget_attempts SET actions=0", "DELETE FROM budget_attempts"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT OR REPLACE INTO budget_attempts VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))


def test_stop_marker_is_immutable_but_repeat_stop_is_idempotent(database):
    _,budgets,_,_,_=setup(database)
    with connect(database) as db,transaction(db):
        first=budgets.stop_in_transaction(db,'run','site_wait_exceeds_budget')
        second=budgets.stop_in_transaction(db,'run','active_time')
        assert first['reason']==second['reason']=='site_wait_exceeds_budget'
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE budget_timers SET stop_reason=NULL,stopped_at=NULL WHERE run_id='run'")


def test_before_claim_failure_does_not_leave_a_budget_or_quota_partial(database):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    add_run(database)
    with connect(database) as db,transaction(db):
        db.execute("INSERT INTO quota_buckets(quota_date,quota_type,used) VALUES('2026-09-30','public',50)")
        assert_error(lambda:budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'}),'DAILY_QUOTA_EXCEEDED')
        for table in ('budget_limits','budget_timers','run_budgets','quota_debits'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


def test_claim_hook_rolls_back_all_budget_rows_with_caller_failure(database):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    add_run(database)
    with connect(database) as db:
        with pytest.raises(RuntimeError),transaction(db):
            budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'})
            raise RuntimeError('synthetic caller failure')
        for table in ('budget_limits','budget_timers','run_budgets','quota_debits','quota_buckets'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


def test_unknown_monitor_fixture_gets_no_reserved_privilege(database):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    add_run(database)
    with connect(database) as db,transaction(db):
        budgets.before_claim(db,{'run_id':'run','queue_class':'monitoring'})
        assert db.execute('SELECT debit_kind FROM quota_debits').fetchone()[0]=='ordinary'
        assert not db.execute('SELECT 1 FROM quota_monitor_sources').fetchone()


def test_webarena_has_budget_metering_without_public_debit(database):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    add_run(database)
    with connect(database) as db,transaction(db):
        budgets.before_claim(db,{'run_id':'run','queue_class':'webarena'})
        assert db.execute('SELECT count(*) FROM run_budgets').fetchone()[0]==1
        assert not db.execute('SELECT 1 FROM quota_debits').fetchone()
        assert not db.execute('SELECT 1 FROM quota_buckets').fetchone()


def test_status_uninitialized_run_and_missing_run_are_distinct(database):
    add_run(database)
    budgets=BudgetStore(database,clock=Clock())
    assert budgets.status('run')=={'run_id':'run','initialized':False,'exhausted':False,'reason':None}
    assert_error(lambda:budgets.status('missing'),'NOT_FOUND')


def test_same_run_hook_reentry_preserves_budget_and_single_debit(database):
    clock,budgets,_,_,token=setup(database)
    budgets.consume(token,kind='action',attempt_id='one')
    with connect(database) as db,transaction(db):
        budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'})
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM budget_limits').fetchone()[0]==1
    assert budgets.status('run')['actions_used']==1


def test_handoff_monotonic_deadline_cannot_be_extended_by_wall_rollback(database):
    clock,budgets,scheduler,_,token=setup(database,profile={'max_handoff_seconds':5})
    scheduler.defer(token,'WAITING_HANDOFF')
    clock.advance(5,wall=-3600)
    assert budgets.sweep_due()==[{'run_id':'run','reason':'handoff'}]


def test_handoff_new_clock_domain_and_wall_rollback_stops_conservatively(database):
    clock,budgets,scheduler,_,token=setup(database)
    scheduler.defer(token,'WAITING_HANDOFF')
    clock.domain='replacement-boot'; clock.ns=0; clock.wall-=timedelta(seconds=1)
    assert budgets.sweep_due()==[{'run_id':'run','reason':'handoff'}]


def test_first_handoff_monotonic_anchor_is_immutable(database):
    _,budgets,scheduler,_,token=setup(database)
    scheduler.defer(token,'WAITING_HANDOFF')
    with connect(database) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE budget_timers SET handoff_started_mono_ns=100 WHERE run_id='run'")


def test_handoff_anchor_survives_multiple_mode_changes(database):
    clock,budgets,scheduler,generation,token=setup(database,profile={'max_handoff_seconds':20})
    waiting=scheduler.defer(token,'WAITING_HANDOFF')
    clock.advance(5)
    scheduler.resume('run',waiting['run_state_version'])
    token=scheduler.claim('worker',generation)
    token=scheduler.reconcile('run',token.state_version)
    waiting=scheduler.defer(token,'PAUSED')
    clock.advance(15)
    assert budgets.sweep_due()==[{'run_id':'run','reason':'handoff'}]
    assert budgets.status('run')['active_ms']==0


def test_qualified_read_after_exact_action_limit_can_verify_before_next_rejection(database):
    _,budgets,scheduler,generation,token=setup(database,profile={'max_actions':1})
    budgets.consume(token,kind='action',attempt_id='last')
    waiting=scheduler.defer(token,'PAUSED')
    scheduler.resume('run',waiting['run_state_version'])
    token=scheduler.claim('worker',generation)
    result=budgets.consume(token,kind='observation',attempt_id='verify-read')
    assert result['actions_used']==1 and result['observations_used']==1 and not result['exhausted']


def test_navigation_statistics_keep_realm_boundaries_separate(database):
    clock,budgets,scheduler,generation,public=setup(database,site='fixture')
    budgets.consume(public,kind='action',attempt_id='public-nav',site_id='fixture',navigation=True)
    add_run(database,'arena')
    scheduler.enqueue('arena',[Resource.site_identity('fixture','arena-user',realm='webarena'),Resource.browser_context('arena')],
                      expected_state_version=0,queue_class='webarena')
    arena=scheduler.claim('worker',generation)
    assert budgets.consume(arena,kind='action',attempt_id='arena-nav',site_id='webarena:fixture',navigation=True)['charged']
    with connect(database) as db:
        assert {row[0] for row in db.execute('SELECT site_id FROM site_pacing')}=={'public:fixture','webarena:fixture'}


def test_zero_recovery_limit_prevents_any_recovery_but_allows_normal_work(database):
    _,budgets,_,_,token=setup(database,profile={'max_recoveries_per_obstacle':0})
    budgets.consume(token,kind='action',attempt_id='normal')
    assert_error(lambda:budgets.consume(token,kind='recovery',attempt_id='forbidden-recovery',site_id='local-fixture',
                                      subgoal='inspect',obstacle_type='network_error'),field='recovery_limit')
    assert budgets.status('run')['actions_used']==1


def test_before_claim_requires_explicit_transaction(database):
    add_run(database)
    budgets=BudgetStore(database,clock=Clock())
    with connect(database) as db:
        with pytest.raises(ValueError):
            budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'})
        with pytest.raises(ValueError):
            budgets.flush_in_transaction(db,'run')


def test_invalid_execution_token_is_a_business_conflict(database):
    _,budgets,_,_,_=setup(database)
    assert_error(lambda:budgets.consume(None,kind='action',attempt_id='none'),'RESOURCE_CONFLICT')
    assert budgets.status('run')['actions_used']==0


def test_existing_debit_without_budget_is_linked_without_redebit(database):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    add_run(database)
    with connect(database) as db,transaction(db):
        db.execute("INSERT INTO quota_buckets(quota_date,quota_type,used) VALUES('2026-09-29','public',1)")
        db.execute("INSERT INTO quota_debits(debit_id,run_id,quota_date,quota_type,debit_kind,debited_at) VALUES('historic','run','2026-09-29','public','ordinary',?)",(utc_text(clock.wall),))
        budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'})
        assert db.execute("SELECT quota_debit_id FROM run_budgets WHERE run_id='run'").fetchone()[0]=='historic'
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM quota_buckets').fetchone()[0]==1


def test_v10_migration_preserves_existing_budget_and_debit(tmp_path):
    from webagent.db import migrate
    path=tmp_path/'legacy.sqlite3'
    migrate(path,target=10)
    add_run(path)
    now='2026-09-29T00:00:00.000000Z'
    with connect(path) as db,transaction(db):
        db.execute("INSERT INTO quota_buckets(quota_date,quota_type,used) VALUES('2026-09-29','public',1)")
        db.execute("INSERT INTO quota_debits(debit_id,run_id,quota_date,quota_type,debit_kind,debited_at) VALUES('historic','run','2026-09-29','public','ordinary',?)",(now,))
        db.execute("INSERT INTO run_budgets(budget_record_id,run_id,quota_debit_id,actions_used,content_pages_used,active_ms) VALUES('historic-budget','run','historic',5,2,1234)")
        before=tuple(db.execute('SELECT * FROM run_budgets').fetchone())
    assert migrate(path,target=11)['schema_version']==11
    with connect(path) as db:
        assert tuple(db.execute('SELECT * FROM run_budgets').fetchone())==before
        assert db.execute("SELECT count(*) FROM quota_debits WHERE debit_id='historic'").fetchone()[0]==1
        assert not db.execute('SELECT 1 FROM budget_limits').fetchone()
    budgets=BudgetStore(path,clock=Clock())
    with connect(path) as db,transaction(db):
        budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'})
    status=budgets.status('run')
    assert (status['actions_used'],status['content_pages_used'],status['active_ms'])==(5,2,1234)
    assert status['quota']['quota_date']=='2026-09-29'


def test_persistent_budget_timestamp_rejects_invalid_calendar_value(database):
    _,_,_,_,_=setup(database)
    with connect(database) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE budget_timers SET anchor_utc='2026-02-30T00:00:00.000000Z' WHERE run_id='run'")


def test_invalid_stop_reason_cannot_create_arbitrary_budget_marker(database):
    _,budgets,_,_,_=setup(database)
    with connect(database) as db,transaction(db):
        with pytest.raises(ValueError):
            budgets.stop_in_transaction(db,'run','arbitrary message')
    assert not budgets.status('run')['exhausted']


def test_legacy_unclosed_wall_interval_is_charged_once_before_first_v11_claim(database):
    add_run(database)
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    with connect(database) as db,transaction(db):
        db.execute('INSERT INTO run_budgets(budget_record_id,run_id,active_ms,active_interval_started_at) VALUES(?,?,?,?)',
                   ('legacy','run',5000,utc_text(clock.wall-timedelta(seconds=17))))
        budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'})
        budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'})
        result=db.execute("SELECT active_ms,active_interval_started_at FROM run_budgets WHERE run_id='run'").fetchone()
        assert tuple(result)==(22_000,None)
    assert budgets.status('run')['active_ms']==22_000


@pytest.mark.parametrize('offset',[-1300,60])
def test_exhausted_or_rolled_back_legacy_interval_cannot_receive_fresh_budget(database,offset):
    add_run(database)
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    with connect(database) as db,transaction(db):
        since=utc_text(clock.wall+timedelta(seconds=offset))
        db.execute('INSERT INTO run_budgets(budget_record_id,run_id,active_ms,active_interval_started_at) VALUES(?,?,?,?)',
                   ('legacy','run',5000,since))
        assert_error(lambda:budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'}),field='active_time')
        assert not db.execute('SELECT 1 FROM quota_debits').fetchone()
        assert not db.execute('SELECT 1 FROM budget_limits').fetchone()
        assert tuple(db.execute("SELECT active_ms,active_interval_started_at FROM run_budgets WHERE run_id='run'").fetchone())==(5000,since)


@pytest.mark.parametrize('profile',[{'min_site_interval_seconds':2**63},{'min_ci_poll_seconds':10**100}])
def test_stricter_intervals_still_must_fit_the_persistent_integer_domain(database,profile):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    add_run(database,profile=profile)
    with connect(database) as db,transaction(db):
        assert_error(lambda:budgets.before_claim(db,{'run_id':'run','queue_class':'ordinary'}),'INVALID_PARAMETER')
        assert not db.execute('SELECT 1 FROM quota_debits').fetchone()


def legacy_monitor_debits(path, count, clock):
    with connect(path) as db,transaction(db):
        db.execute("INSERT INTO quota_buckets(quota_date,quota_type,used) VALUES('2026-09-30','public',?)",(count,))
    for index in range(count):
        name=f'historic-monitor-{index}'
        add_run(path,name)
        with connect(path) as db,transaction(db):
            db.execute("INSERT INTO quota_debits(debit_id,run_id,quota_date,quota_type,debit_kind,debited_at) VALUES(?,?,'2026-09-30','public','monitoring',?)",
                       (name,name,utc_text(clock.wall)))


@pytest.mark.parametrize('source_kind',['cisa_kev','security_community'])
@pytest.mark.parametrize('historic_count',[4,8])
def test_monitoring_without_historical_source_provenance_cannot_gift_reserved_capacity(database,source_kind,historic_count):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    legacy_monitor_debits(database,historic_count,clock)
    add_run(database,'new-monitor',source_kind=source_kind)
    with connect(database) as db,transaction(db):
        assert_error(lambda:budgets.before_claim(db,{'run_id':'new-monitor','queue_class':'monitoring'}),'DAILY_QUOTA_EXCEEDED')
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==historic_count
        assert not db.execute("SELECT 1 FROM budget_limits WHERE run_id='new-monitor'").fetchone()
    # Unknown monitor provenance does not take a second ordinary debit.
    add_run(database,'ordinary')
    with connect(database) as db,transaction(db):
        budgets.before_claim(db,{'run_id':'ordinary','queue_class':'ordinary'})
        assert db.execute("SELECT debit_kind FROM quota_debits WHERE run_id='ordinary'").fetchone()[0]=='ordinary'


def test_unknown_monitoring_provenance_consumes_each_sources_remaining_ceiling(database):
    clock=Clock(); budgets=BudgetStore(database,clock=clock)
    legacy_monitor_debits(database,2,clock)
    for source_kind in ('cisa_kev','security_community'):
        for index in range(3):
            name=f'{source_kind}-{index}'
            add_run(database,name,source_kind=source_kind)
            with connect(database) as db,transaction(db):
                if index<2:
                    budgets.before_claim(db,{'run_id':name,'queue_class':'monitoring'})
                else:
                    assert_error(lambda:budgets.before_claim(db,{'run_id':name,'queue_class':'monitoring'}),'DAILY_QUOTA_EXCEEDED')
                    assert not db.execute('SELECT 1 FROM budget_limits WHERE run_id=?',(name,)).fetchone()
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM quota_debits WHERE debit_kind='monitoring'").fetchone()[0]==6
        assert db.execute('SELECT count(*) FROM quota_monitor_sources').fetchone()[0]==4
