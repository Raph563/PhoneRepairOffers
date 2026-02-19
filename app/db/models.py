from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class PartType(str, Enum):
    PHONE_WITHOUT_SCREEN = "phone_without_screen"
    REPLACEMENT_SCREEN = "replacement_screen"


class SourceName(str, Enum):
    LBC = "leboncoin"
    EBAY = "ebay"
    ALIEXPRESS = "aliexpress"


class SearchCategory(str, Enum):
    MOBILE_PHONE_PARTS = "mobile_phone_parts"
    AUTO = "auto"


class SearchRequest(BaseModel):
    brand: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=80)
    partType: PartType
    category: SearchCategory = SearchCategory.MOBILE_PHONE_PARTS
    maxPriceEur: Optional[float] = Field(default=None, ge=0)
    sources: list[SourceName] = Field(
        default_factory=lambda: [SourceName.LBC, SourceName.EBAY, SourceName.ALIEXPRESS]
    )
    forceRefresh: bool = False


class Offer(BaseModel):
    id: str
    source: SourceName
    sourceOfferId: str
    title: str
    url: HttpUrl
    imageUrl: Optional[HttpUrl] = None
    priceEur: float = Field(ge=0)
    shippingEur: float = Field(ge=0, default=0)
    totalEur: float = Field(ge=0)
    location: Optional[str] = None
    conditionText: Optional[str] = None
    postedAt: Optional[str] = None
    isRecentlyAdded: bool = False
    queryType: PartType
    rankScore: float


class Favorite(BaseModel):
    favoriteId: int
    createdAt: str
    offer: Offer


class ToggleFavoriteRequest(BaseModel):
    source: SourceName
    sourceOfferId: str = Field(min_length=1, max_length=120)
    offer: Optional[Offer] = None
