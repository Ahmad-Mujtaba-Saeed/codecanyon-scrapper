"""robots.txt matching.

The standard library's urllib.robotparser is unusable here for two reasons:

  1. It fetches robots.txt with Python's own user agent, gets a 403 from
     Cloudflare, and then fails closed by disallowing every URL. Verified:
     it returns disallow_all=True with zero parsed entries.
  2. It does not implement the wildcard patterns Envato actually uses, such
     as "Disallow: *?sort=*".

So we fetch robots.txt through the same client as every other request and
match with Google's rules: '*' matches any run of characters, a trailing
'$' anchors the end, and the longest matching rule wins with Allow beating
Disallow on ties.
"""

import re
from urllib.parse import urlsplit


def _compile(pattern):
    """Translate a robots path pattern into a regex."""
    anchored_end = pattern.endswith("$")
    if anchored_end:
        pattern = pattern[:-1]
    parts = [re.escape(p) for p in pattern.split("*")]
    regex = ".*".join(parts)
    return re.compile("^" + regex + ("$" if anchored_end else ""))


class RobotsRules:
    """Rules from the User-agent: * group of a robots.txt file."""

    def __init__(self, rules=None, source=None):
        # list of (pattern_length, allowed, compiled_regex)
        self._rules = rules or []
        self.source = source

    @classmethod
    def parse(cls, text, source=None):
        rules = []
        in_star_group = False
        # A blank line ends a group; consecutive User-agent lines share rules.
        previous_was_agent = False

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                previous_was_agent = False
                continue
            if ":" not in line:
                continue

            field, value = line.split(":", 1)
            field = field.strip().lower()
            value = value.strip()

            if field == "user-agent":
                if not previous_was_agent:
                    in_star_group = False
                if value == "*":
                    in_star_group = True
                previous_was_agent = True
                continue

            previous_was_agent = False
            if field not in ("allow", "disallow") or not in_star_group:
                continue
            if field == "disallow" and value == "":
                continue    # "Disallow:" with no value means allow everything
            if not value:
                continue

            rules.append((len(value), field == "allow", _compile(value)))

        return cls(rules, source=source)

    @classmethod
    def permissive(cls):
        """Used when robots.txt is unreachable and the caller opts out."""
        return cls([], source="permissive")

    def allowed(self, url):
        """Is this URL crawlable under the User-agent: * group?"""
        split = urlsplit(url)
        path = split.path or "/"
        if split.query:
            path += "?" + split.query

        best = None   # (length, allowed)
        for length, is_allow, regex in self._rules:
            if regex.match(path):
                if best is None or length > best[0] or (
                        length == best[0] and is_allow):
                    best = (length, is_allow)

        return True if best is None else best[1]

    def reason(self, url):
        """The matching rule for a blocked URL, for error messages."""
        split = urlsplit(url)
        path = split.path or "/"
        if split.query:
            path += "?" + split.query
        hits = [r.pattern for length, is_allow, r in self._rules
                if not is_allow and r.match(path)]
        return hits[0] if hits else None


def fetch_rules(client, base_url):
    """Load robots.txt using our own HTTP client so the UA is a real one."""
    url = f"{base_url.rstrip('/')}/robots.txt"
    status, body, _headers = client.get_raw(url)
    if status != 200 or not body:
        raise RuntimeError(f"could not read robots.txt ({status})")
    return RobotsRules.parse(body.decode("utf-8", "replace"), source=url)
