import ctypes
import json
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox
from ctypes import wintypes

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:  # pragma: no cover - friendly message for unusual Python installs
    Image = ImageDraw = ImageTk = None


APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
CONFIG_FILE = os.path.join(APP_DIR, "settings.json")
BUNDLED_IMAGE = os.path.join(RESOURCE_DIR, "default_privacy_ai.png")
DEFAULT_IMAGE = os.path.join(APP_DIR, "default_privacy_ai.png")


def enable_per_monitor_dpi_awareness():
    """Make EnumDisplayMonitors and Tk geometry use real per-monitor pixels."""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    try:
        # Windows 10+: PER_MONITOR_AWARE_V2.
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        # Windows 8.1 fallback: PROCESS_PER_MONITOR_DPI_AWARE.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


enable_per_monitor_dpi_awareness()


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_uint),
    ]


def idle_seconds():
    """Return Windows' global keyboard/mouse idle time."""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    # DWORD wraps roughly every 49 days; unsigned arithmetic handles it.
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0


def enumerate_monitors():
    """Return one physical-monitor rectangle per Windows display.

    rcMonitor is used intentionally (not rcWork), so each overlay covers the
    complete physical display including its taskbar area.
    """
    user32 = ctypes.windll.user32
    monitors = []

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )

    def callback(handle, _hdc, _clip, _lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append({
                "left": int(rect.left),
                "top": int(rect.top),
                "right": int(rect.right),
                "bottom": int(rect.bottom),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
                "primary": bool(info.dwFlags & 1),
            })
        return 1

    callback_ref = monitor_enum_proc(callback)
    if not user32.EnumDisplayMonitors(None, None, callback_ref, 0):
        return []
    monitors.sort(key=lambda item: (item["top"], item["left"]))
    return monitors


def fit_cover(image, width, height):
    """Create a display-sized cover crop from one already-loaded source image."""
    ratio = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(width, int(round(image.width * ratio))), max(height, int(round(image.height * ratio)))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def monitor_geometry(width, height, left, top):
    """Build a Tk geometry string while preserving negative monitor coordinates."""
    x = f"+{left}" if left >= 0 else str(left)
    y = f"+{top}" if top >= 0 else str(top)
    return f"{width}x{height}{x}{y}"


def monitor_scale_percent(monitor):
    """Read effective Windows scaling for display-only UI information."""
    try:
        point = POINT(monitor["left"] + monitor["width"] // 2,
                      monitor["top"] + monitor["height"] // 2)
        handle = ctypes.windll.user32.MonitorFromPoint(point, 2)
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.windll.shcore.GetDpiForMonitor(handle, 0,
                                                 ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            return max(100, int(round(dpi_x.value / 96 * 100 / 25) * 25))
    except (AttributeError, OSError):
        pass
    return 100


class PrivacyOverlay:
    def __init__(self, app):
        self.app = app
        self.windows = []
        self.activated_idle = 0.0
        self.poll_id = None

    def show(self, image_path, opacity):
        if self.windows:
            return
        monitors = enumerate_monitors()
        if not monitors:
            return

        source_image = None
        if Image is not None and os.path.exists(image_path):
            try:
                with Image.open(image_path) as opened:
                    source_image = opened.convert("RGB")
            except Exception:
                source_image = None

        overlay_entries = []
        for monitor in monitors:
            left, top = monitor["left"], monitor["top"]
            width, height = monitor["width"], monitor["height"]
            win = tk.Toplevel(self.app)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", max(.15, min(1.0, opacity)))
            win.geometry(monitor_geometry(width, height, left, top))
            win.configure(bg="#081226", cursor="none")
            canvas = tk.Canvas(win, bg="#081226", highlightthickness=0,
                               width=width, height=height)
            canvas.pack(fill="both", expand=True)

            photo = None
            if source_image is not None:
                try:
                    # Each monitor receives an independently sized/cropped image.
                    display_image = fit_cover(source_image, width, height)
                    photo = ImageTk.PhotoImage(display_image)
                    canvas.create_image(0, 0, image=photo, anchor="nw")
                except Exception:
                    photo = None
            win.bind("<KeyPress>", lambda _e: self.hide())
            win.bind("<Button>", lambda _e: self.hide())
            win.focus_force()
            overlay_entries.append({"window": win, "canvas": canvas, "photo": photo,
                                    "monitor": monitor})

        self.windows = overlay_entries
        self.activated_idle = idle_seconds()
        self.poll_id = self.app.after(250, self.poll_activity)

    def poll_activity(self):
        if not self.windows:
            return
        # Ignore the tiny input event caused by focusing the overlay itself.
        if idle_seconds() < self.activated_idle - 0.15:
            self.hide()
            return
        if idle_seconds() < 0.45:
            self.hide()
            return
        self.poll_id = self.app.after(250, self.poll_activity)

    def hide(self):
        if self.poll_id:
            self.app.after_cancel(self.poll_id)
            self.poll_id = None
        for entry in self.windows:
            try:
                entry["window"].destroy()
            except tk.TclError:
                pass
        self.windows = []
        self.app.deiconify()
        self.app.lift()


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("隐幕 · Privacy Screen")
        self.root.geometry("1000x680")
        self.root.minsize(900, 600)
        self.root.configure(bg="#090D15")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.overlay = PrivacyOverlay(root)
        self.settings = self.load_settings()
        self.timeout = tk.IntVar(value=self.settings.get("timeout", 30))
        self.image_path = tk.StringVar(value=self.settings.get("image", DEFAULT_IMAGE))
        self.opacity = tk.DoubleVar(value=self.settings.get("opacity", .98))
        self.protection_enabled = True
        self.current_page = None
        self.nav_buttons = {}
        self.pages = {}
        self.countdown_text = tk.StringVar(value="--:--")
        self.status_title = tk.StringVar(value="隐私保护正常运行")
        self.status_detail = tk.StringVar(value="正在等待用户操作")
        self.timeout_display = tk.StringVar(value=f"{self.timeout.get()} 分钟")
        self.opacity_display = tk.StringVar(value=f"{int(self.opacity.get() * 100)}%")
        self.image_name = tk.StringVar(value=os.path.basename(self.image_path.get()))
        self.preview_ref = None
        self._preview_job = None
        self._monitor_job = None
        self._appearance_layout_job = None
        self._appearance_rebuild_job = None
        self._rebuilding_appearance = False
        self._ui_rebuild_job = None
        self._rebuilding_ui = False
        self._dpi_value = None
        self.ensure_default_image()
        self.center_window()
        self.sync_dpi_scaling()
        self.build_ui()
        self.root.bind("<Configure>", self.on_window_configure)
        self.update_preview()
        self.show_page("overview")
        self.tick()

    BG = "#090D15"
    SIDEBAR = "#0C111B"
    SURFACE = "#101722"
    SURFACE_2 = "#141D2A"
    BORDER = "#202A39"
    TEXT = "#F2F5F9"
    MUTED = "#8995A7"
    SUBTLE = "#596578"
    ACCENT = "#4D7CFE"
    ACCENT_HOVER = "#628CFF"
    SUCCESS = "#65C9A5"

    def center_window(self):
        self.root.update_idletasks()
        width, height = 1000, 680
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def sync_dpi_scaling(self):
        """Keep Tk logical units aligned with the monitor hosting the window."""
        if os.name != "nt":
            return
        try:
            dpi = int(ctypes.windll.user32.GetDpiForWindow(self.root.winfo_id()))
        except (AttributeError, OSError):
            dpi = 96
        if dpi <= 0:
            dpi = 96
        if self._dpi_value is None:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
            self._dpi_value = dpi
        elif self._dpi_value != dpi:
            # Tk does not reliably reflow already-created widgets when its
            # global scaling changes at runtime.  Apply the new scale, then
            # recreate only the appearance page so its controls get fresh
            # geometry metrics; the other pages and overlay remain untouched.
            self.root.tk.call("tk", "scaling", dpi / 72.0)
            self._dpi_value = dpi
            self.schedule_ui_rebuild()

    def on_window_configure(self, _event=None):
        self.sync_dpi_scaling()
        self.schedule_preview_refresh()
        if hasattr(self, "monitor_canvas"):
            if self._monitor_job:
                self.root.after_cancel(self._monitor_job)
            self._monitor_job = self.root.after(80, self.render_monitor_layout)

    def load_settings(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self):
        data = {"timeout": self.timeout.get(), "image": self.image_path.get(), "opacity": self.opacity.get()}
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def ensure_default_image(self):
        if not os.path.exists(DEFAULT_IMAGE) and os.path.exists(BUNDLED_IMAGE):
            try:
                with open(BUNDLED_IMAGE, "rb") as source, open(DEFAULT_IMAGE, "wb") as target:
                    target.write(source.read())
            except OSError:
                pass
        if not os.path.exists(self.image_path.get()):
            self.image_path.set(DEFAULT_IMAGE)

    def build_ui(self):
        shell = tk.Frame(self.root, bg=self.BG)
        self.shell = shell
        shell.pack(fill="both", expand=True)
        self.build_sidebar_refined(shell)
        main = tk.Frame(shell, bg=self.BG)
        main.pack(side="left", fill="both", expand=True)
        self.build_topbar_refined(main)
        self.content = tk.Frame(main, bg=self.BG)
        self.content.pack(fill="both", expand=True)
        self.build_pages_refined()
        self.build_footer_refined(main)

    # UI refinement layer. The existing Overlay, monitor enumeration and input
    # code below are intentionally left untouched; these methods only compose
    # the presentation layer around that already-tested core.
    def build_sidebar_refined(self, parent):
        sidebar = tk.Frame(parent, bg=self.SIDEBAR, width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=self.SIDEBAR)
        brand.pack(fill="x", padx=24, pady=(26, 30))
        tk.Label(brand, text="隐幕", bg=self.SIDEBAR, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(brand, text="PRIVACY SCREEN", bg=self.SIDEBAR, fg=self.SUBTLE,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(3, 0))

        nav = tk.Frame(sidebar, bg=self.SIDEBAR)
        nav.pack(fill="x", padx=12)
        for key, title in (("overview", "总览"), ("auto", "自动隐藏"),
                           ("appearance", "屏保画面"), ("monitors", "显示器"),
                           ("settings", "设置")):
            button = self.nav_button_refined(nav, title, lambda page=key: self.show_page(page))
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button
        tk.Frame(sidebar, bg=self.BORDER, height=1).pack(fill="x", padx=20, pady=18)
        tk.Label(sidebar, text="安静地保护你的屏幕", bg=self.SIDEBAR, fg=self.SUBTLE,
                 anchor="w", padx=16, font=("Microsoft YaHei UI", 8)).pack(fill="x", padx=12)

    def nav_button_refined(self, parent, text, command):
        button = tk.Button(parent, text="  " + text, command=command, anchor="w",
                           bg=self.SIDEBAR, fg=self.MUTED,
                           activebackground=self.SURFACE_2, activeforeground=self.TEXT,
                           relief="flat", bd=0, padx=9, pady=10, cursor="hand2",
                           font=("Microsoft YaHei UI", 10))
        button._nav_text = text
        return button

    def build_topbar_refined(self, parent):
        topbar = tk.Frame(parent, bg=self.BG, height=72)
        topbar.pack(fill="x", padx=36)
        topbar.pack_propagate(False)
        self.top_title = tk.Label(topbar, text="总览", bg=self.BG, fg=self.TEXT,
                                  font=("Microsoft YaHei UI", 17, "bold"))
        self.top_title.pack(side="left", pady=(2, 0))
        self.header_status = tk.Label(topbar, text="●  正常运行", bg=self.BG, fg=self.SUCCESS,
                                      font=("Microsoft YaHei UI", 9, "bold"))
        self.header_status.pack(side="right")

    def build_footer_refined(self, parent):
        footer = tk.Frame(parent, bg=self.BG, height=38)
        footer.pack(fill="x", padx=36)
        tk.Label(footer, text="隐幕 · Privacy Screen", bg=self.BG, fg=self.SUBTLE,
                 font=("Segoe UI", 8)).pack(side="left")

    def build_pages_refined(self):
        builders = {
            "overview": self.build_overview_refined,
            "auto": self.build_auto_refined,
            "appearance": self.build_appearance_refined,
            "monitors": self.build_monitors_refined,
            "settings": self.build_settings_refined,
        }
        for key, builder in builders.items():
            page = tk.Frame(self.content, bg=self.BG)
            self.pages[key] = page
            builder(page)

    def page_subtitle(self, parent, text):
        tk.Label(parent, text=text, bg=self.BG, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9), wraplength=720,
                 justify="left").pack(anchor="w", padx=36, pady=(0, 24))

    def refined_section(self, parent):
        return tk.Frame(parent, bg=self.SURFACE, highlightthickness=1,
                        highlightbackground=self.BORDER)

    def refined_button(self, parent, text, command, primary=False, quiet=False):
        bg = self.ACCENT if primary else (self.BG if quiet else self.SURFACE_2)
        fg = "#FFFFFF" if primary else (self.MUTED if quiet else self.TEXT)
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=self.ACCENT_HOVER if primary else self.BORDER,
                         activeforeground="#FFFFFF", relief="flat", bd=0, cursor="hand2",
                         padx=17, pady=9, font=("Microsoft YaHei UI", 9, "bold"))

    def schedule_appearance_rebuild(self):
        if not hasattr(self, "pages") or "appearance" not in self.pages:
            return
        if self._appearance_rebuild_job:
            try:
                self.root.after_cancel(self._appearance_rebuild_job)
            except tk.TclError:
                pass
        self._appearance_rebuild_job = self.root.after_idle(self.rebuild_appearance_page)

    def schedule_ui_rebuild(self):
        if not hasattr(self, "shell") or self._rebuilding_ui:
            return
        if self._ui_rebuild_job:
            try:
                self.root.after_cancel(self._ui_rebuild_job)
            except tk.TclError:
                pass
        self._ui_rebuild_job = self.root.after_idle(self.rebuild_ui_for_dpi)

    def rebuild_ui_for_dpi(self):
        """Recreate the settings UI so Tk recalculates all DPI metrics."""
        self._ui_rebuild_job = None
        if self._rebuilding_ui or not hasattr(self, "shell"):
            return
        self._rebuilding_ui = True
        current_page = self.current_page if self.current_page in self.pages else "overview"
        for attr in ("_appearance_layout_job", "_appearance_rebuild_job", "_preview_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        self.shell.destroy()
        self.nav_buttons = {}
        self.pages = {}
        self.current_page = None
        self.preview_ref = None
        self.build_ui()
        self.show_page(current_page)
        self._rebuilding_ui = False

    def rebuild_appearance_page(self):
        self._appearance_rebuild_job = None
        if self._rebuilding_appearance or "appearance" not in self.pages:
            return
        self._rebuilding_appearance = True
        was_visible = self.current_page == "appearance"
        old_page = self.pages["appearance"]
        old_page.destroy()
        page = tk.Frame(self.content, bg=self.BG)
        self.pages["appearance"] = page
        self.build_appearance_refined(page)
        if was_visible:
            page.pack(fill="both", expand=True)
        self._rebuilding_appearance = False
        if was_visible:
            self.schedule_appearance_layout()
            self.schedule_preview_refresh()

    def schedule_preview_refresh(self, delay=80):
        if self._preview_job:
            try:
                self.root.after_cancel(self._preview_job)
            except tk.TclError:
                pass
        self._preview_job = self.root.after(delay, self.update_preview)

    def build_overview_refined(self, page):
        self.page_subtitle(page, "隐幕正在后台监测用户操作，后台程序不会被暂停。")
        status = self.refined_section(page)
        status.pack(fill="x", padx=36)
        left = tk.Frame(status, bg=self.SURFACE)
        left.pack(side="left", fill="both", expand=True, padx=28, pady=26)
        tk.Label(left, textvariable=self.status_title, bg=self.SURFACE, fg=self.SUCCESS,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        tk.Label(left, textvariable=self.status_detail, bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(7, 0))
        right = tk.Frame(status, bg=self.SURFACE)
        right.pack(side="right", padx=34, pady=22)
        tk.Label(right, text="距离自动隐藏", bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="e")
        tk.Label(right, textvariable=self.countdown_text, bg=self.SURFACE, fg=self.TEXT,
                 font=("Segoe UI Variable Display", 29, "bold")).pack(anchor="e", pady=(3, 0))

        info = tk.Frame(page, bg=self.BG)
        info.pack(fill="x", padx=36, pady=18)
        self.metric_refined(info, "自动屏保", self.timeout_display).pack(side="left", fill="x", expand=True, padx=(0, 8))
        monitor_count = tk.StringVar(value=f"{len(enumerate_monitors())} 个")
        self.metric_refined(info, "已连接显示器", monitor_count).pack(side="left", fill="x", expand=True, padx=8)
        self.metric_refined(info, "屏幕模式", tk.StringVar(value="独立覆盖")).pack(side="left", fill="x", expand=True, padx=(8, 0))

        actions = tk.Frame(page, bg=self.BG)
        actions.pack(fill="x", padx=36, pady=(8, 0))
        self.refined_button(actions, "立即预览", self.test_overlay, primary=True).pack(side="left")
        self.refined_button(actions, "立即进入屏保", self.test_overlay).pack(side="left", padx=10)
        self.pause_button = tk.Button(actions, text="暂停保护", command=self.toggle_protection,
                                      bg=self.BG, fg=self.MUTED, activebackground=self.BG,
                                      activeforeground=self.TEXT, relief="flat", bd=0,
                                      cursor="hand2", padx=4, pady=9,
                                      font=("Microsoft YaHei UI", 9))
        self.pause_button.pack(side="left")

    def metric_refined(self, parent, label, variable):
        box = tk.Frame(parent, bg=self.BG, padx=2, pady=8)
        tk.Label(box, text=label, bg=self.BG, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Label(box, textvariable=variable, bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(5, 0))
        return box

    def build_auto_refined(self, page):
        self.page_subtitle(page, "当电脑一段时间没有检测到键盘或鼠标操作时，自动显示隐私屏幕。")
        body = self.refined_section(page)
        body.pack(fill="x", padx=36)
        tk.Label(body, text="自动进入屏保", bg=self.SURFACE, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=26, pady=(24, 4))
        tk.Label(body, textvariable=self.timeout_display, bg=self.SURFACE, fg=self.ACCENT,
                 font=("Segoe UI Variable Display", 24, "bold")).pack(anchor="w", padx=26)
        presets = tk.Frame(body, bg=self.SURFACE)
        presets.pack(fill="x", padx=26, pady=(22, 16))
        self.preset_buttons = {}
        for value in (5, 10, 15, 30, 45, 60):
            button = tk.Button(presets, text=f"{value} 分钟", command=lambda v=value: self.set_timeout(v),
                               bg=self.SURFACE_2, fg=self.MUTED, activebackground=self.BORDER,
                               activeforeground=self.TEXT, relief="flat", bd=0, cursor="hand2",
                               padx=13, pady=9, font=("Microsoft YaHei UI", 9))
            button.pack(side="left", padx=(0, 7))
            self.preset_buttons[value] = button
        custom = tk.Frame(body, bg=self.SURFACE)
        custom.pack(fill="x", padx=26, pady=(0, 24))
        tk.Label(custom, text="自定义", bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.custom_timeout = tk.Entry(custom, bg=self.BG, fg=self.TEXT, insertbackground=self.TEXT,
                                       relief="flat", bd=0, width=6, justify="center",
                                       font=("Segoe UI", 11))
        self.custom_timeout.insert(0, str(self.timeout.get()))
        self.custom_timeout.pack(side="left", padx=(12, 6), ipady=8)
        tk.Label(custom, text="分钟", bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.refined_button(custom, "应用", self.apply_custom_timeout).pack(side="left", padx=14)
        self.refresh_preset_buttons()

    def build_appearance_refined(self, page):
        self.page_subtitle(page, "选择一张安静的背景图片，隐幕会按每台显示器的分辨率独立裁剪。")
        body = tk.Frame(page, bg=self.BG)
        body.pack(fill="both", expand=True, padx=36)
        body.grid_columnconfigure(0, weight=1, minsize=0)
        body.grid_columnconfigure(1, weight=0, minsize=220)
        body.grid_rowconfigure(0, weight=1)
        self.appearance_body = body
        preview_box = self.refined_section(body)
        preview_box.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        self.preview = tk.Label(preview_box, bg="#050810")
        self.preview.pack(fill="both", expand=True, padx=14, pady=14)
        controls = tk.Frame(body, bg=self.BG, width=250)
        controls.grid(row=0, column=1, sticky="nsew")
        controls.grid_propagate(False)
        self.appearance_controls = controls
        tk.Label(controls, text="当前图片", bg=self.BG, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Label(controls, textvariable=self.image_name, bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI", 9, "bold"), wraplength=230,
                 justify="left").pack(anchor="w", pady=(6, 14))
        self.refined_button(controls, "更换图片", self.choose_image).pack(fill="x")
        self.refined_button(controls, "恢复默认", self.restore_default, quiet=True).pack(fill="x", pady=(8, 0))
        tk.Frame(controls, bg=self.BORDER, height=1).pack(fill="x", pady=22)
        tk.Label(controls, text="显示方式", bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        modes = tk.Frame(controls, bg=self.BG)
        modes.pack(fill="x", pady=(10, 20))
        for label, selected in (("填充", True), ("适应", False), ("原始比例", False)):
            tk.Label(modes, text=("● " if selected else "○ ") + label, bg=self.BG,
                     fg=self.ACCENT if selected else self.SUBTLE,
                     font=("Microsoft YaHei UI", 9, "bold" if selected else "normal")).pack(anchor="w", pady=2)
        opacity_row = tk.Frame(controls, bg=self.BG)
        opacity_row.pack(fill="x")
        tk.Label(opacity_row, text="屏保不透明度", bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        tk.Label(opacity_row, textvariable=self.opacity_display, bg=self.BG, fg=self.ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(side="right")
        self.opacity_scale = tk.Scale(controls, from_=35, to=100, orient="horizontal", showvalue=False,
                                      command=self.change_opacity, bg=self.BG, troughcolor=self.BORDER,
                                      activebackground=self.ACCENT, highlightthickness=0, bd=0,
                                      sliderrelief="flat", length=240)
        self.opacity_scale.set(self.opacity.get() * 100)
        self.opacity_scale.pack(fill="x", pady=(9, 0))
        tk.Label(controls, text="支持 JPG、JPEG、PNG、WEBP", bg=self.BG, fg=self.SUBTLE,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(16, 0))

    def build_monitors_refined(self, page):
        self.page_subtitle(page, "每台显示器将独立显示隐私画面，不会出现跨屏拉伸。")
        top = tk.Frame(page, bg=self.BG)
        top.pack(fill="x", padx=36)
        self.monitor_summary = tk.Label(top, bg=self.BG, fg=self.TEXT,
                                        font=("Microsoft YaHei UI", 11, "bold"))
        self.monitor_summary.pack(side="left")
        status = tk.Label(top, text="● 独立覆盖模式", bg=self.BG, fg=self.SUCCESS,
                          font=("Microsoft YaHei UI", 9, "bold"))
        status.pack(side="left", padx=18)
        self.refined_button(top, "重新检测", self.render_monitor_layout, quiet=True).pack(side="right")
        self.monitor_canvas = tk.Canvas(page, bg=self.SURFACE, highlightthickness=1,
                                        highlightbackground=self.BORDER, bd=0)
        self.monitor_canvas.pack(fill="both", expand=True, padx=36, pady=(16, 8))
        self.monitor_canvas.bind("<Configure>", lambda _e: self.render_monitor_layout())
        tk.Label(page, text="每台显示器将独立显示隐私画面。", bg=self.BG, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=36, pady=(0, 8))

    def build_settings_refined(self, page):
        self.page_subtitle(page, "隐幕的设置保持简单，只保留当前可用的功能。")
        body = self.refined_section(page)
        body.pack(fill="x", padx=36)
        self.setting_row_refined(body, "自动隐藏", "已启用", "")
        self.separator_refined(body)
        self.setting_row_refined(body, "快速进入屏保", "点击总览页的“立即进入屏保”", "")
        self.separator_refined(body)
        self.setting_row_refined(body, "当前版本", "Version 1.0.0", "")
        about = tk.Frame(page, bg=self.BG)
        about.pack(fill="x", padx=36, pady=(28, 0))
        tk.Label(about, text="隐幕", bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        tk.Label(about, text="Privacy Screen  ·  Version 1.0.0", bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(5, 0))

    def setting_row_refined(self, parent, title, subtitle, _state):
        row = tk.Frame(parent, bg=self.SURFACE)
        row.pack(fill="x", padx=24, pady=16)
        tk.Label(row, text=title, bg=self.SURFACE, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(row, text=subtitle, bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(4, 0))

    def separator_refined(self, parent):
        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", padx=24)

    def build_sidebar(self, parent):
        sidebar = tk.Frame(parent, bg=self.SIDEBAR, width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=self.SIDEBAR)
        brand.pack(fill="x", padx=24, pady=(26, 30))
        tk.Label(brand, text="隐幕", bg=self.SIDEBAR, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(brand, text="PRIVACY SCREEN", bg=self.SIDEBAR, fg=self.SUBTLE,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(3, 0))

        nav = tk.Frame(sidebar, bg=self.SIDEBAR)
        nav.pack(fill="x", padx=12)
        for key, title in [("overview", "总览"), ("auto", "自动隐藏"),
                           ("appearance", "屏保画面"), ("monitors", "显示器"),
                           ("settings", "设置")]:
            button = self.nav_button(nav, title, lambda page=key: self.show_page(page))
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button
        tk.Frame(sidebar, height=1, bg=self.BORDER).pack(fill="x", padx=20, pady=18)
        about = self.nav_button(sidebar, "关于", lambda: self.show_page("about"))
        about.pack(fill="x", padx=12)
        self.nav_buttons["about"] = about

    def nav_button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, anchor="w",
                         bg=self.SIDEBAR, fg=self.MUTED,
                         activebackground=self.SURFACE_2, activeforeground=self.TEXT,
                         relief="flat", bd=0, padx=16, pady=11, cursor="hand2",
                         font=("Microsoft YaHei UI", 10))

    def build_topbar(self, parent):
        topbar = tk.Frame(parent, bg=self.BG, height=72)
        topbar.pack(fill="x", padx=36)
        topbar.pack_propagate(False)
        self.top_title = tk.Label(topbar, text="总览", bg=self.BG, fg=self.TEXT,
                                  font=("Microsoft YaHei UI", 16, "bold"))
        self.top_title.pack(side="left")
        self.header_status = tk.Label(topbar, text="●  正常运行", bg=self.BG, fg=self.SUCCESS,
                                      font=("Microsoft YaHei UI", 9, "bold"))
        self.header_status.pack(side="right")

    def build_footer(self, parent):
        footer = tk.Frame(parent, bg=self.BG, height=38)
        footer.pack(fill="x", padx=36)
        tk.Label(footer, text="Privacy Screen", bg=self.BG, fg=self.SUBTLE,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Label(footer, text="v1.0.0", bg=self.BG, fg=self.SUBTLE,
                 font=("Segoe UI", 8)).pack(side="right")

    def build_pages(self):
        builders = {
            "overview": self.build_overview_page,
            "auto": self.build_auto_page,
            "appearance": self.build_appearance_page,
            "monitors": self.build_monitors_page,
            "settings": self.build_settings_page,
            "about": self.build_about_page,
        }
        for key, builder in builders.items():
            page = tk.Frame(self.content, bg=self.BG)
            self.pages[key] = page
            builder(page)

    def page_intro(self, parent, title, subtitle):
        block = tk.Frame(parent, bg=self.BG)
        block.pack(fill="x", padx=36, pady=(10, 26))
        tk.Label(block, text=title, bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w")
        tk.Label(block, text=subtitle, bg=self.BG, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9), wraplength=650, justify="left").pack(anchor="w", pady=(7, 0))

    def section(self, parent):
        return tk.Frame(parent, bg=self.SURFACE, highlightthickness=1,
                        highlightbackground=self.BORDER)

    def action_button(self, parent, text, command, primary=False, subtle=False):
        bg = self.ACCENT if primary else (self.BG if subtle else self.SURFACE_2)
        fg = "#FFFFFF" if primary else (self.MUTED if subtle else self.TEXT)
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=self.ACCENT_HOVER if primary else self.BORDER,
                         activeforeground="#FFFFFF", relief="flat", bd=0, cursor="hand2",
                         padx=18, pady=10, font=("Microsoft YaHei UI", 9, "bold"))

    def build_overview_page(self, page):
        self.page_intro(page, "一切正常", "隐幕正在后台监测用户操作，后台程序不会被暂停。")
        status = self.section(page)
        status.pack(fill="x", padx=36)
        left = tk.Frame(status, bg=self.SURFACE)
        left.pack(side="left", fill="both", expand=True, padx=28, pady=26)
        tk.Label(left, textvariable=self.status_title, bg=self.SURFACE, fg=self.SUCCESS,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(left, textvariable=self.status_detail, bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(7, 0))
        right = tk.Frame(status, bg=self.SURFACE)
        right.pack(side="right", padx=34, pady=22)
        tk.Label(right, text="距离自动隐藏", bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="e")
        tk.Label(right, textvariable=self.countdown_text, bg=self.SURFACE, fg=self.TEXT,
                 font=("Segoe UI Variable Display", 28, "bold")).pack(anchor="e", pady=(3, 0))

        info = tk.Frame(page, bg=self.BG)
        info.pack(fill="x", padx=36, pady=18)
        self.metric(info, "自动屏保", self.timeout_display).pack(side="left", fill="x", expand=True, padx=(0, 8))
        monitor_count = tk.StringVar(value=f"{len(enumerate_monitors())} 个")
        self.metric(info, "已连接显示器", monitor_count).pack(side="left", fill="x", expand=True, padx=8)
        mode = tk.StringVar(value="独立覆盖")
        self.metric(info, "屏幕模式", mode).pack(side="left", fill="x", expand=True, padx=(8, 0))

        actions = tk.Frame(page, bg=self.BG)
        actions.pack(fill="x", padx=36, pady=(8, 0))
        self.action_button(actions, "立即预览", self.test_overlay, primary=True).pack(side="left")
        self.pause_button = self.action_button(actions, "暂停保护", self.toggle_protection)
        self.pause_button.pack(side="left", padx=10)
        self.action_button(actions, "立即进入屏保", self.test_overlay, subtle=True).pack(side="left")

    def metric(self, parent, label, variable):
        box = tk.Frame(parent, bg=self.BG, padx=2, pady=8)
        tk.Label(box, text=label, bg=self.BG, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Label(box, textvariable=variable, bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(5, 0))
        return box

    def build_auto_page(self, page):
        self.page_intro(page, "自动隐藏", "当电脑一段时间没有检测到键盘或鼠标操作时，自动显示隐私屏幕。")
        body = self.section(page)
        body.pack(fill="x", padx=36)
        tk.Label(body, text="自动进入屏保", bg=self.SURFACE, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=26, pady=(24, 4))
        tk.Label(body, textvariable=self.timeout_display, bg=self.SURFACE, fg=self.ACCENT,
                 font=("Segoe UI Variable Display", 24, "bold")).pack(anchor="w", padx=26)
        presets = tk.Frame(body, bg=self.SURFACE)
        presets.pack(fill="x", padx=26, pady=(22, 16))
        self.preset_buttons = {}
        for value in (5, 10, 15, 30, 45, 60):
            button = tk.Button(presets, text=f"{value} 分钟", command=lambda v=value: self.set_timeout(v),
                               bg=self.SURFACE_2, fg=self.MUTED, activebackground=self.BORDER,
                               activeforeground=self.TEXT, relief="flat", bd=0, cursor="hand2",
                               padx=13, pady=9, font=("Microsoft YaHei UI", 9))
            button.pack(side="left", padx=(0, 7))
            self.preset_buttons[value] = button
        custom = tk.Frame(body, bg=self.SURFACE)
        custom.pack(fill="x", padx=26, pady=(0, 24))
        tk.Label(custom, text="自定义", bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.custom_timeout = tk.Entry(custom, bg=self.BG, fg=self.TEXT, insertbackground=self.TEXT,
                                       relief="flat", bd=0, width=8, justify="center",
                                       font=("Segoe UI", 11))
        self.custom_timeout.insert(0, str(self.timeout.get()))
        self.custom_timeout.pack(side="left", padx=(12, 6), ipady=8)
        tk.Label(custom, text="分钟", bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.action_button(custom, "应用", self.apply_custom_timeout).pack(side="left", padx=14)
        self.refresh_preset_buttons()

    def build_appearance_page(self, page):
        self.page_intro(page, "屏保画面", "所有显示器使用同一张图片，并针对各自分辨率独立裁剪。")
        body = tk.Frame(page, bg=self.BG)
        body.pack(fill="both", expand=True, padx=36)
        preview_box = self.section(body)
        preview_box.pack(side="left", fill="both", expand=True, padx=(0, 18))
        self.preview = tk.Label(preview_box, bg="#050810")
        self.preview.pack(fill="both", expand=True, padx=14, pady=14)
        controls = tk.Frame(body, bg=self.BG, width=260)
        controls.pack(side="right", fill="y")
        controls.pack_propagate(False)
        tk.Label(controls, text="当前图片", bg=self.BG, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Label(controls, textvariable=self.image_name, bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 10, "bold"), wraplength=250,
                 justify="left").pack(anchor="w", pady=(6, 16))
        self.action_button(controls, "更换图片", self.choose_image).pack(fill="x")
        self.action_button(controls, "恢复默认图片", self.restore_default, subtle=True).pack(fill="x", pady=(8, 0))
        tk.Frame(controls, bg=self.BORDER, height=1).pack(fill="x", pady=24)
        opacity_row = tk.Frame(controls, bg=self.BG)
        opacity_row.pack(fill="x")
        tk.Label(opacity_row, text="屏保不透明度", bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        tk.Label(opacity_row, textvariable=self.opacity_display, bg=self.BG, fg=self.ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(side="right")
        self.opacity_scale = tk.Scale(controls, from_=35, to=100, orient="horizontal", showvalue=False,
                                      command=self.change_opacity, bg=self.BG, troughcolor=self.BORDER,
                                      activebackground=self.ACCENT, highlightthickness=0, bd=0,
                                      sliderrelief="flat", length=250)
        self.opacity_scale.set(self.opacity.get() * 100)
        self.opacity_scale.pack(fill="x", pady=(9, 0))
        tk.Label(controls, text="支持 JPG、JPEG、PNG、WEBP", bg=self.BG, fg=self.SUBTLE,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(18, 0))

    def build_monitors_page(self, page):
        self.page_intro(page, "显示器", "每个物理显示器拥有独立的全屏遮罩和图片渲染。")
        top = tk.Frame(page, bg=self.BG)
        top.pack(fill="x", padx=36)
        self.monitor_summary = tk.Label(top, bg=self.BG, fg=self.TEXT,
                                        font=("Microsoft YaHei UI", 11, "bold"))
        self.monitor_summary.pack(side="left")
        self.action_button(top, "重新检测", self.render_monitor_layout, subtle=True).pack(side="right")
        self.monitor_canvas = tk.Canvas(page, bg=self.SURFACE, highlightthickness=1,
                                        highlightbackground=self.BORDER, bd=0)
        self.monitor_canvas.pack(fill="both", expand=True, padx=36, pady=(16, 12))
        self.monitor_canvas.bind("<Configure>", lambda _e: self.render_monitor_layout())

    def build_settings_page(self, page):
        self.page_intro(page, "设置", "保持简洁、稳定；系统集成功能将在后续阶段实现。")
        body = self.section(page)
        body.pack(fill="x", padx=36)
        self.setting_row(body, "开机启动", "尚未启用", "后续版本")
        self.separator(body)
        self.setting_row(body, "启动后最小化到托盘", "系统托盘尚未实现", "后续版本")
        self.separator(body)
        self.setting_row(body, "快速进入屏保", "Ctrl + Alt + P", "尚未启用")
        self.separator(body)
        self.setting_row(body, "当前版本", "隐幕 Privacy Screen", "v1.0.0")

    def setting_row(self, parent, title, subtitle, state):
        row = tk.Frame(parent, bg=self.SURFACE)
        row.pack(fill="x", padx=24, pady=18)
        text = tk.Frame(row, bg=self.SURFACE)
        text.pack(side="left")
        tk.Label(text, text=title, bg=self.SURFACE, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(text, text=subtitle, bg=self.SURFACE, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(4, 0))
        tk.Label(row, text=state, bg=self.SURFACE_2, fg=self.MUTED, padx=11, pady=6,
                 font=("Microsoft YaHei UI", 8)).pack(side="right")

    def separator(self, parent):
        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", padx=24)

    def build_about_page(self, page):
        self.page_intro(page, "关于隐幕", "Quiet · Private · Minimal")
        body = tk.Frame(page, bg=self.BG)
        body.pack(fill="both", expand=True, padx=36, pady=24)
        tk.Label(body, text="隐幕", bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 30, "bold")).pack(anchor="center", pady=(30, 0))
        tk.Label(body, text="Privacy Screen", bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 12)).pack(anchor="center", pady=(5, 18))
        tk.Label(body, text="一个安静、可靠的 Windows 隐私屏幕保护工具。\n只遮住屏幕，不锁定电脑，不影响后台任务。",
                 bg=self.BG, fg=self.MUTED, justify="center",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="center")
        tk.Label(body, text="v1.0.0", bg=self.BG, fg=self.SUBTLE,
                 font=("Segoe UI", 9)).pack(anchor="center", pady=24)

    def show_page(self, key):
        if self.current_page == key:
            return
        if self.current_page:
            self.pages[self.current_page].pack_forget()
            old_button = self.nav_buttons[self.current_page]
            old_button.configure(text="  " + old_button._nav_text,
                                 bg=self.SIDEBAR, fg=self.MUTED,
                                 font=("Microsoft YaHei UI", 10))
        self.current_page = key
        titles = {"overview": "总览", "auto": "自动隐藏", "appearance": "屏保画面",
                  "monitors": "显示器", "settings": "设置", "about": "关于"}
        self.top_title.configure(text=titles[key])
        active_button = self.nav_buttons[key]
        active_button.configure(text="▎ " + active_button._nav_text,
                                bg=self.SURFACE_2, fg=self.TEXT,
                                font=("Microsoft YaHei UI", 10, "bold"))
        self.pages[key].pack(fill="both", expand=True)
        if key == "appearance":
            self.schedule_preview_refresh()
        elif key == "monitors":
            self.root.after(40, self.render_monitor_layout)

    def change_opacity(self, value):
        self.opacity.set(float(value) / 100)
        self.opacity_display.set(f"{int(float(value))}%")
        self.save_settings()

    def update_preview(self):
        self._preview_job = None
        if not hasattr(self, "preview") or Image is None or not os.path.exists(self.image_path.get()):
            return
        try:
            width = max(1, self.preview.winfo_width() - 8)
            height = max(1, self.preview.winfo_height() - 8)
            if width < 80 or height < 60:
                return
            with Image.open(self.image_path.get()) as source:
                image = source.convert("RGB")
            # Cover the available preview area while preserving aspect ratio;
            # this mirrors the already-verified per-monitor overlay behavior.
            image = fit_cover(image, width, height)
            self.preview_ref = ImageTk.PhotoImage(image)
            self.preview.configure(image=self.preview_ref)
        except Exception:
            pass

    def choose_image(self):
        path = filedialog.askopenfilename(title="选择屏保图片", filetypes=[
            ("支持的图片", "*.png *.jpg *.jpeg *.webp"), ("所有文件", "*.*")])
        if path:
            self.image_path.set(path)
            self.image_name.set(os.path.basename(path))
            self.save_settings()
            self.update_preview()

    def restore_default(self):
        self.image_path.set(DEFAULT_IMAGE)
        self.image_name.set(os.path.basename(DEFAULT_IMAGE))
        self.save_settings()
        self.update_preview()

    def set_timeout(self, value):
        self.timeout.set(value)
        self.timeout_display.set(f"{value} 分钟")
        if hasattr(self, "custom_timeout"):
            self.custom_timeout.delete(0, "end")
            self.custom_timeout.insert(0, str(value))
        self.refresh_preset_buttons()
        self.save_settings()

    def apply_custom_timeout(self):
        try:
            value = max(1, min(1440, int(self.custom_timeout.get())))
        except ValueError:
            self.custom_timeout.delete(0, "end")
            self.custom_timeout.insert(0, str(self.timeout.get()))
            return
        self.set_timeout(value)

    def refresh_preset_buttons(self):
        if not hasattr(self, "preset_buttons"):
            return
        for value, button in self.preset_buttons.items():
            selected = value == self.timeout.get()
            button.configure(bg=self.ACCENT if selected else self.SURFACE_2,
                             fg="#FFFFFF" if selected else self.MUTED)

    def toggle_protection(self):
        self.protection_enabled = not self.protection_enabled
        if self.protection_enabled:
            self.status_title.set("正在保护")
            self.status_detail.set("正在等待用户操作")
            self.header_status.configure(text="●  正常运行", fg=self.SUCCESS)
            self.pause_button.configure(text="暂停保护")
        else:
            self.status_title.set("保护已暂停")
            self.status_detail.set("自动隐私屏幕暂时不会启动")
            self.header_status.configure(text="●  已暂停", fg="#E5B567")
            self.pause_button.configure(text="恢复保护")

    def render_monitor_layout(self):
        if not hasattr(self, "monitor_canvas"):
            return
        monitors = enumerate_monitors()
        self.monitor_summary.configure(text=f"检测到 {len(monitors)} 个显示器")
        canvas = self.monitor_canvas
        canvas.delete("all")
        if not monitors:
            canvas.create_text(canvas.winfo_width() // 2, canvas.winfo_height() // 2,
                               text="没有检测到显示器", fill=self.MUTED,
                               font=("Microsoft YaHei UI", 10))
            return
        min_left = min(m["left"] for m in monitors)
        min_top = min(m["top"] for m in monitors)
        max_right = max(m["right"] for m in monitors)
        max_bottom = max(m["bottom"] for m in monitors)
        total_w, total_h = max_right - min_left, max_bottom - min_top
        cw, ch = max(100, canvas.winfo_width()), max(100, canvas.winfo_height())
        scale = min((cw - 80) / max(1, total_w), (ch - 80) / max(1, total_h))
        offset_x = (cw - total_w * scale) / 2
        offset_y = (ch - total_h * scale) / 2
        for index, monitor in enumerate(monitors, 1):
            x1 = offset_x + (monitor["left"] - min_left) * scale
            y1 = offset_y + (monitor["top"] - min_top) * scale
            x2 = x1 + monitor["width"] * scale
            y2 = y1 + monitor["height"] * scale
            canvas.create_rectangle(x1, y1, x2, y2, fill=self.SURFACE_2,
                                    outline=self.ACCENT if monitor.get("primary") else self.BORDER,
                                    width=2 if monitor.get("primary") else 1)
            canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 9,
                               text=f"显示器 {index}", fill=self.TEXT,
                               font=("Microsoft YaHei UI", 10, "bold"))
            canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2 + 13,
                               text=f"{monitor['width']} × {monitor['height']}", fill=self.MUTED,
                               font=("Segoe UI", 9))
            canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2 + 31,
                               text=f"缩放 {monitor_scale_percent(monitor)}%", fill=self.SUBTLE,
                               font=("Segoe UI", 8))
            if monitor.get("primary"):
                canvas.create_text(x1 + 10, y1 + 9, text="主显示器", anchor="nw",
                                   fill=self.ACCENT, font=("Microsoft YaHei UI", 7, "bold"))

    def test_overlay(self):
        self.save_settings()
        self.root.withdraw()
        self.overlay.show(self.image_path.get(), self.opacity.get())

    def tick(self):
        idle = idle_seconds()
        timeout_seconds = max(1, self.timeout.get()) * 60
        remaining = max(0, int(timeout_seconds - idle))
        hours, remainder = divmod(remaining, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.countdown_text.set(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        if self.protection_enabled and not self.overlay.windows and idle >= timeout_seconds:
            self.root.withdraw()
            self.overlay.show(self.image_path.get(), self.opacity.get())
        self.root.after(1000, self.tick)


if __name__ == "__main__":
    if os.name != "nt":
        print("此程序需要在 Windows 上运行。")
    root = tk.Tk()
    App(root)
    root.mainloop()
