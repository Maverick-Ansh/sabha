"""Box settings and the research taste: two small JSON files in the hub's data dir.

settings.json  optional keys a person may give: Tavily key (web search), Hugging Face token, a git repo
               and token for research sync. File mode 600. A key never goes back to the browser: the API
               answers "set" and the last four characters (PROTOCOL.md section 9).
taste.json     what the Research tab's taste form holds: the taste text, papers the person loves, the
               question of the next run and its length in hours.

The notebook launcher (sabha/box.py) fills empty settings from Kaggle / Colab secrets before the hub starts.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .util import ApiError

SECRET_FIELDS = ("tavily_key", "hf_token", "git_token")
ENV_OF = {"tavily_key": "TAVILY_API_KEY", "hf_token": "HF_TOKEN"}
GIT_REPO = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9][A-Za-z0-9._\-]*)/([A-Za-z0-9._\-]+?)(?:\.git)?/?$")
ARXIV_ID = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
TASTE_MAX, TOPIC_MAX, LOVES_MAX = 6000, 3000, 30
RUN_MODES = ("instant", "continuous")  # instant: one short pass (~20 min); continuous: hours of work in waves


def _read(path: Path) -> dict:
    try:
        d = json.loads(path.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, obj: dict, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


class Settings:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "settings.json"
        self.d = _read(self.path)
        self._ours: set[str] = set()  # environment variables this object set (so clearing a key removes only ours)
        self.apply()

    def get(self, field: str) -> str:
        return str(self.d.get(field) or "")

    def apply(self) -> None:
        """Keys into the hub's environment: web tools, dsh runs and the research daemon inherit it."""
        for field, var in ENV_OF.items():
            value = self.get(field)
            if value:
                os.environ[var] = value
                self._ours.add(var)
            elif var in self._ours:
                os.environ.pop(var, None)
                self._ours.discard(var)

    def public(self) -> dict:
        out: dict = {"git_repo": self.get("git_repo")}
        for field in SECRET_FIELDS:
            value = self.get(field)
            env = os.environ.get(ENV_OF.get(field, ""), "") if not value and field in ENV_OF else ""
            shown = value or env
            out[field] = {"set": bool(shown), "hint": ("…" + shown[-4:]) if len(shown) >= 12 else "",
                          "source": "settings" if value else "environment" if env else ""}
        search = next((n for n, var in (("tavily", "TAVILY_API_KEY"), ("brave", "BRAVE_API_KEY"), ("serper", "SERPER_API_KEY"))
                       if os.environ.get(var, "").strip()), "")
        out["search"] = search or "free engines (DuckDuckGo, Bing, Wikipedia)"
        out["git_sync"] = bool(self.get("git_repo") and self.get("git_token"))
        return out

    def update(self, body: dict) -> dict:
        for field in (*SECRET_FIELDS, "git_repo"):
            if field not in body:
                continue
            value = body[field] if body[field] is not None else ""
            if not isinstance(value, str) or len(value) > 400 or re.search(r"\s", value.strip()):
                raise ApiError(400, f"{field} must be one short string without spaces")
            value = value.strip()
            if field == "git_repo" and value:
                m = GIT_REPO.match(value)
                if not m:
                    raise ApiError(400, "git_repo must look like owner/name (a GitHub repository)")
                value = f"{m.group(1)}/{m.group(2)}"
            if value:
                self.d[field] = value
            else:
                self.d.pop(field, None)
        _write(self.path, self.d, 0o600)
        self.apply()
        return self.public()


def clean_loves(value) -> list[str]:
    """arXiv ids out of links and free text; a line with no id is kept as a paper title."""
    if isinstance(value, str):
        items = [x for x in re.split(r"[\n,;]+", value)]
    elif isinstance(value, list):
        items = [str(x) for x in value if isinstance(x, (str, int, float))]
    else:
        raise ApiError(400, "loves must be a list of arXiv ids, links or titles")
    out: list[str] = []
    for item in items:
        item = " ".join(item.split())
        ids = ARXIV_ID.findall(item)
        for x in ids or ([item[:160]] if len(item) >= 4 else []):
            if x not in out:
                out.append(x)
    return out[:LOVES_MAX]


class Taste:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "taste.json"

    def load(self) -> dict:
        d = _read(self.path)
        try:
            hours = float(d["hours"] if d.get("hours") is not None else 6)
        except (TypeError, ValueError):
            hours = 6.0
        mode = d.get("mode") if d.get("mode") in RUN_MODES else "continuous"
        try:
            wave = float(d.get("wave_min") or 0)
        except (TypeError, ValueError):
            wave = 0.0
        return {"taste": str(d.get("taste") or ""), "loves": [str(x) for x in d.get("loves") or [] if isinstance(x, str)],
                "topic": str(d.get("topic") or ""), "hours": 0.0 if hours <= 0 else min(max(hours, 0.25), 12.0),
                "mode": mode, "wave_min": min(max(wave, 0.0), 180.0), "updated": float(d.get("updated") or 0)}

    def save(self, body: dict) -> dict:
        cur = self.load()
        for key, limit in (("taste", TASTE_MAX), ("topic", TOPIC_MAX)):
            if key in body:
                if not isinstance(body[key], str):
                    raise ApiError(400, f"{key} must be a string")
                cur[key] = body[key].strip()[:limit]
        if "loves" in body:
            cur["loves"] = clean_loves(body["loves"])
        if "hours" in body:  # 0 = until the session ends
            try:
                h = float(body["hours"])
            except (TypeError, ValueError):
                raise ApiError(400, "hours must be a number") from None
            cur["hours"] = 0.0 if h <= 0 else min(max(h, 0.25), 12.0)
        if "mode" in body:
            if body["mode"] not in RUN_MODES:
                raise ApiError(400, "mode must be instant or continuous")
            cur["mode"] = body["mode"]
        if "wave_min" in body:  # 0 = automatic
            try:
                cur["wave_min"] = min(max(float(body["wave_min"] or 0), 0.0), 180.0)
            except (TypeError, ValueError):
                raise ApiError(400, "wave_min must be a number of minutes") from None
        cur["updated"] = time.time()
        _write(self.path, cur)
        return cur
