from decimal import Decimal

from pydantic import Field, model_validator

from investment_manager.kernel.configuration import StrictConfig


class ShadowSimulationPolicy(StrictConfig):
    version: str
    initial_quote_balance: Decimal = Field(default=Decimal("10000"), gt=0)
    analysis_deadline_seconds: int = Field(default=300, ge=30, le=900)


class BinanceTestnetPolicy(StrictConfig):
    version: str
    rest_base_url: str = "https://testnet.binance.vision/api"
    recv_window_ms: int = Field(default=5000, ge=1000, le=5000)
    request_timeout_seconds: int = Field(default=10, ge=1, le=10)
    time_sync_ttl_seconds: int = Field(default=30, ge=1, le=300)
    quote_asset: str = Field(default="USDT", pattern=r"^[A-Z0-9]{2,16}$")
    credential_environment_prefix: str = "INVESTMENT_MANAGER_BINANCE"

    @model_validator(mode="after")
    def testnet_endpoint_and_credentials_must_be_fixed(self):
        if self.rest_base_url != "https://testnet.binance.vision/api":
            raise ValueError("Binance Testnet 只允许官方 REST 端点")
        if self.credential_environment_prefix != "INVESTMENT_MANAGER_BINANCE":
            raise ValueError("Binance Testnet 凭证环境变量前缀不可变更")
        return self
