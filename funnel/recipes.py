"""Data-driven per-game tweaks ("recipes").

Built-in recipes live in ``recipes.json`` next to this file. Users can add
their own in ``$XDG_CONFIG_HOME/funnel/recipes.json`` (usually
``~/.config/funnel/recipes.json``); those run after the built-ins, so they can
override environment variables or the menu category.

A recipe looks like::

    {
      "name": "my-game",
      "note": "optional human explanation",
      "when": {"files_any": ["*.xp3"], "text_regex": "my game"},
      "hints": ["engine:kirikiri"],
      "env": {"LANG": "ja_JP.UTF-8"},
      "args": ["-windowed"],
      "category": "Game"
    }

``when`` conditions (all present keys must match; lists match if any item does):

- ``files_any`` / ``files_all``: case-insensitive globs relative to the game
  folder, e.g. ``"unityplayer.dll"`` or ``"movie/*.alm"`` (two levels deep max)
- ``exe_names``: globs for the chosen executable's file name
- ``text_regex``: regex searched in "<input path> <display name> <exe name>",
  lowercased
- ``hints_any``: globs matched against hints so far (including hints added by
  earlier recipes)
- ``any``: a list of nested ``when`` objects; at least one must match
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

BUILTIN = Path(__file__).with_name("recipes.json")
SCAN_DEPTH = 2
SCAN_LIMIT = 20_000

_WHEN_KEYS = {"files_any", "files_all", "exe_names", "text_regex", "hints_any", "any"}
_RECIPE_KEYS = {"name", "note", "when", "hints", "env", "args", "category"}
# Environment variables whose values combine across recipes instead of replacing.
_LIST_ENV = {"WINEDLLOVERRIDES": ";"}


class RecipeError(ValueError):
    """A recipes file is malformed."""


@dataclass
class Applied:
    names: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    category: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "+".join(self.names) or "default"


def user_recipes_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "funnel" / "recipes.json"


def _validate_when(when: object, where: str) -> None:
    if not isinstance(when, dict) or not when:
        raise RecipeError(f"{where}: 'when' must be a non-empty object")
    unknown = set(when) - _WHEN_KEYS
    if unknown:
        raise RecipeError(f"{where}: unknown 'when' keys {sorted(unknown)}; allowed: {sorted(_WHEN_KEYS)}")
    for key in ("files_any", "files_all", "exe_names", "hints_any"):
        if key in when and not (isinstance(when[key], list) and all(isinstance(v, str) for v in when[key])):
            raise RecipeError(f"{where}: '{key}' must be a list of strings")
    if "text_regex" in when:
        try:
            re.compile(when["text_regex"])
        except (re.error, TypeError) as exc:
            raise RecipeError(f"{where}: bad text_regex: {exc}") from exc
    if "any" in when:
        if not isinstance(when["any"], list) or not when["any"]:
            raise RecipeError(f"{where}: 'any' must be a non-empty list")
        for index, nested in enumerate(when["any"]):
            _validate_when(nested, f"{where}.any[{index}]")


def _validate(recipe: object, where: str) -> dict:
    if not isinstance(recipe, dict):
        raise RecipeError(f"{where}: each recipe must be an object")
    unknown = set(recipe) - _RECIPE_KEYS
    if unknown:
        raise RecipeError(f"{where}: unknown keys {sorted(unknown)}; allowed: {sorted(_RECIPE_KEYS)}")
    if not isinstance(recipe.get("name"), str) or not recipe["name"]:
        raise RecipeError(f"{where}: 'name' is required")
    _validate_when(recipe.get("when"), f"{where} ({recipe['name']})")
    if not isinstance(recipe.get("hints", []), list):
        raise RecipeError(f"{where}: 'hints' must be a list")
    if not isinstance(recipe.get("args", []), list):
        raise RecipeError(f"{where}: 'args' must be a list")
    env = recipe.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise RecipeError(f"{where}: 'env' must map strings to strings")
    return recipe


def load_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RecipeError(f"Cannot read recipes {path}: {exc}") from exc
    except ValueError as exc:
        raise RecipeError(f"Recipes file {path} is not valid JSON: {exc}") from exc
    recipes = data.get("recipes") if isinstance(data, dict) else data
    if not isinstance(recipes, list):
        raise RecipeError(f"{path}: expected a 'recipes' list")
    return [_validate(recipe, f"{path} recipe #{index + 1}") for index, recipe in enumerate(recipes)]


def load_recipes(user_path: Path | None = None) -> list[dict]:
    recipes = load_file(BUILTIN)
    extra = user_path or user_recipes_path()
    if extra.is_file():
        recipes += load_file(extra)
    return recipes


def _listing(app_dir: Path) -> list[str]:
    """Lowercase relative paths under the game folder, a couple of levels deep."""
    found: list[str] = []
    root_depth = len(app_dir.parts)
    try:
        for current, directories, files in os.walk(app_dir, followlinks=False):
            depth = len(Path(current).parts) - root_depth
            relative = Path(current).relative_to(app_dir)
            for name in directories + files:
                found.append((relative / name).as_posix().casefold())
            if depth + 1 >= SCAN_DEPTH:
                directories[:] = []
            if len(found) >= SCAN_LIMIT:
                break
    except OSError:
        pass
    return found


def _globs_hit(patterns: Iterable[str], names: list[str]) -> list[bool]:
    return [any(fnmatch.fnmatchcase(name, pattern.casefold()) for name in names) for pattern in patterns]


def _matches(when: dict, *, files: list[str], exe: str, text: str, hints: list[str]) -> bool:
    if "files_any" in when and not any(_globs_hit(when["files_any"], files)):
        return False
    if "files_all" in when and not all(_globs_hit(when["files_all"], files)):
        return False
    if "exe_names" in when and not any(_globs_hit(when["exe_names"], [exe])):
        return False
    if "text_regex" in when and not re.search(when["text_regex"], text):
        return False
    if "hints_any" in when and not any(
        fnmatch.fnmatchcase(hint, pattern) for pattern in when["hints_any"] for hint in hints
    ):
        return False
    if "any" in when and not any(
        _matches(nested, files=files, exe=exe, text=text, hints=hints) for nested in when["any"]
    ):
        return False
    return True


def apply_recipes(
    *, app_dir: Path, exe: Path, text: str, hints: list[str], recipes: list[dict] | None = None,
) -> Applied:
    """Run every recipe in order against one game and collect the effects."""
    applied = Applied(hints=list(hints))
    files = _listing(app_dir)
    exe_name = exe.name.casefold()
    lowered = text.casefold()
    for recipe in recipes if recipes is not None else load_recipes():
        if not _matches(recipe["when"], files=files, exe=exe_name, text=lowered, hints=applied.hints):
            continue
        applied.names.append(recipe["name"])
        if recipe.get("note"):
            applied.notes.append(f"{recipe['name']}: {recipe['note']}")
        for hint in recipe.get("hints", []):
            if hint not in applied.hints:
                applied.hints.append(hint)
        for key, value in recipe.get("env", {}).items():
            separator = _LIST_ENV.get(key)
            if separator and applied.env.get(key):
                existing = applied.env[key].split(separator)
                applied.env[key] = separator.join(existing + [v for v in value.split(separator) if v not in existing])
            else:
                applied.env[key] = value
        applied.args.extend(str(arg) for arg in recipe.get("args", []))
        if recipe.get("category"):
            applied.category = recipe["category"]
    return applied
