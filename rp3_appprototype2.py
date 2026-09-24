from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import random
import socket
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import font as tkfont

import matplotlib
matplotlib.use("TkAgg")  # must be set before pyplot-related imports
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# --------------------------------------------------------------------------
# Look and feel
# --------------------------------------------------------------------------
# Deep river blue with a single warm accent for the live curve. The curve is
# no longer the dominant element (the metronome is), but it keeps the same
# palette so the two sections read as one app.

BACKGROUND = "#0B1F2E"
PANEL = "#12324A"
PANEL_EDGE = "#1E4763"
GRID = "#1B4059"
CURVE = "#F2B441"      # this stroke
GHOST = "#5B86A5"      # previous stroke, for comparison
PEAK = "#7EE0C6"       # peak force marker and its vertical line
A70_COLOR = "#B48CE0"  # A70 marker, kept visually distinct from peak
TEXT = "#EDF4F9"
MUTED = "#8FB0C7"
OK = "#7EE0C6"         # good rhythm / drive phase-independent "good" colour
WARN = "#F2B441"       # drive phase colour, and mild feedback
BAD = "#E8715A"        # errors, session id missing, "too long/short"
RECOVERY_COLOR = "#6FA8DC"  # recovery phase colour, distinct from WARN/OK

# Column order of the CSV, identical to the original logging script.
CSV_COLUMNS = [
    "timestampMs",
    "powerWatts",
    "paceSecondsPer500Meters",
    "strokesPerMinute",
    "driveTimeSeconds",
    "recoverTimeSeconds",
    "peakForceNewtons",
    "strokeLengthMeters",
    "peakForcePositionMeters",
    "relativePeakForcePositionFraction",
    "energyJoules",
    "dragFactor",
    "forceCurve",
]


def pick_font(root: tk.Tk) -> str:
    """First available of a short preference list, so the window looks the
    same-ish on Windows, macOS and Linux without shipping a font file."""
    available = {name.lower() for name in tkfont.families(root)}
    for candidate in ("Inter", "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial"):
        if candidate.lower() in available:
            return candidate
    return "TkDefaultFont"


# --------------------------------------------------------------------------
# CSV logging  (unchanged)
# --------------------------------------------------------------------------
class StrokeLogger:
    """Appends one row per stroke and flushes immediately.

    Flushing every stroke costs nothing at 20-30 strokes per minute and means
    a crash or a yanked cable never loses more than the stroke in progress.
    """

    def __init__(self, path: str):
        self.path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._file = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if is_new:
            self._writer.writerow(CSV_COLUMNS)
            self._file.flush()

    def write(self, packet: dict) -> None:
        row = []
        for column in CSV_COLUMNS:
            value = packet.get(column)
            # forceCurve is a list, so store it as a JSON string in one cell.
            row.append(json.dumps(value) if column == "forceCurve" else value)
        self._writer.writerow(row)
        self._file.flush()

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Background socket thread  (unchanged)
# --------------------------------------------------------------------------
class StrokeReader(threading.Thread):
    """Reads newline-delimited JSON from the RP3 and posts it to a queue.

    Messages placed on the queue:
        ("stroke", packet_dict)
        ("status", text, state)   where state is "ok" | "wait" | "error"
    """

    def __init__(self, host: str, port: int, out_queue: "queue.Queue", logger: StrokeLogger | None):
        super().__init__(daemon=True, name="StrokeReader")
        self.host = host
        self.port = port
        self.out = out_queue
        self.logger = logger
        self.stop_event = threading.Event()

    def _status(self, text: str, state: str = "ok") -> None:
        self.out.put(("status", text, state))

    def run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._status(f"Connecting to {self.host}:{self.port}", "wait")
                with socket.create_connection((self.host, self.port), timeout=5.0) as sock:
                    # A short recv timeout lets the thread notice stop_event
                    # promptly even when the rower is resting between pieces.
                    sock.settimeout(1.0)
                    self._status(f"Connected to {self.host}:{self.port}", "ok")
                    backoff = 1.0
                    self._read_forever(sock)
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self._status(f"No connection ({exc}). Retrying in {backoff:.0f} s", "error")
                self.stop_event.wait(backoff)  # still reacts to a window close
                backoff = min(backoff * 1.7, 10.0)

    def _read_forever(self, sock: socket.socket) -> None:
        """Buffer bytes and emit every complete line.

        The buffer stays as *bytes* rather than str: a single recv() can cut a
        multi-byte UTF-8 character in half, and decoding half a character
        raises. Splitting on b"\\n" first and decoding whole lines is safe.
        """
        buffer = b""

        while not self.stop_event.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue  # simply no stroke finished in the last second
            if not chunk:
                raise ConnectionError("the RP3 closed the connection")

            buffer += chunk

            # One recv may hold a fragment, one packet, or several packets.
            # Loop until no complete line is left, and keep the remainder.
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    packet = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._status(f"Skipped a malformed packet ({exc})", "error")
                    continue

                if self.logger is not None:
                    try:
                        self.logger.write(packet)
                    except Exception as exc:
                        self._status(f"CSV write failed: {exc}", "error")

                self.out.put(("stroke", packet))

    def stop(self) -> None:
        self.stop_event.set()


