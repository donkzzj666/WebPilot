"""Fail-closed metadata parsing for one explicit arXiv abstract-page version.

This is a direct-page reader, not search, PDF extraction or a publication
aggregator. The only business inputs are authenticated original visible text
and its actual source URL. No proposal or expected field value enters parsing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

PARSER_VERSION = 'arxiv-direct-visible-v1'
_ID = r'(?:[0-9]{4}\.[0-9]{4,5}|[a-z][a-z.-]*/[0-9]{7})'
_PAGE = re.compile(r'/abs/(' + _ID + r')v([1-9][0-9]{0,3})')
_HISTORY = re.compile(r'\[v([1-9][0-9]{0,3})\]\s*(?:[A-Z][a-z]{2},\s*)?'
    r'([0-9]{1,2})\s+([A-Z][a-z]{2})\s+([0-9]{4})\s+([0-9]{2}:[0-9]{2}:[0-9]{2})\s+UTC\b')
_MONTHS = {name: index for index, name in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}


def direct_reference(source_url):
    """Return (canonical ID, version) only for a credential-free fixed page."""
    if type(source_url) is not str:
        return None
    try:
        parts = urlsplit(source_url)
        match = _PAGE.fullmatch(parts.path)
        if (parts.scheme != 'https' or parts.hostname != 'arxiv.org' or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or '%' in source_url or '\\' in source_url
                or not match):
            return None
        return match[1], 'v' + match[2]
    except ValueError:
        return None


def parse_visible(source_url, envelope):
    """Extract all required metadata, or return None for any ambiguity.

Only exact labeled title/authors and UTC submission-history entries qualify.
The selected revision date comes from that version's own entry. Empty claims
and relations describe this parser's deliberately metadata-only projection;
they are never populated from an abstract, candidate assessment or contract.
"""
    reference = direct_reference(source_url)
    if (reference is None or type(envelope) is not dict or set(envelope) != {'title', 'text', 'text_truncated'}
            or type(envelope['title']) is not str or type(envelope['text']) is not str
            or envelope['text_truncated'] is not False
            or not 0 < len(envelope['text']) < 12000):
        return None
    text = envelope['text']
    if re.search(r'(?:^|\n)\s*\[truncated\]\s*$', text, re.I):
        return None
    if (text.count('Title:') != 1 or text.count('Authors:') != 1
            or text.count('Submission history') != 1):
        return None
    match = re.search(r'(?:^|\n)\s*Title:\s*(.*?)\s*Authors:\s*(.*?)\s*'
        r'(?:View a PDF|View PDF|Download PDF|Abstract:)', text, re.S)
    if not match:
        return None
    title = ' '.join(match[1].split())
    authors_text = ' '.join(match[2].split())
    authors = [part.strip() for part in authors_text.split(',')]
    if (not title or len(title) > 2000 or not 1 <= len(authors) <= 100
            or any(not author or len(author) > 200 or ':' in author or '<' in author or '>' in author
                   or len(author.split()) < 2 or any(char.isdigit() for char in author) for author in authors)
            or len(authors) != len(set(authors))):
        return None
    canonical_id, version = reference
    # The page's own identifier must agree with actual navigation metadata.
    visible_references = re.findall(r'arXiv:(' + _ID + r')(v[1-9][0-9]*)?(?![0-9])', text)
    if ({value[0] for value in visible_references} != {canonical_id}
            or any(explicit and explicit != version for _, explicit in visible_references)):
        return None
    history_text = text.split('Submission history', 1)[1]
    entries = {}
    try:
        for row in _HISTORY.finditer(history_text):
            number = int(row[1])
            if number in entries or row[3] not in _MONTHS:
                return None
            hour, minute, second = map(int, row[5].split(':'))
            entries[number] = datetime(int(row[4]), _MONTHS[row[3]], int(row[2]),
                hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None
    # An invalid timestamp/version row cannot be silently skipped by regex.
    versions = [int(value) for value in re.findall(r'\[v([0-9]+)\]', history_text)]
    selected = int(version[1:])
    if (not entries or len(versions) != len(entries) or set(versions) != set(entries)
            or 1 not in entries or selected not in entries
            or sorted(entries) != list(range(1, max(entries) + 1))
            or any(entries[a] > entries[b] for a, b in zip(sorted(entries), sorted(entries)[1:]))):
        return None
    stamp = lambda value: value.isoformat().replace('+00:00', 'Z')
    return {'canonical_id': canonical_id, 'version': version, 'title': title, 'authors': authors,
        'first_published_at': stamp(entries[1]),
        'revised_at': None if selected == 1 else stamp(entries[selected]),
        'source_url': source_url, 'claims': [], 'relations': []}
