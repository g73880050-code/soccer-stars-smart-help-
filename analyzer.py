"""
analyzer.py — Detection, Trajectory & Turn-Detection Engine
============================================================
Pure Python / OpenCV module.  No Android imports — fully portable.

Classes
-------
AnalyzerConfig      HSV ranges, physics parameters, resolution scale.
TurnDetectorConfig  Sensitivity knobs for the auto-detector.
TurnDetector        Stateful "is it my turn?" detector (two strategies).
DetectedObject      (x, y, radius) with coordinate-scaling helper.

Functions
---------
detect_ball()       Colour-mask → contour → enclosing circle.
detect_player()     Same pipeline for the active player disc.
compute_trajectory()Ray-cast with wall reflections (NumPy).
analyse_frame()     Full pipeline: scale → detect → trajectory.
check_turn()        Lightweight turn-detection for hibernate mode.

Power-saving note
-----------------
analyse_frame() downscales by cfg.scale_factor (default 0.5) before any
OpenCV work, then scales detected coordinates back to native resolution.
Halving both dimensions cuts pixel count to 25 %, cutting CPU time ~4×.
"""

from __future__ import annotations
import numpy as np
import cv2
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# AnalyzerConfig
# ---------------------------------------------------------------------------

@dataclass
class AnalyzerConfig:
    """Detection and physics configuration (all HSV arrays are np.uint8[3])."""

    # Ball colour  — default: white / very bright
    ball_lower_hsv: np.ndarray = field(
        default_factory=lambda: np.array([0,   0,   200], np.uint8))
    ball_upper_hsv: np.ndarray = field(
        default_factory=lambda: np.array([180, 40,  255], np.uint8))

    # Active player disc colour  — default: bright blue
    player_lower_hsv: np.ndarray = field(
        default_factory=lambda: np.array([100, 150, 100], np.uint8))
    player_upper_hsv: np.ndarray = field(
        default_factory=lambda: np.array([130, 255, 255], np.uint8))

    # Minimum contour area (px²) — noise filter
    ball_min_area: int   = 200
    player_min_area: int = 500

    # Physics
    max_bounces: int = 5
    ray_length:  int = 800
    margin:      int = 10

    # Resolution scaling: 0.5 → 50 % W×H = 25 % pixel count
    scale_factor: float = 0.5

    @classmethod
    def from_prefs(cls, prefs: dict) -> "AnalyzerConfig":
        cfg = cls()
        b = prefs.get("ball",   {})
        p = prefs.get("player", {})
        if b:
            cfg.ball_lower_hsv = np.array(
                [b.get("h_lo", 0),   b.get("s_lo", 0),   b.get("v_lo", 200)], np.uint8)
            cfg.ball_upper_hsv = np.array(
                [b.get("h_hi", 180), b.get("s_hi", 40),  b.get("v_hi", 255)], np.uint8)
        if p:
            cfg.player_lower_hsv = np.array(
                [p.get("h_lo", 100), p.get("s_lo", 150), p.get("v_lo", 100)], np.uint8)
            cfg.player_upper_hsv = np.array(
                [p.get("h_hi", 130), p.get("s_hi", 255), p.get("v_hi", 255)], np.uint8)
        if "scale_factor" in prefs:
            cfg.scale_factor = max(0.1, min(1.0, float(prefs["scale_factor"])))
        return cfg


# ---------------------------------------------------------------------------
# TurnDetectorConfig
# ---------------------------------------------------------------------------

@dataclass
class TurnDetectorConfig:
    """
    Sensitivity parameters for the 'your turn' auto-detector.

    Pixel measurements are in native resolution.

    enabled              Master on/off (toggled from HomeScreen).
    scan_radius          Half-side (px) of the ROI square around the player.
    line_brightness      Min grayscale value included in Hough mask (0-255).
    hough_threshold      Hough accumulator votes to accept a line.
    min_line_length      Shortest accepted aiming-line segment (px).
    max_line_gap         Max gap inside a line before it is split (px).
    motion_pixel_thresh  Per-pixel absolute diff that counts as 'moved'.
    motion_area_fraction Fraction of ROI pixels that must change (0–1).
    """
    enabled:              bool  = True
    scan_radius:          int   = 180
    line_brightness:      int   = 180
    hough_threshold:      int   = 20
    min_line_length:      int   = 40
    max_line_gap:         int   = 15
    motion_pixel_thresh:  int   = 25
    motion_area_fraction: float = 0.02

    @classmethod
    def from_prefs(cls, prefs: dict) -> "TurnDetectorConfig":
        cfg = cls()
        td  = prefs.get("turn_detector", {})
        if "enabled" in td:
            cfg.enabled = bool(td["enabled"])
        for key in ("scan_radius", "line_brightness", "hough_threshold",
                    "min_line_length", "max_line_gap", "motion_pixel_thresh"):
            if key in td:
                setattr(cfg, key, int(td[key]))
        if "motion_area_fraction" in td:
            cfg.motion_area_fraction = float(td["motion_area_fraction"])
        return cfg


