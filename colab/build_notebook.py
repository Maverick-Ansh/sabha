"""Writes the one-cell launcher notebooks: kaggle/sabha_kaggle.ipynb and colab/sabha_colab.ipynb.

Run: python colab/build_notebook.py   (edit this file, never the .ipynb files)
The cell clones (or updates) the public repo and runs sabha/box.py up: hub + tunnel link in about
2 minutes, tools and models set up in the background, then a heartbeat so the session stays alive.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/Maverick-Ansh/sabha.git"

INTRO = {
    "kaggle": """
# Sabha on Kaggle

Your own agent council on your own Kaggle GPUs: talk to the agents, convene a council on a paper, give them work with web search,
and let them research for hours by **your research taste**.

1. Right panel, **Session options**: Accelerator **GPU T4 x2**, Internet **on** (Kaggle asks for a verified phone number once).
2. Run the cell below. In about 2 minutes it prints **one link**. Open it on your laptop or phone.
3. The app shows the box coming up. The models are ready about 15 minutes later. Then: Talk, Council, Work, Research.

Keep the link private: whoever has it controls this box. Keep this cell running and this Kaggle tab open on an awake computer:
that keeps the session alive (Kaggle stops an idle session after about 20 minutes). A Kaggle session lasts at most 12 hours, and everything on the box goes when it ends,
so download your research results from the Research tab before that.

Optional, as Kaggle Secrets (Add-ons > Secrets, then tick each one for this notebook) or in the app later: `TAVILY_API_KEY` (much better web search,
free tier at tavily.com), `HF_TOKEN`, `SABHA_GIT_REPO` + `SABHA_GIT_TOKEN` (push research results to your own GitHub repo).
""",
    "colab": """
# Sabha on Colab

Your own agent council on a Colab GPU. Runtime > Change runtime type > **T4 GPU**, then run the cell below. In about 2 minutes it
prints **one link**. Colab has one GPU, so Sabha runs one model (Qwen3.5-4B) for everything. On Kaggle (2x T4) it runs two.

Keep the link private. Keep this cell running. Optional secrets (key icon on the left): `TAVILY_API_KEY`, `HF_TOKEN`,
`SABHA_GIT_REPO`, `SABHA_GIT_TOKEN`.
""",
}

CELL = f"""
# Sabha: run this one cell. It prints your link, then keeps the session alive. Safe to run again.
import importlib, os, subprocess, sys

# os.environ["TAVILY_API_KEY"] = "tvly-..."   # optional: better web search (or a Kaggle / Colab secret)
# os.environ["SABHA_NEW_LINK"] = "1"          # optional: a fresh link (the models keep running)

BASE = next((d for d in ("/kaggle/working", "/content") if os.path.isdir(d)), os.path.expanduser("~"))
APP = os.path.join(BASE, "sabha")
if os.path.isdir(os.path.join(APP, ".git")):
    subprocess.run(["git", "-C", APP, "pull", "-q", "--ff-only"], check=False)
else:
    r = subprocess.run(["git", "clone", "-q", "--depth", "1", "{REPO}", APP], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("Could not download Sabha: is the Internet on? Right panel, Session options: Internet ON "
                         "(Kaggle asks for a verified phone number once). Then run this cell again. " + r.stderr[-200:])
if APP not in sys.path:
    sys.path.insert(0, APP)
import sabha.box as box
box = importlib.reload(box)
box.up()
"""

KAGGLE_META = {"accelerator": "nvidiaTeslaT4", "isGpuEnabled": True, "isInternetEnabled": True,
               "language": "python", "sourceType": "notebook", "dataSources": []}


def lines(text):
    t = text.strip("\n").split("\n")
    return [x + "\n" for x in t[:-1]] + [t[-1]]


def notebook(kind):
    meta = {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"}}
    if kind == "kaggle":
        meta["kaggle"] = KAGGLE_META
    else:
        meta["accelerator"] = "GPU"
        meta["colab"] = {"gpuType": "T4", "provenance": []}
    return {"cells": [{"cell_type": "markdown", "metadata": {}, "source": lines(INTRO[kind])},
                      {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": lines(CELL)}],
            "metadata": meta, "nbformat": 4, "nbformat_minor": 5}


for kind, path in (("kaggle", ROOT / "kaggle" / "sabha_kaggle.ipynb"), ("colab", ROOT / "colab" / "sabha_colab.ipynb")):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(notebook(kind), indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print("wrote", path.relative_to(ROOT))
