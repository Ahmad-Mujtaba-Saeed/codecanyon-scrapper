"""OpenAI client for keyword generation and market analysis.

Uses gpt-4o-mini through the chat completions API, over the standard library
so the project keeps its single dependency.

The API key is read from the environment (OPENAI_API_KEY by default) and is
never written to disk, never logged, and never stored in the run's config
snapshot. If the key is absent, every AI feature degrades to producing a
paste-ready bundle instead of failing -- the dataset is the deliverable, and
the model is a convenience on top of it.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request


class AIUnavailable(Exception):
    """No API key configured. Callers should fall back to the bundle."""


class AIError(Exception):
    """The API was reachable but the call failed."""


def api_key(cfg_ai):
    return os.environ.get(cfg_ai.get("api_key_env", "OPENAI_API_KEY"), "").strip()


def available(cfg_ai):
    return bool(api_key(cfg_ai))


class OpenAIClient:
    """Minimal chat-completions client.

    `transport` exists so tests can exercise request building and response
    handling without network access or a key.
    """

    def __init__(self, cfg_ai, transport=None, sleep=time.sleep):
        self.cfg = cfg_ai
        self.model = cfg_ai.get("model", "gpt-4o-mini")
        self.api_base = cfg_ai.get("api_base",
                                   "https://api.openai.com/v1").rstrip("/")
        self.timeout = cfg_ai.get("timeout", 180)
        self._transport = transport
        self._sleep = sleep

    # ----------------------------------------------------------- transport

    def _post(self, path, payload):
        if self._transport:
            return self._transport(path, payload)

        key = api_key(self.cfg)
        if not key:
            raise AIUnavailable(
                f"{self.cfg.get('api_key_env', 'OPENAI_API_KEY')} is not set")

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}{path}", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})

        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = json.loads(e.read().decode("utf-8")).get(
                        "error", {}).get("message", "")
                except Exception:                    # noqa: BLE001
                    pass
                if e.code == 401:
                    raise AIError(
                        "OpenAI rejected the API key (401). Check "
                        f"{self.cfg.get('api_key_env', 'OPENAI_API_KEY')}.")
                if e.code in (429, 500, 502, 503):
                    last_error = f"HTTP {e.code}: {detail}"
                    self._sleep(min(30, 3 * (2 ** attempt)))
                    continue
                raise AIError(f"HTTP {e.code}: {detail or e.reason}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = repr(e)
                self._sleep(min(30, 3 * (2 ** attempt)))

        raise AIError(f"gave up after 3 attempts: {last_error}")

    # ---------------------------------------------------------------- chat

    def chat(self, system, user, temperature=None, max_tokens=None,
             json_mode=False):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": (self.cfg.get("temperature", 0.4)
                            if temperature is None else temperature),
            "max_tokens": max_tokens or self.cfg.get("max_output_tokens", 4000),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        response = self._post("/chat/completions", payload)
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise AIError(f"unexpected response shape: {e}") from e


# ------------------------------------------------------------------ prompts

KEYWORD_SYSTEM = """\
You generate search keywords for market research on CodeCanyon, the Envato \
marketplace for scripts, plugins and modules.

The keywords are typed into CodeCanyon's own search box, so they must read \
like what a buyer would search there: short product-and-capability phrases, \
lowercase, two to four words, no boolean operators, no quotes, no site: \
filters.

A good keyword set spans three bands:
  - broad, to size the whole market (e.g. "perfex")
  - capability, to size a segment (e.g. "perfex api", "perfex automation")
  - speculative, to test whether a niche is empty (e.g. "perfex mcp")

Keywords expected to return nothing are valuable, not wasted: an empty result \
is evidence about competition. Include a few deliberately.

Return JSON: {"keywords": [{"keyword": "...", "band": "broad|capability|\
speculative", "priority": "high|medium|low", "rationale": "one short line"}]}"""

ANALYSIS_SYSTEM = """\
You are a market analyst advising a software company deciding what product to \
build next for a marketplace niche.

