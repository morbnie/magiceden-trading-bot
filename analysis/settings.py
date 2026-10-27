"""Loads config/default.yaml (or the file named by BOT_CONFIG) and exposes
the handful of values the analysis scripts need."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("BOT_CONFIG") or ROOT / "config" / "default.yaml")

with CONFIG_PATH.open() as f:
    CONFIG: dict = yaml.safe_load(f) or {}

COLLECTION: str = CONFIG["collection"]
SIGNAL_COLLECTIONS: list[str] = list(CONFIG.get("signal_collections") or [])
COLLECTIONS: list[str] = [COLLECTION] + [s for s in SIGNAL_COLLECTIONS if s != COLLECTION]

_labels = dict(CONFIG.get("signal_labels") or {})
_labels[COLLECTION] = CONFIG.get("collection_label") or COLLECTION
LABELS: dict[str, str] = {s: _labels.get(s, s) for s in COLLECTIONS}

COINGECKO_NFT_IDS: dict[str, str] = dict(CONFIG.get("coingecko_nft_ids") or {})
WATCH_WALLETS: dict[str, str] = {str(k): str(v) for k, v in (CONFIG.get("watch_wallets") or {}).items()}
EVENTS: list[dict] = list(CONFIG.get("events") or [])
FIAT: str = str(CONFIG.get("fiat_currency") or "EUR").upper()
PHASES: dict = dict(CONFIG.get("phases") or {})
