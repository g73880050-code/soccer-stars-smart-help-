"""
service/main.py — Android Background Capture Service
=====================================================
Entry point for the python-for-android foreground service.

Architecture
------------
  MediaProjection → ScreenCaptureManager → BGR frames
       │                                        │
       │                                        ▼ (active mode)
       │                              analyse_frame()
       │                                        │ JSON result
       │                                        ▼
       │                              UDP 54321 → overlay
       │
       └──(hibernate mode)──► check_turn() every 0.5 s
                                        │ if turn detected
                                        ▼
                              wake_until_holder set → re-enter active mode

Power states
------------
  ACTIVE    : capture → analyse_frame() → broadcast, capped at 15 FPS.
  HIBERNATE : drain ImageReader; run check_turn() every 0.5 s;
              auto-wake for WAKE_HOLD_SECONDS when a turn is detected.
  WAKE GRANT: "wake" UDP command overrides HIBERNATE for WAKE_HOLD_SECONDS.
              Fired by overlay button touch-down.

IPC commands (UDP 54322)
------------------------
  set_hsv         {"cmd":"set_hsv","prefs":{...}}
  set_power       {"cmd":"set_power","state":"active"|"hibernate"}
  wake            {"cmd":"wake"}
  set_scale       {"cmd":"set_scale","factor":float}
  set_auto_detect {"cmd":"set_auto_detect","enabled":bool}
"""

from __future__ import annotations
import json
import os
import sys
import socket
import threading
import time
import numpy as np
from kivy.logger import Logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analyzer import (
    AnalyzerConfig, TurnDetectorConfig, TurnDetector,
    analyse_frame, check_turn,
)

IS_ANDROID = os.path.exists("/system/build.prop")

IPC_OVERLAY_PORT = 54321
IPC_SERVICE_PORT = 54322

NOTIFICATION_ID = 1001
CHANNEL_ID      = "SoccerStarsChannel"

# ---------------------------------------------------------------------------
# Power constants
# ---------------------------------------------------------------------------
TARGET_FPS          = 15
FRAME_INTERVAL      = 1.0 / TARGET_FPS
HIBERNATE_INTERVAL  = 0.5
WAKE_HOLD_SECONDS   = 3.0
TURN_CHECK_INTERVAL = 0.5

POWER_ACTIVE    = "active"
POWER_HIBERNATE = "hibernate"

# ---------------------------------------------------------------------------
# Android imports
# ---------------------------------------------------------------------------
if IS_ANDROID:
    from jnius import autoclass, cast  # type: ignore

    PythonService  = autoclass("org.kivy.android.PythonService")
    Handler        = autoclass("android.os.Handler")
    Looper         = autoclass("android.os.Looper")
    ImageReader    = autoclass("android.media.ImageReader")
    DisplayMetrics = autoclass("android.util.DisplayMetrics")
    Context        = autoclass("android.content.Context")
    NotifChannel   = autoclass("android.app.NotificationChannel")
    NotifBuilder   = autoclass("androidx.core.app.NotificationCompat$Builder")


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def _make_channel(ctx):
    if not IS_ANDROID:
        return
    mgr = cast("android.app.NotificationManager",
               ctx.getSystemService(Context.NOTIFICATION_SERVICE))
    mgr.createNotificationChannel(NotifChannel(CHANNEL_ID, "Soccer Stars Analyzer", 2))


def _make_notif(ctx):
    return (NotifBuilder(ctx, CHANNEL_ID)
            .setContentTitle("Soccer Stars Analyzer")
            .setContentText("Overlay active — capturing screen")
            .setSmallIcon(ctx.getResources().getIdentifier(
                "ic_launcher", "mipmap", ctx.getPackageName()))
            .build())


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

