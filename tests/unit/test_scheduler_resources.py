"""Logical collisions and qualification grammar, independent of scheduling SQL."""
from dataclasses import replace

import pytest

from webagent.errors import BusinessError
from webagent.scheduler.models import (ExecutionToken, Resource, canonical_repository,
    canonical_site, ordered_resources, resource_site)


@pytest.mark.parametrize('site', ['github', 'GitHub', 'github.com', 'github.dev',
    'github_editor', 'github-editor', 'github-web-editor', 'github_web_editor'])
def test_web_editor_and_github_share_account_lock(site):
    assert canonical_site(site) == 'github'
    resource = Resource.site_identity(site, 'opaque-account-one')
    assert resource == Resource.site_identity('github', 'opaque-account-one')
    assert resource_site(resource) == ('public', 'github')
    assert resource.logical_hold


def test_anonymous_account_named_anonymous_and_different_realms_do_not_collide():
    anonymous = Resource.site_identity('github')
    assert anonymous != Resource.site_identity('github', 'anonymous')
    assert anonymous != Resource.site_identity('other-site')
    assert anonymous != Resource.site_identity('github', realm='webarena')
    assert Resource.site_identity('github', 'account-one') != Resource.site_identity('github', 'account-two')
    assert resource_site(Resource.repository_write('owner/repo')) is None
    assert resource_site('browser_context:unparsed') is None


@pytest.mark.parametrize('repository', ['Owner/Repo', 'owner/repo.git', 'OWNER/REPO.GIT',
    'https://github.com/Owner/Repo', 'https://github.com:443/owner/repo.git/',
    'https://github.dev/owner/repo'])
def test_repository_target_normalization_is_independent_of_account(repository):
    assert canonical_repository(repository) == 'owner/repo'
    assert Resource.repository_write(repository) == Resource.repository_write('owner/repo')
    assert Resource.repository_write(repository).logical_hold


@pytest.mark.parametrize('repository', ['', ' owner/repo', 'owner/repo ', 'owner',
    'owner/repo/tree/main', 'owner/.git', 'owner/..', '-owner/repo', 'owner-/repo',
    'two--hyphens/repo', 'owner/repo%2fother', 'https://evil.test/owner/repo',
    'http://github.com/owner/repo', 'https://github.com:8443/owner/repo',
    'https://secret@github.com/owner/repo', 'https://user:secret@github.com/owner/repo',
    'https://github.com/owner/repo?token=secret', 'https://github.com/owner/repo#fragment',
    'https://github.com/owner/repo/tree/main', 'https://github.com.evil.test/owner/repo',
    'owner/repo\n', None, [], 42])
def test_repository_metadata_rejects_ambiguous_targets_and_credentials(repository):
    with pytest.raises(BusinessError) as rejected:
        Resource.repository_write(repository)
    assert rejected.value.code == 'INVALID_PARAMETER'
    assert 'secret' not in str(rejected.value)


@pytest.mark.parametrize('site', ['', ' github', 'github ', 'https://github.com',
    'github.com:443', 'site/path', 'site\n', '站点', 'a' * 101, None, []])
def test_site_scope_is_metadata_instead_of_an_arbitrary_url(site):
    with pytest.raises(BusinessError):
        Resource.site_identity(site)


def test_identity_and_context_identifiers_cannot_overflow_database_resource_keys():
    site = 's' * 100
    identity = 'i' * 200
    resource = Resource.site_identity(site, identity, realm='webarena')
    assert len(resource.resource_key) <= 200 and identity not in resource.resource_key
    context = Resource.browser_context('r' * 200)
    assert len(context.resource_key) <= 200 and context.logical_hold
    assert context == Resource.browser_context('r' * 200)
    assert context != Resource.browser_context('other-run')


def test_complete_resource_acquisition_order_is_fixed_and_duplicates_do_not_change_it():
    resources = [Resource.browser_context('run-one'), Resource.webarena_environment(),
        Resource.repository_write('owner/repo'), Resource.site_identity('github', 'account'),
        Resource.active_slot(1)]
    ordered = ordered_resources(resources + resources)
    assert [item.resource_type for item in ordered] == ['active_slot', 'site_identity',
        'repository_write', 'webarena_environment', 'browser_context']
    assert ordered_resources(list(reversed(resources))) == ordered
    assert not ordered[0].logical_hold
    with pytest.raises(BusinessError):
        ordered_resources([resources[3], replace(resources[3], logical_hold=False)])


@pytest.mark.parametrize('resource', [('active_slot', 'active_slot:2', False),
    ('active_slot', 'active_slot:0', True), ('site_identity', 'site_identity:public:github.dev:anonymous', True),
    ('site_identity', 'site_identity:private:github:anonymous', True),
    ('repository_write', 'repository_write:github:Owner/Repo', True),
    ('browser_context', 'browser_context:run-one', True),
    ('webarena_environment', 'webarena_environment:environment-one', True),
    ('unknown', 'unknown:a', False), ([], 'browser_context:a', False)])
def test_raw_resource_construction_cannot_bypass_canonical_keys(resource):
    with pytest.raises(BusinessError):
        Resource(*resource)


def token():
    resources = ordered_resources([Resource.site_identity('github', 'account'),
                                  Resource.active_slot(0), Resource.browser_context('run-one')])
    return ExecutionToken('run-one', 'worker-one', 1, 2, 3,
        '2030-01-01T00:00:00.000000Z', tuple(item.resource_key for item in resources))


def test_execution_qualification_round_trip_has_only_nonsecret_metadata():
    current = token()
    assert ExecutionToken.from_dict(current.as_dict()) == current
    assert set(current.as_dict()) == {'run_id', 'worker_id', 'worker_generation', 'epoch',
        'state_version', 'expires_at', 'resources'}


@pytest.mark.parametrize('field,value', [('epoch', True), ('epoch', 0), ('worker_generation', 0),
    ('state_version', -1), ('state_version', 1.0), ('worker_id', ''), ('run_id', ' run'),
    ('expires_at', '2030-01-01T00:00:00Z'), ('expires_at', '2030-02-30T00:00:00.000000Z'),
    ('resources', []), ('resources', ()), ('resources', ('active_slot:0', 'active_slot:0')),
    ('resources', ('browser_context:' + 'a' * 64, 'active_slot:0'))])
def test_execution_qualification_rejects_ambiguous_or_malformed_metadata(field, value):
    with pytest.raises(BusinessError):
        replace(token(), **{field: value})


def test_execution_qualification_transport_rejects_extra_or_missing_fields():
    value = token().as_dict()
    with pytest.raises(BusinessError):
        ExecutionToken.from_dict({**value, 'password': 'synthetic-secret'})
    del value['worker_generation']
    with pytest.raises(BusinessError):
        ExecutionToken.from_dict(value)
