"""Pydantic data models for the Polymarket trading bot."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Market(BaseModel):
    market_id: str
    question: str
    yes_price: float = Field(ge=0.0, le=1.0)
    volume_24h: float = Field(ge=0.0)
    end_date: datetime
    category: str

    @field_validator("yes_price")
    @classmethod
    def round_price(cls, v: float) -> float:
        return round(v, 2)


class NewsArticle(BaseModel):
    title: str
    summary: str
    source: str
    published_date: datetime
    url: str
    quality_flag: Optional[str] = None  # "paywall", "state_media", "partisan", etc.


class Prediction(BaseModel):
    prediction_id: Optional[str] = None
    market_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    claude_probability: float = Field(ge=0.0, le=1.0)
    market_price: float = Field(ge=0.0, le=1.0)
    edge: float
    confidence: str = Field(pattern=r"^(low|medium|high)$")
    reasoning: str
    bayesian_prior: float = Field(ge=0.0, le=1.0)
    key_evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    news_articles: list[str] = Field(default_factory=list)
    news_quality_score: Optional[int] = None  # number of articles found


class Trade(BaseModel):
    trade_id: Optional[str] = None
    prediction_id: str
    direction: str = Field(pattern=r"^(YES|NO)$")
    size: float = Field(ge=0.0, le=1.00)
    limit_price: float = Field(ge=0.0, le=1.0)
    status: str = Field(
        default="simulated",
        pattern=r"^(simulated|pending|filled|cancelled)$",
    )
    outcome: Optional[float] = None  # 1.0 win, 0.0 loss, None pending
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("size")
    @classmethod
    def round_size(cls, v: float) -> float:
        return round(v, 2)


class TradeOpportunity(BaseModel):
    market: Market
    prediction: Prediction
    direction: str = Field(pattern=r"^(YES|NO)$")
    suggested_size: float
    edge_pct: float
    confidence: str
    kelly_fraction: float
