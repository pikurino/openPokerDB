"""Theme palettes and persisted GUI settings for the OpenPokerDB main window.

Why the stylesheet rather than only `QPalette`: on Windows the native style paints headers,
alternate rows, menu panels and tick boxes from its own pixmaps and ignores most palette roles, so
a dark theme set through `QPalette` alone leaves a white grid inside a black window. `QStyle Fusion`
paints every one of those from the palette, which is why `apply()` switches the style for all
themes, light ones included -- one code path, and the same widget geometry he already knows.

The colours a widget cannot get from the palette (the TOTAL banner, the green/red money text, the
chart's background and gridlines, the mode buttons' checked state) are read from the theme dict by
the widgets that draw them, so a theme change repaints without rebuilding the window. Inline
`setStyleSheet` calls in HudMain are avoided for the same reason: object-name selectors in the app
stylesheet win over nothing and lose to a later inline call, so putting a colour in both places
means the theme change silently does not apply.

The four named themes are *looks modelled on* the screenshots of those products, not copies of
them: no colour value here was sampled from another vendor's install.

Settings live in one small JSON file next to the rest of fpdb's configuration, so the window comes
back with the same theme, ticked graph lines and hand-list length after a restart:

    %APPDATA%/fpdb/HudMain.settings

The file is optional; a missing or unreadable one falls back to DEFAULTS rather than raising,
because it is loaded before the window exists and a bad setting must not stop the GUI opening.
"""

import os
import json

from PySide6 import QtCore, QtGui, QtWidgets

import Configuration

SETTINGS_FILE = os.path.join(Configuration.CONFIG_PATH, "HudMain.settings")

# key -> default. `graph_lines` is a list of GraphView line keys; `graph_x` is 'hands' or 'days'.
# `hero` is not a graph setting even though it started life as one: it is the seat that is him, and
# the By-Stakes, By-Day, By-Game and By-Position rows all count that name's hands.
DEFAULTS = {"theme": "Black",
            "graph_lines": ["net", "ev", "sd", "nsd"],
            "graph_x": "hands",
            "hero": "Hero",
            "history_hands": 250}

# A line colour has to read on black *and* on white, so the series palette lives here instead of in
# each theme: the same stake plotted in two themes keeps its colour, which is the point of the tick
# boxes. Dashed lines are set per series in GraphView, not from this table.
SERIES_COLORS = {
    "net": "#2f8fd6", "ev": "#a86ee0", "sd": "#2fae5f", "nsd": "#e34a4a",
    "sd_won": "#7ac74f", "sd_lost": "#ff6f61", "nsd_won": "#ffd166",
    "nsd_lost": "#b3599c", "rake": "#9aa0a8",
}

_CLASSIC = {
    "label": "Classic light",
    "window": "#f0f0f0", "base": "#ffffff", "text": "#1c1c1c", "button": "#e4e4e4",
    "accent": "#2d6cdf", "alt_row": "#f5f6f8", "grid_line": "#d8dadf",
    "header": "#e9eaee", "header_text": "#333333", "row0": "#dfe9f5",
    "money_pos": "#1a7f37", "money_neg": "#c0392b",
    "chart_bg": "#ffffff", "chart_grid": "#dcdcdc", "chart_text": "#202020",
    "summary_bg": "#fffbe6", "summary_border": "#d9b23a",
}