# ---------------------------------------------------------------------------
# TurnDetector
# ---------------------------------------------------------------------------

class TurnDetector:
    """
    Stateful 'your turn' detector.

    Strategy 1 — Hough aiming-line scan
        Threshold the player ROI for bright pixels, erase the disc body,
        run HoughLinesP.  The Soccer Stars aiming guide is a long bright
        line that reliably fires this strategy.

    Strategy 2 — Motion detection (frame difference)
        Absolute pixel diff in the player ROI between consecutive calls.
        Catches the moving aiming arrow even when Strategy 1 misses
        (e.g. faint dotted lines or low-contrast stadiums).

    Either strategy returning True triggers a wake.
    """

    def __init__(self) -> None:
        self._prev_gray: Optional[np.ndarray] = None

    def is_your_turn(
        self,
        frame: np.ndarray,
        player: Optional["DetectedObject"],
        cfg: TurnDetectorConfig,
    ) -> bool:
        if not cfg.enabled or player is None:
            self._prev_gray = None
            return False
        return (self._detect_aiming_line(frame, player, cfg)
                or self._detect_motion(frame, player, cfg))

    # ------------------------------------------------------------------

    def _detect_aiming_line(self, frame, player, cfg) -> bool:
        roi, cx, cy = self._roi(frame, player, cfg.scan_radius)
        if roi.size == 0:
            return False
        gray    = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, cfg.line_brightness, 255, cv2.THRESH_BINARY)
        cv2.circle(mask, (cx, cy), max(player.radius + 10, 18), 0, -1)
        lines = cv2.HoughLinesP(
            mask, 1, np.pi / 180,
            threshold=cfg.hough_threshold,
            minLineLength=cfg.min_line_length,
            maxLineGap=cfg.max_line_gap,
        )
        return lines is not None and len(lines) > 0

    def _detect_motion(self, frame, player, cfg) -> bool:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        r    = cfg.scan_radius
        h, w = gray.shape
        x1, y1 = max(0, player.x - r), max(0, player.y - r)
        x2, y2 = min(w, player.x + r), min(h, player.y + r)
        curr = gray[y1:y2, x1:x2]

        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            return False

        prev = self._prev_gray[y1:y2, x1:x2]
        if curr.shape != prev.shape or curr.size == 0:
            self._prev_gray = gray
            return False

        diff     = cv2.absdiff(curr, prev)
        changed  = int(np.count_nonzero(diff > cfg.motion_pixel_thresh))
        fraction = changed / curr.size
        self._prev_gray = gray
        return fraction > cfg.motion_area_fraction

    @staticmethod
    def _roi(frame, player, radius) -> tuple[np.ndarray, int, int]:
        h, w = frame.shape[:2]
        x1   = max(0, player.x - radius)
        y1   = max(0, player.y - radius)
        x2   = min(w, player.x + radius)
        y2   = min(h, player.y + radius)
        return frame[y1:y2, x1:x2], player.x - x1, player.y - y1


# ---------------------------------------------------------------------------
# DetectedObject
# ---------------------------------------------------------------------------

@dataclass
class DetectedObject:
    x: int
    y: int
    radius: int

    @property
    def centre_f(self) -> np.ndarray:
        return np.array([self.x, self.y], dtype=np.float64)

    def to_list(self) -> list:
        return [self.x, self.y, self.radius]

    def scaled_up(self, factor: float) -> "DetectedObject":
        """Scale coordinates back to native resolution after downscaled detection."""
        if factor >= 1.0:
            return self
        inv = 1.0 / factor
        return DetectedObject(
            int(round(self.x * inv)),
            int(round(self.y * inv)),
            int(round(self.radius * inv)),
        )


# ---------------------------------------------------------------------------
# Detection primitives
# ---------------------------------------------------------------------------

def _detect_by_colour(
    frame: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    min_area: int,
) -> Optional[DetectedObject]:
    hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, lower, upper)
    k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    valid = [c for c in contours if cv2.contourArea(c) >= min_area]
    if not valid:
        return None
    largest    = max(valid, key=cv2.contourArea)
    (cx, cy), r = cv2.minEnclosingCircle(largest)
    return DetectedObject(int(cx), int(cy), int(r))


def detect_ball(frame: np.ndarray, cfg: AnalyzerConfig) -> Optional[DetectedObject]:
    return _detect_by_colour(
        frame, cfg.ball_lower_hsv, cfg.ball_upper_hsv, cfg.ball_min_area)


def detect_player(frame: np.ndarray, cfg: AnalyzerConfig) -> Optional[DetectedObject]:
    return _detect_by_colour(
        frame, cfg.player_lower_hsv, cfg.player_upper_hsv, cfg.player_min_area)


