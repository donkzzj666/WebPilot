"""Facts issued by a trusted page adapter, never by the model's write hint."""
from dataclasses import dataclass
import re

from ..errors import BusinessError
from ..tasks.models import RepositoryWritePolicy


@dataclass(frozen=True)
class WriteAuthorization:
    repository: str
    branch: str
    base_sha: str
    operation: str
    files: tuple[str, ...]
    identity_ref: str
    allowed_mutations: tuple[tuple[str, str], ...]
    # Adapter facts, distinct from the model's attempt/run identifiers. Older
    # structured adapters use the action digest and current page fingerprint.
    expected_change_sha256: str | None = None
    precondition_version: str | None = None
    adapter_id: str | None = None


def validate_write_authorization(grant, action, contract):
    policy, scope = contract.action_policy, action.target.write_scope
    if (type(grant) is not WriteAuthorization or not isinstance(policy, RepositoryWritePolicy)
            or scope is None or scope.identity_ref != contract.identity_ref
            or (grant.repository, grant.branch, grant.base_sha, grant.operation, grant.identity_ref)
            != (policy.repository, policy.branch, policy.base_sha, scope.operation, contract.identity_ref)
            or type(grant.files) is not tuple or grant.files != tuple(scope.files)
            or scope.operation not in policy.allowed_operations
            or any(not policy.permits_file(path) for path in grant.files)
            or type(grant.allowed_mutations) is not tuple
            or grant.adapter_id is not None and (type(grant.adapter_id) is not str or not 1 <= len(grant.adapter_id) <= 200)
            or grant.expected_change_sha256 is not None and (type(grant.expected_change_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', grant.expected_change_sha256) is None)
            or grant.precondition_version is not None and (type(grant.precondition_version) is not str
                or not 1 <= len(grant.precondition_version) <= 200)):
        raise BusinessError('FORBIDDEN', 'Trusted page facts differ from the frozen write policy', status=403)
    for request in grant.allowed_mutations:
        if (type(request) is not tuple or len(request) != 2 or request[0] not in ('POST', 'PUT', 'PATCH', 'DELETE')
                or not any(source.permits(request[1]) for source in contract.sources)):
            raise BusinessError('FORBIDDEN', 'Mutation endpoint is outside the frozen source scope', status=403)
    return grant