THEMES = {
    "classic": dict(_CLASSIC),
    "black": dict(_CLASSIC, label="Black",
                  window="#121212", base="#181818", text="#e6e6e6", button="#232323",
                  accent="#2d6cdf", alt_row="#1d1d1d", grid_line="#2c2c2c",
                  header="#1f1f1f", header_text="#e6e6e6", row0="#26384f",
                  money_pos="#3ec46d", money_neg="#ff6b6b",
                  chart_bg="#0e0e0e", chart_grid="#343434", chart_text="#e6e6e6",
                  summary_bg="#1e1e1e", summary_border="#8a7a2f"),
    # Holdem Manager 2's look: a slate-grey shell around a light grid, olive-green selection.
    "hm2": dict(_CLASSIC, label="HM2-like",
                window="#3f4650", base="#f8f8f6", text="#1c1c1c", button="#4a5260",
                accent="#6f9c3f", alt_row="#eef0ec", grid_line="#c9ccc6",
                header="#2f353d", header_text="#eaeef2", row0="#dbe8cd",
                chart_bg="#ffffff", chart_grid="#d6d8d4", chart_text="#22252a",
                summary_bg="#f2f7ea", summary_border="#6f9c3f"),
    # PokerTracker 4's look: a light grey shell, navy toolbars, blue selection.
    "pt4": dict(_CLASSIC, label="PT4-like",
                window="#f2f3f5", base="#ffffff", text="#17181a", button="#dfe2e6",
                accent="#12395b", alt_row="#f6f7f9", grid_line="#d5d8dc",
                header="#e2e6ea", header_text="#12395b", row0="#dbe7f5",
                money_pos="#0b6f3b", money_neg="#b3261e",
                chart_bg="#ffffff", chart_grid="#dde1e6", chart_text="#12395b",
                summary_bg="#eef4fb", summary_border="#12395b"),
    # Holdem Manager 3's look: flat dark navy with a cyan accent.
    "hm3": dict(_CLASSIC, label="HM3-like",
                window="#1b2027", base="#22282f", text="#e8edf2", button="#2b333c",
                accent="#38a1db", alt_row="#262d34", grid_line="#333b44",
                header="#151a20", header_text="#cfd8e0", row0="#2a3644",
                money_pos="#4caf72", money_neg="#ef6b6b",
                chart_bg="#161b21", chart_grid="#2e3841", chart_text="#e8edf2",
                summary_bg="#1f2830", summary_border="#38a1db"),
}

_order = ["black", "hm2", "pt4", "hm3", "classic"]
_current = dict(THEMES["black"])

QPalette = QtGui.QPalette


def names():
    """(key, label) pairs for the settings combo, in the order he asked for them."""
    return [(key, THEMES[key]["label"]) for key in _order]


def theme(key=None):
    return _current if key is None else THEMES.get(key, _current)


def load_settings():
    settings = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as handle:
            stored = json.load(handle)
    except (IOError, OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        for key in DEFAULTS:
            if key in stored and isinstance(stored[key], type(DEFAULTS[key])):
                settings[key] = stored[key]
        if "hero" not in stored and "graph_player" in stored:
            # The name that used to only seed the graph's player box became the report's identity.
            settings["hero"] = stored["graph_player"]
    if settings["theme"] not in THEMES:
        settings["theme"] = DEFAULTS["theme"]
    return settings


def save_settings(settings):
    """Write the settings; returns the path or None, so the dialog can report which happened."""
    try:
        if not os.path.isdir(Configuration.CONFIG_PATH):
            os.makedirs(Configuration.CONFIG_PATH)
        with open(SETTINGS_FILE, "w") as handle:
            json.dump(settings, handle, indent=1, sort_keys=True)
    except (IOError, OSError):
        return None
    return SETTINGS_FILE


def _palette(t):
    # PySide6 6.12 spells the roles in CamelCase and also exposes the same words lowercase as
    # accessor methods, so `QPalette.TooltipBase` is not a typo Qt tolerates: it is a method, and
    # the setColor call raises. Every name below was checked against the class.
    p = QPalette()
    window = QtGui.QColor(t["window"])
    base = QtGui.QColor(t["base"])
    text = QtGui.QColor(t["text"])
    accent = QtGui.QColor(t["accent"])
    button = QtGui.QColor(t["button"])
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, QtGui.QColor(t["alt_row"]))
    p.setColor(QPalette.ToolTipBase, base)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, button)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QtGui.QColor("#ffffff"))
    p.setColor(QPalette.Highlight, accent)
    p.setColor(QPalette.HighlightedText, QtGui.QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, text.darker(180))
    for role in (QPalette.Link, QPalette.LinkVisited):
        p.setColor(role, accent)
    p.setColor(QPalette.Light, window.darker(140))
    p.setColor(QPalette.Midlight, window.darker(105))
    p.setColor(QPalette.Mid, window.darker(130))
    p.setColor(QPalette.Dark, window.darker(160))
    p.setColor(QPalette.Shadow, window.darker(200))
    # A disabled control has to stay legible against both a slate and a near-black shell, so it is
    # a fixed step off the theme text rather than the palette's own darker() of ButtonText.
    for group in (QPalette.Disabled,):
        for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
            p.setColor(group, role, text.darker(210) if _is_dark(t) else text.lighter(180))
    return p


