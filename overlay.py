"""
overlay.py — Floating Overlay Manager
======================================
Manages two Android WindowManager views that sit on top of Soccer Stars:

  1. Floating button  — small, draggable, always-on-top.
       • ACTION_DOWN  → sends "wake" (on-demand engine activation)
       • Short tap    → toggle canvas + hibernate / active
       • Draws a status dot: green = active/turn detected, yellow = watching

  2. Prediction canvas — full-screen transparent View (FLAG_NOT_TOUCHABLE).
       • Draws trajectory polyline, bounce dots, ball/player outlines.
       • postInvalidate() fires on every IPC result from the service.

IPC
---
  Service → Overlay : UDP 54321  (JSON result packets)
  Overlay → Service : UDP 54322  (JSON command packets)

Power-saving integration
------------------------
  button touch-down  → "wake"
  canvas hidden      → "set_power hibernate"
  canvas shown       → "set_power active"
  notify_background()→ "set_power hibernate"  (called by on_pause)
  notify_foreground()→ "set_power active"     (called by on_resume)
  set_auto_detect()  → "set_auto_detect"
"""

from __future__ import annotations
import os
import json
import threading
import socket
from kivy.logger import Logger

IS_ANDROID = (
    os.environ.get("ANDROID_ARGUMENT") is not None
    or os.path.exists("/system/build.prop")
)

if IS_ANDROID:
    from jnius import autoclass, cast, PythonJavaClass, java_method  # type: ignore

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Context        = autoclass("android.content.Context")
    LayoutParams   = autoclass("android.view.WindowManager$LayoutParams")
    PixelFormat    = autoclass("android.graphics.PixelFormat")
    Gravity        = autoclass("android.view.Gravity")
    ImageView      = autoclass("android.widget.ImageView")
    Paint          = autoclass("android.graphics.Paint")

IPC_OVERLAY_PORT = 54321   # service → overlay
IPC_SERVICE_PORT = 54322   # overlay → service


# ---------------------------------------------------------------------------
# FloatingOverlayManager
# ---------------------------------------------------------------------------

