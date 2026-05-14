"""
main.py — Soccer Stars Analyzer  (Android entry point)
=======================================================
Kivy 2.3.0 application.

Responsibilities
----------------
1. Request all Android runtime permissions on first launch.
2. Present a MediaProjection consent dialog (screen-capture permission).
3. Home screen  : Start/Stop overlay, Auto-detect ON/OFF, HSV Settings.
4. Settings screen (HSVTunerScreen): live colour calibration for the engine.
5. Manage FloatingOverlayManager lifetime and propagate app pause/resume
   to the background capture service (battery-saving hibernate/wake cycle).

Compatibility
-------------
- Kivy 2.3.0   (Python 3.11, arm64-v8a)
- Android API 26–34
- Degrades gracefully on desktop (skips all Android-specific code paths).

Run on device
-------------
    buildozer android debug deploy run
"""

from __future__ import annotations
import os
import json

from kivy.app              import App
from kivy.uix.boxlayout   import BoxLayout
from kivy.uix.button      import Button
from kivy.uix.label       import Label
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.popup       import Popup
from kivy.logger          import Logger

IS_ANDROID = (
    os.environ.get("ANDROID_ARGUMENT") is not None
    or os.path.exists("/system/build.prop")
)

if IS_ANDROID:
    from android.permissions import request_permissions           # type: ignore
    from android             import activity as _android_activity # type: ignore
    from jnius               import autoclass, cast               # type: ignore

    PythonActivity = autoclass("org.kivy.android.PythonActivity")

from hsv_tuner import HSVTunerScreen
from overlay   import FloatingOverlayManager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFS_FILE      = "hsv_prefs.json"
MP_REQUEST_CODE = 100

REQUIRED_PERMISSIONS = [
    "android.permission.SYSTEM_ALERT_WINDOW",
    "android.permission.FOREGROUND_SERVICE",
    "android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION",
] if IS_ANDROID else []


# ---------------------------------------------------------------------------
# Home Screen
# ---------------------------------------------------------------------------