class DemoReader(threading.Thread):
    """Generates strokes locally, with no socket at all (--demo).

    To test the real network path instead, run rp3_simulator.py in a second
    terminal and point the dashboard at 127.0.0.1.
    """

    def __init__(self, out_queue: "queue.Queue", logger: StrokeLogger | None, speed: float = 1.0):
        super().__init__(daemon=True, name="DemoReader")
        self.out = out_queue
        self.logger = logger
        self.speed = speed
        self.stop_event = threading.Event()

    def run(self) -> None:
        from rp3_simulator import make_stroke_packet

        self.out.put(("status", "Demo mode: simulated strokes, no RP3 connected", "wait"))
        rng = random.Random()
        start = time.time()
        while not self.stop_event.is_set():
            packet = make_stroke_packet(int((time.time() - start) * 1000), skill=0.6, rng=rng)
            if self.logger is not None:
                self.logger.write(packet)
            self.out.put(("stroke", packet))
            self.stop_event.wait(60.0 / packet["strokesPerMinute"] / max(self.speed, 0.1))

    def stop(self) -> None:
        self.stop_event.set()


# --------------------------------------------------------------------------
# Formatting helpers  (unchanged, plus percent())
# --------------------------------------------------------------------------
def format_pace(seconds_per_500m) -> str:
    """Seconds per 500 m as m:ss.s, which is how rowers read pace."""
    try:
        seconds = float(seconds_per_500m)
    except (TypeError, ValueError):
        return "–"
    if seconds <= 0 or seconds > 3600:
        return "–"
    minutes = int(seconds // 60)
    return f"{minutes}:{seconds - minutes * 60:04.1f}"


def number(value, decimals: int = 1) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "–"


def percent(fraction, decimals: int = 0) -> str:
    """0.317 -> '32%'. Used everywhere a *_fraction analysis value is shown,
    since a beginner reads percentages faster than raw fractions."""
    try:
        return f"{float(fraction) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "–"


def parse_force_curve(raw, stroke_length_m: float):
    """Return (positions_m, forces_N) from the packet's forceCurve.

    Tolerant on purpose: the field may arrive as a list of dicts, as a JSON
    string (if it was read back from the CSV), or as bare force values with no
    positions, in which case the samples are spread evenly over the drive.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [], []
    if not raw:
        return [], []

    positions, forces = [], []
    for point in raw:
        if isinstance(point, dict):
            position = point.get("strokeLengthMeters", point.get("x"))
            force = point.get("forceNewtons", point.get("y"))
        else:
            position, force = None, point
        if force is None:
            continue
        positions.append(position)
        forces.append(float(force))

    if any(p is None for p in positions):
        span = stroke_length_m if stroke_length_m and stroke_length_m > 0 else 1.0
        divisor = max(len(forces) - 1, 1)
        positions = [span * i / divisor for i in range(len(forces))]
    else:
        positions = [float(p) for p in positions]

    return positions, forces


# --------------------------------------------------------------------------
# Analysis  (unchanged from the attached file)
# --------------------------------------------------------------------------
def analyze_stroke(packet: dict) -> dict:
    """
    Calculate technique-related variables from one RP3 stroke.

    Everything returned here is either:
    - measured directly by the RP3, or
    - calculated from RP3 data.

    Deliberately does NOT infer body-segment sequencing (legs/trunk/arms):
    the RP3 gives handle force and timing only, no joint angles or segment
    velocities, so that transition cannot actually be measured from this
    packet and the analysis does not pretend otherwise.
    """

    analysis = {}

    # --------------------------------------------------
    # 1. TIMING / RHYTHM
    # --------------------------------------------------

    drive = packet.get("driveTimeSeconds")
    recovery = packet.get("recoverTimeSeconds")

    try:
        drive = float(drive)
        recovery = float(recovery)

        cycle_time = drive + recovery

        analysis["cycle_time"] = cycle_time

        # How much of the stroke cycle is drive?
        analysis["drive_fraction"] = drive / cycle_time

        # Recovery time relative to drive time
        analysis["recovery_drive_ratio"] = recovery / drive

        # Stroke rate calculated from the measured times
        analysis["calculated_spm"] = 60 / cycle_time

    except (TypeError, ValueError, ZeroDivisionError):
        analysis["cycle_time"] = None
        analysis["drive_fraction"] = None
        analysis["recovery_drive_ratio"] = None
        analysis["calculated_spm"] = None


    # --------------------------------------------------
    # 2. FORCE CURVE
    # --------------------------------------------------

    stroke_length = float(packet.get("strokeLengthMeters") or 0)

    positions, forces = parse_force_curve(
        packet.get("forceCurve"),
        stroke_length
    )

    analysis["a70"] = None
    analysis["peak_position_fraction"] = None

    if len(positions) >= 2 and len(forces) >= 2:

        # Find actual maximum force
        peak_index = max(
            range(len(forces)),
            key=lambda i: forces[i]
        )

        peak_force = forces[peak_index]
        peak_position = positions[peak_index]

        analysis["peak_force"] = peak_force
        analysis["peak_position_m"] = peak_position


        # --------------------------------------------------
        # 3. RELATIVE PEAK POSITION
        # --------------------------------------------------

        curve_start = positions[0]
        curve_end = positions[-1]

        curve_length = curve_end - curve_start

        if curve_length > 0:

            analysis["peak_position_fraction"] = (
                (peak_position - curve_start)
                / curve_length
            )


            # --------------------------------------------------
            # 4. A70
            # --------------------------------------------------

            force_70 = 0.70 * peak_force

            # Only search on the RISING part of the curve,
            # from start until peak.
            for i in range(1, peak_index + 1):

                if forces[i] >= force_70:

                    # Linear interpolation between the point below
                    # 70% and the point above 70%
                    x1 = positions[i - 1]
                    x2 = positions[i]

                    y1 = forces[i - 1]
                    y2 = forces[i]

                    if y2 != y1:

                        fraction_between = (
                            (force_70 - y1)
                            / (y2 - y1)
                        )

                        x70 = (
                            x1
                            + fraction_between * (x2 - x1)
                        )

                    else:
                        x70 = x2

                    analysis["a70"] = (
                        (x70 - curve_start)
                        / curve_length
                    )

                    analysis["a70_position_m"] = x70

                    break


    # --------------------------------------------------
    # 5. VALUES ALREADY PROVIDED BY RP3
    # --------------------------------------------------

    analysis["rp3_spm"] = packet.get("strokesPerMinute")
    analysis["stroke_length"] = packet.get("strokeLengthMeters")
    analysis["energy"] = packet.get("energyJoules")

    return analysis


# --------------------------------------------------------------------------
# Coaching feedback — one rule set, used by the banner. Kept as a plain
# function of (analysis, targets) so it can be unit-tested or replayed over
# a logged CSV without any tkinter involved.
# --------------------------------------------------------------------------
def feedback_from_analysis(analysis: dict, targets: "MetronomeTargets", tolerance: float = 0.03) -> tuple[str, str]:
    """Return (message, colour_key) for the single feedback line.

    colour_key is one of "ok", "warn", "bad", "muted" so the caller decides
    the actual colour — this function only judges the numbers.

    Only ever compares drive_fraction and recovery_drive_ratio against the
    prototype targets; nothing here claims to see body position.
    """
    drive_fraction = analysis.get("drive_fraction")
    recovery_ratio = analysis.get("recovery_drive_ratio")

    if drive_fraction is None:
        return "COLLECTING DATA…", "muted"

    drive_error = drive_fraction - targets.target_drive_fraction

    if drive_error > tolerance:
        return "DRIVE TOO LONG", "bad"
    if drive_error < -tolerance:
        return "DRIVE TOO SHORT", "bad"

    # Drive length is on target; check whether the recovery was rushed.
    # target_recovery_drive_ratio comes from the same two target fractions,
    # so a recovery that is too short shows up as a ratio below target.
    if recovery_ratio is not None:
        target_ratio = targets.target_recovery_fraction / targets.target_drive_fraction
        if recovery_ratio < target_ratio - tolerance * target_ratio:
            return "SLOW DOWN THE RECOVERY", "warn"

    return "GOOD RHYTHM", "ok"


# --------------------------------------------------------------------------
# Metronome targets — one small object instead of six loose attributes on
# Dashboard, so _build_metronome / update_metronome / feedback all read from
# the same place and a future "difficulty" selector only has to replace this.
# --------------------------------------------------------------------------
class MetronomeTargets:
    def __init__(self, target_spm: float = 20.0,
                 target_drive_fraction: float = 0.30,
                 target_recovery_fraction: float = 0.70):
        self.target_spm = target_spm
        self.target_drive_fraction = target_drive_fraction
        self.target_recovery_fraction = target_recovery_fraction

        self.cycle_time = 60.0 / target_spm
        self.target_drive_time = self.cycle_time * target_drive_fraction
        self.target_recovery_time = self.cycle_time * target_recovery_fraction


# --------------------------------------------------------------------------
# The metronome widget: a phase word, a moving dot on a catch-finish track,
# and a thin progress bar for the current phase. Pure display — it is told
# the phase and fraction each tick, it does not know about time itself.
# --------------------------------------------------------------------------
class MetronomeView(tk.Frame):
    TRACK_MARGIN = 46
    def __init__(self, parent, family: str):

        super().__init__(
            parent,
            bg=PANEL,
            highlightbackground=PANEL_EDGE,
            highlightthickness=1,
            bd=0
        )
        self.family = family


        # Container for donut + text
        content = tk.Frame(self, bg=PANEL)
        content.pack(padx=20, pady=10)

        # -----------------------------
        # Donut on the LEFT
        # -----------------------------

        self.canvas = tk.Canvas(
            content,
            bg=PANEL,
            highlightthickness=0,
            width=220,
            height=220
        )

        self.canvas.pack(
            side="left",
            padx=(0, 100)
        )

        # -----------------------------
        # DRIVE / RECOVERY on the RIGHT
        # -----------------------------

        self.phase_label = tk.Label(
            content,
            text="READY",
            bg=PANEL,
            fg=MUTED,
            font=(family, 32, "bold"), width=10, anchor="center"
        )

        self.phase_label.pack(
            side="left",
            padx=(10, 10)
        )

        #self.canvas.bind(
        #    "<Configure>",
        #    self._on_resize
        #)

        # One consistent geometry
        self._width = 220
        self._height = 220

        self.center_x = 110
        self.center_y = 110
        self.radius = 75

        self._background_arc = None
        self._progress_arc = None
        self._dot = None

        self._draw_static()
    # --------------------------------------------------
    # Resize
    # --------------------------------------------------

    def _on_resize(self, _event=None):

        self._draw_static()


    # --------------------------------------------------
    # Draw donut
    # --------------------------------------------------

    def _draw_static(self):

        self.canvas.delete("all")

        center_x = self.center_x
        center_y = self.center_y
        radius = self.radius

        x1 = center_x - radius
        y1 = center_y - radius
        x2 = center_x + radius
        y2 = center_y + radius

        # Background ring
        self._background_arc = self.canvas.create_arc(
            x1,
            y1,
            x2,
            y2,
            start=90,
            extent=-359.9,
            style="arc",
            outline=GRID,
            width=18
        )

        # Progress ring
        self._progress_arc = self.canvas.create_arc(
            x1,
            y1,
            x2,
            y2,
            start=90,
            extent=0,
            style="arc",
            outline=MUTED,
            width=18
        )

        # Dot starts at top
        dot_radius = 9

        dot_x = center_x
        dot_y = center_y - radius

        self._dot = self.canvas.create_oval(
            dot_x - dot_radius,
            dot_y - dot_radius,
            dot_x + dot_radius,
            dot_y + dot_radius,
            fill=MUTED,
            outline=PANEL,
            width=2
        )
        
    # Update phase
    # --------------------------------------------------

    def set_phase(self, phase: str, fraction: float):

        # Keep fraction between 0 and 1
        fraction = min(
            max(fraction, 0.0),
            1.0
        )

        if phase == "drive":
            colour = WARN

        else:
            colour = RECOVERY_COLOR


        # Update text
        self.phase_label.config(
            text=phase.upper(),
            fg=colour
        )


        # -----------------------------
        # Update donut progress
        # -----------------------------

        angle = 360 * fraction

        self.canvas.itemconfig(
            self._progress_arc,
            extent=-angle,
            outline=colour
        )


        # -----------------------------
        # Move dot around the donut
        # -----------------------------

        center_x = self.center_x
        center_y = self.center_y
        radius = self.radius

        # Start at top (-90 degrees)
        angle_degrees = -90 + (360 * fraction)

        angle_radians = math.radians(
            angle_degrees
        )


        dot_x = (
            center_x
            + radius * math.cos(angle_radians)
        )

        dot_y = (
            center_y
            + radius * math.sin(angle_radians)
        )


        dot_radius = 9

        self.canvas.coords(
            self._dot,
            dot_x - dot_radius,
            dot_y - dot_radius,
            dot_x + dot_radius,
            dot_y + dot_radius
        )


        self.canvas.itemconfig(
            self._dot,
            fill=colour
        )


    # --------------------------------------------------
    # Idle
    # --------------------------------------------------

    def set_idle(self):

        self.phase_label.config(
            text="READY",
            fg=MUTED
        )

        self.canvas.itemconfig(
            self._progress_arc,
            extent=0,
            outline=MUTED
        )

        center_x = self.center_x
        center_y = self.center_y
        radius = self.radius

        radius = 90
        dot_radius = 9

        dot_x = center_x
        dot_y = center_y - radius

        self.canvas.coords(
            self._dot,
            dot_x - dot_radius,
            dot_y - dot_radius,
            dot_x + dot_radius,
            dot_y + dot_radius
        )

        self.canvas.itemconfig(
            self._dot,
            fill=MUTED
        )

# --------------------------------------------------------------------------
# A small "traffic light" style banner for the one-line coaching message.
# --------------------------------------------------------------------------
class FeedbackBanner(tk.Frame):
    COLOURS = {"ok": OK, "warn": WARN, "bad": BAD, "muted": MUTED}

    def __init__(self, parent, family: str):
        super().__init__(parent, bg=BACKGROUND)
        self.label = tk.Label(self, text="COLLECTING DATA…", bg=BACKGROUND, fg=MUTED,
                              font=(family, 22, "bold"))
        self.label.pack(pady=(4, 4))

    def set_message(self, text: str, colour_key: str) -> None:
        self.label.config(text=text, fg=self.COLOURS.get(colour_key, MUTED))


# --------------------------------------------------------------------------
# A small block of secondary numbers. Deliberately not the MetricTile grid
# from the old data-dashboard: four values, small type, off to one side.
# --------------------------------------------------------------------------
class KpiStrip(tk.Frame):
    def __init__(self, parent, family: str):
        super().__init__(parent, bg=PANEL, highlightbackground=PANEL_EDGE,
                         highlightthickness=1, bd=0)
        self.values: dict[str, tk.Label] = {}
        definitions = [
            ("a70", "A70"),
            ("peak_pos", "Peak force position"),
            ("drive_pct", "Drive % of cycle"),
            ("ratio", "Recovery : drive"),
        ]
        for row, (key, name) in enumerate(definitions):
            cell = tk.Frame(self, bg=PANEL)
            cell.pack(fill="x", padx=14, pady=(12 if row == 0 else 6, 6))
            value = tk.Label(cell, text="–", bg=PANEL, fg=TEXT, font=(family, 18, "bold"))
            value.pack(anchor="w")
            tk.Label(cell, text=name, bg=PANEL, fg=MUTED, font=(family, 8)).pack(anchor="w")
            self.values[key] = value
        tk.Frame(self, bg=PANEL, height=6).pack()

    def update_from_analysis(self, analysis: dict) -> None:
        a70 = analysis.get("a70")
        self.values["a70"].config(text=percent(a70) if a70 is not None else "–")

        peak_pos = analysis.get("peak_position_fraction")
        self.values["peak_pos"].config(text=percent(peak_pos) if peak_pos is not None else "–")

        drive_fraction = analysis.get("drive_fraction")
        self.values["drive_pct"].config(text=percent(drive_fraction) if drive_fraction is not None else "–")

        ratio = analysis.get("recovery_drive_ratio")
        self.values["ratio"].config(text=f"1 : {ratio:.1f}" if ratio is not None else "–")


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class Dashboard:
    def __init__(self, root: tk.Tk, source_description: str, participant_id: str = "", session_id: str = ""):
        self.root = root
        self.participant_id = participant_id
        self.session_id = session_id
        self.queue: "queue.Queue" = queue.Queue()
        self.stroke_count = 0
        self.previous_curve = None      # (positions, forces) of the last stroke
        self.recent_peaks: deque = deque(maxlen=20)
        self.recent_lengths: deque = deque(maxlen=20)
        self.fill = None                # matplotlib collection, replaced each stroke
        self.recent_analysis: deque = deque(maxlen=5)
        self.latest_analysis: dict | None = None

        self.targets = MetronomeTargets(target_spm=20.0,
                                        target_drive_fraction=0.30,
                                        target_recovery_fraction=0.70)

        self.metronome_running = False
        self.metronome_start_time = 0.0
        self.session_finished = False

        root.title("RP3 rowing trainer")
        root.geometry("1180x900")
        root.configure(bg=BACKGROUND)
        root.minsize(900, 760)
        self.family = pick_font(root)

        self._build_layout(source_description)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._running = True
        # The single point where background data enters the GUI thread.
        self.root.after(40, self.drain_queue)

    # -- construction -----------------------------------------------------
    def _build_layout(self, source_description: str) -> None:
        outer = tk.Frame(self.root, bg=BACKGROUND)
        outer.pack(fill="both", expand=True, padx=18, pady=14)

        self._build_header(outer)
        self._build_controls(outer)
        self.metronome_view = MetronomeView(outer, self.family)
        self.metronome_view.pack(fill="x", pady=(12, 8))
         
        self._build_feedback(outer)
        

        middle = tk.Frame(outer, bg=BACKGROUND)
        middle.pack(fill="both", expand=True, pady=(8, 8))
        middle.columnconfigure(0, weight=1)
        middle.columnconfigure(1, weight=0)
        middle.rowconfigure(0, weight=1)

        self._build_force_plot(middle)

        right = tk.Frame(middle, bg=BACKGROUND, width=230)
        right.grid(row=0, column=1, sticky="ns", padx=(12, 0))
        right.grid_propagate(False)
        self.kpi_strip = KpiStrip(right, self.family)
        self.kpi_strip.pack(fill="both", expand=True)

        

        self.status = tk.Label(outer, text="● " + source_description, bg=BACKGROUND,
                               fg=MUTED, font=(self.family, 9), anchor="w")
        self.status.pack(fill="x", pady=(6, 0))

    def _build_header(self, parent) -> None:
        """Participant ID, session ID, target stroke rate, current measured
        stroke rate — the only things a rower needs to confirm before they
        start pulling."""
        header = tk.Frame(parent, bg=PANEL, highlightbackground=PANEL_EDGE,
                          highlightthickness=1, bd=0)
        header.pack(fill="x")

        def cell(text_top, text_var):
            box = tk.Frame(header, bg=PANEL)
            box.pack(side="left", expand=True, fill="x", padx=14, pady=10)
            tk.Label(box, text=text_top, bg=PANEL, fg=MUTED,
                     font=(self.family, 8)).pack(anchor="w")
            tk.Label(box, textvariable=text_var, bg=PANEL, fg=TEXT,
                     font=(self.family, 19, "bold")).pack(anchor="w")

        self.participant_var = tk.StringVar(value=self.participant_id or "—")
        self.session_var = tk.StringVar(value=self.session_id or "—")
        self.target_spm_var = tk.StringVar(value=f"{self.targets.target_spm:.0f} spm")
        self.current_spm_var = tk.StringVar(value="–")

        cell("PARTICIPANT", self.participant_var)
        cell("SESSION", self.session_var)
        cell("TARGET RATE", self.target_spm_var)
        cell("CURRENT RATE", self.current_spm_var)

    def _build_feedback(self, parent) -> None:
        """One line of coaching text — GOOD RHYTHM / DRIVE TOO LONG / etc,
        computed by feedback_from_analysis() from the existing analyze_stroke
        output. Kept as its own method so the rule set can grow without
        touching layout code."""
        self.feedback = FeedbackBanner(parent, self.family)
        self.feedback.pack(fill="x")

    def _build_force_plot(self, parent) -> None:
        """Same plot as before (current stroke, ghost of the previous one,
        peak marker) with an A70 marker added, kept secondary in size."""
        panel = tk.Frame(parent, bg=PANEL, highlightbackground=PANEL_EDGE,
                         highlightthickness=1, bd=0)
        panel.grid(row=0, column=0, sticky="nsew")

        header = tk.Frame(panel, bg=PANEL)
        header.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(header, text="Force through the drive", bg=PANEL, fg=TEXT,
                 font=(self.family, 12, "bold")).pack(side="left")
        self.stroke_label = tk.Label(header, text="waiting for your first stroke",
                                     bg=PANEL, fg=MUTED, font=(self.family, 9))
        self.stroke_label.pack(side="right")

        self.figure = Figure(figsize=(6.2, 3.2), dpi=100, facecolor=PANEL)
        self.ax = self.figure.add_subplot(111)
        self.ax.set_facecolor(PANEL)
        self.ax.set_xlabel("Stroke length (m)", color=MUTED, fontsize=10)
        self.ax.set_ylabel("Force (N)", color=MUTED, fontsize=10)
        self.ax.tick_params(colors=MUTED, labelsize=8)
        for side in self.ax.spines.values():
            side.set_color(GRID)
        self.ax.grid(True, color=GRID, alpha=0.5, linewidth=0.7)
        self.ax.set_xlim(0, 1.5)
        self.ax.set_ylim(0, 700)

        (self.ghost_line,) = self.ax.plot([], [], color=GHOST, linewidth=1.4,
                                          linestyle="--", zorder=2)
        (self.curve_line,) = self.ax.plot([], [], color=CURVE, linewidth=2.2, zorder=3)
        self.peak_line = self.ax.axvline(0, color=PEAK, linewidth=1.0,
                                         linestyle=":", zorder=4, visible=False)
        (self.peak_point,) = self.ax.plot([], [], "o", color=PEAK, markersize=7,
                                          markeredgecolor=PANEL, markeredgewidth=1.3,
                                          zorder=5)
        self.peak_text = self.ax.text(0, 0, "", color=PEAK, fontsize=9, ha="center", va="bottom", zorder=6)
        (self.a70_point,) = self.ax.plot([], [], "o", color=A70_COLOR, markersize=6,
                                         markeredgecolor=PANEL, markeredgewidth=1.2,
                                         zorder=5)
        self.figure.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.figure, master=panel)
        self.canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=6)
        self.canvas.draw()

        legend = tk.Frame(panel, bg=PANEL)
        legend.pack(fill="x", padx=16, pady=(0, 10))
        for colour, text in ((CURVE, "━ this stroke"), (GHOST, "╍ stroke before"),
                             (PEAK, "● peak force"), (A70_COLOR, "● A70")):
            tk.Label(legend, text=text + "   ", bg=PANEL, fg=colour,
                     font=(self.family, 8)).pack(side="left")

    def _build_controls(self, parent) -> None:
        """Start/stop for the metronome guide, separate from the TCP
        connection: the reader keeps running (and logging) regardless, this
        just starts and stops the visual/rhythm guide and marks the
        session as finished for the status line."""
        bar = tk.Frame(parent, bg=BACKGROUND)
        bar.pack(fill="x", pady=(10, 0))

        self.start_button = tk.Button(bar, text="Start metronome", command=self.start_metronome,
                                      bg=OK, fg=BACKGROUND, activebackground=OK,
                                      font=(self.family, 11, "bold"), relief="flat",
                                      padx=16, pady=8)
        self.start_button.pack(side="left")

        self.finish_button = tk.Button(bar, text="Finish session", command=self.finish_session,
                                       bg=BAD, fg=BACKGROUND, activebackground=BAD,
                                       font=(self.family, 11, "bold"), relief="flat",
                                       padx=16, pady=8)
        self.finish_button.pack(side="left", padx=(10, 0))

    # -- data handling ----------------------------------------------------
    def drain_queue(self) -> None:
        """Take everything waiting, but only redraw for the newest stroke."""
        newest_stroke = None
        while True:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break

            if message[0] == "stroke":
                newest_stroke = message[1]
                self.stroke_count += 1
            elif message[0] == "status":
                _, text, state = message
                colour = {"ok": OK, "wait": WARN, "error": BAD}.get(state, MUTED)
                self.status.config(text="● " + text, fg=colour)

        if newest_stroke is not None:
            self.show_stroke(newest_stroke)

        if self._running:
            self.root.after(40, self.drain_queue)

    def show_stroke(self, packet: dict) -> None:
        # analyze_stroke() is the one and only place these numbers are
        # computed; everything below just reads from its result.
        analysis = analyze_stroke(packet)
        self.recent_analysis.append(analysis)
        self.latest_analysis = analysis

        rp3_spm = analysis.get("rp3_spm")
        self.current_spm_var.set(f"{float(rp3_spm):.1f} spm" if rp3_spm is not None else "–")

        message, colour_key = feedback_from_analysis(analysis, self.targets)
        self.feedback.set_message(message, colour_key)

        self.kpi_strip.update_from_analysis(analysis)

        self._update_force_plot(packet, analysis)

    def _update_force_plot(self, packet: dict, analysis: dict) -> None:
        stroke_length = float(packet.get("strokeLengthMeters") or 0.0)
        positions, forces = parse_force_curve(packet.get("forceCurve"), stroke_length)

        if not (positions and forces):
            return

        # Fade the stroke that was on screen a moment ago into the ghost, so
        # a rower can see whether two strokes look alike.
        if self.previous_curve:
            self.ghost_line.set_data(*self.previous_curve)
        self.curve_line.set_data(positions, forces)
        self.previous_curve = (positions, forces)

        # fill_between makes a new collection each call, so drop the old one.
        if self.fill is not None:
            self.fill.remove()
        self.fill = self.ax.fill_between(positions, forces, color=CURVE,
                                         alpha=0.10, zorder=1)

        peak_x = packet.get("peakForcePositionMeters")
        peak_y = packet.get("peakForceNewtons")
        if peak_x is None or peak_y is None:
            peak_index = max(range(len(forces)), key=lambda i: forces[i])
            peak_x, peak_y = positions[peak_index], forces[peak_index]
        peak_x, peak_y = float(peak_x), float(peak_y)

        self.peak_point.set_data([peak_x], [peak_y])
        self.peak_line.set_xdata([peak_x, peak_x])
        self.peak_line.set_visible(True)

        peak_fraction = analysis.get("peak_position_fraction")

        if peak_fraction is not None:
            self.peak_text.set_text(
                f"{peak_y:.0f} N\n{peak_fraction * 100:.0f}%"
            )
        else:
            self.peak_text.set_text(
                f"{peak_y:.0f} N"
            )

        self.peak_text.set_position(
            (peak_x, peak_y * 1.04)
        )

        a70_x = analysis.get("a70_position_m")
        if a70_x is not None:
            # A70 is interpolated on the rising curve; look up its force by
            # linear interpolation between the same two samples so the dot
            # sits on the drawn line rather than floating off it.
            a70_y = self._interpolate_force(positions, forces, float(a70_x))
            self.a70_point.set_data([a70_x], [a70_y])
        else:
            self.a70_point.set_data([], [])

        self.recent_peaks.append(peak_y)
        self.recent_lengths.append(max(positions))
        self._rescale_axes()
        self.canvas.draw_idle()

        self.stroke_label.config(text=f"stroke {self.stroke_count}")

    @staticmethod
    def _interpolate_force(positions: list[float], forces: list[float], x: float) -> float:
        for i in range(1, len(positions)):
            if positions[i - 1] <= x <= positions[i]:
                x1, x2 = positions[i - 1], positions[i]
                y1, y2 = forces[i - 1], forces[i]
                if x2 == x1:
                    return y1
                return y1 + (y2 - y1) * (x - x1) / (x2 - x1)
        return forces[-1]

    def _rescale_axes(self) -> None:
        """Keep the axes steady.

        Ranges follow the recent maximum instead of the current stroke, so the
        picture does not jump around between strokes; a shape that changes is
        then a real change in the rowing, not in the scaling.
        """
        x_max = max(max(self.recent_lengths, default=1.0) * 1.08, 1.0)
        y_max = max(max(self.recent_peaks, default=300.0) * 1.20, 200.0)
        self.ax.set_xlim(0, x_max)
        self.ax.set_ylim(0, y_max)

    # -- metronome ----------------------------------------------------
    def start_metronome(self) -> None:
        if self.session_finished:
            return  # a finished session does not restart from this screen
        self.metronome_running = True
        self.metronome_start_time = time.time()
        self.start_button.config(text="Metronome running", state="disabled")
        self.update_metronome()

    def update_metronome(self) -> None:
        if not self.metronome_running:
            return

        elapsed = time.time() - self.metronome_start_time
        position_in_cycle = elapsed % self.targets.cycle_time

        if position_in_cycle < self.targets.target_drive_time:
            fraction = position_in_cycle / self.targets.target_drive_time
            self.metronome_view.set_phase("drive", fraction)
        else:
            recovery_elapsed = position_in_cycle - self.targets.target_drive_time
            fraction = recovery_elapsed / self.targets.target_recovery_time
            self.metronome_view.set_phase("recovery", fraction)

        # ~33 Hz: smooth enough for a moving dot without loading the CPU.
        self.root.after(30, self.update_metronome)

    def stop_metronome(self) -> None:
        self.metronome_running = False
        self.metronome_view.set_idle()
        self.start_button.config(text="Start metronome", state="normal")

    def finish_session(self) -> None:
        self.stop_metronome()
        self.session_finished = True
        self.finish_button.config(state="disabled")
        self.feedback.set_message("SESSION FINISHED", "muted")
        # A post-session summary (stroke-to-stroke consistency, average
        # drive %, etc. from self.recent_analysis / the CSV) is the natural
        # next screen here; left as a hook rather than guessed at.

    # -- shutdown ---------------------------------------------------------
    def on_close(self) -> None:
        self._running = False
        self.metronome_running = False
        self.root.destroy()


# --------------------------------------------------------------------------
# Initial window: participant's information  (unchanged)
# --------------------------------------------------------------------------
def get_session_info():
    """Display a simple dialog to get the participant's information."""
    info_window = tk.Tk()
    info_window.title("Session Information")
    info_window.geometry("800x700")
    info_window.configure(bg=MUTED)

    result = {}

    tk.Label(info_window, text="Participant ID:", bg=BACKGROUND, fg=TEXT).pack(pady=(190, 5))
    participant_code = tk.Entry(info_window)
    participant_code.pack()

    tk.Label(info_window, text="Session ID:", bg=BACKGROUND, fg=TEXT).pack(pady=(10, 5))
    session_number = tk.Entry(info_window)
    session_number.pack()

    def submit_info():
        result["participant_id"] = participant_code.get()
        result["session_id"] = session_number.get()
        info_window.destroy()
        # You can store or use the participant_id and session_id as needed
        print(f"Participant ID: {result['participant_id']}, Session ID: {result['session_id']}")

    submit_button = tk.Button(info_window, text="Start Session", command=submit_info)
    submit_button.pack(pady=(20, 0))
    info_window.mainloop()
    return result["participant_id"], result["session_id"]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:

    participant_id, session_id = get_session_info()

    print(participant_id)
    print(session_id)

    parser = argparse.ArgumentParser(description="RP3 beginner rowing trainer (tkinter).")
    parser.add_argument("--host", default="localhost", help="RP3 IP address")
    parser.add_argument("--port", type=int, default=64649, help="RP3 TCP port")
    parser.add_argument("--csv", default=None,
                        help="CSV file to append to (default: <participant>_<session>.csv in ./logs)")
    parser.add_argument("--no-csv", action="store_true", help="Do not log to CSV")
    parser.add_argument("--demo", action="store_true",
                        help="Generate strokes internally instead of connecting to anything")
    args = parser.parse_args()

    logger = None
    if not args.no_csv:
        path = args.csv
        if path is None:
            os.makedirs("logs", exist_ok=True)
            filename = f"{participant_id}_{session_id}.csv"
            path = os.path.join("logs", filename)
        logger = StrokeLogger(path)

    source = "Demo mode" if args.demo else f"Connecting to {args.host}:{args.port}"
    if logger is not None:
        source += f" — logging to {logger.path}"

    root = tk.Tk()
    window = Dashboard(root, source, participant_id, session_id)

    reader = (DemoReader(window.queue, logger)
              if args.demo else StrokeReader(args.host, args.port, window.queue, logger))
    reader.start()

    try:
        root.mainloop()
    finally:
        reader.stop()
        reader.join(timeout=2.0)
        if logger is not None:
            logger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
