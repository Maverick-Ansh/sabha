"""Which models this box can serve, and the switch that serves them (PROTOCOL.md section 11).

The app's Models panel shows the GPUs, a curated catalog with a fit verdict per model for THIS hardware
(fits / tight / too big / GPU too old), live Hugging Face search for anything else, and presets per box.
"Use" writes <data>/run/layout.json; the box supervisor (sabha/box.py) sees the change within seconds,
stops the old server for that role, downloads the new weights, starts vLLM and re-registers the model.
The research agents and the crew reach models by role (deep, reader), so nothing else changes.

Fit, per server: weights / TP + the KV cache of one full-length request + overhead <= GPU memory * share.
Every number here is an estimate from file sizes and config.json; vLLM has the last word (the boot strip
shows its log line if it refuses).

Measured repo sizes (Hugging Face API from a Kaggle box, 2026-11-17) are in CATALOG. Search results get
their size from the safetensors metadata (element counts per dtype) and their KV size from config.json.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request

from .util import ApiError

HF_API = "https://huggingface.co/api/"
GIB = 2 ** 30
UA = {"User-Agent": "sabha-box/1"}
PORTS = {"deep": 8001, "reader": 8002}
DTYPE_BYTES = {"F64": 8, "I64": 8, "F32": 4, "I32": 4, "U32": 4, "BF16": 2, "F16": 2, "I16": 2, "U16": 2,
               "F8_E4M3": 1, "F8_E5M2": 1, "I8": 1, "U8": 1, "BOOL": 1}
BAD_REPO = re.compile(r"gguf|mlx|exl2|exl3|nvfp4|-onnx|openvino|tiny-random|\bbnb\b|bnb-4bit|-4bit-mlx", re.I)

# family -> serving flags. Parsers only where they are known to work (an unknown parser name stops vLLM).
FAMILIES = {
    "qwen3.5": {"tool_parser": "qwen3_xml", "reasoning_parser": "qwen3", "mm": True, "no_think": True},
    "gemma4": {"tool_parser": "", "reasoning_parser": "", "mm": True, "no_think": False},
    "llama3": {"tool_parser": "llama3_json", "reasoning_parser": "", "mm": False, "no_think": False},
    "mistral": {"tool_parser": "mistral", "reasoning_parser": "", "mm": False, "no_think": False},
    "gpt-oss": {"tool_parser": "", "reasoning_parser": "", "mm": False, "no_think": False},
    "glm": {"tool_parser": "", "reasoning_parser": "", "mm": False, "no_think": False},
    "other": {"tool_parser": "hermes", "reasoning_parser": "", "mm": False, "no_think": False},
}

# id, label, family, weights GiB (files), KV KiB per token (fp16), min compute capability x10, max context, note
CATALOG = [
    ("Qwen/Qwen3.5-0.8B", "Qwen3.5 0.8B", "qwen3.5", 1.6, 24, 75, 32768, "tiny and fast; too weak for agent work"),
    ("Qwen/Qwen3.5-2B", "Qwen3.5 2B", "qwen3.5", 4.2, 24, 75, 32768, "fast triage; weak as an agent"),
    ("Qwen/Qwen3.5-4B", "Qwen3.5 4B", "qwen3.5", 8.7, 32, 75, 32768, "the default reader: reads papers fast"),
    ("cyankiwi/Qwen3.5-9B-AWQ-4bit", "Qwen3.5 9B (4-bit)", "qwen3.5", 8.4, 32, 75, 32768, "the default deep model on a T4"),
    ("Qwen/Qwen3.5-9B", "Qwen3.5 9B", "qwen3.5", 18.0, 32, 75, 32768, "full precision 9B: better than 4-bit, needs 20+ GB"),
    ("cyankiwi/Qwen3.5-27B-AWQ-4bit", "Qwen3.5 27B (4-bit)", "qwen3.5", 18.7, 64, 75, 32768,
     "much stronger agent; on 2x T4 it takes both GPUs"),
    ("Qwen/Qwen3.5-27B-GPTQ-Int4", "Qwen3.5 27B (GPTQ 4-bit)", "qwen3.5", 28.2, 64, 80, 32768, "official 4-bit 27B"),
    ("Qwen/Qwen3.5-27B-FP8", "Qwen3.5 27B (FP8)", "qwen3.5", 28.7, 64, 80, 32768, "near full quality at half the memory"),
    ("Qwen/Qwen3.5-27B", "Qwen3.5 27B", "qwen3.5", 51.7, 64, 80, 32768, "full precision; scored 7/7 on our agent probes"),
    ("cyankiwi/Qwen3.5-35B-A3B-AWQ-4bit", "Qwen3.5 35B-A3B MoE (4-bit)", "qwen3.5", 22.8, 40, 75, 32768,
     "mixture of experts: 35B knowledge, 3B speed"),
    ("Qwen/Qwen3.5-35B-A3B", "Qwen3.5 35B-A3B MoE", "qwen3.5", 67.0, 40, 80, 32768, "full precision MoE, fast for its size"),
    ("Qwen/Qwen3.5-122B-A10B-GPTQ-Int4", "Qwen3.5 122B-A10B MoE (4-bit)", "qwen3.5", 73.4, 96, 80, 32768,
     "experimental: needs two 80 GB GPUs"),
    ("google/gemma-4-E2B-it", "Gemma 4 E2B", "gemma4", 9.5, 40, 75, 32768, "small Gemma"),
    ("google/gemma-4-E4B-it", "Gemma 4 E4B", "gemma4", 14.9, 48, 80, 32768, "mid Gemma"),
    ("google/gemma-4-12b-it", "Gemma 4 12B", "gemma4", 22.3, 96, 80, 32768, "strong agent in our probes (6/7)"),
    ("google/gemma-4-26B-A4B-it", "Gemma 4 26B-A4B MoE", "gemma4", 48.1, 96, 80, 32768, "MoE Gemma"),
    ("google/gemma-4-31B-it", "Gemma 4 31B", "gemma4", 58.3, 128, 80, 32768, "the biggest dense Gemma"),
    ("openai/gpt-oss-20b", "gpt-oss 20B", "gpt-oss", 13.8, 48, 80, 32768, "OpenAI open weights (MXFP4); Ampere or newer"),
    ("openai/gpt-oss-120b", "gpt-oss 120B", "gpt-oss", 65.3, 72, 80, 32768, "needs one 80 GB GPU"),
    ("cyankiwi/GLM-4.7-Flash-AWQ-4bit", "GLM-4.7 Flash (4-bit)", "glm", 18.8, 48, 80, 32768, "Zhipu MoE, 4-bit"),
    ("zai-org/GLM-4.7-Flash", "GLM-4.7 Flash", "glm", 58.2, 48, 80, 32768, "Zhipu MoE, full precision"),
    ("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16", "Nemotron 3 Nano 30B-A3B", "other", 58.8, 24, 80, 32768,
     "NVIDIA hybrid MoE; experimental here"),
    ("microsoft/phi-4", "Phi-4 14B", "other", 27.3, 200, 80, 16384, "Microsoft 14B"),
    ("stelterlab/Mistral-Small-24B-Instruct-2501-AWQ", "Mistral Small 24B (4-bit)", "mistral", 13.3, 160, 75, 32768,
     "Mistral 24B, 4-bit"),
    ("casperhansen/llama-3.3-70b-instruct-awq", "Llama 3.3 70B (4-bit)", "llama3", 37.0, 320, 80, 32768,
     "Meta 70B; needs 48+ GB (an A100 80 GB)"),
]


def family_of(hf: str, arch: str = "") -> str:
    s = f"{hf} {arch}".lower()
    if "qwen3.5" in s or "qwen3_5" in s or "qwen3next" in s:
        return "qwen3.5"
    if "gemma-4" in s or "gemma4" in s:
        return "gemma4"
    if "llama" in s:
        return "llama3"
    if "mistral" in s or "devstral" in s:
        return "mistral"
    if "gpt-oss" in s or "gptoss" in s:
        return "gpt-oss"
    if "glm" in s:
        return "glm"
    return "other"


def gpu_sm(name: str) -> int:
    """Compute capability x10 from the GPU's name (what vLLM kernels care about)."""
    n = name.upper()
    for key, sm in (("B200", 100), ("B100", 100), ("GB200", 100), ("BLACKWELL", 120), ("RTX 50", 120), ("H200", 90), ("H100", 90),
                    ("L40", 89), ("L4", 89), ("RTX 6000 ADA", 89), ("RTX 40", 89), ("A100", 80), ("A800", 80), ("A10", 86),
                    ("A40", 86), ("A6000", 86), ("RTX 30", 86), ("T4", 75), ("RTX 20", 75), ("V100", 70), ("P100", 60), ("P4", 61)):
        if key in n:
            return sm
    return 80