You will receive a structured dataset collected from CodeCanyon search \
results. Base every claim on it, and quote the numbers you rely on. Where the \
data cannot answer something, say so plainly rather than speculating.

Three things you must keep straight:
  - Sales figures are lifetime totals, not rates. An old product's total \
reflects years of accumulation, so pair it with the last-updated date before \
calling it evidence of current demand.
  - Zero results for a keyword means no CodeCanyon competition. It does NOT \
mean opportunity: it may equally mean no demand. Say which you think it is \
and why.
  - Feature counts come from product titles, so they measure what vendors \
advertise, not what products do.

Write in Markdown, in the section order given by the user. Be concrete and \
brief; a reader should be able to act on it."""


def _analysis_user_prompt(bundle_text, topic):
    return f"""\
Analyse this CodeCanyon market research dataset for: {topic}

Produce a report with exactly these sections:

## Summary
Three sentences: what this market is, how big, and how contested.

## Market demand
Total products, total sales, distribution, top sellers. What the median vs \
average gap tells us.

## Competition
How many competitors, how concentrated, who dominates and how hard they would \
be to displace.

## Segments
Size each capability segment present in the data (API, automation, AI, MCP, \
integrations, and any others that stand out). Give numbers per segment.

## Product age and maintenance
Which incumbents look neglected, and which fresh products are gaining.

## Pricing
The price band that sells, and where the gaps are.

## Market gaps
Specific unserved or underserved needs, each with the evidence line that \
supports it. For every gap, state whether the evidence suggests genuine \
unmet demand or simply no demand.

## Opportunities
A Markdown table with columns: Opportunity | Demand | Competition | \
Difficulty | Potential. Rate each Low/Medium/High and add a one-line reason \
column at the end.

## Recommendation
One product to build, an MVP feature list of 6-10 items, a confidence level \
(low/medium/high), and the three strongest reasons. If the data does not \
support building anything here, say that instead.

## What this data cannot tell you
The honest limits of this dataset for this decision.

Dataset follows.

{bundle_text}"""


# --------------------------------------------------------------- operations

def generate_keywords(client, topic, existing=None, count=14):
    """Ask the model for a keyword set. Returns a list of dicts."""
    existing = existing or []
    user = f"Product or market to research: {topic}\n"
    user += f"Generate {count} keywords.\n"
    if existing:
        user += ("\nAlready in the list, do not repeat these but stay "
                 "consistent with their style:\n"
                 + "\n".join(f"- {k}" for k in existing[:40]))

    raw = client.chat(KEYWORD_SYSTEM, user, json_mode=True)
    return parse_keyword_response(raw, topic)


def parse_keyword_response(raw, topic):
    """Tolerant parsing: models occasionally wrap JSON in prose or fences."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)

    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise AIError(f"could not parse a JSON object from: {text[:200]}")
        data = json.loads(match.group(0))

    items = data.get("keywords") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise AIError("response did not contain a keywords list")

    out, seen = [], set()
    for item in items:
        if isinstance(item, str):
            item = {"keyword": item}
        keyword = (item.get("keyword") or "").strip().lower()
        # Guard against the model returning search syntax rather than a phrase.
        if not keyword or len(keyword) > 80 or keyword in seen:
            continue
        if any(c in keyword for c in '"()[]:'):
            continue
        seen.add(keyword)
        out.append({
            "keyword": keyword,
            "parent_topic": topic,
            "source": "ai",
            "approved": False,          # nothing is crawled until approved
            "priority": (item.get("priority") or "medium").strip().lower(),
            "band": (item.get("band") or "").strip().lower(),
            "rationale": (item.get("rationale") or "").strip(),
        })
    if not out:
        raise AIError("model returned no usable keywords")
    return out


def analyse(client, bundle_text, topic):
    return client.chat(ANALYSIS_SYSTEM,
                       _analysis_user_prompt(bundle_text, topic))
