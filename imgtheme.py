"""The dark theme shared by imgmacro's windows: one palette and the ttk styles."""

from tkinter import ttk

BG = "#161412"        # window background
PANEL = "#1d1a17"     # bars and side panels
CARD = "#26221e"      # nodes, fields, lists
CARD_HI = "#2f2a25"   # hovered / pressed
BORDER = "#3a342d"
TEXT = "#ece6dc"
MUTED = "#9b9387"
FAINT = "#6b645b"
ACCENT = "#d9a55b"    # gold, for the main actions and the selection
ACCENT_DARK = "#8a6a3c"
GOOD = "#5fd38a"
BAD = "#ef6b5b"
BLUE = "#5aa9e6"
PURPLE = "#a78bfa"
ORANGE = "#e8915a"

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)
FONT_TITLE = ("Segoe UI", 12, "bold")


def apply(root):
    """Style every ttk widget, and the plain tk bits ttk doesn't cover, dark."""
    root.configure(background=BG)
    for pattern, value in (("*Background", BG), ("*Foreground", TEXT),
                           ("*TCombobox*Listbox.background", CARD),
                           ("*TCombobox*Listbox.foreground", TEXT),
                           ("*TCombobox*Listbox.selectBackground", ACCENT_DARK),
                           ("*TCombobox*Listbox.selectForeground", TEXT),
                           ("*Menu.background", CARD), ("*Menu.foreground", TEXT),
                           ("*Menu.activeBackground", ACCENT_DARK),
                           ("*Menu.activeForeground", TEXT), ("*Menu.relief", "flat"),
                           ("*Menu.borderWidth", 1)):
        root.option_add(pattern, value)

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=TEXT, fieldbackground=CARD, bordercolor=BORDER,
                lightcolor=BORDER, darkcolor=BORDER, troughcolor=PANEL, focuscolor=ACCENT,
                selectbackground=ACCENT_DARK, selectforeground=TEXT, insertcolor=TEXT,
                arrowcolor=MUTED, font=FONT)
    s.map(".", foreground=[("disabled", FAINT)])
    s.configure("TFrame", background=BG)
    s.configure("Panel.TFrame", background=PANEL)
    s.configure("Card.TFrame", background=CARD)
    s.configure("TLabel", background=BG, foreground=TEXT)
    s.configure("Panel.TLabel", background=PANEL)
    s.configure("Muted.TLabel", foreground=MUTED)
    s.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
    s.configure("Title.TLabel", font=FONT_TITLE)
    s.configure("PanelTitle.TLabel", background=PANEL, font=FONT_TITLE)
    s.configure("Status.TLabel", background=PANEL, foreground=MUTED, padding=(10, 4))
    s.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid")
    s.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=FONT_SMALL)
    s.configure("Panel.TLabelframe", background=PANEL)
    s.configure("Panel.TLabelframe.Label", background=PANEL)

    s.configure("TButton", background=CARD, foreground=TEXT, bordercolor="#4a433a", lightcolor=CARD,
                darkcolor=CARD, padding=(10, 4))
    s.map("TButton", background=[("disabled", PANEL), ("pressed", BORDER), ("active", CARD_HI)],
          bordercolor=[("focus", ACCENT_DARK)])
    s.configure("Big.TButton", font=FONT_BOLD, padding=(12, 5))
    s.configure("Accent.TButton", background=ACCENT_DARK, foreground="#fff7ea", font=FONT_BOLD,
                bordercolor=ACCENT, padding=(12, 5))
    s.map("Accent.TButton", background=[("disabled", PANEL), ("pressed", ACCENT_DARK),
                                        ("active", "#a27c46")])
    s.configure("Nav.TButton", background=PANEL, foreground=MUTED, bordercolor=PANEL, padding=(14, 5))
    s.map("Nav.TButton", background=[("active", CARD)], foreground=[("active", TEXT)])
    s.configure("NavOn.TButton", background=CARD_HI, foreground=ACCENT, bordercolor=ACCENT_DARK,
                padding=(14, 5), font=FONT_BOLD)
    s.map("NavOn.TButton", background=[("active", CARD_HI)])
    s.configure("Tool.TButton", background=CARD, bordercolor="#4a433a", lightcolor=CARD,
                darkcolor=CARD, padding=(9, 4))
    s.map("Tool.TButton", background=[("disabled", PANEL), ("pressed", BORDER), ("active", CARD_HI)],
          bordercolor=[("disabled", BORDER), ("active", ACCENT_DARK)])

    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        s.configure(widget, fieldbackground=CARD, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=CARD, darkcolor=CARD, arrowcolor=MUTED, insertcolor=TEXT)
        s.map(widget, fieldbackground=[("disabled", PANEL), ("readonly", CARD)],
              foreground=[("disabled", FAINT)], bordercolor=[("focus", ACCENT_DARK)],
              lightcolor=[("focus", ACCENT_DARK)])
    s.configure("TSpinbox", arrowsize=11)
    for widget in ("TCheckbutton", "TRadiobutton"):
        s.configure(widget, background=BG, foreground=TEXT, indicatorbackground=CARD,
                    indicatorforeground=ACCENT, upperbordercolor=BORDER, lowerbordercolor=BORDER)
        s.map(widget, background=[("active", BG)], indicatorbackground=[("selected", CARD)],
              foreground=[("disabled", FAINT)])
        s.configure("Panel." + widget, background=PANEL)
        s.map("Panel." + widget, background=[("active", PANEL)])

    s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                bordercolor=BORDER, rowheight=38)
    s.map("Treeview", background=[("selected", ACCENT_DARK)], foreground=[("selected", TEXT)])
    s.configure("Treeview.Heading", background=PANEL, foreground=MUTED, bordercolor=BORDER,
                relief="flat", font=FONT_SMALL)
    s.map("Treeview.Heading", background=[("active", CARD)])
    s.configure("TScrollbar", background=CARD, troughcolor=PANEL, bordercolor=PANEL,
                arrowcolor=MUTED, lightcolor=CARD, darkcolor=CARD)
    s.map("TScrollbar", background=[("active", CARD_HI)])
    s.configure("Horizontal.TScale", background=ACCENT_DARK, troughcolor=CARD, bordercolor=BORDER,
                lightcolor=ACCENT_DARK, darkcolor=ACCENT_DARK)
    s.configure("TSeparator", background=BORDER)
    s.configure("TNotebook", background=BG, bordercolor=BORDER)
    return s
