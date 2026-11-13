"""Web tools for agents and for the council: search, paper search, page reading.

Standard library only, so one file works both as a module inside the hub and
as command line tools the agents call from their shell:

    websearch "query"                         web results: title, link, snippet
    papers "query" [--new]                    arXiv papers: title, authors, date, abstract
    webread URL [--offset N] [--find WORD]    a page or PDF as plain text, one page at a time

No API keys. Search tries DuckDuckGo, then Bing, then Wikipedia. Pages are cut
into windows because the models that call this have small context windows.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "sabha_webcache")
CACHE_TTL_S = 6 * 3600
ARXIV_ID = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(v\d+)?(?!\d)")
ATOM = {"a": "http://www.w3.org/2005/Atom"}
DROP_SECTION = re.compile(r"^\W*(references|bibliography|acknowledge?ments?|acknowledge?ment)\b", re.I)


class WebError(Exception):
    pass


def fetch(url: str, timeout: float = 25, max_bytes: int = 12_000_000,
          data: bytes | None = None, headers: dict | None = None) -> tuple[bytes, str, str]:
    """GET (or POST) a URL. Returns (body, content type, final url)."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "en", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes), r.headers.get("Content-Type", ""), r.geturl()
    except urllib.error.HTTPError as e:
        raise WebError(f"{url} answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise WebError(f"could not reach {url}: {getattr(e, 'reason', e)}") from None


# ---------------------------------------------------------------- html -> text


class _Text(HTMLParser):
    """Readable text from HTML. Headings become markdown '#' lines, math becomes its LaTeX."""

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "form", "button", "iframe",
            "template", "select", "annotation", "annotation-xml"}
    BLOCK = {"p", "div", "section", "article", "tr", "br", "ul", "ol", "table", "blockquote",
             "pre", "figure", "figcaption", "dt", "dd", "header", "main"}
    SKIP_CLASS = frozenset(("ltx_bibliography", "ltx_page_footer", "ltx_page_navbar", "ltx_role_footnote",
                            "ltx_authors", "ltx_dates", "package-alerts", "ltx_pagination"))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self._in_title = False
        self._skip_tag: str | None = None
        self._depth = 0
        self._close: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._depth += 1
            return
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "math":
            if a.get("alttext"):
                self.out.append(f" ${html.unescape(a['alttext'])}$ ")
            self._skip_tag, self._depth = tag, 1
            return
        if tag in self.SKIP or not self.SKIP_CLASS.isdisjoint(cls.split()):
            self._skip_tag, self._depth = tag, 1
            return
        if tag == "title":
            self._in_title = True
        elif "ltx_title_abstract" in cls:
            self.out.append("\n\n## ")
            self._close.append((tag, "\n"))
        elif tag in ("h1", "h2", "h3", "h4"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._depth -= 1
                if self._depth == 0:
                    self._skip_tag = None
            return
        if tag == "title":
            self._in_title = False
        if self._close and self._close[-1][0] == tag:
            self.out.append(self._close.pop()[1])
        elif tag in ("h1", "h2", "h3", "h4", "p", "tr", "table", "section", "div", "li"):
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip_tag:
            return
        if self._in_title:
            self.title += data
        else:
            self.out.append(data)


def tidy(text: str) -> str:
    text = text.replace("\r", "").replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t\f]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"(\n#+) *\n+", r"\1 ", text)          # heading text on the heading's own line
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(page: str) -> tuple[str, str]:
    p = _Text()
    try:
        p.feed(page)
        p.close()
    except Exception:  # a broken page still gives what was parsed so far
        pass
    return tidy(p.title), tidy("".join(p.out))


def pdf_to_text(data: bytes) -> str:
    """PDF bytes to text with whatever the box has: pdftotext, PyMuPDF or pypdf. '' if none."""
    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if shutil.which("pdftotext"):
            r = subprocess.run(["pdftotext", "-q", path, "-"], capture_output=True, timeout=120)
            if r.returncode == 0 and len(r.stdout) > 200:
                return r.stdout.decode("utf-8", "replace")
        try:
            import fitz  # type: ignore
            with fitz.open(path) as doc:
                return "\n".join(page.get_text() for page in doc)
        except Exception:
            pass
        try:
            from pypdf import PdfReader  # type: ignore
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages)
        except Exception:
            pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return ""


# ---------------------------------------------------------------- pages