def entry(hf: str) -> dict | None:
    for row in CATALOG:
        if row[0].lower() == hf.lower():
            return _row(row)
    return None


def _row(row) -> dict:
    hf, label, fam, gib, kv, sm, ctx, note = row
    return {"hf": hf, "label": label, "family": fam, "weights_gib": gib, "kv_kib": kv, "min_sm": sm, "max_ctx": ctx,
            "note": note, "curated": True}


# ---------------------------------------------------------------- fit


def plan_for(m: dict, gpus: list[dict], share: float = 1.0, ctx_want: int = 32768) -> dict:
    """The best way to serve model m on these GPUs: one GPU, or tensor-parallel over all of them.
    share: the fraction of each GPU this server may use (two servers can split one big GPU)."""
    if not gpus:
        return {"fits": "no", "why": "no GPU on this box"}
    sm = min(gpu_sm(g["name"]) for g in gpus)
    if sm < m.get("min_sm", 75):
        return {"fits": "no", "why": f"needs a newer GPU than {gpus[0]['name']} (compute {m['min_sm'] / 10:.1f}+)"}
    best = None
    for tp in sorted({1, len(gpus)} | ({2} if len(gpus) >= 2 else set())):
        use = gpus[:tp]
        mem = min(g["mem_mb"] for g in use) / 1024
        util = round((0.90 if mem < 30 else 0.92) * share, 3)  # a T4 at 0.92 left vLLM no room for its warm-up
        w = m["weights_gib"] / tp
        overhead = 1.5 + 0.015 * w + (0.4 if tp > 1 else 0)  # activations + CUDA graphs, roughly flat in model size
        free = mem * util - w - overhead
        kv_tok = m.get("kv_kib", 64) / tp / (1024 * 1024)  # GiB per token on each GPU
        ctx = min(ctx_want, m.get("max_ctx", 32768))
        while ctx > 8192 and free < kv_tok * ctx * 1.5:
            ctx //= 2
        if free <= 0 or free < kv_tok * ctx * 1.05:
            verdict = "no"
        elif free >= kv_tok * ctx * 3 and ctx >= min(ctx_want, m.get("max_ctx", 32768)):
            verdict = "yes"
        else:
            verdict = "tight"
        p = {"fits": verdict, "tp": tp, "gpus": [g["index"] for g in use], "util": util, "ctx": int(ctx),
             "free_gib": round(free, 1), "per_gpu_gib": round(w, 1),
             "why": (f"{m['weights_gib']:.0f} GB of weights" + (f" over {tp} GPUs" if tp > 1 else "") +
                     f", {max(free, 0):.0f} GB left for the context on each GPU")}
        rank = {"yes": 2, "tight": 1, "no": 0}[verdict]
        if best is None or rank > best[0] or (rank == best[0] and rank > 0 and tp < best[1]["tp"]):
            best = (rank, p)
    if best[0] == 0:
        total = sum(g["mem_mb"] for g in gpus) / 1024
        best[1]["why"] = f"too big: {m['weights_gib']:.0f} GB of weights, this box has {total:.0f} GB of GPU memory"
    return best[1]


