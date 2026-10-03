"""Application errors shared by services and HTTP adapters."""


class BusinessError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 422,
                 current_state_version: int | None = None,
                 current_contract_version: int | None = None, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.current_state_version = current_state_version
        self.current_contract_version = current_contract_version
        self.field = field
