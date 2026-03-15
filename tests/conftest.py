"""Shared test fixtures and import helpers for korg helper scripts.

The scripts in this repository use hyphenated filenames like
git-patchwork-bot.py, which are not valid Python identifiers.
Use the load_script() helper to import them in tests.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

HELPERS_DIR = Path(__file__).resolve().parent.parent


def load_script(script_name: str):
    """Import a helper script as a module.

    Handles hyphenated filenames by converting them to a valid module
    name internally. Returns the imported module object.

    Usage::

        pwbot = load_script('git-patchwork-bot.py')
        result = pwbot.get_url_path('https://example.com/repo.git')
    """
    script_path = HELPERS_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f'{script_path} not found')

    # Convert hyphenated filename to a valid Python module name
    module_name = script_name.replace('-', '_').removesuffix('.py')

    # Return cached module if already loaded
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
