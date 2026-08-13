"""Keyword list handling.

keywords.csv is a permanent record of *why* something was searched (spec
section 24), so it carries provenance and an approval flag, not just the
search string. The AI generator writes rows with source=ai, approved=no;
nothing is crawled until a human flips that to yes.
"""

import csv
import os

FIELDS = ["keyword", "parent_topic", "source", "approved", "priority"]
TRUTHY = {"yes", "y", "true", "1", "approved"}


def load(path, include_unapproved=False):
    """Read keywords.csv. Returns a list of row dicts."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"no keyword file at {path}")

    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            keyword = (row.get("keyword") or "").strip()
            if not keyword or keyword.startswith("#"):
                continue
            approved = (row.get("approved") or "yes").strip().lower() in TRUTHY
            if not approved and not include_unapproved:
                continue
            rows.append({
                "keyword": keyword,
                "parent_topic": (row.get("parent_topic") or "").strip() or None,
                "source": (row.get("source") or "manual").strip(),
                "approved": approved,
                "priority": (row.get("priority") or "").strip() or None,
            })

    # Deduplicate while preserving file order.
    seen = set()
    unique = []
    for row in rows:
        key = row["keyword"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def merge(path, new_rows):
    """Add generated keywords to the file without disturbing existing ones.

    Existing rows keep their approval state -- regenerating must never
    silently re-approve something a human previously rejected, nor unapprove
    something they approved.
    """
    try:
        existing = load(path, include_unapproved=True)
    except FileNotFoundError:
        existing = []

    known = {row["keyword"].lower() for row in existing}
    added = [row for row in new_rows if row["keyword"].lower() not in known]

    save(path, existing + added)
    return added, [row for row in new_rows if row["keyword"].lower() in known]


def set_approval(path, approved, keywords=None):
    """Approve or unapprove keywords. keywords=None means all of them."""
    rows = load(path, include_unapproved=True)
    wanted = {k.lower() for k in keywords} if keywords else None

    changed = []
    for row in rows:
        if wanted is None or row["keyword"].lower() in wanted:
            if row["approved"] != approved:
                row["approved"] = approved
                changed.append(row["keyword"])

    save(path, rows)
    return changed


def save(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "keyword": row["keyword"],
                "parent_topic": row.get("parent_topic") or "",
                "source": row.get("source") or "manual",
                "approved": "yes" if row.get("approved") else "no",
                "priority": row.get("priority") or "",
            })