class FloatingOverlayManager:

    def __init__(
        self,
        hsv_prefs: dict,
        media_projection_token=None,
        auto_detect_enabled: bool = True,
    ):
        self._hsv_prefs   = hsv_prefs
        self._mp_token    = media_projection_token
        self._auto_detect = auto_detect_enabled

        self._wm          = None
        self._btn_view    = None
        self._canvas_view = None
        self._ipc_thread  = None
        self._running     = False

        # Latest data from service — read by _PredictionView.onDraw
        self.trajectory:    list       = []
        self.ball:          list | None = None
        self.player:        list | None = None
        self.turn_detected: bool       = False
        self.hibernating:   bool       = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        if IS_ANDROID:
            self._setup_wm()
            self._add_floating_button()
            self._add_prediction_canvas()
        self._running    = True
        self._ipc_thread = threading.Thread(target=self._ipc_loop, daemon=True)
        self._ipc_thread.start()
        self._start_service()
        Logger.info("Overlay: started.")

    def stop(self):
        self._running = False
        if IS_ANDROID and self._wm is not None:
            for v in (self._btn_view, self._canvas_view):
                try:
                    if v:
                        self._wm.removeView(v)
                except Exception as exc:
                    Logger.warning(f"Overlay: removeView: {exc}")
        self._stop_service()
        Logger.info("Overlay: stopped.")

    def update_hsv_prefs(self, prefs: dict):
        self._hsv_prefs = prefs
        self._cmd({"cmd": "set_hsv", "prefs": prefs})

    def notify_background(self):
        Logger.info("Overlay: → hibernate (app backgrounded).")
        self._cmd({"cmd": "set_power", "state": "hibernate"})

    def notify_foreground(self):
        Logger.info("Overlay: → active (app foregrounded).")
        if IS_ANDROID and self._canvas_view is not None:
            if self._canvas_view.getVisibility() == 0:   # VISIBLE
                self._cmd({"cmd": "set_power", "state": "active"})
        else:
            self._cmd({"cmd": "set_power", "state": "active"})

    def set_auto_detect(self, enabled: bool):
        self._auto_detect = enabled
        self._cmd({"cmd": "set_auto_detect", "enabled": enabled})

    # ------------------------------------------------------------------
    # IPC helper
    # ------------------------------------------------------------------

    def _cmd(self, msg: dict):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(json.dumps(msg).encode(), ("127.0.0.1", IPC_SERVICE_PORT))
            s.close()
        except Exception as exc:
            Logger.warning(f"Overlay: _cmd error: {exc}")

    # ------------------------------------------------------------------
    # WindowManager helpers
    # ------------------------------------------------------------------

    def _setup_wm(self):
        ctx      = PythonActivity.mActivity
        self._wm = cast("android.view.WindowManager",
                        ctx.getSystemService(Context.WINDOW_SERVICE))

    def _lp(self, w, h, gravity, x=0, y=0, extra_flags=0):
        FLAGS = 0x00000008 | 0x00000100 | extra_flags   # NOT_FOCUSABLE | LAYOUT_IN_SCREEN
        lp    = LayoutParams(w, h, 2038, FLAGS, PixelFormat.TRANSLUCENT)
        lp.gravity = gravity
        lp.x, lp.y = x, y
        return lp

    # ------------------------------------------------------------------
    # Floating button
    # ------------------------------------------------------------------

    def _add_floating_button(self):
        ctx = PythonActivity.mActivity
        btn = ImageView(ctx)
        btn.setImageResource(ctx.getResources().getIdentifier(
            "ic_launcher", "mipmap", ctx.getPackageName()))
        btn.setAlpha(0.88)
        lp       = self._lp(120, 120, Gravity.TOP | Gravity.START, x=40, y=180)
        listener = _DragToggleTouchListener(
            wm=self._wm, lp=lp,
            on_tap=self._on_tap,
            on_down=self._on_touch_down,
        )
        btn.setOnTouchListener(listener)
        self._wm.addView(btn, lp)
        self._btn_view = btn
        Logger.info("Overlay: floating button added.")

    def _on_touch_down(self):
        self._cmd({"cmd": "wake"})

    def _on_tap(self):
        if self._canvas_view is None:
            return
        if IS_ANDROID:
            vis = self._canvas_view.getVisibility()
            if vis == 0:     # VISIBLE → hide
                self._canvas_view.setVisibility(4)
                self._cmd({"cmd": "set_power", "state": "hibernate"})
            else:            # hidden → show
                self._canvas_view.setVisibility(0)
                self._cmd({"cmd": "set_power", "state": "active"})

    # ------------------------------------------------------------------
    # Prediction canvas
    # ------------------------------------------------------------------

    def _add_prediction_canvas(self):
        ctx  = PythonActivity.mActivity
        lp   = self._lp(-1, -1, Gravity.TOP | Gravity.START,
                         extra_flags=0x00000010)   # FLAG_NOT_TOUCHABLE
        view = _PredictionView(ctx, overlay=self)
        self._wm.addView(view, lp)
        self._canvas_view = view
        Logger.info("Overlay: prediction canvas added.")

    # ------------------------------------------------------------------
    # IPC listener
    # ------------------------------------------------------------------

    def _ipc_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", IPC_OVERLAY_PORT))
        sock.settimeout(1.0)
        Logger.info(f"Overlay: IPC listener on UDP {IPC_OVERLAY_PORT}")

        while self._running:
            try:
                data, _ = sock.recvfrom(65535)
                msg      = json.loads(data.decode())
                self.trajectory    = msg.get("waypoints", [])
                self.ball          = msg.get("ball")
                self.player        = msg.get("player")
                self.turn_detected = bool(msg.get("turn_detected", False))
                self.hibernating   = bool(msg.get("hibernating",   False))
                if IS_ANDROID and self._canvas_view is not None:
                    self._canvas_view.postInvalidate()
            except socket.timeout:
                continue
            except Exception as exc:
                Logger.warning(f"Overlay IPC: {exc}")

        sock.close()

    # ------------------------------------------------------------------
    # Service lifecycle
    # ------------------------------------------------------------------

    def _start_service(self):
        if not IS_ANDROID:
            return
        Intent = autoclass("android.content.Intent")
        PySvc  = autoclass("org.kivy.android.PythonService")
        ctx    = PythonActivity.mActivity
        si     = Intent(ctx, PySvc)
        si.putExtra("python_service_argument", json.dumps({
            "hsv_prefs":   self._hsv_prefs,
            "auto_detect": self._auto_detect,
        }))
        ctx.startForegroundService(si)
        Logger.info("Overlay: capture service started.")

    def _stop_service(self):
        if not IS_ANDROID:
            return
        try:
            Intent = autoclass("android.content.Intent")
            PySvc  = autoclass("org.kivy.android.PythonService")
            PythonActivity.mActivity.stopService(
                Intent(PythonActivity.mActivity, PySvc))
        except Exception as exc:
            Logger.warning(f"Overlay: stopService: {exc}")


