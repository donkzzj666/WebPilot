"""Small version-bound public requests and one internal dispatch barrier."""
from typing import Annotated, Literal
from pydantic import Field
from ..errors import BusinessError
from ..tasks.models import StrictModel, IdempotencyKey

Action = Literal['start','retry','pause','resume','cancel']


class ControlRequest(StrictModel):
    expected_state_version: Annotated[int, Field(ge=0,le=2**63-2)]
    contract_version: Annotated[int, Field(ge=1,le=2**63-2)]
    settings_version: Annotated[int, Field(ge=0,le=2**63-2)]
    idempotency_key: IdempotencyKey | None = None


class ControlPending(BusinessError):
    def __init__(self, operation: dict):
        self.operation_id=operation['operation_id']
        self.action=operation['action']
        super().__init__('CONTROL_PENDING','A durable user control request is awaiting a safe boundary',
                         status=409,field=self.action)
