#!/usr/bin/env bash
# Box setup for Colab / Kaggle. Safe to re-run: every step checks before it installs.
#   1. Node >= 24.9      (DeepSeek Harness needs it; the boxes ship Node 20)
#   2. dsh, pinned       (only pre-releases exist, so the version must be named)
#   3. vLLM in its own venv at /opt/vllm, linked as /usr/local/bin/vllm
# Usage: bash scripts/box_setup.sh [--no-vllm]
# Last line printed is SETUP_OK or SETUP_FAIL <what>.
set -u
export PATH=/usr/local/bin:$PATH
DSH_VERSION="${DSH_VERSION:-0.2.1-rc.5}"
VLLM_SPEC="${VLLM_SPEC:-vllm}"
WANT_VLLM=1
[ "${1:-}" = "--no-vllm" ] && WANT_VLLM=0

say() { echo "[setup $(date +%H:%M:%S)] $*"; }
fail() { echo "SETUP_FAIL $*"; exit 1; }
node_ok() {
  command -v node >/dev/null 2>&1 && node -e \
    "const [a,b]=process.versions.node.split('.').map(Number);process.exit(a>24||(a===24&&b>=9)?0:1)"
}

if node_ok; then
  say "node $(node -v) is new enough"
else
  say "installing Node 24 (found: $(node -v 2>/dev/null || echo none))"
  command -v npm >/dev/null 2>&1 || apt-get -qq install -y nodejs npm >/dev/null 2>&1
  npm i -g n >/dev/null 2>&1 || fail "npm i -g n"
  n 24 >/dev/null 2>&1 || fail "n 24"
  hash -r
  node_ok || fail "node is still $(node -v 2>/dev/null) after install"
  say "node $(node -v)"
fi

if command -v dsh >/dev/null 2>&1; then
  say "dsh already installed: $(command -v dsh)"
else
  say "installing dsh $DSH_VERSION"
  npm i -g "@deepseek-ai/dsh@$DSH_VERSION" >/tmp/dsh_install.log 2>&1 || { tail -20 /tmp/dsh_install.log; fail "dsh install"; }
  command -v dsh >/dev/null 2>&1 || fail "dsh not on PATH after install"
  say "dsh: $(command -v dsh)"
fi

if [ "$WANT_VLLM" = 1 ]; then
  if [ -x /opt/vllm/bin/vllm ]; then
    say "vLLM already installed"
  else
    say "installing $VLLM_SPEC into /opt/vllm (a few minutes)"
    command -v uv >/dev/null 2>&1 || pip install -q uv >/dev/null 2>&1 || fail "pip install uv"
    uv venv /opt/vllm --python 3.12 >/dev/null 2>&1 || fail "uv venv"
    uv pip install --python /opt/vllm/bin/python "$VLLM_SPEC" >/tmp/vllm_install.log 2>&1 \
      || { tail -20 /tmp/vllm_install.log; fail "vllm install"; }
  fi
  ln -sf /opt/vllm/bin/vllm /usr/local/bin/vllm
  say "vllm $(/opt/vllm/bin/python -c 'import vllm;print(vllm.__version__)' 2>/dev/null)"
fi
echo SETUP_OK
