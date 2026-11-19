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

SECRET_FIELDS = ("tavily_key", "hf_token", "git_token", "brave_key", "serper_key")
ENV_OF = {"tavily_key": "TAVILY_API_KEY", "hf_token": "HF_TOKEN", "brave_key": "BRAVE_API_KEY", "serper_key": "SERPER_API_KEY"}
PROFILE_FIELDS = {"name": 60, "about": 240}  # the profile: plain text, shown back as is (not secret)
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
        out: dict = {"git_repo": self.get("git_repo"), "profile": {k: self.get(k) for k in PROFILE_FIELDS}}
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
        for field, limit in PROFILE_FIELDS.items():
            if field in body:
                value = body[field] if body[field] is not None else ""
                if not isinstance(value, str):
                    raise ApiError(400, f"{field} must be text")
                value = " ".join(value.split())[:limit]
                if value:
                    self.d[field] = value
                else:
                    self.d.pop(field, None)
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


def forget_keys(settings: "Settings") -> dict:
    """Remove every key this box holds (the profile's name stays). Keys from notebook secrets come back on the next
    run of the cell; this clears the copies the hub keeps."""
    for field in (*SECRET_FIELDS, "git_repo"):
        settings.d.pop(field, None)
    _write(settings.path, settings.d, 0o600)
    settings.apply()
    return settings.public()


def check_github(settings: "Settings") -> dict:
    """Try the GitHub repository and token the box holds, and say what is risky about them. Never returns the token."""
    import urllib.error
    import urllib.request
    repo, tok = settings.get("git_repo"), settings.get("git_token")
    if not (repo and tok):
        return {"ok": False, "error": "set a repository and a token first", "warnings": []}
    kind = "fine-grained" if tok.startswith("github_pat_") else "classic" if tok.startswith(("ghp_", "gho_")) else "other"
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}", headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "User-Agent": "sabha-profile"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read() or b"{}")
            scopes = [x.strip() for x in (r.headers.get("X-OAuth-Scopes") or "").split(",") if x.strip()]
    except urllib.error.HTTPError as e:
        return {"ok": False, "kind": kind, "error": {401: "GitHub does not accept this token (expired or revoked?)",
                                                     404: "the repository does not exist, or this token cannot see it"}.get(e.code, f"GitHub said {e.code}"),
                "warnings": []}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": kind, "error": f"GitHub not reachable: {type(e).__name__}", "warnings": []}
    perms = d.get("permissions") or {}
    warn = []
    if not d.get("private"):
        warn.append("The repository is public: anyone can read the reports kept there. Make it private on GitHub.")
    if kind == "classic":
        warn.append("This is a classic token" + (f" with scopes {', '.join(scopes)}" if scopes else "") + ": it reaches every repository "
                    "you have. A fine-grained token limited to this one repository (Contents: read and write) is safer.")
    if not perms.get("push"):
        warn.append("This token cannot write to the repository: reports will not be kept there.")
    return {"ok": True, "kind": kind, "repo": d.get("full_name") or repo, "private": bool(d.get("private")), "can_write": bool(perms.get("push")),
            "scopes": scopes, "warnings": warn}


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