class ScreenCaptureManager:
    """Wraps Android MediaProjection + ImageReader → BGR numpy frames."""

    def __init__(self, result_code, data_intent, width, height, density):
        self._rc, self._di = result_code, data_intent
        self.width, self.height, self._density = width, height, density
        self._proj, self._ir, self._vd = None, None, None

    def start(self):
        if not IS_ANDROID:
            Logger.info("ScreenCapture: no-op on non-Android.")
            return
        ctx   = PythonService.mService
        mpm   = cast("android.media.projection.MediaProjectionManager",
                     ctx.getSystemService(Context.MEDIA_PROJECTION_SERVICE))
        self._proj = mpm.getMediaProjection(self._rc, self._di)
        self._ir   = ImageReader.newInstance(self.width, self.height, 1, 2)   # RGBA_8888
        VD         = autoclass("android.hardware.display.VirtualDisplay")
        self._vd   = self._proj.createVirtualDisplay(
            "SoccerStarsCapture", self.width, self.height, self._density, 4,
            self._ir.getSurface(), None, Handler(Looper.getMainLooper()),
        )
        Logger.info("ScreenCapture: started.")

    def acquire_bgr(self):
        """Return one BGR frame or None if the buffer is empty."""
        if not IS_ANDROID or self._ir is None:
            return None
        image = self._ir.acquireLatestImage()
        if image is None:
            return None
        try:
            planes = image.getPlanes()
            buf    = planes[0].getBuffer()
            rs, ps = planes[0].getRowStride(), planes[0].getPixelStride()
            raw    = bytearray(buf.remaining())
            buf.get(raw)
            flat   = np.frombuffer(raw, dtype=np.uint8)
            rgba   = flat.reshape((self.height, rs // ps, 4))[:, :self.width, :]
            return np.ascontiguousarray(rgba[:, :, [2, 1, 0]])
        finally:
            image.close()

    def drain(self):
        """Discard the latest buffered frame (prevents buffer overflow in hibernate)."""
        if not IS_ANDROID or self._ir is None:
            return
        try:
            img = self._ir.acquireLatestImage()
            if img:
                img.close()
        except Exception:
            pass

    def stop(self):
        if IS_ANDROID:
            try:
                if self._vd:   self._vd.release()
                if self._proj: self._proj.stop()
            except Exception as exc:
                Logger.warning(f"ScreenCapture: stop error: {exc}")


# ---------------------------------------------------------------------------
# IPC command listener
# ---------------------------------------------------------------------------

def _cmd_listener(cfg_h, td_h, pwr_h, wake_h, stop_ev):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", IPC_SERVICE_PORT))
    sock.settimeout(1.0)
    Logger.info(f"Service: cmd listener on UDP {IPC_SERVICE_PORT}")

    while not stop_ev.is_set():
        try:
            data, _ = sock.recvfrom(8192)
            msg      = json.loads(data.decode())
            cmd      = msg.get("cmd")

            if cmd == "set_hsv":
                cfg_h[0] = AnalyzerConfig.from_prefs(msg.get("prefs", {}))
            elif cmd == "set_power":
                pwr_h[0] = msg.get("state", POWER_ACTIVE)
                Logger.info(f"Service: power → {pwr_h[0]}")
            elif cmd == "wake":
                wake_h[0] = time.monotonic() + WAKE_HOLD_SECONDS
                Logger.info(f"Service: wake granted for {WAKE_HOLD_SECONDS}s")
            elif cmd == "set_scale":
                cfg_h[0].scale_factor = max(0.1, min(1.0, float(msg.get("factor", 0.5))))
            elif cmd == "set_auto_detect":
                td_h[0].enabled = bool(msg.get("enabled", True))
                Logger.info(f"Service: auto_detect → {td_h[0].enabled}")

        except socket.timeout:
            continue
        except Exception as exc:
            Logger.warning(f"Service cmd error: {exc}")

    sock.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _screen_dims():
    if not IS_ANDROID:
        return 1080, 1920, 480
    ctx = PythonService.mService
    wm  = cast("android.view.WindowManager",
               ctx.getSystemService(Context.WINDOW_SERVICE))
    dm  = DisplayMetrics()
    wm.getDefaultDisplay().getRealMetrics(dm)
    return dm.widthPixels, dm.heightPixels, dm.densityDpi


def _parse_args():
    try:
        return json.loads(os.environ.get("PYTHON_SERVICE_ARGUMENT", "{}"))
    except Exception:
        return {}


def _broadcast(sock, result):
    sock.sendto(json.dumps(result).encode(), ("127.0.0.1", IPC_OVERLAY_PORT))


# ---------------------------------------------------------------------------
# Service main loop
# ---------------------------------------------------------------------------

def main():
    Logger.info("SoccerStarsService: starting.")
    args = _parse_args()

    cfg_h  = [AnalyzerConfig.from_prefs(args.get("hsv_prefs", {}))]
    td_h   = [TurnDetectorConfig.from_prefs(args.get("hsv_prefs", {}))]
    pwr_h  = [POWER_ACTIVE]
    wake_h = [0.0]

    if args.get("auto_detect") is False:
        td_h[0].enabled = False

    detector = TurnDetector()

    if IS_ANDROID:
        ctx = PythonService.mService
        _make_channel(ctx)
        PythonService.mService.startForeground(NOTIFICATION_ID, _make_notif(ctx))

    mp_rc  = int(os.environ.get("MP_RESULT_CODE", "-1"))
    mp_data = os.environ.get("MP_DATA", None)
    w, h, dpi = _screen_dims()

    if IS_ANDROID and mp_rc != -1:
        capture = ScreenCaptureManager(mp_rc, mp_data, w, h, dpi)
        capture.start()
    else:
        Logger.info("SoccerStarsService: no MediaProjection — idle mode.")
        capture = None

    stop_ev = threading.Event()
    threading.Thread(
        target=_cmd_listener,
        args=(cfg_h, td_h, pwr_h, wake_h, stop_ev),
        daemon=True,
    ).start()

    out_sock             = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    last_frame_t         = 0.0
    last_turn_check_t    = 0.0
    sent_hibernate_clear = False

    try:
        while not stop_ev.is_set():
            now         = time.monotonic()
            wake_active = now < wake_h[0]
            is_active   = (pwr_h[0] == POWER_ACTIVE) or wake_active

            # ----------------------------------------------------------------
            # HIBERNATE
            # ----------------------------------------------------------------
            if not is_active:
                if not sent_hibernate_clear:
                    _broadcast(out_sock, {
                        "waypoints": [], "ball": None, "player": None,
                        "turn_detected": False, "hibernating": True,
                    })
                    sent_hibernate_clear = True
                    Logger.info("SoccerStarsService: hibernating.")

                if capture is not None:
                    capture.drain()

                if (capture is not None
                        and td_h[0].enabled
                        and now - last_turn_check_t >= TURN_CHECK_INTERVAL):
                    last_turn_check_t = now
                    frame = capture.acquire_bgr()
                    if frame is not None and check_turn(frame, cfg_h[0], td_h[0], detector):
                        wake_h[0] = now + WAKE_HOLD_SECONDS
                        Logger.info("TurnDetector: your turn — waking engine.")
                        _broadcast(out_sock, {
                            "waypoints": [], "ball": None, "player": None,
                            "turn_detected": True, "hibernating": False,
                        })
                        sent_hibernate_clear = False
                        continue

                time.sleep(HIBERNATE_INTERVAL)
                continue

            # ----------------------------------------------------------------
            # ACTIVE
            # ----------------------------------------------------------------
            sent_hibernate_clear = False

            elapsed = now - last_frame_t
            if elapsed < FRAME_INTERVAL:
                time.sleep(FRAME_INTERVAL - elapsed)
                continue

            if capture is None:
                _broadcast(out_sock, {
                    "waypoints": [], "ball": None, "player": None,
                    "turn_detected": False, "hibernating": False,
                })
                time.sleep(FRAME_INTERVAL)
                last_frame_t = time.monotonic()
                continue

            frame = capture.acquire_bgr()
            if frame is None:
                time.sleep(0.01)
                continue

            result = analyse_frame(frame, cfg_h[0])
            result["turn_detected"] = result.get("player") is not None
            result["hibernating"]   = False
            _broadcast(out_sock, result)
            last_frame_t = time.monotonic()

    except KeyboardInterrupt:
        Logger.info("SoccerStarsService: interrupted.")
    finally:
        stop_ev.set()
        if capture:
            capture.stop()
        out_sock.close()
        Logger.info("SoccerStarsService: stopped.")


if __name__ == "__main__":
    main()