def _is_dark(t):
    return QtGui.QColor(t["window"]).lightness() < 128


def stylesheet(t):
    """The few colours Fusion cannot express through the palette, as object-name selectors.

    Every selector here is keyed on an objectName that HudMain sets, so no widget in this app has a
    competing inline stylesheet -- which is what would make a theme change do nothing.
    """
    dark = _is_dark(t)
    return """
QMainWindow::separator {{ background:{grid}; width:4px; height:4px; }}
QHeaderView::section {{ background:{header}; color:{header_text}; padding:4px 8px;
                       border:0; border-right:1px solid {grid}; border-bottom:1px solid {grid};
                       font-weight:bold; }}
QTableView {{ background:{base}; alternate-background-color:{alt}; gridline-color:{grid};
              border:1px solid {grid}; }}
QTableView::item:selected {{ background:{accent}; color:#ffffff; }}
QMenu {{ background:{base}; color:{text}; border:1px solid {grid}; }}
QMenu::item:selected {{ background:{accent}; color:#ffffff; }}
QMenuBar {{ background:{window}; color:{text}; }}
QMenuBar::item:selected {{ background:{accent}; color:#ffffff; }}
QStatusBar {{ background:{window}; color:{text}; }}
QToolButton, QToolBar {{ background:{window}; }}
QPushButton#modeButton {{ background:{button}; color:{text}; padding:4px 14px;
                          border:1px solid {grid}; }}
QPushButton#modeButton:checked {{ background:{accent}; color:#ffffff; font-weight:bold; }}
QPushButton#applyButton {{ background:{accent}; color:#ffffff; font-weight:bold; }}
QCheckBox#linePicker {{ color:{text}; padding-left:4px; }}
QTextEdit#graphSummary {{ background:{summary}; border:1px solid {summary_border}; padding:6px;
                          color:{text}; }}
QLabel#historyTitle {{ font-weight:bold; color:{text}; }}
QSpinBox, QComboBox, QLineEdit {{ background:{base}; color:{text};
                                  border:1px solid {grid}; padding:2px; }}
QComboBox QAbstractItemView {{ background:{base}; color:{text};
                               selection-background-color:{accent};
                               selection-color:#ffffff; }}
QSplitter::handle {{ background:{grid}; }}
QToolTip {{ background:{base}; color:{text}; border:1px solid {summary_border}; }}
""".format(base=t["base"], text=t["text"], grid=t["grid_line"], header=t["header"],
           header_text=t["header_text"], accent=t["accent"], button=t["button"],
           window=t["window"], summary=t["summary_bg"],
           summary_border=t["summary_border"], alt=t["alt_row"])


def apply(app, key):
    """Switch the whole application to theme `key`; safe to call at any time, including from a dialog."""
    global _current
    _current = dict(THEMES.get(key, THEMES["classic"]))
    _current["key"] = key
    app.setStyle("Fusion")
    app.setPalette(_palette(_current))
    app.setStyleSheet(stylesheet(_current))
    return _current


def chart_colors():
    t = _current
    return {"background": t["chart_bg"], "grid": t["chart_grid"], "text": t["chart_text"]}


def money_colors():
    t = _current
    return (QtGui.QColor(t["money_pos"]), QtGui.QColor(t["money_neg"]))


def row_banner():
    return QtGui.QColor(_current["row0"])