class HomeScreen(Screen):
    """
    Three controls on the home screen
    ----------------------------------
    Start / Stop Overlay   — launches or destroys the floating overlay.
    Auto-detect ON / OFF   — toggles the 'your turn' auto-detector.
    HSV Colour Settings    — navigates to the tuner screen.
    """

    def __init__(self, app_ref: "SoccerStarsApp", **kwargs):
        super().__init__(name="home", **kwargs)
        self._app            = app_ref
        self._overlay_active = False
        self._auto_detect    = True
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = BoxLayout(orientation="vertical", padding=24, spacing=14)

        root.add_widget(Label(
            text="[b]Soccer Stars Analyzer[/b]",
            markup=True, font_size="20sp",
            size_hint=(1, 0.09), halign="center",
        ))

        self._status = Label(
            text="Overlay: OFF  |  Auto-detect: ON",
            font_size="12sp", size_hint=(1, 0.07),
            halign="center", color=(0.7, 0.7, 0.7, 1),
        )
        root.add_widget(self._status)

        # Overlay toggle
        self._ov_btn = Button(
            text="Start Overlay", font_size="16sp",
            size_hint=(1, 0.17),
            background_color=(0.18, 0.76, 0.28, 1),
        )
        self._ov_btn.bind(on_release=self._toggle_overlay)
        root.add_widget(self._ov_btn)

        # Auto-detect toggle
        self._ad_btn = Button(
            text="Auto-detect: ON", font_size="15sp",
            size_hint=(1, 0.14),
            background_color=(0.15, 0.55, 0.85, 1),
        )
        self._ad_btn.bind(on_release=self._toggle_auto_detect)
        root.add_widget(self._ad_btn)

        # HSV settings
        hsv_btn = Button(
            text="HSV Colour Settings", font_size="15sp",
            size_hint=(1, 0.14),
            background_color=(0.45, 0.25, 0.75, 1),
        )
        hsv_btn.bind(on_release=lambda *_: setattr(self.manager, "current", "hsv_tuner"))
        root.add_widget(hsv_btn)

        root.add_widget(Label(
            text=(
                "1. Tap [b]Start Overlay[/b] to launch the floating button.\n"
                "2. Switch to Soccer Stars — the overlay stays on screen.\n"
                "3. [b]Auto-detect[/b] wakes the engine when your turn begins.\n"
                "4. Tap the floating button to show / hide the prediction line.\n"
                "5. Touch-and-hold the button to drag it to any screen edge.\n\n"
                "Grant SYSTEM_ALERT_WINDOW in Android Settings if prompted."
            ),
            markup=True, font_size="12sp", halign="center",
            size_hint=(1, 0.39), color=(0.65, 0.65, 0.65, 1),
        ))

        self.add_widget(root)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _toggle_overlay(self, *_):
        if self._overlay_active:
            self._app.stop_overlay()
            self._ov_btn.text             = "Start Overlay"
            self._ov_btn.background_color = (0.18, 0.76, 0.28, 1)
        else:
            self._app.start_overlay()
            self._ov_btn.text             = "Stop Overlay"
            self._ov_btn.background_color = (0.82, 0.18, 0.18, 1)
        self._overlay_active = not self._overlay_active
        self._refresh_status()

    def _toggle_auto_detect(self, *_):
        self._auto_detect = not self._auto_detect
        self._app.set_auto_detect(self._auto_detect)
        if self._auto_detect:
            self._ad_btn.text             = "Auto-detect: ON"
            self._ad_btn.background_color = (0.15, 0.55, 0.85, 1)
        else:
            self._ad_btn.text             = "Auto-detect: OFF"
            self._ad_btn.background_color = (0.38, 0.38, 0.38, 1)
        self._refresh_status()

    def _refresh_status(self):
        ov = "ON"  if self._overlay_active else "OFF"
        ad = "ON"  if self._auto_detect    else "OFF"
        self._status.text = f"Overlay: {ov}  |  Auto-detect: {ad}"


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class SoccerStarsApp(App):
    """
    Kivy application lifecycle
    --------------------------
    on_start  → request permissions → request MediaProjection consent
    on_pause  → hibernate service; return True (keep process alive)
    on_resume → restore service
    on_stop   → tear down overlay
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._overlay: "FloatingOverlayManager | None" = None
        self._mp_token                                 = None
        self._auto_detect_enabled                      = True
        self.hsv_prefs                                 = self._load_prefs()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def build(self):
        sm = ScreenManager()
        sm.add_widget(HomeScreen(app_ref=self))
        sm.add_widget(HSVTunerScreen(app_ref=self))
        return sm

    def on_start(self):
        if IS_ANDROID:
            self._request_permissions()
        else:
            Logger.info("SoccerStars: desktop mode — Android setup skipped.")

    def on_pause(self):
        """
        App sent to background (user switched to Soccer Stars).
        Returning True keeps the process alive so the overlay persists.
        Hibernating the service stops all OpenCV work → near-zero battery drain.
        """
        if self._overlay:
            self._overlay.notify_background()
        Logger.info("SoccerStars: paused — service hibernated.")
        return True

    def on_resume(self):
        if self._overlay:
            self._overlay.notify_foreground()
        Logger.info("SoccerStars: resumed.")

    def on_stop(self):
        self.stop_overlay()

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    def _request_permissions(self):
        request_permissions(REQUIRED_PERMISSIONS,
                            callback=self._on_permissions)

    def _on_permissions(self, permissions, grants):
        denied = [p for p, g in zip(permissions, grants) if not g]
        if denied:
            Logger.warning(f"SoccerStars: denied: {denied}")
            self._show_denied_popup(denied)
        else:
            Logger.info("SoccerStars: all permissions granted.")
            self._request_media_projection()

    def _show_denied_popup(self, denied: list):
        body = BoxLayout(orientation="vertical", padding=12, spacing=8)
        body.add_widget(Label(
            text=("Permissions denied:\n" + "\n".join(denied) +
                  "\n\nGo to: Android Settings → Apps → "
                  "Soccer Stars Analyzer → Permissions"),
            halign="center",
        ))
        btn = Button(text="OK", size_hint=(1, 0.28))
        popup = Popup(title="Permissions Required",
                      content=body, size_hint=(0.88, 0.52))
        btn.bind(on_release=popup.dismiss)
        body.add_widget(btn)
        popup.open()

    # ------------------------------------------------------------------
    # MediaProjection
    # ------------------------------------------------------------------

    def _request_media_projection(self):
        ctx = PythonActivity.mActivity
        mgr = cast(
            "android.media.projection.MediaProjectionManager",
            ctx.getSystemService(ctx.MEDIA_PROJECTION_SERVICE),
        )
        ctx.startActivityForResult(mgr.createScreenCaptureIntent(), MP_REQUEST_CODE)
        _android_activity.bind(on_activity_result=self._on_mp_result)

    def _on_mp_result(self, req_code, result_code, data):
        if req_code != MP_REQUEST_CODE:
            return
        if result_code == -1:          # Activity.RESULT_OK
            self._mp_token = (result_code, data)
            Logger.info("SoccerStars: MediaProjection granted.")
        else:
            Logger.warning("SoccerStars: MediaProjection denied by user.")

    # ------------------------------------------------------------------
    # Overlay control
    # ------------------------------------------------------------------

    def start_overlay(self):
        if self._overlay is not None:
            return
        self._overlay = FloatingOverlayManager(
            hsv_prefs=self.hsv_prefs,
            media_projection_token=self._mp_token,
            auto_detect_enabled=self._auto_detect_enabled,
        )
        self._overlay.start()
        Logger.info("SoccerStars: overlay started.")

    def stop_overlay(self):
        if self._overlay is not None:
            self._overlay.stop()
            self._overlay = None
        Logger.info("SoccerStars: overlay stopped.")

    def set_auto_detect(self, enabled: bool):
        self._auto_detect_enabled = enabled
        if self._overlay is not None:
            self._overlay.set_auto_detect(enabled)

    # ------------------------------------------------------------------
    # HSV preferences
    # ------------------------------------------------------------------

    def _load_prefs(self) -> dict:
        defaults = {
            "ball":   {"h_lo": 0,   "s_lo": 0,   "v_lo": 200,
                       "h_hi": 180, "s_hi": 40,  "v_hi": 255},
            "player": {"h_lo": 100, "s_lo": 150, "v_lo": 100,
                       "h_hi": 130, "s_hi": 255, "v_hi": 255},
        }
        if os.path.exists(PREFS_FILE):
            try:
                with open(PREFS_FILE) as fh:
                    return json.load(fh)
            except Exception:
                pass
        return defaults

    def save_hsv_prefs(self, prefs: dict):
        self.hsv_prefs = prefs
        try:
            with open(PREFS_FILE, "w") as fh:
                json.dump(prefs, fh)
        except Exception as exc:
            Logger.warning(f"SoccerStars: prefs save error: {exc}")
        if self._overlay is not None:
            self._overlay.update_hsv_prefs(prefs)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    SoccerStarsApp().run()
