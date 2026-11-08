from __future__ import annotations

from datetime import date, time

from pydantic import BaseModel, Field, model_validator


class CostModel(BaseModel):
    brokerage_per_order: float = Field(ge=0)
    stt_sell_percent: float = Field(ge=0)
    exchange_txn_percent: float = Field(ge=0)
    sebi_per_crore: float = Field(ge=0)
    stamp_buy_percent: float = Field(ge=0)
    gst_percent: float = Field(ge=0)
    slippage_percent: float = Field(ge=0)


class StrategyConfig(BaseModel):
    symbol: str = Field(pattern="^(NIFTY|BANKNIFTY)$")
    from_date: date
    to_date: date
    entry_time: time
    exit_time: time
    entry_signal: str
    option_type: str = Field(pattern="^(CE|PE)$")
    strike_offset_steps: int = 0
    expiry_rule: str = Field(pattern="^(weekly|monthly|specific)$")
    expiry_date: date | None = None
    entry_premium_min: float | None = Field(default=None, gt=0)
    entry_premium_max: float | None = Field(default=None, gt=0)
    target_percent: float = Field(gt=0, le=500)
    stop_loss_percent: float = Field(gt=0, lt=100)
    max_trades_per_day: int = Field(ge=1, le=20)
    max_daily_loss: float = Field(gt=0)
    max_open_positions: int = Field(ge=1, le=10)
    quantity_lots: int = Field(ge=1, le=100)
    starting_capital: float = Field(gt=0)
    costs: CostModel

    @model_validator(mode="after")
    def check_rules(self) -> "StrategyConfig":
        if self.from_date > self.to_date:
            raise ValueError("from_date must be on or before to_date")
        if self.exit_time <= self.entry_time:
            raise ValueError("exit_time must be after entry_time")
        if self.entry_signal != "scheduled_time":
            raise ValueError(
                "entry_signal must be scheduled_time. Other signals are not assumed; supply an approved rule before use."
            )
        if self.expiry_rule == "specific" and self.expiry_date is None:
            raise ValueError("expiry_date is required when expiry_rule is specific")
        if self.entry_premium_min is not None and self.entry_premium_max is not None:
            if self.entry_premium_min > self.entry_premium_max:
                raise ValueError("entry_premium_min cannot exceed entry_premium_max")
        return self

    def as_engine(self) -> dict:
        payload = self.model_dump(mode="json")
        return payload


class StrategySaveRequest(StrategyConfig):
    pass


class RunRequest(BaseModel):
    strategy_id: str | None = None
    strategy: StrategyConfig | None = None

    @model_validator(mode="after")
    def one_source(self) -> "RunRequest":
        if self.strategy_id is None and self.strategy is None:
            raise ValueError("Provide strategy_id or strategy")
        return self


class AtmPreviewRequest(BaseModel):
    spot_price: float = Field(gt=0)
    strike_step: int = Field(gt=0, description="Listed strike interval. Prefer the value inferred from contracts.")
