"""Where papers come from, with per-host pacing.

Measured on a Kaggle IP (2026-11-16): the arXiv API, OpenAlex and Semantic Scholar
answer 429. These work: arxiv.org/abs, /html, /pdf, /search (HTML), OAI-PMH,
Tavily, and the GitHub API with the box's token.
"""
from __future__ import annotations

import html as htmlmod
import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse

from .. import webtools as wt

ARXIV_ID = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?![\d])")
GH = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


class Pace:
    """At most one request per `gap` seconds per key, shared by all threads; backoff after 429/503."""

    def __init__(self):
        self.next = {}
        self.lock = threading.Lock()

    def wait(self, key: str, gap: float) -> None:
        with self.lock:
            now = time.time()
            t = max(now, self.next.get(key, 0.0))
            self.next[key] = t + gap
        if t > now:
            time.sleep(t - now)

    def backoff(self, key: str, seconds: float) -> None:
        with self.lock:
            self.next[key] = max(self.next.get(key, 0.0), time.time() + seconds)


PACE = Pace()


def _get(url: str, key: str, gap: float, timeout: float = 40, tries: int = 4, **kw) -> tuple[bytes, str, str]:
    wait = 30
    for i in range(tries):
        PACE.wait(key, gap)
        try:
            return wt.fetch(url, timeout=timeout, **kw)
        except wt.WebError as e:
            msg = str(e)
            if "HTTP 404" in msg or "HTTP 410" in msg:
                raise
            if "HTTP 429" in msg or "HTTP 503" in msg:
                PACE.backoff(key, wait)
                wait = min(wait * 2, 600)
            if i == tries - 1:
                raise
    raise wt.WebError("unreachable")


