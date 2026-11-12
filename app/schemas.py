"""API 请求 / 响应模型（Pydantic v2）。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class MonthlyUsage(BaseModel):
    month: int = Field(ge=1, le=12, description="月份，1-12")
    peak_kwh: float = Field(ge=0, description="当月峰段用电量（千瓦时）")
    valley_kwh: float = Field(ge=0, description="当月谷段用电量（千瓦时）")


class CompareRequest(BaseModel):
    list_url: str = Field(min_length=1, description="报价站套餐列表页 URL，支持 http/https/file")
    region: str = Field(min_length=1, description="门店所在地区，需与套餐页地区标签匹配，如“上海”")
    monthly_usage: list[MonthlyUsage] = Field(min_length=12, max_length=12, description="未来 12 个月逐月峰谷用电量")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    @field_validator("list_url")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("list_url 仅支持 http://、https:// 或 file:// 协议")
        return value

    @field_validator("monthly_usage")
    @classmethod
    def _validate_months(cls, value: list[MonthlyUsage]) -> list[MonthlyUsage]:
        months = sorted(item.month for item in value)
        if months != list(range(1, 13)):
            raise ValueError("monthly_usage 必须恰好包含 1-12 月各一条记录")
        return value


class TierChargeOut(BaseModel):
    tier_index: int
    lower_kwh: float
    upper_kwh: float | None
    rate_cny_per_kwh: float
    charged_kwh: float
    charge_cny: float


class MonthlyBillOut(BaseModel):
    month: int
    peak_kwh: float
    valley_kwh: float
    peak_charges: list[TierChargeOut]
    valley_charges: list[TierChargeOut]
    peak_energy_fee_cny: float
    valley_energy_fee_cny: float
    base_fee_cny: float
    monthly_total_cny: float


class RankedPlanOut(BaseModel):
    rank: int
    plan_id: str
    plan_name: str
    supplier: str | None
    regions: list[str]
    annual_total_cny: float
    detail_url: str
    list_url: str
    monthly_bills: list[MonthlyBillOut]


class ExcludedPlanOut(BaseModel):
    plan_id: str
    plan_name: str
    supplier: str | None
    regions: list[str]
    reason: str
    detail: str
    detail_url: str
    list_url: str


class CrawlFailureOut(BaseModel):
    url: str
    stage: str
    reason: str
    status: int | None


# --------------------------------------------------------------------------- #
# 抄表整理 API
# --------------------------------------------------------------------------- #
class MeterRequest(BaseModel):
    index_url: str = Field(
        min_length=1, description="抄表站目录首页 URL，支持 http/https/file"
    )
    year: int = Field(ge=2000, le=2100, description="需要还原逐月用量的年份")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    @field_validator("index_url")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("index_url 仅支持 http://、https:// 或 file:// 协议")
        return value


class MeterSegmentOut(BaseModel):
    meter_id: str
    start_time: str
    end_time: str
    peak_start_kwh: float | None
    peak_end_kwh: float | None
    valley_start_kwh: float | None
    valley_end_kwh: float | None
    peak_start_record_id: str | None
    peak_end_record_id: str | None
    valley_start_record_id: str | None
    valley_end_record_id: str | None


class MeterMonthOut(BaseModel):
    month: int
    available: bool
    peak_kwh: float | None
    valley_kwh: float | None
    reasons: list[str]
    source_record_ids: list[str]
    source_urls: list[str]
    segments: list[MeterSegmentOut]


class MeterMonthlyUsageOut(BaseModel):
    """形状与比价接口 MonthlyUsage 一致，可直接传给 /compare-plans。"""
    month: int
    peak_kwh: float
    valley_kwh: float


class MeterResponse(BaseModel):
    index_url: str
    year: int
    comparable: bool = Field(description="整份 12 个月用量是否可直接用于比价")
    unavailable_months: list[int]
    crawl: dict
    duplicates: list[str] = Field(description="同表同刻的重复记录 id（已只计一次）")
    failures: list[CrawlFailureOut]
    monthly_usage: list[MeterMonthlyUsageOut] = Field(
        description="仅包含可用月份；不可用月份不填零，见 months"
    )
    months: list[MeterMonthOut]


class CompareResponse(BaseModel):
    request_region: str
    list_url: str
    crawl: dict
    conclusion: dict
    ranked_plans: list[RankedPlanOut]
    excluded_plans: list[ExcludedPlanOut]
    failures: list[CrawlFailureOut]
