#!/usr/bin/env python3
"""Shared config helpers for the new portfolio engine chain."""

import os
from typing import Any, Dict

import yaml


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")


def load_middle_config(section: str, default: Dict[str, Any] = None) -> Dict[str, Any]:
    """Load one section from `pyramid_middle_layer`.

    Engine modules keep local defaults so a config read failure does not break
    imports/tests, but production runs should still use `config-check` first.
    """
    fallback = dict(default or {})
    try:
        with open(DEFAULT_CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        section_cfg = (cfg.get("pyramid_middle_layer") or {}).get(section, {})
        if isinstance(section_cfg, dict):
            merged = dict(fallback)
            merged.update(section_cfg)
            return merged
    except Exception:
        pass
    return fallback
