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
    "sphinxcontrib.mermaid",        # Mermaid diagrams
]

# ── Mermaid ────────────────────────────────────────────────────────
mermaid_version = "11"              # CDN version (used for HTML output)
mermaid_init_js = """
mermaid.initialize({
  startOnLoad: true,
  theme: 'dark',
  themeVariables: {
    primaryColor:        '#1a4731',
    primaryTextColor:    '#7dc829',
    primaryBorderColor:  '#7dc829',
    lineColor:           '#7dc829',
    secondaryColor:      '#2e3440',
    tertiaryColor:       '#0f2318',
    mainBkg:             '#1a4731',
    nodeBorder:          '#7dc829',
    clusterBkg:          '#0f2318',
    titleColor:          '#7dc829',
    edgeLabelBackground: '#0f2318',
    fontFamily:          'Segoe UI, sans-serif',
  }
});
"""

# ── Static files ──────────────────────────────────────────────────
html_static_path = ["_static"]
html_js_files = ["mermaid-theme.js"]   # CJ logo colors for all Mermaid diagrams

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
html_logo = "_static/logo.png"
html_static_path = ["_static"]

# ── sphinx-click ─────────────────────────────────────────────────
sphinx_click_mock_imports = []

# ── Suppress known harmless warnings ─────────────────────────────
suppress_warnings = [
    "ref.duplicate",          # dataclass field double-indexing
    "ref.python",             # Metric re-exported from metrics & metrics.base
]

# ── Source ────────────────────────────────────────────────────────
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}
master_doc = "index"
