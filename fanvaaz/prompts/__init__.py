"""Versioned prompt registry.

Prompts live in YAML next to this file, one per task, each with an id, a version
and the output schema it expects. Every LLM call records the prompt id, version
and a hash of the exact text, so a change in quality can be traced to the prompt
change that caused it, and promptfoo can compare two versions side by side.

Variables use {name} and are substituted with str.replace, never str.format,
because the prompts contain literal JSON braces.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str
    schema: str | None

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.system.encode("utf-8")).hexdigest()[:12]

    @property
    def tag(self) -> str:
        return f"{self.id}@{self.version}"


@lru_cache
def _load(prompt_id: str) -> dict:
    path = DIR / f"{prompt_id}.yaml"
    if not path.exists():
        raise KeyError(f"no prompt '{prompt_id}'")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("id", "version", "system"):
        if key not in data:
            raise ValueError(f"prompt {path.name} is missing '{key}'")
    return data


def get(prompt_id: str, **variables: str) -> Prompt:
    data = _load(prompt_id)
    text = data["system"].rstrip()
    for k, v in variables.items():
        text = text.replace("{" + k + "}", str(v))
    return Prompt(id=data["id"], version=str(data["version"]), system=text, schema=data.get("schema"))


def catalog() -> list[dict]:
    """Every prompt with its version and hash (shown in the AI Ops panel)."""
    out = []
    for p in sorted(DIR.glob("*.yaml")):
        d = _load(p.stem)
        out.append(
            {
                "id": d["id"],
                "version": str(d["version"]),
                "sha": hashlib.sha256(d["system"].rstrip().encode("utf-8")).hexdigest()[:12],
                "schema": d.get("schema"),
                "description": " ".join(str(d.get("description", "")).split()),
            }
        )
    return out
