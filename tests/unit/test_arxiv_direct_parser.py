"""Synthetic original text only; no live page or retained benchmark answers."""
from copy import deepcopy

import pytest

from webagent.verification.arxiv import direct_reference, parse_visible

URL = 'https://arxiv.org/abs/2401.00001v1'
TEXT = '''arXiv:2401.00001 [cs.AI]
[Submitted on 1 Jan 2024]
Title:
Synthetic direct metadata example
Authors:
Ada Example
,
Bo Example
View a PDF of the paper titled Synthetic direct metadata example
Abstract: This text is a test fixture, not a real publication fact.
Submission history
From: Synthetic fixture
[v1] Mon, 1 Jan 2024 01:02:03 UTC (10 KB)
[v2] Tue, 2 Jan 2024 02:03:04 UTC (11 KB)
[v3] Wed, 3 Jan 2024 03:04:05 UTC (12 KB)
'''


def envelope(text=TEXT):
    return {'title': 'Fixture browser title', 'text': text, 'text_truncated': False}


def test_exact_fields_come_from_original_labels_and_utc_history():
    result = parse_visible(URL, envelope())
    assert result == {'canonical_id': '2401.00001', 'version': 'v1',
        'title': 'Synthetic direct metadata example', 'authors': ['Ada Example', 'Bo Example'],
        'first_published_at': '2024-01-01T01:02:03Z', 'revised_at': None,
        'source_url': URL, 'claims': [], 'relations': []}


def test_selected_revision_uses_its_history_row_not_latest_revision():
    result = parse_visible(URL[:-1] + '2', envelope())
    assert result['version'] == 'v2'
    assert result['revised_at'] == '2024-01-02T02:03:04Z'
    assert result['first_published_at'] == '2024-01-01T01:02:03Z'


@pytest.mark.parametrize('url', [URL.replace('https:', 'http:'), URL.replace('arxiv.org', 'evil.invalid'),
    URL.replace('arxiv.org', 'arxiv.org.evil.invalid'), URL[:-2], URL + '?x=1', URL + '#history',
    URL.replace('arxiv.org', 'user@arxiv.org'), URL.replace('/abs/', '/html/'),
    URL.replace('v1', 'v0'), URL.replace('00001', '00001%32'), URL.replace('arxiv.org', 'arxiv.org:444')])
def test_unversioned_or_noncanonical_source_does_not_qualify(url):
    assert direct_reference(url) is None
    assert parse_visible(url, envelope()) is None


@pytest.mark.parametrize('before,after', [
    ('Title:', 'Heading:'), ('Authors:', 'Creators:'), ('Submission history', 'History'),
    ('arXiv:2401.00001', 'arXiv:2401.99999'), (' UTC', ' GMT'),
    ('01:02:03', '25:02:03'), ('1 Jan 2024', '31 Feb 2024'),
    ('[v1]', '[v9]'), ('[v2]', '[v3]'), ('[v2]', '[v4]'),
    ('Ada Example\n,\nBo Example', 'Ada Example,,Bo Example'),
    ('Ada Example\n,\nBo Example', 'Ada Example,Ada Example'),
    ('Ada Example\n,\nBo Example', 'Ada Example, Jr., Bo Example'),
    ('2 Jan 2024 02:03:04', '1 Jan 2024 00:00:01'),
])
def test_missing_ambiguous_or_invalid_business_facts_fail_closed(before, after):
    assert parse_visible(URL, envelope(TEXT.replace(before, after))) is None


@pytest.mark.parametrize('suffix', ['\nTitle: Another title\n', '\nAuthors: Another author\n',
    '\nSubmission history\n', '\narXiv:2401.00002\n', '\n[v1] invalid timestamp\n', '\n[truncated]'])
def test_duplicate_sections_identity_or_unparsed_history_are_not_skipped(suffix):
    assert parse_visible(URL, envelope(TEXT + suffix)) is None


def test_selected_missing_version_and_truncated_capture_fail_closed():
    assert parse_visible(URL[:-1] + '4', envelope()) is None
    assert parse_visible(URL, envelope(TEXT[:TEXT.index('Submission history')])) is None
    assert parse_visible(URL, envelope(TEXT + ' ' * (12000 - len(TEXT)))) is None


def test_derivative_or_extra_envelope_fields_cannot_supply_original_projection():
    wrong = deepcopy(envelope())
    wrong['arxiv_direct'] = {'title': 'Candidate supplied fact'}
    assert parse_visible(URL, wrong) is None
    assert parse_visible(URL, {'text': TEXT}) is None


def test_explicit_visible_version_conflict_cannot_borrow_an_older_history_row():
    assert parse_visible(URL, envelope(TEXT.replace('arXiv:2401.00001', 'arXiv:2401.00001v2'))) is None
    assert parse_visible(URL, envelope(TEXT.replace('arXiv:2401.00001', 'arXiv:2401.00001v1'))) is not None


@pytest.mark.parametrize('flag', [True, None, 0, 'false'])
def test_capture_completeness_is_an_explicit_bool_not_a_length_inference(flag):
    raw = envelope()
    raw['text_truncated'] = flag
    assert parse_visible(URL, raw) is None
    del raw['text_truncated']
    assert parse_visible(URL, raw) is None
