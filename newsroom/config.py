from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    with open(os.path.join(ROOT, "config", name), encoding="utf-8") as f:
        return json.load(f)


def load_all() -> dict:
    return {"sources": _load("sources.json"), "settings": _load("settings.json"), "keywords": _load("keywords.json")}


def sources_for(cfg: dict, bot: str) -> list[dict]:
    return [s for s in cfg["sources"]["sources"] if s["bot"] == bot and s.get("enabled", True)]