def presets(gpus: list[dict]) -> list[dict]:
    """Ready layouts for this hardware, best first. Each is a list of servers (role, model, plan)."""
    if not gpus:
        return []
    mem = min(g["mem_mb"] for g in gpus) / 1024
    n = len(gpus)
    sm = min(gpu_sm(g["name"]) for g in gpus)
    out = []

    def lay(title, why, servers):
        out.append({"id": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"), "title": title, "why": why, "servers": servers})

    if n >= 2 and mem < 30:  # Kaggle 2x T4 (or 2x L4)
        lay("Balanced (default)", "a deep model and a fast reader, one per GPU: the most papers per hour",
            [("deep", "cyankiwi/Qwen3.5-9B-AWQ-4bit", [gpus[0]["index"]], 1.0), ("reader", "Qwen/Qwen3.5-4B", [gpus[1]["index"]], 1.0)])
        lay("One strong model", "Qwen3.5 27B across both GPUs serves every role: better judgement, fewer papers per hour",
            [("deep", "cyankiwi/Qwen3.5-27B-AWQ-4bit", [g["index"] for g in gpus[:2]], 1.0)])
    elif mem >= 70:  # A100 80 GB / H100
        lay("Big (default)", "Qwen3.5 27B in full precision serves every role: 7/7 on our agent probes",
            [("deep", "Qwen/Qwen3.5-27B", [gpus[0]["index"]], 1.0)])
        lay("Big and a fast reader", "Qwen3.5 27B for thinking and Qwen3.5 9B (4-bit) for reading, two servers on one GPU",
            [("deep", "Qwen/Qwen3.5-27B", [gpus[0]["index"]], 0.80), ("reader", "cyankiwi/Qwen3.5-9B-AWQ-4bit", [gpus[0]["index"]], 0.19)])
        lay("Fast MoE", "Qwen3.5 35B-A3B in full precision serves every role",
            [("deep", "Qwen/Qwen3.5-35B-A3B", [gpus[0]["index"]], 1.0)])
        if sm >= 80:
            lay("gpt-oss 120B", "OpenAI's open 120B model serves every role",
                [("deep", "openai/gpt-oss-120b", [gpus[0]["index"]], 1.0)])
        lay("Llama 3.3 70B", "Meta 70B (4-bit) serves every role",
            [("deep", "casperhansen/llama-3.3-70b-instruct-awq", [gpus[0]["index"]], 1.0)])
    elif mem >= 35:  # A100 40 GB / A6000 / L40
        lay("Strong (default)", "Qwen3.5 27B (4-bit) serves every role", [("deep", "cyankiwi/Qwen3.5-27B-AWQ-4bit", [gpus[0]["index"]], 1.0)])
        lay("Strong and a fast reader", "Qwen3.5 27B (4-bit) for thinking and Qwen3.5 9B (4-bit) for reading, two servers on one GPU",
            [("deep", "cyankiwi/Qwen3.5-27B-AWQ-4bit", [gpus[0]["index"]], 0.64), ("reader", "cyankiwi/Qwen3.5-9B-AWQ-4bit", [gpus[0]["index"]], 0.35)])
        lay("Fast MoE", "Qwen3.5 35B-A3B (4-bit) serves every role",
            [("deep", "cyankiwi/Qwen3.5-35B-A3B-AWQ-4bit", [gpus[0]["index"]], 1.0)])
        if sm >= 80:
            lay("gpt-oss 20B", "OpenAI's open 20B model serves every role", [("deep", "openai/gpt-oss-20b", [gpus[0]["index"]], 1.0)])
    elif mem >= 20:  # L4 / A10G 24 GB
        lay("Single GPU (default)", "Qwen3.5 9B (4-bit) serves every role, with room for long context",
            [("deep", "cyankiwi/Qwen3.5-9B-AWQ-4bit", [gpus[0]["index"]], 1.0)])
        lay("Full 9B", "Qwen3.5 9B in full precision serves every role", [("deep", "Qwen/Qwen3.5-9B", [gpus[0]["index"]], 1.0)])
        lay("Fast MoE", "Qwen3.5 35B-A3B (4-bit) serves every role", [("deep", "cyankiwi/Qwen3.5-35B-A3B-AWQ-4bit", [gpus[0]["index"]], 1.0)])
    else:  # one T4
        lay("Single GPU (default)", "Qwen3.5 9B (4-bit) serves every role", [("deep", "cyankiwi/Qwen3.5-9B-AWQ-4bit", [gpus[0]["index"]], 1.0)])
        lay("Fast reader", "Qwen3.5 4B serves every role: faster, weaker", [("deep", "Qwen/Qwen3.5-4B", [gpus[0]["index"]], 1.0)])
    by_index = {g["index"]: g for g in gpus}
    for p in out:
        servers = []
        for role, hf, idx, share in p["servers"]:
            m = entry(hf)
            plan = plan_for(m, [by_index[i] for i in idx], share)
            servers.append(server(role, m, plan, gpus))
        p["servers"] = servers
        p["fits"] = min((s["fits"] for s in servers), key=lambda v: {"yes": 2, "tight": 1, "no": 0}[v])
    return [p for p in out if p["fits"] != "no"]


def server(role: str, m: dict, plan: dict, gpus: list[dict]) -> dict:
    """One entry of layout.json: everything serve.sh needs."""
    fam = FAMILIES.get(m.get("family") or "other", FAMILIES["other"])
    sm = min((gpu_sm(g["name"]) for g in gpus), default=80)
    return {"role": role, "hf": m["hf"], "name": f"{m['label']} ({role})", "label": m["label"], "family": m.get("family"),
            "gpus": plan.get("gpus") or [], "tp": plan.get("tp", 1), "port": PORTS.get(role, 8003),
            "util": plan.get("util", 0.9), "ctx": plan.get("ctx", 16384), "seqs": 8 if plan.get("ctx", 0) >= 16384 else 6,
            "dtype": "float16" if sm < 80 else "auto", "tool_parser": fam["tool_parser"],
            "reasoning_parser": fam["reasoning_parser"], "mm": fam["mm"], "no_think": fam["no_think"],
            "weights_gib": m["weights_gib"], "fits": plan.get("fits", "no"), "why": plan.get("why", "")}


# ---------------------------------------------------------------- Hugging Face lookups (blocking: call in a thread)

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def _get(url: str, ttl: float = 900):
    with _lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ApiError(404 if e.code in (401, 404) else 502, f"Hugging Face answered HTTP {e.code} for this model") from None
    except Exception as e:
        raise ApiError(502, f"Hugging Face is not reachable from the box ({type(e).__name__})") from None
    with _lock:
        _cache[url] = (time.time(), d)
    return d


def _bytes(st: dict) -> float:
    return sum(n * DTYPE_BYTES.get(k, 2) for k, n in ((st or {}).get("parameters") or {}).items())


def _kv_kib(cfg: dict) -> float:
    c = cfg.get("text_config") or cfg.get("llm_config") or cfg
    layers = int(c.get("num_hidden_layers") or 32)
    heads = int(c.get("num_attention_heads") or 32)
    kv = int(c.get("num_key_value_heads") or heads)
    hd = int(c.get("head_dim") or (int(c.get("hidden_size") or 4096) // max(heads, 1)))
    full = layers
    lt = c.get("layer_types")
    if isinstance(lt, list) and lt:
        full = sum(1 for x in lt if "full" in str(x) or str(x) == "attention") or layers
    elif c.get("full_attention_interval"):
        full = max(layers // int(c["full_attention_interval"]), 1)
    if c.get("kv_lora_rank"):  # MLA (DeepSeek-style): a compressed cache
        return layers * (int(c["kv_lora_rank"]) + int(c.get("qk_rope_head_dim") or 64)) * 2 / 1024
    return 2 * full * kv * hd * 2 / 1024


def lookup(hf: str) -> dict:
    """A model card for any Hugging Face repo: size from the safetensors metadata, KV per token from config.json."""
    hf = hf.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][\w.\-]*/[\w.\-]+", hf):
        raise ApiError(400, "a model id looks like owner/name, for example Qwen/Qwen3.5-9B")
    cur = entry(hf)
    if cur:
        return cur
    info = _get(HF_API + f"models/{urllib.parse.quote(hf, safe='/')}?expand[]=safetensors&expand[]=config&expand[]=pipeline_tag&expand[]=gated&expand[]=downloads")
    if info.get("gated"):
        gated = True
    else:
        gated = False
    size = _bytes(info.get("safetensors") or {}) / GIB
    if size <= 0:
        raise ApiError(400, "this repo has no safetensors weights vLLM can load (GGUF, MLX and ONNX repos are not supported)")
    cfg = info.get("config") or {}
    try:  # the API's config is a summary; the file has the numbers
        cfg = _get(f"https://huggingface.co/{hf}/resolve/main/config.json", ttl=3600) or cfg
    except ApiError:
        pass
    arch = " ".join(cfg.get("architectures") or [])
    quant = (cfg.get("quantization_config") or (cfg.get("text_config") or {}).get("quantization_config") or {}).get("quant_method", "")
    min_sm = 80 if str(quant).lower() in ("fp8", "mxfp4", "compressed-tensors") and "awq" not in hf.lower() else 75
    if str(quant).lower() in ("nvfp4", "modelopt_fp4"):
        min_sm = 100
    c = cfg.get("text_config") or cfg
    max_ctx = int(c.get("max_position_embeddings") or 32768)
    return {"hf": hf, "label": hf.split("/")[-1], "family": family_of(hf, arch), "weights_gib": round(size * 1.02, 1),
            "kv_kib": round(_kv_kib(cfg), 1), "min_sm": min_sm, "max_ctx": min(max_ctx, 131072),
            "note": f"{info.get('pipeline_tag') or 'model'}, {int(info.get('downloads') or 0):,} downloads"
                    + (", gated: needs your HF token and accepted terms" if gated else "") + (f", {quant}" if quant else ""),
            "curated": False, "gated": gated}


def search(q: str, limit: int = 20) -> list[dict]:
    q = " ".join(q.split())[:80]
    if len(q) < 2:
        return []
    out, seen = [], set()
    for tag in ("text-generation", "image-text-to-text"):
        url = HF_API + "models?" + urllib.parse.urlencode({"search": q, "sort": "downloads", "direction": "-1", "limit": str(limit),
                                                            "filter": tag}) + "&expand[]=safetensors&expand[]=downloads&expand[]=gated"
        for r in _get(url, ttl=600) or []:
            rid = r.get("id") or ""
            if rid in seen or BAD_REPO.search(rid):
                continue
            size = _bytes(r.get("safetensors") or {}) / GIB
            if size <= 0.2:
                continue
            seen.add(rid)
            cur = entry(rid)
            out.append(cur or {"hf": rid, "label": rid.split("/")[-1], "family": family_of(rid), "weights_gib": round(size * 1.02, 1),
                               "kv_kib": 64.0, "min_sm": 75, "max_ctx": 32768, "curated": False, "gated": bool(r.get("gated")),
                               "note": f"{int(r.get('downloads') or 0):,} downloads" + (", gated" if r.get("gated") else ""),
                               "downloads": int(r.get("downloads") or 0)})
    out.sort(key=lambda m: -(m.get("downloads") or 10 ** 9 if m.get("curated") else m.get("downloads") or 0))
    return out[:limit]
