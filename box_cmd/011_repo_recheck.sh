# One-time recheck of every card's "verified" repo with the official-repo rule (sources.official_repo).
# Pulls the new code first; the running daemon keeps its old modules until the steering restart.
git pull -q --autostash && git log --oneline -1
python3 - <<'PY'
import glob, json
from sabha.research import sources as src
from sabha.research.agents import card_md
from sabha.research.memory import write, write_json
D = "/kaggle/working/sabha-research/missions/001-experiential-world-models/cards/"
n = {"kept": 0, "candidate": 0, "skip": 0, "err": 0}
for f in sorted(glob.glob(D + "*.json")):
    c = json.load(open(f, encoding="utf-8"))
    r = c.get("official_repo") or {}
    if r.get("status") != "verified" or not r.get("url"):
        n["skip"] += 1
        continue
    full = ""
    if r.get("src") == "paper":
        try:
            full = src.paper_text(c["id"])["full"]
        except Exception as e:
            print("paper fetch failed", c["id"], e)
    try:
        g = src.official_repo(r["url"], c["id"], c.get("title", ""), full)
    except Exception as e:
        print("ERR", c["id"], type(e).__name__, e)
        n["err"] += 1
        continue
    if not g.get("exists"):
        n["err"] += 1
        continue
    r["why"] = g.get("why", "")
    if g.get("official"):
        n["kept"] += 1
    else:
        r["status"] = "candidate"
        n["candidate"] += 1
        c.setdefault("quality", {}).setdefault("lint", []).append("repo is a candidate: nothing ties it to this paper")
    c["official_repo"] = r
    write_json(f, c)
    write(f[:-5] + ".md", card_md(c))
    print(c["id"], r["status"], r.get("src"), r["url"], "|", r["why"], flush=True)
print("RECHECK", n)
PY