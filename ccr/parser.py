"""Search results page -> structured records.

Selector notes, verified against a live page on 2026-08-12:

  * Cards are selected by [data-price][data-item-id] rather than by class
    name. Envato's class names are build hashes (shared-item_cards-...)
    that rotate on deploy; the data attributes are behavioural and change
    far less often. data-item-id alone is not enough -- it also appears on
    favourite and collection buttons, five times per card.

  * A missing sales element means zero sales, not unknown sales. Twenty-nine
    of thirty cards on the sample page had one. Treating the absence as NULL
    would quietly bias every average in the analysis.

  * published_date is NOT available on search cards, only last_updated.
    Collecting it requires visiting each item page.

  * Pagination end is taken from <link rel="next"> in the head, which is
    authoritative, rather than from guessing at the pagination widget.
"""

import datetime
import json
import re

from bs4 import BeautifulSoup

from .models import Product, Occurrence, PageResult


class ParserHealthError(Exception):
    """Raised when the page parses so badly the selectors are likely stale."""


# --------------------------------------------------------------- normalizers

def parse_price(raw):
    """'$49' / '49 USD' / 'USD 29' / '49.00' -> 49.0"""
    if raw is None:
        return None
    m = re.search(r"(\d[\d,]*\.?\d*)", str(raw))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_sales(raw):
    """'145 Sales' -> 145, '3K Sales' -> 3000, '3,000 Sales' -> 3000"""
    if raw is None:
        return None
    text = str(raw).strip()
    m = re.search(r"(\d[\d,]*\.?\d*)\s*([KkMm])?", text)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def parse_rating(aria_label):
    """'Rated 5.0 out of 5, 3 reviews' -> (5.0, 3)"""
    if not aria_label:
        return None, None
    rating = None
    reviews = None
    m = re.search(r"Rated\s+([\d.]+)\s+out of", aria_label, re.I)
    if m:
        rating = float(m.group(1))
    m = re.search(r"([\d,]+)\s+review", aria_label, re.I)
    if m:
        reviews = int(m.group(1).replace(",", ""))
    return rating, reviews


def parse_updated(raw):
    """'Last updated: 04 May 26' -> '2026-05-04'"""
    if not raw:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{2,4})", raw)
    if not m:
        return None
    day, month, year = m.groups()
    year = int(year)
    if year < 100:
        year += 2000
    try:
        month_num = datetime.datetime.strptime(month[:3], "%b").month
    except ValueError:
        return None
    try:
        return datetime.date(year, month_num, int(day)).isoformat()
    except ValueError:
        return None


def parse_item_id(url):
    """Item id is the trailing path segment of the item URL."""
    if not url:
        return None
    m = re.search(r"/item/[^/]+/(\d+)", url)
    return m.group(1) if m else None


# ------------------------------------------------------------------ page bits

def _text(node):
    return node.get_text(" ", strip=True) if node else None


def extract_total_results(soup):
    """The '150 results' line above the grid."""
    text = soup.get_text(" ", strip=True)
    m = re.search(r"([\d,]+)\s+results?\b", text, re.I)
    if m:
        return int(m.group(1).replace(",", ""))
    if re.search(r"\bno results?\b|didn'?t match", text, re.I):
        return 0
    return None


def extract_has_next(soup):
    return soup.select_one('link[rel="next"]') is not None


def _split_category(href):
    """'/category/php-scripts/add-ons' -> ('php-scripts', 'add-ons')"""
    if not href:
        return None, None
    m = re.search(r"/category/(.+)$", href.split("?")[0].rstrip("/"))
    if not m:
        return None, None
    parts = [p for p in m.group(1).split("/") if p]
    if not parts:
        return None, None
    return parts[0], ("/".join(parts[1:]) or None)


# ------------------------------------------------------------------ the card

