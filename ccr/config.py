"""Configuration loading.

Everything tunable lives in config.json so pacing can be adjusted without
touching code. Paths are resolved relative to the project root.
"""

import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


class Config:
    def __init__(self, data, path):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path=None):
        path = path or DEFAULT_CONFIG_PATH
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f), path)

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    @property
    def base_url(self):
        return self._data["base_url"].rstrip("/")

    @property
    def sorts(self):
        return list(self._data.get("sorts") or ["relevance"])

    @property
    def respect_robots(self):
        return bool(self._data.get("respect_robots", True))

    @property
    def max_pages_per_keyword(self):
        return int(self._data.get("max_pages_per_keyword", 40))

    @property
    def user_agent(self):
        return self._data["http"]["user_agent"]

    def resolve(self, key):
        """Absolute path for one of the configured paths."""
        return os.path.join(PROJECT_ROOT, self._data["paths"][key])

    def snapshot(self):
        """JSON blob stored with each run so old runs stay interpretable."""
        return json.dumps(self._data, sort_keys=True)