# ---------------------------------------------------------------------------
# Android Java helper classes
# ---------------------------------------------------------------------------

if IS_ANDROID:

    class _DragToggleTouchListener(PythonJavaClass):
        """
        OnTouchListener — makes the floating button draggable.
        ACTION_DOWN  : record start; fire on_down() immediately (wake engine).
        ACTION_MOVE  : reposition via WindowManager.updateViewLayout.
        ACTION_UP    : movement < 8 px → fire on_tap() (toggle canvas).
        """
        __javainterfaces__ = ["android/view/View$OnTouchListener"]
        __javacontext__    = "app"

        def __init__(self, wm, lp, on_tap, on_down=None):
            super().__init__()
            self._wm, self._lp = wm, lp
            self._tap, self._down = on_tap, on_down
            self._lx = self._ly = self._sx = self._sy = 0.0

        @java_method("(Landroid/view/View;Landroid/view/MotionEvent;)Z")
        def onTouch(self, view, event):
            action = event.getAction()
            if action == 0:   # DOWN
                self._lx = self._sx = event.getRawX()
                self._ly = self._sy = event.getRawY()
                if self._down:
                    self._down()
                return True
            if action == 2:   # MOVE
                self._lp.x += int(event.getRawX() - self._lx)
                self._lp.y += int(event.getRawY() - self._ly)
                self._wm.updateViewLayout(view, self._lp)
                self._lx, self._ly = event.getRawX(), event.getRawY()
                return True
            if action == 1:   # UP
                if abs(event.getRawX()-self._sx) + abs(event.getRawY()-self._sy) < 8:
                    self._tap()
                return True
            return False

    class _PredictionView(autoclass("android.view.View")):
        """
        Transparent custom View that draws the trajectory overlay.
        Also draws a small status dot beside the floating button.
        """
        __javacontext__ = "app"

        def __init__(self, ctx, overlay: FloatingOverlayManager):
            super().__init__(ctx)
            self._ov    = overlay
            self._paint = Paint()
            self._paint.setAntiAlias(True)

        def onDraw(self, canvas):
            canvas.drawColor(0x00000000)
            ov         = self._ov
            trajectory = ov.trajectory
            p          = self._paint

            # Trajectory polyline
            p.setColor(0xFF00FF00)
            p.setStyle(Paint.Style.STROKE)
            p.setStrokeWidth(4.0)
            for i in range(1, len(trajectory)):
                x0, y0 = trajectory[i - 1]
                x1, y1 = trajectory[i]
                canvas.drawLine(float(x0), float(y0), float(x1), float(y1), p)

            # Bounce markers
            p.setColor(0xFFFFA500)
            p.setStyle(Paint.Style.FILL)
            for pt in trajectory[1:-1]:
                canvas.drawCircle(float(pt[0]), float(pt[1]), 12.0, p)

            # Ball outline
            if ov.ball:
                p.setColor(0xFF2196F3)
                p.setStyle(Paint.Style.STROKE)
                p.setStrokeWidth(3.0)
                canvas.drawCircle(float(ov.ball[0]), float(ov.ball[1]),
                                  float(ov.ball[2]) + 6.0, p)

            # Player outline
            if ov.player:
                p.setColor(0xFFF44336)
                p.setStyle(Paint.Style.STROKE)
                p.setStrokeWidth(3.0)
                canvas.drawCircle(float(ov.player[0]), float(ov.player[1]),
                                  float(ov.player[2]) + 6.0, p)

            # Status dot  (beside the floating button at ~160,200)
            # Green  = turn detected / active
            # Yellow = hibernate, watching
            # Grey   = hibernate, auto-detect off
            if ov.turn_detected or not ov.hibernating:
                dot_col = 0xFF4CAF50   # green
            elif ov._auto_detect:
                dot_col = 0xFFFFEB3B   # yellow — watching
            else:
                dot_col = 0xFF9E9E9E   # grey — off

            p.setColor(dot_col)
            p.setStyle(Paint.Style.FILL)
            canvas.drawCircle(172.0, 200.0, 11.0, p)
            p.setColor(0xFFFFFFFF)
            p.setStyle(Paint.Style.STROKE)
            p.setStrokeWidth(2.0)
            canvas.drawCircle(172.0, 200.0, 11.0, p)