def parse_card(card):
    """One item card -> Product, or None if it lacks an identity."""
    product_id = card.get("data-item-id")

    name_link = (card.select_one('h3 a[href*="/item/"]')
                 or card.select_one('a[class*="item_name_component__"]'))
    url = name_link.get("href") if name_link else None
    if not url:
        overlay = card.select_one('a[class*="itemLinkOverlay"]')
        url = overlay.get("href") if overlay else None

    if not product_id:
        product_id = parse_item_id(url)
    if not product_id or not url:
        return None

    # get_text drops the <mark> highlight tags the search engine injects
    title = _text(name_link)
    if not title:
        overlay = card.select_one('a[class*="itemLinkOverlay"]')
        title = overlay.get("title") if overlay else None

    author_link = card.select_one('a[href*="/user/"]')
    category_link = card.select_one('a[href*="/category/"]')
    category, subcategory = _split_category(
        category_link.get("href") if category_link else None)

    # data-price is the exact decimal; the visible text is rounded ("$49")
    price_el = card.select_one('[class*="price_component__root"]')
    price_raw = _text(price_el)
    price = parse_price(card.get("data-price"))
    if price is None:
        price = parse_price(price_raw)

    # Absence means zero, not unknown.
    sales_el = card.select_one('[class*="sales_component__root"]')
    sales_raw = _text(sales_el)
    sales = parse_sales(sales_raw) if sales_el is not None else 0

    rating_el = card.select_one('[class*="starRating"]')
    rating_raw = rating_el.get("aria-label") if rating_el else None
    rating, review_count = parse_rating(rating_raw)

    attributes = {}
    for li in card.select('[class*="attributes_component__attribute"]'):
        label_el = li.select_one('[class*="__label"]')
        value_el = li.select_one('[class*="__value"]')
        if label_el and value_el:
            label = _text(label_el).rstrip(":").strip()
            attributes[label] = _text(value_el)

    file_types = [_text(x) for x in
                  card.select('[class*="included_files_component__fileType"]')]

    updated_el = card.select_one('[class*="__lastUpdated"]')
    updated_raw = _text(updated_el)

    return Product(
        product_id=str(product_id),
        title=title,
        url=url,
        author_name=_text(author_link),
        author_url=author_link.get("href") if author_link else None,
        category=category,
        subcategory=subcategory,
        price=price,
        price_raw=price_raw,
        sales=sales,
        sales_raw=sales_raw,
        rating=rating,
        review_count=review_count,
        rating_raw=rating_raw,
        software_version=attributes.get("Software Version"),
        framework=attributes.get("Software Framework"),
        compatible_with=attributes.get("Compatible With"),
        file_types=", ".join(t for t in file_types if t) or None,
        attributes_raw=attributes,
        last_updated=parse_updated(updated_raw),
        last_updated_raw=updated_raw,
    )


def parse_search_page(html, keyword, sort, page, url,
                      items_per_page=30, min_success_ratio=0.9):
    """Parse a full results page into a PageResult.

    Raises ParserHealthError when cards are present but mostly unparseable,
    which is the signature of rotated class names. Failing loudly beats
    writing four hundred rows of nulls.
    """
    soup = BeautifulSoup(html, "html.parser")

    result = PageResult(keyword=keyword, sort=sort, page=page, url=url)
    result.total_results = extract_total_results(soup)
    result.has_next = extract_has_next(soup)

    cards = soup.select("[data-price][data-item-id]")
    result.cards_seen = len(cards)

    seen_on_page = set()
    for index, card in enumerate(cards):
        product = parse_card(card)
        if product is None:
            continue
        result.cards_parsed += 1

        if product.product_id in seen_on_page:
            continue    # same item rendered twice on one page
        seen_on_page.add(product.product_id)

        result.products.append(product)
        result.occurrences.append(Occurrence(
            product_id=product.product_id,
            keyword=keyword,
            sort=sort,
            page=page,
            position=(page - 1) * items_per_page + index + 1,
        ))

    # Two distinct failure shapes, both meaning "the selectors are stale":
    # cards found but not parseable, and no cards found at all on a page the
    # site says has results. The second is the dangerous one -- without this
    # check a rotated attribute name looks exactly like an empty result set.
    if result.cards_seen and result.parse_ratio < min_success_ratio:
        raise ParserHealthError(
            f"only {result.cards_parsed}/{result.cards_seen} cards parsed on "
            f"{url}; selectors are probably stale")

    if not result.cards_seen and (result.total_results or 0) > 0:
        raise ParserHealthError(
            f"{url} reports {result.total_results} results but no item cards "
            f"were found; selectors are probably stale")

    return result
