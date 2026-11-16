"""Harvest arXiv metadata (id, title, abstract, categories, dates) over OAI-PMH.

Why: from a Kaggle IP the arXiv search API, OpenAlex and Semantic Scholar all
answer 429, but export.arxiv.org/oai2 serves ~1300 records per page in ~5 s.
A local copy of every abstract lets the System-1 triage search all of arXiv
with embeddings, no search API needed.

Resumable: the resumption token is saved after every page, so a restart
continues where it stopped. Output: <out>/<set>/page_00001.jsonl ...

    python -m sabha.research.harvest --out /kaggle/working/arxiv_meta --set cs --range 2025-01-01:
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

OAI = "https://export.arxiv.org/oai2"
UA = {"User-Agent": "sabha-research/0.1 (literature harvester)"}
NS = {"o": "http://www.openarchives.org/OAI/2.0/", "a": "http://arxiv.org/OAI/arXiv/"}


def _fetch(params: dict, tries: int = 8) -> str:
    url = OAI + "?" + urllib.parse.urlencode(params)
    wait = 10
    for _ in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if e.headers else None
            wait = int(ra) if (ra and ra.isdigit()) else min(wait * 2, 300)
            print(f"[harvest] HTTP {e.code}, retry in {wait}s", flush=True)
        except Exception as e:  # network blip
            print(f"[harvest] {type(e).__name__}: {str(e)[:80]}, retry in {wait}s", flush=True)
            wait = min(wait * 2, 300)
        time.sleep(wait)
    raise RuntimeError("OAI fetch failed after retries")


def _text(el, path):
    x = el.find(path, NS)
    return " ".join((x.text or "").split()) if x is not None else ""


def parse_page(xml: str):
    root = ET.fromstring(xml)
    recs = []
    for rec in root.iter("{%s}record" % NS["o"]):
        meta = rec.find("o:metadata/a:arXiv", NS)
        if meta is None:
            continue
        authors = []
        for au in meta.findall("a:authors/a:author", NS)[:4]:
            authors.append((_text(au, "a:forenames") + " " + _text(au, "a:keyname")).strip())
        recs.append({
            "id": _text(meta, "a:id"),
            "title": _text(meta, "a:title"),
            "abstract": _text(meta, "a:abstract"),
            "cats": _text(meta, "a:categories").split(),
            "created": _text(meta, "a:created"),
            "updated": _text(meta, "a:updated"),
            "authors": authors,
        })
    tok = root.find(".//o:resumptionToken", NS)
    token = (tok.text or "").strip() if tok is not None else ""
    total = tok.get("completeListSize") if tok is not None else None
    return recs, token, total


def harvest(out: str, oai_set: str, since: str, until: str = "", pause: float = 3.0) -> None:
    d = os.path.join(out, f"{oai_set}_{since}_{until or 'now'}")
    os.makedirs(d, exist_ok=True)
    state_p = os.path.join(d, "_state.json")
    state = json.load(open(state_p)) if os.path.exists(state_p) else {"page": 0, "token": None, "n": 0}
    if state.get("done"):
        print(f"[harvest] {oai_set} already complete ({state['n']} records)", flush=True)
        return
    while True:
        if state["token"]:
            params = {"verb": "ListRecords", "resumptionToken": state["token"]}
        else:
            params = {"verb": "ListRecords", "metadataPrefix": "arXiv", "set": oai_set, "from": since}
            if until:
                params["until"] = until
        recs, token, total = parse_page(_fetch(params))
        state["page"] += 1
        tmp = os.path.join(d, f"page_{state['page']:05d}.jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, tmp[:-4])
        state["n"] += len(recs)
        state["token"] = token or None
        state["total"] = total
        state["done"] = not token
        json.dump(state, open(state_p, "w"))
        print(f"[harvest] {oai_set} page {state['page']} +{len(recs)} = {state['n']}/{total}", flush=True)
        if not token:
            print(f"HARVEST_DONE {oai_set} {state['n']}", flush=True)
            return
        time.sleep(pause)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--set", dest="sets", action="append", default=None,
                    help="OAI set, e.g. cs, stat, eess, math (repeatable)")
    ap.add_argument("--range", dest="ranges", action="append", default=None,
                    help="FROM:UNTIL datestamp window (UNTIL may be empty), newest first; repeatable")
    a = ap.parse_args()
    # newest windows first, so the most recent papers are searchable soonest
    for rng in a.ranges or ["2025-01-01:", "2023-01-01:2024-12-31", "2015-01-01:2022-12-31"]:
        since, until = rng.split(":")
        for s in a.sets or ["cs"]:
            harvest(a.out, s, since, until)
