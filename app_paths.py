"""Path resolution shared by the finance dashboard modules.

There are two roots, and which physical directory each maps to depends on
whether we're running from source (dev) or from a frozen PyInstaller bundle:

  resource_dir()  read-only bundled assets (market_data.json, dolphin.png, the
                  favicons). In a frozen app this is the unpacked bundle
                  (sys._MEIPASS); in dev it's the repo directory.

  data_dir()      per-user writable state (ingested transactions, the generated
                  dashboard HTML, finance_config.json, networth.json, the Empower
                  browser profile, and any user-added prior_years CSVs). In a
                  frozen app this is ~/Library/Application Support/FinProject so
                  the app in /Applications never writes into its own (read-only)
                  bundle. In dev it's the repo directory, so the developer flow —
                  files landing next to the code, the .gitignore rules, the
                  data-hygiene purge — is completely unchanged.

Keeping this in one module means project.py, dashboard_server.py, and
empower_playwright.py all agree on where a given file lives.
"""
import os
import sys

APP_NAME = "FinProject"
_HERE = os.path.dirname(os.path.abspath(__file__))


def is_frozen() -> bool:
    """True when running inside a PyInstaller (or similar) frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> str:
    """Directory holding read-only bundled assets."""
    if is_frozen():
        # PyInstaller unpacks bundled data files under _MEIPASS; fall back to the
        # executable's directory for other freezers.
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return _HERE


def data_dir() -> str:
    """Directory for per-user writable state (created on first use).

    FINPROJECT_DATA_DIR overrides the location (useful for isolated test
    instances); otherwise it's ~/Library/Application Support/FinProject when
    frozen, or the repo directory when run from source.
    """
    override = os.environ.get("FINPROJECT_DATA_DIR")
    if override:
        d = os.path.expanduser(override)
    elif is_frozen():
        d = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    else:
        d = _HERE
    os.makedirs(d, exist_ok=True)
    return d