def norm(s: str) -> str:
    """Folding used by the quote check: NFKC, lower case, letters and digits only."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    return " ".join(re.findall(r"[a-z0-9]+", s))


# ---------------------------------------------------------------- arXiv pages


def abs_meta(aid: str) -> dict:
    """Title, authors, versions with dates, comments, journal ref from the abs page."""
    raw, _, _ = _get(f"https://arxiv.org/abs/{aid}", "arxiv", 3.0)
    page = raw.decode("utf-8", "replace")

    def meta(name):
        return [htmlmod.unescape(x) for x in re.findall(rf'<meta name="{name}" content="([^"]*)"', page)]

    versions = re.findall(r"\[v(\d+)\]</a>\s*</strong>\s*([A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4})", page)
    if not versions:
        versions = re.findall(r"\[v(\d+)\]\s*</strong>\s*([A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4})", page)

    def cell(cls):
        m = re.search(rf'<td class="tablecell {cls}[^"]*">(.*?)</td>', page, re.S)
        return " ".join(re.sub(r"<[^>]+>", " ", htmlmod.unescape(m.group(1))).split()) if m else ""

    abstract = (meta("citation_abstract") or [""])[0]
    return {
        "id": aid, "title": " ".join((meta("citation_title") or [""])[0].split()),
        "authors": meta("citation_author")[:8], "abstract": " ".join(abstract.split()),
        "versions": [{"v": int(v), "date": d} for v, d in versions],
        "first_submitted": versions[0][1] if versions else (meta("citation_date") or [""])[0],
        "last_revised": versions[-1][1] if versions else "",
        "version": f"v{versions[-1][0]}" if versions else "",
        "comments": cell("comments"), "journal_ref": cell("jref"),
    }


def _refs_from(text: str, own: str) -> list[str]:
    ids = []
    for m in ARXIV_ID.finditer(text):
        i = m.group(1)
        if i != own and i not in ids and 7 <= int(i[:2]) <= 26:
            ids.append(i)
    return ids[:120]


def paper_text(aid: str, max_section: int = 10000) -> dict:
    """Full text cut into sections, plus the arXiv ids cited in the references.

    {title, abstract, sections:[{title,text}], full, refs, kind: arxiv_html|arxiv_pdf}
    """
    kind, body, refs = "arxiv_html", "", []
    try:
        raw, _, _ = _get(f"https://arxiv.org/html/{aid}", "arxiv", 3.0, timeout=60)
        page = raw.decode("utf-8", "replace")
        bib = page.find("ltx_bibliography")
        refs = _refs_from(page[bib:] if bib > 0 else "", aid)
        _, body = wt.html_to_text(page)
    except wt.WebError:
        body = ""
    if len(body) < 3000:
        kind = "arxiv_pdf"
        raw, _, _ = _get(f"https://arxiv.org/pdf/{aid}", "arxiv", 3.0, timeout=90, max_bytes=40_000_000)
        body = wt.tidy(wt.pdf_to_text(raw))
        cut = max(body.rfind("\nReferences"), body.rfind("\nREFERENCES"))
        refs = _refs_from(body[cut:] if cut > 0 else body[-30000:], aid)
    p = wt.paper(text=body, max_section=max_section)
    full = "\n".join([p.get("abstract", "")] + [s["title"] + "\n" + s["text"] for s in p["sections"]])
    return {"title": p["title"], "abstract": p.get("abstract", ""), "sections": p["sections"],
            "full": full, "refs": refs, "kind": kind}


def arxiv_search(query: str, n: int = 25, order: str = "") -> list[dict]:
    """arxiv.org/search HTML (the JSON API is blocked from Kaggle). [{id,title,abstract,submitted}]
    order "" = relevance (the site default is newest first, which hides the original papers)."""
    q = urllib.parse.urlencode({"query": query, "searchtype": "all", "abstracts": "show", "order": order,
                                "size": 50 if n > 25 else 25})
    raw, _, _ = _get(f"https://arxiv.org/search/?{q}", "arxiv_search", 8.0, timeout=60)
    page = raw.decode("utf-8", "replace")
    out = []
    for block in page.split('<li class="arxiv-result">')[1:]:
        m = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", block)
        if not m:
            continue

        def grab(pat):
            x = re.search(pat, block, re.S)
            return " ".join(re.sub(r"<[^>]+>", " ", htmlmod.unescape(x.group(1))).split()) if x else ""

        out.append({"id": m.group(1), "title": grab(r'<p class="title is-5 mathjax">(.*?)</p>'),
                    "abstract": grab(r'<span class="abstract-full[^>]*>(.*?)<a').replace("△ Less", "").strip(),
                    "submitted": grab(r"Submitted</span>\s*([^;<]+)")})
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- Tavily, with a hard budget


class Budget:
    def __init__(self, path: str, cap: int = 300):
        self.path, self.lock = path, threading.Lock()
        self.d = {"tavily_used": 0, "cap": cap, "by": {}}
        try:
            self.d.update(json.load(open(path)))
        except (OSError, ValueError):
            pass

    def take(self, purpose: str) -> bool:
        with self.lock:
            if self.d["tavily_used"] >= self.d["cap"]:
                return False
            self.d["tavily_used"] += 1
            self.d["by"][purpose] = self.d["by"].get(purpose, 0) + 1
            tmp = self.path + ".tmp"
            json.dump(self.d, open(tmp, "w"))
            os.replace(tmp, self.path)
            return True


def tavily(query: str, n: int = 5, domains: list[str] | None = None) -> list[dict]:
    key = os.environ.get("TAVILY_API_KEY") or (open("/root/.tavily").read().strip() if os.path.exists("/root/.tavily") else "")
    if not key:
        return []
    body = {"api_key": key, "query": query, "max_results": n, "search_depth": "basic"}
    if domains:
        body["include_domains"] = domains
    PACE.wait("tavily", 1.0)
    raw, _, _ = wt.fetch("https://api.tavily.com/search", timeout=40, data=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json"})
    return [{"url": r.get("url", ""), "title": r.get("title", ""), "content": (r.get("content") or "")[:600]}
            for r in json.loads(raw).get("results", [])]


# ---------------------------------------------------------------- GitHub


def repo_urls(text: str) -> list[str]:
    out = []
    for o, r in GH.findall(text or ""):
        r = re.sub(r"\.git$|[.)\],;:]+$", "", r)
        u = f"https://github.com/{o}/{r}"
        if o.lower() not in ("features", "topics", "orgs", "about") and u not in out:
            out.append(u)
    return out[:8]


def github_repo(url: str) -> dict:
    m = GH.search(url or "")
    if not m:
        return {"exists": False}
    o, r = m.group(1), re.sub(r"\.git$|[.)\],;:]+$", "", m.group(2))
    tok = open("/root/.gh_token").read().strip() if os.path.exists("/root/.gh_token") else ""
    hdr = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {tok}"} if tok else {})}
    try:
        raw, _, _ = _get(f"https://api.github.com/repos/{o}/{r}", "github", 0.5, timeout=20, tries=2, headers=hdr)
    except wt.WebError:
        return {"exists": False, "url": f"https://github.com/{o}/{r}"}
    d = json.loads(raw)
    return {"exists": True, "url": d.get("html_url"), "license": (d.get("license") or {}).get("spdx_id"),
            "last_push": (d.get("pushed_at") or "")[:10], "stars": d.get("stargazers_count", 0),
            "archived": d.get("archived", False)}
