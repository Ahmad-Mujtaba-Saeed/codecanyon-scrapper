"""Record shapes shared between the parser, the store and the exporters.

Every parsed value keeps its raw source string alongside the normalized one.
When a number looks wrong six months from now, the raw string is what lets
you tell a parsing bug from a real market change.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict


@dataclass
class Product:
    """One item card, as seen on a search results page."""

    product_id: str
    title: str
    url: str

    author_name: Optional[str] = None
    author_url: Optional[str] = None

    category: Optional[str] = None
    subcategory: Optional[str] = None

    price: Optional[float] = None
    price_raw: Optional[str] = None

    # Absent sales element means zero sales, not unknown. The parser is
    # responsible for making that distinction explicit.
    sales: Optional[int] = None
    sales_raw: Optional[str] = None

    rating: Optional[float] = None
    review_count: Optional[int] = None
    rating_raw: Optional[str] = None

    software_version: Optional[str] = None
    framework: Optional[str] = None
    compatible_with: Optional[str] = None
    file_types: Optional[str] = None
    attributes_raw: Dict[str, str] = field(default_factory=dict)

    last_updated: Optional[str] = None       # ISO yyyy-mm-dd
    last_updated_raw: Optional[str] = None

    def as_dict(self):
        return asdict(self)


@dataclass
class Occurrence:
    """Where a product showed up: keyword x sort x page x position."""

    product_id: str
    keyword: str
    sort: str
    page: int
    position: int          # 1-based rank across the whole result set


@dataclass
class PageResult:
    """Outcome of fetching and parsing a single search results page."""

    keyword: str
    sort: str
    page: int
    url: str
    http_status: Optional[int] = None
    fetched_at: Optional[str] = None
    raw_path: Optional[str] = None

    total_results: Optional[int] = None
    has_next: bool = False
    from_cache: bool = False
    error: Optional[str] = None

    products: List[Product] = field(default_factory=list)
    occurrences: List[Occurrence] = field(default_factory=list)

    cards_seen: int = 0
    cards_parsed: int = 0

    @property
    def parse_ratio(self):
        if not self.cards_seen:
            return 1.0
        return self.cards_parsed / self.cards_seen

    @property
    def ok(self):
        return self.error is None and self.http_status == 200
