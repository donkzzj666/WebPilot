"""Public settings input and immutable, nonsecret runtime snapshots."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from ..models.transport import ModelConfig
from ..tasks.models import Hash, Positive, StrictModel

DISCLOSURE_VERSION = 'model-data-v1'
DISCLOSURE = {
    'version': DISCLOSURE_VERSION,
    'provider': 'DeepSeek',
    'items': ['任务指令、目标与必要参数', '经过过滤的任务相关网页正文', '明确选中的必要截图'],
    'message': '执行模型任务时，上述内容可能发送给 DeepSeek。请先确认这些内容可以交给该供应商处理；密钥不会加入模型提示或普通日志。',
}


class ModelConnection(ModelConfig):
    # The first supported connection is intentionally the frozen M0 provider.
    # An arbitrary base URL must never turn this endpoint into secret forwarding.
    model_id: Literal['deepseek-flash'] = 'deepseek-flash'
    base_url: Literal['https://api.deepseek.com', 'https://api.deepseek.com/v1'] = 'https://api.deepseek.com'
    prompt_version: Literal['m1-05-model-v1'] = 'm1-05-model-v1'

    @model_validator(mode='after')
    def configured_price_required(self):
        if self.price_version is not None and self.pricing is None:
            raise ValueError('price version requires a configured token schedule')
        return self


class ModelSettingsRequest(StrictModel):
    expected_version: Annotated[int, Field(ge=0, le=2**63 - 2)]
    model: ModelConnection
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    accept_data_sharing: Literal[True]

    @field_validator('accept_data_sharing', mode='before')
    @classmethod
    def explicit_acceptance(cls, value):
        if value is not True:
            raise ValueError('explicit data sharing acceptance is required')
        return value

    @field_validator('api_key', mode='before')
    @classmethod
    def nonempty_credential(cls, value):
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if (type(raw) is not str or not 1 <= len(raw) <= 8192
                or any(not 33 <= ord(char) <= 126 for char in raw)):
            raise ValueError('a nonempty bounded credential is required when supplied')
        return value


class RuntimeConfig(StrictModel):
    schema_version: Literal['m1-runtime-v1'] = 'm1-runtime-v1'
    settings_version: Positive
    model_config_sha256: Hash
    credential_ref: str | None
    disclosure_version: Literal['model-data-v1'] = DISCLOSURE_VERSION
    observation_policy: Literal['filtered-task-data-v1'] = 'filtered-task-data-v1'
    output_schema_version: Literal['m0-contract-v1'] = 'm0-contract-v1'
    arbitrary_code_tools_enabled: Literal[False] = False

    @field_validator('credential_ref')
    @classmethod
    def opaque_reference(cls, value):
        if value is not None and (str(UUID(value)) != value or UUID(value).version != 4):
            raise ValueError('credential reference must be a canonical UUID v4')
        return value
