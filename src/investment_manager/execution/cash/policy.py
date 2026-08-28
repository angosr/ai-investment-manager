from pydantic import Field, model_validator

from investment_manager.kernel.configuration import StrictConfig


class CashYieldEvidencePolicy(StrictConfig):
    version: str
    enabled: bool = False
    rest_base_url: str = "https://api.binance.com"
    product_id: str = Field(default="USDT001", pattern=r"^[A-Z0-9._-]+$")
    asset: str = Field(default="USDT", pattern=r"^[A-Z0-9._-]+$")
    refresh_seconds: int = Field(default=3600, ge=3600, le=86400)
    request_timeout_seconds: int = Field(default=10, ge=1, le=10)
    recv_window_ms: int = Field(default=5000, ge=1000, le=5000)
    credential_environment_prefix: str = "INVESTMENT_MANAGER_BINANCE"

    @model_validator(mode="after")
    def endpoint_and_identity_are_explicit(self):
        if self.rest_base_url != "https://api.binance.com":
            raise ValueError("现金收益证据只允许 Binance 官方主站只读端点")
        if self.credential_environment_prefix != "INVESTMENT_MANAGER_BINANCE":
            raise ValueError("现金收益证据凭证前缀不可漂移")
        return self
