"""Local embedding index over the harvested arXiv abstracts: System-1 search.

Builder (a long-running process, one GPU, a little memory):
    python -m sabha.research.index build --device cuda:0
embeds every new arxiv_meta/**/page_*.jsonl with bge-small (title + abstract,
256 tokens, CLS, L2-normalised) into arxiv_index/<shard>.npy + <shard>.jsonl.
The .npy is written last, so a shard counts only once it is complete.

Searcher (inside the daemon, CPU): Index().refresh() loads new shards,
.query(text, k) returns the nearest papers, .by_title(title) finds an id.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import threading
import time

import numpy as np

META = os.environ.get("SABHA_ARXIV_META", "/kaggle/working/arxiv_meta")
IDX = os.environ.get("SABHA_ARXIV_INDEX", "/kaggle/working/arxiv_index")
MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def load_model(device: str):
    import torch
    from transformers import AutoModel, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(MODEL)
    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    model = AutoModel.from_pretrained(MODEL, torch_dtype=dtype).to(device).eval()
    return tok, model


def embed(tok, model, texts: list[str], device: str, bs: int = 256, max_len: int = 256) -> np.ndarray:
    import torch
    out = []
    with torch.inference_mode():
        for i in range(0, len(texts), bs):
            b = tok(texts[i:i + bs], padding=True, truncation=True, max_length=max_len, return_tensors="pt").to(device)
            h = model(**b).last_hidden_state[:, 0].float()
            out.append(torch.nn.functional.normalize(h, dim=-1).cpu().numpy().astype(np.float16))
    return np.concatenate(out) if out else np.zeros((0, 384), np.float16)


def build(device: str = "cuda:0", once: bool = False) -> None:
    os.makedirs(IDX, exist_ok=True)
    tok, model = load_model(device)
    print(f"[index] model on {device}", flush=True)
    while True:
        new = 0
        for p in sorted(glob.glob(f"{META}/**/page_*.jsonl", recursive=True)):
            key = os.path.relpath(p, META).replace(os.sep, "__")[:-len(".jsonl")]
            if os.path.exists(f"{IDX}/{key}.npy"):
                continue
            recs = []
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        pass
            t0 = time.time()
            vec = embed(tok, model, [f"{r['title']}. {r['abstract']}" for r in recs], device)
            with open(f"{IDX}/{key}.jsonl.tmp", "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps({"id": r["id"], "title": r["title"], "abstract": r["abstract"][:1500],
                                        "cats": r.get("cats", []), "created": r.get("created", "")},
                                       ensure_ascii=False) + "\n")
            os.replace(f"{IDX}/{key}.jsonl.tmp", f"{IDX}/{key}.jsonl")
            np.save(f"{IDX}/{key}.tmp.npy", vec)
            os.replace(f"{IDX}/{key}.tmp.npy", f"{IDX}/{key}.npy")
            new += 1
            dt = time.time() - t0
            print(f"[index] {key} {len(recs)} rows {dt:.1f}s", flush=True)
            # share the GPU with the vLLM server on it: work at most 1/(1+SLEEP) of the time
            time.sleep(dt * float(os.environ.get("SABHA_INDEX_SLEEP", "3")))
        if once:
            return
        time.sleep(1 if new else 60)


class Index:
    """In-memory float32 matrix of every complete shard, deduplicated by arXiv id."""

    def __init__(self, device: str = "cpu"):
        self.device = device
        self.vecs = np.zeros((0, 384), np.float32)
        self.meta: list[dict] = []
        self.pos: dict[str, int] = {}
        self.titles: dict[str, str] = {}
        self.loaded: set[str] = set()
        self.lock = threading.Lock()
        self._model = None
        self.last_refresh = 0.0

    def __len__(self) -> int:
        return len(self.meta)

    def refresh(self) -> int:
        add_v, add_m = [], []
        seen = set(self.pos)
        for npy in sorted(glob.glob(f"{IDX}/*.npy")):
            key = os.path.basename(npy)[:-4]
            if key in self.loaded or key.endswith(".tmp"):
                continue
            try:
                v = np.load(npy)
                rows = [json.loads(line) for line in open(f"{IDX}/{key}.jsonl", encoding="utf-8")]
            except (OSError, ValueError):
                continue
            if len(rows) != len(v):
                continue
            keep = []
            for i, r in enumerate(rows):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    keep.append(i)
            add_v.append(v[keep].astype(np.float32))
            add_m.extend(rows[i] for i in keep)
            self.loaded.add(key)
        if add_m:
            with self.lock:
                base = len(self.meta)
                self.vecs = np.concatenate([self.vecs] + add_v)
                self.meta.extend(add_m)
                for j, r in enumerate(add_m):
                    self.pos[r["id"]] = base + j
                    self.titles.setdefault(norm_title(r["title"]), r["id"])
        self.last_refresh = time.time()
        return len(add_m)

    def _qvec(self, text: str) -> np.ndarray:
        if self._model is None:
            self._model = load_model(self.device)
        return embed(*self._model, [QUERY_PREFIX + text], self.device)[0].astype(np.float32)

    def vec_of(self, text: str) -> np.ndarray:
        """Document-side embedding (no query prefix), for comparing abstracts to each other."""
        if self._model is None:
            self._model = load_model(self.device)
        return embed(*self._model, [text], self.device)[0].astype(np.float32)

    def query(self, text: str, k: int = 40, exclude: set | None = None) -> list[dict]:
        with self.lock:
            vecs, meta = self.vecs, self.meta
        if not meta:
            return []
        s = vecs @ self._qvec(text)
        top = np.argpartition(-s, min(k * 3, len(s) - 1))[:k * 3]
        out = []
        for i in top[np.argsort(-s[top])]:
            r = meta[i]
            if exclude and r["id"] in exclude:
                continue
            out.append({**r, "cos": float(s[i])})
            if len(out) >= k:
                break
        return out

    def cos(self, text: str, aid: str) -> float:
        i = self.pos.get(aid)
        return float(self.vecs[i] @ self._qvec(text)) if i is not None else 0.0

    def get(self, aid: str) -> dict | None:
        i = self.pos.get(re.sub(r"v\d+$", "", aid))
        return self.meta[i] if i is not None else None

    def by_title(self, title: str) -> str | None:
        return self.titles.get(norm_title(title))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        dev = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "cuda:0"
        build(dev, once="--once" in sys.argv)
    else:
        print(__doc__)
