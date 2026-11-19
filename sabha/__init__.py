"""Sabha: run and watch AI agents on borrowed cloud GPUs (Colab / Kaggle).

This package is the hub: one aiohttp process that serves the web UI, a REST
API and a WebSocket event stream, starts harness runs, and keeps its state on
disk. The contract with the web UI is docs/PROTOCOL.md.
"""

__version__ = "1.0.0"
