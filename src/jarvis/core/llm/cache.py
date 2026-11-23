import hashlib
import json
from pathlib import Path

import diskcache

from jarvis.core.config import settings

_cache: diskcache.Cache | None = None


def get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        Path(settings.cache_dir).mkdir(parents=True, exist_ok=True)
        _cache = diskcache.Cache(settings.cache_dir)
    return _cache


def prompt_key(model: str, prompt: str, schema_name: str, *, schema_hash: str = "",
               task: str = "", prompt_version: str = "1", max_tokens: int | None = None,
               fallback_model: str = "") -> str:
    """Separate cached output contracts and invalidate the old name-only namespace."""
    identity = {"cache_policy": 2, "model": model, "prompt": prompt, "schema_name": schema_name,
                "schema_hash": schema_hash, "task": task, "prompt_version": prompt_version,
                "max_tokens": max_tokens, "fallback_model": fallback_model}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
