"""Sphinx configuration for chaos-jungle documentation."""

import os
import sys

sys.path.insert(0, os.path.abspath(".."))

# ── Project ───────────────────────────────────────────────────────
project = "chaos-jungle"
author = "chaos-jungle contributors"
release = "0.1.0"

# ── Extensions ────────────────────────────────────────────────────
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",          # NumPy / Google docstrings
    "sphinx.ext.viewcode",          # source links
    "sphinx.ext.intersphinx",       # cross-project links
    "sphinx_autodoc_typehints",     # type hints in docs
    "myst_parser",                  # Markdown support
]

# ── Napoleon (NumPy docstrings) ───────────────────────────────────
napoleon_numpy_docstring = True
napoleon_google_docstring = False
napoleon_use_param = True
napoleon_use_rtype = True

# ── Autodoc ───────────────────────────────────────────────────────
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
autodoc_typehints = "description"

# ── Intersphinx ───────────────────────────────────────────────────
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# ── Theme ─────────────────────────────────────────────────────────
html_theme = "furo"
html_title = "chaos-jungle"
html_static_path = ["_static"]

# ── sphinx-click ─────────────────────────────────────────────────
sphinx_click_mock_imports = []

# ── Suppress known harmless warnings ─────────────────────────────
suppress_warnings = [
    "ref.duplicate",          # dataclass field double-indexing
]

# ── Source ────────────────────────────────────────────────────────
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}
master_doc = "index"
