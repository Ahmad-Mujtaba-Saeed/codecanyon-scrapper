"""Raw HTML archive.

Every fetched page is stored gzipped and never modified. Two reasons:

  1. Envato's CSS class names are build hashes and will rotate on a deploy.
     When the parser breaks, the archive lets you fix the selectors and
     re-parse historical runs instead of re-scraping them.
  2. It is the evidence behind every number in the CSVs.

Layout: research/raw/<date>/<run_id>/<keyword-slug>/<sort>/page-N.html.gz
"""

import gzip
import os

from .urls import keyword_slug


class RawArchive:
    def __init__(self, root, run_id, run_date):
        self.root = root
        self.run_id = run_id
        self.run_date = run_date

    def path_for(self, keyword, sort, page):
        return os.path.join(
            self.root, self.run_date, self.run_id,
            keyword_slug(keyword), sort, f"page-{page}.html.gz")

    def save(self, keyword, sort, page, body):
        path = self.path_for(keyword, sort, page)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wb") as f:
            f.write(body)
        return path

    @staticmethod
    def load(path):
        with gzip.open(path, "rb") as f:
            return f.read()

    @staticmethod
    def exists(path):
        return bool(path) and os.path.exists(path)