# ---------------------------------------------------------------------------
# Trajectory physics
# ---------------------------------------------------------------------------

def _reflect(d: np.ndarray, n: np.ndarray) -> np.ndarray:
    n = n / np.linalg.norm(n)
    return d - 2.0 * np.dot(d, n) * n


def _intersect_v(origin, direction, x_wall, y_min, y_max):
    dx = direction[0]
    if abs(dx) < 1e-9:
        return None, None
    t = (x_wall - origin[0]) / dx
    if t <= 1e-3:
        return None, None
    y = origin[1] + t * direction[1]
    return (t, np.array([x_wall, y])) if y_min <= y <= y_max else (None, None)


def _intersect_h(origin, direction, y_wall, x_min, x_max):
    dy = direction[1]
    if abs(dy) < 1e-9:
        return None, None
    t = (y_wall - origin[1]) / dy
    if t <= 1e-3:
        return None, None
    x = origin[0] + t * direction[0]
    return (t, np.array([x, y_wall])) if x_min <= x <= x_max else (None, None)


def compute_trajectory(
    ball: DetectedObject,
    player: DetectedObject,
    frame_shape: tuple,
    cfg: AnalyzerConfig,
) -> list[tuple[int, int]]:
    """
    Ray-cast from ball along (player → ball) direction with wall reflections.
    Returns native-resolution (x, y) waypoints.
    """
    h, w  = frame_shape[:2]
    m     = cfg.margin
    x_min, x_max = float(m), float(w - m)
    y_min, y_max = float(m), float(h - m)

    raw  = ball.centre_f - player.centre_f
    norm = np.linalg.norm(raw)
    if norm < 1e-6:
        return [(ball.x, ball.y)]

    direction = raw / norm
    pos       = ball.centre_f.copy()
    waypoints = [(ball.x, ball.y)]

    walls = [
        (_intersect_v, (x_min, y_min, y_max), np.array([ 1.0,  0.0])),
        (_intersect_v, (x_max, y_min, y_max), np.array([-1.0,  0.0])),
        (_intersect_h, (y_min, x_min, x_max), np.array([ 0.0,  1.0])),
        (_intersect_h, (y_max, x_min, x_max), np.array([ 0.0, -1.0])),
    ]

    for _ in range(cfg.max_bounces + 1):
        best_t, best_pt, best_n = np.inf, pos + direction * cfg.ray_length, None
        for fn, args, normal in walls:
            t, pt = fn(pos, direction, *args)
            if t is not None and t < best_t:
                best_t, best_pt, best_n = t, pt, normal
        waypoints.append((int(best_pt[0]), int(best_pt[1])))
        if best_n is None or best_t >= cfg.ray_length:
            break
        direction = _reflect(direction, best_n)
        pos       = best_pt

    return waypoints


# ---------------------------------------------------------------------------
# High-level pipelines
# ---------------------------------------------------------------------------

def analyse_frame(frame: np.ndarray, cfg: AnalyzerConfig) -> dict:
    """
    Full detection + trajectory pipeline.

    Returns
    -------
    {"waypoints": [[x,y],...], "ball": [x,y,r]|null, "player": [x,y,r]|null}
    All coordinates are in native (full) resolution.
    """
    sf = cfg.scale_factor
    small = (cv2.resize(frame, (0, 0), fx=sf, fy=sf, interpolation=cv2.INTER_LINEAR)
             if sf < 1.0 else frame)

    ball_s   = detect_ball(small,   cfg)
    player_s = detect_player(small, cfg)

    ball   = ball_s.scaled_up(sf)   if ball_s   else None
    player = player_s.scaled_up(sf) if player_s else None

    waypoints: list[list[int]] = []
    if ball is not None and player is not None:
        pts       = compute_trajectory(ball, player, frame.shape, cfg)
        waypoints = [[x, y] for x, y in pts]

    return {
        "waypoints": waypoints,
        "ball":      ball.to_list()   if ball   else None,
        "player":    player.to_list() if player else None,
    }


def check_turn(
    frame: np.ndarray,
    cfg: AnalyzerConfig,
    turn_cfg: TurnDetectorConfig,
    detector: TurnDetector,
) -> bool:
    """
    Lightweight turn-detection for the hibernate loop.

    Runs player detection only (no ball, no trajectory).
    Fast enough to call every 0.5 s without measurable battery impact.
    """
    if not turn_cfg.enabled:
        return False
    sf    = cfg.scale_factor
    small = (cv2.resize(frame, (0, 0), fx=sf, fy=sf, interpolation=cv2.INTER_LINEAR)
             if sf < 1.0 else frame)
    ps     = detect_player(small, cfg)
    player = ps.scaled_up(sf) if ps else None
    return detector.is_your_turn(frame, player, turn_cfg)
