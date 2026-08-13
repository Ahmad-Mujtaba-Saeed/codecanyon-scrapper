"""Search URL construction.

The scraper never takes URLs as input. It takes keywords and builds the
URLs itself (spec sections 4 and 5).
"""

import re
from urllib.parse import quote

# "relevance" is CodeCanyon's default and carries no sort parameter.
DEFAULT_SORT = "relevance"

VALID_SORTS = {
    "relevance", "sales", "date", "rating", "trending", "price-asc", "price-desc",
}


def search_url(base_url, keyword, sort=DEFAULT_SORT, page=1):
    """Build a search URL for one keyword, sort mode and page.

    'ultimate pos integration' -> /search/ultimate%20pos%20integration?page=2
    """
    if sort not in VALID_SORTS:
        raise ValueError(f"unknown sort mode: {sort!r}")
    if page < 1:
        raise ValueError(f"page must be >= 1, got {page}")

    encoded = quote(keyword.strip(), safe="")
    url = f"{base_url}/search/{encoded}"

    params = []
    if sort != DEFAULT_SORT:
        params.append(f"sort={sort}")
    if page > 1:
        params.append(f"page={page}")
    if params:
        url += "?" + "&".join(params)
    return url


def keyword_slug(keyword):
    """Filesystem-safe directory name for a keyword."""
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.strip().lower()).strip("-")
    return slug or "keyword"