def _cache_path(url: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")


def page_text(url: str, use_cache: bool = True) -> tuple[str, str]:
    """(title, text) for a web page or PDF. arXiv links go to the paper's HTML version when it exists."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    path = _cache_path(url)
    if use_cache:
        try:
            if time.time() - os.path.getmtime(path) < CACHE_TTL_S:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                return d["title"], d["text"]
        except (OSError, ValueError, KeyError):
            pass
    title, text = _page_text(url)
    if text:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"title": title, "text": text}, f)
        except OSError:
            pass
    return title, text


def _page_text(url: str) -> tuple[str, str]:
    m = ARXIV_ID.search(url) if "arxiv.org" in url else None
    if m:
        try:
            raw, ctype, _ = fetch(f"https://arxiv.org/html/{m.group(1)}{m.group(2) or ''}")
            title, text = html_to_text(raw.decode("utf-8", "replace"))
            if len(text) > 3000:
                return title, text
        except WebError:
            pass
        url = f"https://arxiv.org/pdf/{m.group(1)}{m.group(2) or ''}"
    raw, ctype, final = fetch(url)
    if raw[:5] == b"%PDF-" or "pdf" in ctype.lower():
        text = tidy(pdf_to_text(raw))
        if len(text) < 200:  # no PDF reader on this box: ask the Jina reader for the text
            raw2, _, _ = fetch("https://r.jina.ai/" + final, timeout=60)
            text = tidy(raw2.decode("utf-8", "replace"))
        return os.path.basename(urllib.parse.urlparse(final).path), text
    if "html" in ctype.lower() or raw.lstrip()[:1] == b"<":
        return html_to_text(raw.decode("utf-8", "replace"))
    return "", tidy(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------- search


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(html.unescape(re.sub(r"<[^>]+>", "", s)))).strip()


def _ddg(query: str, n: int) -> list[dict]:
    raw, _, _ = fetch("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}))
    page = raw.decode("utf-8", "replace")
    out = []
    parts = re.split(r'(?=<a\b[^>]*class="[^"]*result__a)', page)[1:]
    for part in parts:
        m = re.match(r'<a\b([^>]*)>(.*?)</a>', part, re.S)
        href = re.search(r'href="([^"]+)"', m.group(1)) if m else None
        if not href:
            continue
        url = html.unescape(href.group(1))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if q.get("uddg"):
            url = q["uddg"][0]
        if url.startswith("//"):
            url = "https:" + url
        if "duckduckgo.com/y.js" in url:
            continue  # an ad
        sn = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', part, re.S)
        out.append({"title": _strip(m.group(2)), "url": url, "snippet": _strip(sn.group(1)) if sn else ""})
    return out[:n]


def _bing(query: str, n: int) -> list[dict]:
    raw, _, _ = fetch("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en"}))
    page = raw.decode("utf-8", "replace")
    out = []
    for part in re.split(r'(?=<li class="b_algo")', page)[1:]:
        m = re.search(r'<h2[^>]*>\s*<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', part, re.S)
        if not m:
            continue
        url = html.unescape(m.group(1))
        u = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("u", [""])[0]
        if "bing.com/ck/" in url and u.startswith("a1"):
            try:
                url = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode("utf-8", "replace")
            except ValueError:
                pass
        sn = re.search(r'<p\b[^>]*>(.*?)</p>', part, re.S)
        out.append({"title": _strip(m.group(2)), "url": url, "snippet": _strip(sn.group(1)) if sn else ""})
    return out[:n]


def _wikipedia(query: str, n: int) -> list[dict]:
    raw, _, _ = fetch("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": n}))
    hits = json.loads(raw).get("query", {}).get("search", [])
    return [{"title": h["title"] + " (Wikipedia)", "snippet": _strip(h.get("snippet", "")),
             "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(h["title"].replace(" ", "_"))}
            for h in hits][:n]


# Search with a key. Free engines refuse or mislead cloud IPs (see README), so when one of these
# environment variables is set, that service is asked first and the free engines are the fallback.


def _tavily(query: str, n: int, key: str) -> list[dict]:
    raw, _, _ = fetch("https://api.tavily.com/search", timeout=30,
                      data=json.dumps({"query": query, "max_results": n, "search_depth": "basic"}).encode(),
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return [{"title": r.get("title") or "", "url": r.get("url") or "", "snippet": _strip(r.get("content") or "")}
            for r in json.loads(raw).get("results", []) if r.get("url")]


def _brave(query: str, n: int, key: str) -> list[dict]:
    raw, _, _ = fetch("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": n}),
                      headers={"X-Subscription-Token": key, "Accept": "application/json"})
    return [{"title": _strip(r.get("title") or ""), "url": r.get("url") or "", "snippet": _strip(r.get("description") or "")}
            for r in (json.loads(raw).get("web") or {}).get("results", []) if r.get("url")]


def _serper(query: str, n: int, key: str) -> list[dict]:
    raw, _, _ = fetch("https://google.serper.dev/search", data=json.dumps({"q": query, "num": n}).encode(),
                      headers={"X-API-KEY": key, "Content-Type": "application/json"})
    return [{"title": r.get("title") or "", "url": r.get("link") or "", "snippet": r.get("snippet") or ""}
            for r in json.loads(raw).get("organic", []) if r.get("link")]


KEYED = (("tavily", "TAVILY_API_KEY", _tavily), ("brave", "BRAVE_API_KEY", _brave), ("serper", "SERPER_API_KEY", _serper))


def keyed_engine() -> tuple[str, str, object] | None:
    """(name, key, function) of the first search service whose key is in the environment, or None."""
    for name, var, fn in KEYED:
        key = os.environ.get(var, "").strip()
        if key:
            return name, key, fn
    return None


_STOP = frozenset("""a an the of in on at to for with by from and or not no nor but if so as than then into about
between vs versus over under is are was were be been being do does did has have had can could should would will
shall may might must how what why when where which who whom whose it its this that these those their there here
really truly actually just only also paper papers arxiv please find search""".split())


def keywords(query: str, limit: int = 8) -> list[str]:
    """The content words of a query, in order. Models tend to write a whole question; engines want keywords."""
    out: list[str] = []
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", query):
        w = w.strip(".-_").lower()
        if len(w) > 1 and w not in _STOP and w not in out:
            out.append(w)
    return out[:limit]


def relevant(hits: list[dict], words: list[str]) -> list[dict]:
    """Keep results that mention at least two of the query's keywords (one, for a one-word query).

    Numbers do not count: a year in the query matches the date on any page.
    """
    words = [w for w in words if not w.isdigit()] or words
    need = min(2, len(words))
    keep = []
    for h in hits:
        text = (h.get("title", "") + " " + h.get("snippet", "") + " " + h.get("url", "")).lower()
        if sum(w in text for w in words) >= need:
            keep.append(h)
    return keep


def search(query: str, n: int = 6, strict: bool = False, wiki: bool = True) -> tuple[list[dict], str]:
    """Web search without a key. Returns (results, engine name). Results: title, url, snippet.

    With strict=True a result must mention the query's keywords, so an engine that
    answers with unrelated pages is skipped and the next one is tried.
    """
    errors = []
    words = keywords(query, 12) if strict else []
    keyed = keyed_engine()
    if keyed:  # a paid index answers the query itself, so its results are not filtered
        name, key, fn = keyed
        try:
            hits = fn(query, n, key)[:n]
            if hits:
                return hits, name
            errors.append(f"{name}: no results")
        except (WebError, ValueError, KeyError, TypeError, AttributeError) as e:
            errors.append(f"{name}: {e}")
    engines = (("duckduckgo", _ddg), ("bing", _bing), ("wikipedia", _wikipedia))
    for name, fn in engines if wiki else engines[:2]:
        try:
            hits = fn(query, n + 4 if strict else n)
            if strict:
                hits = relevant(hits, words)[:n]
            if hits:
                return hits, name
            errors.append(f"{name}: nothing relevant")
        except (WebError, ValueError, KeyError) as e:
            errors.append(f"{name}: {e}")
    raise WebError("search failed. " + "; ".join(errors))


def lookup(query: str, n: int = 4) -> tuple[list[dict], str, str]:
    """A search for the council: keywords only, relevant results only, arXiv when the web has nothing.

    Returns (results, engine, the query that was really sent).
    """
    words = keywords(query)
    q = " ".join(words) or query.strip()
    hits: list[dict] = []
    engine = ""
    try:
        hits, engine = search(q, n, strict=True, wiki=False)   # Wikipedia matches too loosely to settle a research point
    except WebError:
        pass
    if len(hits) < 2:
        for k in (4, 2):
            try:
                found = papers(" ".join(words[:k]), n) if words else []
            except WebError:
                break
            if found:
                hits = (hits + [{"title": f"{p['title']} ({p['date'][:4]})", "url": p["url"],
                                 "snippet": p["abstract"][:300]} for p in found])[:n]
                engine = f"{engine}+arxiv" if engine else "arxiv"
                break
    return hits, engine, q


def _entry(e) -> dict:
    def t(tag):
        return re.sub(r"\s+", " ", (e.findtext(f"a:{tag}", "", ATOM) or "")).strip()
    return {"title": t("title"), "url": t("id").replace("http://", "https://"), "date": t("published")[:10],
            "authors": [re.sub(r"\s+", " ", a.findtext("a:name", "", ATOM)).strip() for a in e.findall("a:author", ATOM)],
            "abstract": t("summary")}


def _arxiv(params: dict) -> list[dict]:
    raw, _, _ = fetch("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params))
    try:
        return [_entry(e) for e in ET.fromstring(raw).findall("a:entry", ATOM)]
    except ET.ParseError:
        raise WebError("arXiv sent an answer that could not be read") from None


def papers(query: str, n: int = 6, newest: bool = False) -> list[dict]:
    """arXiv search. Every word (or "quoted phrase") must appear."""
    words = re.findall(r'"[^"]+"|\S+', query)
    q = " AND ".join(f"all:{w}" for w in words) or "all:" + query
    return _arxiv({"search_query": q, "start": 0, "max_results": n,
                   "sortBy": "submittedDate" if newest else "relevance", "sortOrder": "descending"})


# ---------------------------------------------------------------- a paper, cut into sections

_NAMED = (r"Abstract|Introduction|Related Works?|Background|Preliminaries|Methods?|Methodology|Approach|"
          r"Experiments?|Experimental Setup|Evaluation|Results|Analysis|Discussion|Limitations|"
          r"Conclusions?|References|Acknowledge?ments?|Appendix[^\n]{0,40}")
_HEAD = re.compile(rf"^(?:(\d{{1,2}})\.? +([A-Z][^\n]{{2,70}}[^\n.,;:])|({_NAMED}))$")


def _split_marked(text: str) -> tuple[str, list[dict]]:
    """Split on '## ' lines. Returns (text before the first heading, sections)."""
    pre: list[str] = []
    sections: list[dict] = []
    buf = pre
    for line in text.split("\n"):
        m = re.match(r"^## +(.+)", line)
        if m:
            sections.append({"title": m.group(1).strip(), "lines": []})
            buf = sections[-1]["lines"]
        else:
            buf.append(line)
    return "\n".join(pre).strip(), [{"title": s["title"], "text": "\n".join(s["lines"]).strip()} for s in sections]


def _split_plain(text: str) -> tuple[str, list[dict]]:
    """Find headings in plain text (from a PDF): numbered lines that count up, or well-known names."""
    marked, last = [], 0
    for line in text.split("\n"):
        s = line.strip()
        m = _HEAD.match(s) if len(s) < 80 else None
        if m and m.group(1):
            num = int(m.group(1))
            if num == last + 1:
                last = num
                marked.append("## " + s)
                continue
        elif m:
            marked.append("## " + s)
            continue
        marked.append(line)
    return _split_marked("\n".join(marked))


def _chunks(text: str, size: int) -> list[str]:
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if cur and len(cur) + len(para) > size:
            out.append(cur.strip())
            cur = ""
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        cur += para + "\n\n"
    if cur.strip():
        out.append(cur.strip())
    return out


def paper(source: str = "", text: str = "", max_section: int = 14000) -> dict:
    """A paper as { title, authors, url, abstract, sections: [{title, text}] }.

    `source` is an arXiv id or link, or any page or PDF link. `text` is pasted
    paper text and wins over `source`. References and acknowledgements are
    dropped; a section longer than max_section is cut into parts.
    """
    source = (source or "").strip()
    meta = {"title": "", "authors": [], "url": "", "abstract": ""}
    if text.strip():
        body = tidy(text)
        meta["url"] = source if re.match(r"^https?://", source) else ""
    else:
        if not source:
            raise WebError("give an arXiv id, a link, or the paper text")
        m = ARXIV_ID.search(source)
        is_arxiv = bool(m) and ("arxiv" in source.lower() or ARXIV_ID.fullmatch(source) is not None)
        if is_arxiv:
            aid = m.group(1) + (m.group(2) or "")
            url = f"https://arxiv.org/abs/{aid}"
            try:
                hits = _arxiv({"id_list": aid})
                if hits and hits[0]["title"]:
                    meta.update(title=hits[0]["title"], authors=hits[0]["authors"], abstract=hits[0]["abstract"])
            except WebError:
                pass
        else:
            url = source if re.match(r"^https?://", source) else "https://" + source
        meta["url"] = url
        page_title, body = page_text(url)
        meta["title"] = meta["title"] or page_title
    if len(body) < 400:
        raise WebError("the paper text came back almost empty")

    pre, sections = _split_marked(body) if "\n## " in "\n" + body else _split_plain(body)
    if len(sections) < 2:
        pre, sections = "", [{"title": f"Part {i + 1}", "text": c} for i, c in enumerate(_chunks(body, 9000))]
    kept = []
    for s in sections:
        if DROP_SECTION.match(s["title"]):
            continue
        if s["title"].lower().strip(" .:") == "abstract":
            meta["abstract"] = meta["abstract"] or s["text"]
            continue
        if len(s["text"]) < 40:
            continue
        if len(s["text"]) > max_section:
            parts = _chunks(s["text"], max_section)
            kept += [{"title": f"{s['title']} ({i + 1}/{len(parts)})", "text": c} for i, c in enumerate(parts)]
        else:
            kept.append(s)
    if not kept:
        raise WebError("no readable sections were found in the paper")
    if not meta["title"]:
        first = next((ln.strip("# ").strip() for ln in (pre or body).split("\n") if ln.strip()), "")
        meta["title"] = first[:160] or "Untitled paper"
    if not meta["abstract"] and pre:
        meta["abstract"] = pre[-1800:]
    meta["sections"] = kept
    return meta


# ---------------------------------------------------------------- command line


def _window(text: str, offset: int, size: int) -> str:
    end = min(len(text), offset + size)
    if end < len(text):  # end on a paragraph or sentence when one is near
        cut = max(text.rfind("\n", offset, end), text.rfind(". ", offset, end) + 1)
        if cut > offset + size // 2:
            end = cut
    more = f"more: --offset {end}" if end < len(text) else "end of page"
    return f"[chars {offset}-{end} of {len(text)}; {more}]\n{text[offset:end].strip()}"


def _find(text: str, word: str, limit: int = 6) -> str:
    out = []
    for m in re.finditer(re.escape(word), text, re.I):
        a, b = max(0, m.start() - 280), min(len(text), m.end() + 420)
        if out and a < out[-1][1]:
            continue
        out.append((a, b))
        if len(out) >= limit:
            break
    if not out:
        return f"'{word}' does not appear on this page."
    return "\n\n".join(f"[at char {a}]\n...{text[a:b].strip()}..." for a, b in out)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="sabha.webtools", description="Web tools for agents. No API keys.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="web search")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", type=int, default=6)
    p = sub.add_parser("papers", help="arXiv search")
    p.add_argument("query", nargs="+")
    p.add_argument("-n", type=int, default=6)
    p.add_argument("--new", action="store_true", help="newest first")
    r = sub.add_parser("read", help="read a page or PDF as text")
    r.add_argument("url")
    r.add_argument("--offset", type=int, default=0)
    r.add_argument("--chars", type=int, default=6000)
    r.add_argument("--find", default="", help="show only the places where this word appears")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "search":
            query, n = " ".join(args.query), max(1, min(args.n, 10))
            try:
                hits, engine = search(query, n, strict=True)
            except WebError:  # engines refused or sent unrelated pages: say so, and offer arXiv instead of junk
                found = papers(" ".join(keywords(query, 4)), n)
                print("No relevant web results (the search engines refused or sent unrelated pages).")
                if not found:
                    print("arXiv has nothing for these keywords either. Try fewer or different keywords.")
                    return 0
                hits, engine = [{"title": f"{p['title']} ({p['date'][:4]})", "url": p["url"],
                                 "snippet": p["abstract"]} for p in found], "arXiv instead"
            print(f"{len(hits)} results ({engine})")
            for i, h in enumerate(hits, 1):
                print(f"\n{i}. {h['title']}\n   {h['url']}\n   {h['snippet'][:300]}")
            print("\nRead one with: webread URL")
        elif args.cmd == "papers":
            hits = papers(" ".join(args.query), max(1, min(args.n, 10)), args.new)
            print(f"{len(hits)} papers (arXiv)")
            for i, h in enumerate(hits, 1):
                who = ", ".join(h["authors"][:3]) + (" et al." if len(h["authors"]) > 3 else "")
                print(f"\n{i}. {h['title']}\n   {who} | {h['date']} | {h['url']}\n   {h['abstract'][:420]}")
            print("\nRead one with: webread URL")
        else:
            title, text = page_text(args.url)
            if title:
                print(f"# {title}")
            print(_find(text, args.find) if args.find
                  else _window(text, max(0, min(args.offset, len(text))), max(500, min(args.chars, 20000))))
    except WebError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
