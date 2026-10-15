from __future__ import annotations

from pathlib import Path
from PIL import Image,ImageTk
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
import statistics
from collections import deque
from tkinter import font as tkfont

from cv2 import phase
import matplotlib
matplotlib.use("TkAgg")  # must be set before pyplot-related imports
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from dataclasses import dataclass

from datetime import datetime

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

BASE_DIR=Path(__file__).resolve().parent
ASSETS_DIR=BASE_DIR/"assets"

APP_DATA_DIR=Path.home()/"RP3 Rowing Experiment"
LOGS_DIR=APP_DATA_DIR/"logs"

LOGS_DIR.mkdir(parents=True,exist_ok=True)

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
    "peakTargetHit",
    "peakPositionError",
    "peakPositionErrorPercent",
]
TARGET_SPM=26.0

DRIVE_FRACTION=1/3
RECOVERY_FRACTION=2/3

TRIAL_DURATION_SECONDS=15
REST_DURATION_SECONDS=5
WARMUP_DURATION_SECONDS=2*60

PEAK_POSITION_MIN=0.32
PEAK_POSITION_TARGET=0.35
PEAK_POSITION_MAX=0.38


def pick_font(root: tk.Tk) -> str:
    """First available of a short preference list, so the window looks the
    same-ish on Windows, macOS and Linux without shipping a font file."""
    available = {name.lower() for name in tkfont.families(root)}
    for candidate in ("Inter", "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial"):
        if candidate.lower() in available:
            return candidate
    return "TkDefaultFont"
def _mix(c1, c2, t):
    """Blend two palette colours (t=0 -> c1, t=1 -> c2). Used only for fades/glows."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class StrokeLogger:

    def __init__(self, path: str):
        self.path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._file = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if is_new:
            self._writer.writerow(CSV_COLUMNS)
            self._file.flush()

    def write(self, packet: dict) -> None:

        packet=dict(packet)
        peak_position=packet.get("relativePeakForcePositionFraction")

        try:
            peak_position=float(peak_position)

            packet["peakTargetHit"]=int(
                PEAK_POSITION_MIN<=peak_position<=PEAK_POSITION_MAX)

            packet["peakPositionError"]=abs(
                peak_position-PEAK_POSITION_TARGET)

            packet["peakPositionErrorPercent"]=abs(
                peak_position-PEAK_POSITION_TARGET)*100

        except (TypeError,ValueError):
            packet["peakTargetHit"]=""
            packet["peakPositionError"]=""
            packet["peakPositionErrorPercent"]=""

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

class MetronomeTargets:
    def __init__(self):
        self.target_spm=TARGET_SPM
        self.target_drive_fraction=DRIVE_FRACTION
        self.target_recovery_fraction=RECOVERY_FRACTION

        self.cycle_time=60.0/self.target_spm
        self.target_drive_time=self.cycle_time*self.target_drive_fraction
        self.target_recovery_time=self.cycle_time*self.target_recovery_fraction

class MetronomeView(tk.Frame):

    TRACK_MARGIN=70
    TRACK_Y=380
    IMAGE_Y=200

    IMAGE_WIDTH=240
    IMAGE_HEIGHT=300

    DOT_RADIUS=14
    TRACK_WIDTH=14

    def _load_image(self,path):
        image=Image.open(path)
        image=image.resize((400,360),Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image)

    def __init__(self, parent, family: str):

        super().__init__(
            parent,
            bg=PANEL,
            highlightbackground=PANEL_EDGE,
            highlightthickness=1,
            bd=0
        )

        self.family = family

        self.phase_label = tk.Label(
            self,
            text="READY",
            bg=PANEL,
            fg=MUTED,
            font=(family, 32, "bold")
        )

        self.phase_label.pack(pady=(4, 0))

        self.phase_images={
            "catch":self._load_image(ASSETS_DIR/"catch.png"),
            "leg_extension":self._load_image(ASSETS_DIR/"leg_extension.png"),
            "trunk_swing":self._load_image(ASSETS_DIR/"trunk_swing.png"),
            "arm_pull":self._load_image(ASSETS_DIR/"arm_pull.png"),
            "arms_away":self._load_image(ASSETS_DIR/"arms_away.png"),
            "recovery_trunk":self._load_image(ASSETS_DIR/"recovery_trunk.png"),
            "leg_compression":self._load_image(ASSETS_DIR/"leg_compression.png")
        }

        #self.rower_image=tk.Label(self,bg=PANEL)
        #self.rower_image.pack(pady=(4,6))

        self.canvas = tk.Canvas(
            self,
            bg=PANEL,
            highlightthickness=0,
            height=500
        )

        self.canvas.pack(
            fill="x",
            padx=40,
            pady=(4, 8)
        )

        self.canvas.bind(
            "<Configure>",
            self._on_resize
        )

        self._width = 0
        self._dot = None
        self._rower_item=None
        self._draw_static()

    def _on_resize(self, _event=None):
        self._draw_static()

    def _draw_static(self):

        self.canvas.delete("all")

        width = max(
            self.canvas.winfo_width(),
            300
        )

        self._width = width

        y = self.TRACK_Y

        left = self.TRACK_MARGIN
        right = width - self.TRACK_MARGIN

        # Track
        self.canvas.create_line(
            left,
            y,
            right,
            y,
            fill=GRID,
            width=10,
            capstyle=tk.ROUND
        )

           

        # Labels
        self.canvas.create_text(
            left,
            y + 18,
            text="FINISH",
            fill=OK,
            font=(self.family, 12),
            anchor="n"
        )

        self.canvas.create_text(
            right,
            y + 18,
            text="CATCH",
            fill=OK,
            font=(self.family, 12),
            anchor="n"
        )
        

        # Dot starts at catch
        dot_radius = self.DOT_RADIUS

        self._dot = self.canvas.create_oval(
            left - dot_radius,
            y - dot_radius,
            left + dot_radius,
            y + dot_radius,
            fill=MUTED,
            outline=PANEL,
            width=2
        )

        
        self.canvas.create_line(
        left,y,right,y,fill=GRID,width=10,capstyle=tk.ROUND)

        self._fill_line=self.canvas.create_line(right,y,right,y,fill=WARN,width=10,capstyle=tk.ROUND)

        self._rower_item=self.canvas.create_image(
        width/2,self.IMAGE_Y,
        image=self.phase_images["catch"])


    def set_phase(self, phase: str, fraction: float):

        fraction = min(
            max(fraction, 0.0), 1.0 )

        colour = (
            WARN
            if phase == "drive"
            else RECOVERY_COLOR)

        self.phase_label.config(
            text=phase.upper(),
            fg=colour)

        width = self._width or max(
            self.canvas.winfo_width(),
            300)

        left = self.TRACK_MARGIN
        right = width - self.TRACK_MARGIN
        y = self.TRACK_Y

        if phase == "drive":

            x = right - fraction * (right - left)

            self.canvas.coords(
                self._fill_line,
                right, y,
                x, y
            )

        else:

            x = left + fraction * (right - left)

            self.canvas.coords(
                self._fill_line,
                left, y,
                x, y
            )

        self.canvas.itemconfig(
            self._fill_line,
            fill=colour
        )



        dot_radius = self.DOT_RADIUS

        self.canvas.coords(
            self._dot,
            x - dot_radius,
            y - dot_radius,
            x + dot_radius,
            y + dot_radius
        )

        self.canvas.itemconfig(
            self._dot,
            fill=colour
        )
        # Move the rower with the dot
        #image_x=max(left+70,min(x,right-70))
        #self.canvas.coords(self._rower_item,image_x,75)

        # Change the rowing pose
        self.update_rowing_image(phase,fraction)

    def set_idle(self):
        self.phase_label.config(text="READY",fg=MUTED)

        width=self._width or max(self.canvas.winfo_width(),300)
        left=self.TRACK_MARGIN
        right=width-self.TRACK_MARGIN
        y=self.TRACK_Y

        # Reset fill
        self.canvas.coords(self._fill_line,right,y,right,y)
        self.canvas.itemconfig(self._fill_line,fill=MUTED)

        # Reset dot to CATCH
        dot_radius=self.DOT_RADIUS
        self.canvas.coords(self._dot,right-dot_radius,y-dot_radius,
                        right+dot_radius,y+dot_radius)
        self.canvas.itemconfig(self._dot,fill=MUTED)

        # Reset rowing image to catch
        self.canvas.itemconfig(self._rower_item,image=self.phase_images["catch"])
            

    def _load_image(self,path):
        image=Image.open(path)
        image=image.resize((400,360),Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image)

    def update_rowing_image(self,phase,fraction):

        if phase=="drive":
            if fraction<0.20:
                key="catch"
            elif fraction<0.55:
                key="leg_extension"
            elif fraction<0.80:
                key="trunk_swing"
            else:
                key="arm_pull"

        else:
            if fraction<0.30:
                key="arms_away"
            elif fraction<0.60:
                key="recovery_trunk"
            else:
                key="leg_compression"

        self.canvas.itemconfig(
        self._rower_item,
        image=self.phase_images[key]
         )


@dataclass
class ExperimentConfig:
    participant_id:str
    session_id:str
    port:int

CONDITIONS=[
    "baseline_pre",
    "rhythm",
    "peak_force",
    "force_curve",
    "baseline_post"
]

class ExperimentApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("RP3 Rowing Experiment")
        self.geometry("1180x900")
        self.configure(bg=BACKGROUND)
        self.minsize(900,760)

        self.family=pick_font(self)

        self.current_frame=None
        self.reader=None
        self.logger=None
        self.config=None

        self.data_queue=queue.Queue()

        self.current_condition_index=0
        self.show_setup()

        self.experiment_stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
        

    def clear_screen(self):
        if self.current_frame is not None:
            self.current_frame.destroy()
            self.current_frame=None

    def show_setup(self):
        self.clear_screen()

        self.current_frame=SetupFrame(self,self)
        self.current_frame.pack(fill="both",expand=True)

    def start_experiment(self,config):
        self.config=config
        self.current_condition_index=0

        print("Participant:",config.participant_id)
        print("Session:",config.session_id)
        print("Port:",config.port)

        self.show_baseline(first=True)



    def show_baseline(self,first=False):
        self.clear_screen()

        if first:
            condition_name="C1_baseline_pre"
        else:
            condition_name="C5_baseline_post"

        self.current_frame=BaselineFrame(self,self)
        self.current_frame.pack(fill="both",expand=True)

        self.start_condition_io(condition_name)

    def show_rhythm(self):
        self.clear_screen()

        self.current_frame=RhythmFrame(self,self)
        self.current_frame.pack(fill="both",expand=True)
        self.start_condition_io("C2_rhythm")

    def show_peak_force(self):
        if isinstance(self.current_frame,RhythmFrame):
            self.current_frame.stop_metronome()

        self.clear_screen()

        self.current_frame=PeakForceFrame(self,self)
        self.current_frame.pack(fill="both",expand=True)

        self.start_condition_io("C3_peak_force")

    def finish_condition(self):
        if isinstance(self.current_frame,RhythmFrame):
            self.current_frame.stop_metronome()

        self.stop_condition_io()
        self.current_condition_index+=1

        if self.current_condition_index>=5:
                self.show_finished()
        else:
            self.show_rest()

    def show_rest(self):
        self.clear_screen()

        self.current_frame=RestFrame(self,self)
        self.current_frame.pack(fill="both",expand=True)

    def start_next_condition(self):
        if self.current_condition_index==1:
            self.show_rhythm()

        elif self.current_condition_index==2:
            self.show_peak_force()

        elif self.current_condition_index==3:
            self.show_force_curve()

        elif self.current_condition_index==4:
            self.show_baseline(first=False)

        else:
            self.show_finished()

    def show_force_curve(self):
        self.clear_screen()
        self.current_frame=ForceCurveFrame(self,self)
        self.current_frame.pack(fill="both",expand=True)
        self.start_condition_io("C4_force_curve")

    def start_condition_io(self,condition_name):
        self.stop_condition_io()

        # Remove any packets left from a previous condition
        while True:
            try:
                self.data_queue.get_nowait()
            except queue.Empty:
                break

        filename=(
            f"{self.config.participant_id}_"
            f"{self.config.session_id}_"
            f"{self.experiment_stamp}_"
            f"{condition_name}.csv"
        )

        path=LOGS_DIR/filename

        self.logger=StrokeLogger(str(path))

        self.reader=StrokeReader(
            "localhost",
            self.config.port,
            self.data_queue,
            self.logger
        )

        self.reader.start()

        print("Started:",condition_name)
        print("Saving to:",path)

    def stop_condition_io(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader.join(timeout=2.0)
            self.reader=None

        if self.logger is not None:
            self.logger.close()
            self.logger=None

    def show_finished(self):
        self.clear_screen()

        self.current_frame=FinishedFrame(self,self)
        self.current_frame.pack(fill="both",expand=True) 

class FinishedFrame(tk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,bg=BACKGROUND)

        self.app=app
        self.family=pick_font(app)

        card=tk.Frame(self,bg=PANEL,highlightbackground=PANEL_EDGE,highlightthickness=1)
        card.pack(expand=True,padx=150,pady=120,fill="both")

        tk.Label(card,text="EXPERIMENT COMPLETE",bg=PANEL,fg=OK,
                 font=(self.family,32,"bold")).pack(pady=(100,20))

        tk.Label(card,text="All five rowing conditions have been completed.",
                 bg=PANEL,fg=TEXT,font=(self.family,14)).pack(pady=(0,15))

        tk.Label(card,text="Thank you for participating.",
                 bg=PANEL,fg=MUTED,font=(self.family,12)).pack(pady=(0,35))

        tk.Button(card,text="Close",command=self.app.destroy,
                  bg=OK,fg=BACKGROUND,font=(self.family,12,"bold"),
                  relief="flat",padx=22,pady=10).pack()   

class RestFrame(tk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,bg=BACKGROUND)

        self.app=app
        self.family=pick_font(app)

        self.remaining=REST_DURATION_SECONDS

        self._build_layout()
        self._update_timer()

    def _build_layout(self):
        card=tk.Frame(self,bg=PANEL,highlightbackground=PANEL_EDGE,highlightthickness=1)
        card.pack(expand=True,padx=150,pady=120,fill="both")

        tk.Label(card,text="REST",bg=PANEL,fg=TEXT,
                 font=(self.family,36,"bold")).pack(pady=(90,20))

        tk.Label(card,text="Take a short break before the next trial.",
                 bg=PANEL,fg=MUTED,font=(self.family,14)).pack(pady=(0,30))

        self.timer_var=tk.StringVar(value="02:00")

        tk.Label(card,textvariable=self.timer_var,bg=PANEL,fg=OK,
                 font=(self.family,42,"bold")).pack(pady=20)

        self.next_button=tk.Button(
            card,
            text="Start next trial",
            command=self.app.start_next_condition,
            bg=OK,
            fg=BACKGROUND,
            font=(self.family,12,"bold"),
            relief="flat",
            padx=22,
            pady=10,
            state="disabled"
        )
        self.next_button.pack(pady=(35,20))

    def _update_timer(self):
        minutes=self.remaining//60
        seconds=self.remaining%60

        self.timer_var.set(f"{minutes:02d}:{seconds:02d}")

        if self.remaining<=0:
            self.next_button.config(state="normal")
            return

        self.remaining-=1
        self.after(1000,self._update_timer)

class SetupFrame(tk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,bg=BACKGROUND)

        self.app=app
        self.family=pick_font(app)

        card=tk.Frame(self,bg=PANEL,highlightbackground=PANEL_EDGE,highlightthickness=1)
        card.pack(expand=True,padx=100,pady=80)

        tk.Label(card,text="RP3 ROWING EXPERIMENT",bg=PANEL,fg=TEXT,
                 font=(self.family,24,"bold")).pack(padx=80,pady=(35,30))

        tk.Label(card,text="Participant ID",bg=PANEL,fg=MUTED,
                 font=(self.family,9)).pack(anchor="w",padx=60)

        self.participant_entry=tk.Entry(card,font=(self.family,12),bg=BACKGROUND,
                                        fg=TEXT,insertbackground=TEXT,relief="flat")
        self.participant_entry.pack(fill="x",padx=60,pady=(5,18),ipady=7)

        tk.Label(card,text="Session ID",bg=PANEL,fg=MUTED,
                 font=(self.family,9)).pack(anchor="w",padx=60)

        self.session_entry=tk.Entry(card,font=(self.family,12),bg=BACKGROUND,
                                    fg=TEXT,insertbackground=TEXT,relief="flat")
        self.session_entry.pack(fill="x",padx=60,pady=(5,18),ipady=7)

        tk.Label(card,text="RP3 Port",bg=PANEL,fg=MUTED,
                 font=(self.family,9)).pack(anchor="w",padx=60)

        self.port_entry=tk.Entry(card,font=(self.family,12),bg=BACKGROUND,
                                 fg=TEXT,insertbackground=TEXT,relief="flat")
        self.port_entry.insert(0,"58382")
        self.port_entry.pack(fill="x",padx=60,pady=(5,25),ipady=7)

        tk.Button(card,text="Start experiment",command=self.submit,
                  bg=OK,fg=BACKGROUND,font=(self.family,11,"bold"),
                  relief="flat",padx=20,pady=10).pack(pady=(5,35))
        
    def submit(self):
        participant=self.participant_entry.get().strip()
        session=self.session_entry.get().strip()

        if not participant or not session:
            return

        try:
            port=int(self.port_entry.get())
        except ValueError:
            return

        config=ExperimentConfig(
            participant_id=participant,
            session_id=session,
            port=port
        )

        self.app.start_experiment(config)



class BaselineFrame(tk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,bg=BACKGROUND)

        self.app=app
        self.family=pick_font(app)

        self.queue=self.app.data_queue
        self.stroke_count=0
        self.start_time=time.time()

        self.power_var=tk.StringVar(value="–")
        self.pace_var=tk.StringVar(value="–")
        self.spm_var=tk.StringVar(value="–")
        self.distance_var=tk.StringVar(value="0 m")
        self.time_var=tk.StringVar(value="00:00")
        self.strokes_var=tk.StringVar(value="0")

        self._build_layout()

        self.after(40,self.drain_queue)
        self.after(200,self.update_time)

        self.remaining=TRIAL_DURATION_SECONDS
        self.timer_after_id=None

        self._update_trial_timer()

    def _build_layout(self):
        outer=tk.Frame(self,bg=BACKGROUND)
        outer.pack(fill="both",expand=True,padx=30,pady=25)

        top=tk.Frame(outer,bg=BACKGROUND)
        top.pack(fill="x",pady=(0,20))

        self.timer_var=tk.StringVar(value="05:00")

        tk.Label(top,textvariable=self.timer_var,bg=BACKGROUND,fg=OK,
         font=(self.family,16,"bold")).pack(side="right",padx=(0,20))

        tk.Label(top,text="BASELINE",bg=BACKGROUND,fg=TEXT,
                 font=(self.family,26,"bold")).pack(side="left")

        tk.Label(top,text=f"Target rate: {TARGET_SPM:.0f} spm",
                 bg=BACKGROUND,fg=MUTED,font=(self.family,12)).pack(side="right")

        grid=tk.Frame(outer,bg=BACKGROUND)
        grid.pack(fill="both",expand=True)

        for c in range(3):
            grid.columnconfigure(c,weight=1)

        for r in range(2):
            grid.rowconfigure(r,weight=1)

        self._metric_card(grid,0,0,"POWER",self.power_var,"W")
        self._metric_card(grid,0,1,"500 M PACE",self.pace_var,"")
        self._metric_card(grid,0,2,"STROKE RATE",self.spm_var,"spm")

        self._metric_card(grid,1,0,"DISTANCE",self.distance_var,"")
        self._metric_card(grid,1,1,"TIME",self.time_var,"")
        self._metric_card(grid,1,2,"TOTAL STROKES",self.strokes_var,"")

        tk.Button(outer,text="Finish Trial",command=self.app.finish_condition,
          bg=OK,fg=BACKGROUND,font=(self.family,11,"bold"),
          relief="flat",padx=18,pady=9).pack(pady=(15,0))

    def _metric_card(self,parent,row,column,title,var,unit):
        card=tk.Frame(parent,bg=PANEL,highlightbackground=PANEL_EDGE,highlightthickness=1)
        card.grid(row=row,column=column,sticky="nsew",padx=8,pady=8)

        tk.Label(card,text=title,bg=PANEL,fg=MUTED,
                 font=(self.family,11,"bold")).pack(pady=(35,8))

        value_row=tk.Frame(card,bg=PANEL)
        value_row.pack(expand=True)

        tk.Label(value_row,textvariable=var,bg=PANEL,fg=TEXT,
                 font=(self.family,36,"bold")).pack(side="left")

        if unit:
            tk.Label(value_row,text=unit,bg=PANEL,fg=MUTED,
                     font=(self.family,14)).pack(side="left",padx=(6,0),pady=(14,0))

    def drain_queue(self):
        newest_stroke=None

        while True:
            try:
                message=self.queue.get_nowait()
            except queue.Empty:
                break

            if message[0]=="stroke":
                newest_stroke=message[1]
                self.stroke_count+=1

        if newest_stroke is not None:
            self.show_stroke(newest_stroke)

        self.after(40,self.drain_queue)

    def show_stroke(self,packet):
        print(packet)

        power=packet.get("powerWatts")
        pace=packet.get("paceSecondsPer500Meters")
        spm=packet.get("strokesPerMinute")

        self.power_var.set(f"{float(power):.0f}" if power is not None else "–")
        self.pace_var.set(format_pace(pace))
        self.spm_var.set(f"{float(spm):.1f}" if spm is not None else "–")
        self.strokes_var.set(str(self.stroke_count))

    def update_time(self):
        elapsed=int(time.time()-self.start_time)

        minutes=elapsed//60
        seconds=elapsed%60

        self.time_var.set(f"{minutes:02d}:{seconds:02d}")

        self.after(200,self.update_time)

    def _update_trial_timer(self):
        minutes=self.remaining//60
        seconds=self.remaining%60

        self.timer_var.set(f"{minutes:02d}:{seconds:02d}")

        if self.remaining<=0:
            self.app.finish_condition()
            return

        self.remaining-=1
        self.timer_after_id=self.after(1000,self._update_trial_timer)
    
class RhythmFrame(tk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,bg=BACKGROUND)

        self.app=app
        self.family=pick_font(app)

        self.targets=MetronomeTargets()
        self.metronome_running=False
        self.metronome_start_time=0.0
        self.metronome_after_id=None
        self.trial_start_time=time.perf_counter()
        self.timer_after_id=None
        self._build_layout()
        self._update_trial_timer()


    def _build_layout(self):
        outer=tk.Frame(self,bg=BACKGROUND)
        outer.pack(fill="both",expand=True,padx=30,pady=25)

        top=tk.Frame(outer,bg=BACKGROUND)
        top.pack(fill="x",pady=(0,20))
        self.timer_var=tk.StringVar(value="00:15")

        tk.Label(top,textvariable=self.timer_var,bg=BACKGROUND,fg=OK,
         font=(self.family,16,"bold")).pack(side="right",padx=(0,20))

        tk.Label(top,text="RHYTHM AND SEQUENCING",bg=BACKGROUND,fg=TEXT,
                font=(self.family,26,"bold")).pack(side="left")

        tk.Label(top,text=f"{TARGET_SPM:.0f} spm",
                bg=BACKGROUND,fg=MUTED,font=(self.family,14,"bold")).pack(side="right")

        self.metronome_view=MetronomeView(outer,self.family)
        self.metronome_view.pack(fill="both",expand=True,pady=(15,20))

        controls=tk.Frame(outer,bg=BACKGROUND)
        controls.pack(fill="x")

        self.start_button=tk.Button(controls,text="Start guide",command=self.start_metronome,
                                    bg=OK,fg=BACKGROUND,font=(self.family,12,"bold"),
                                    relief="flat",padx=20,pady=10)
        self.start_button.pack(side="left")

        tk.Button(controls,text="Finish Trial",command=self.app.finish_condition,
                  bg=WARN,fg=BACKGROUND,font=(self.family,11,"bold"),
                  relief="flat",padx=18,pady=9).pack(side="right")

    def start_metronome(self):
        self.metronome_running=True
        self.metronome_start_time=time.time()

        self.start_button.config(text="Guide running",state="disabled")

        self.update_metronome()

    def update_metronome(self):
        if not self.metronome_running:
            return

        elapsed=time.time()-self.metronome_start_time
        position_in_cycle=elapsed%self.targets.cycle_time

        if position_in_cycle<self.targets.target_drive_time:
            phase="drive"
            fraction=position_in_cycle/self.targets.target_drive_time

        else:
            phase="recovery"
            recovery_elapsed=position_in_cycle-self.targets.target_drive_time
            fraction=recovery_elapsed/self.targets.target_recovery_time

        self.metronome_view.set_phase(phase,fraction)

        self.metronome_after_id=self.after(30,self.update_metronome)

    def stop_metronome(self):
        self.metronome_running=False

        if self.metronome_after_id is not None:
            try:
                self.after_cancel(self.metronome_after_id)
            except tk.TclError:
                pass

            self.metronome_after_id=None

        self.metronome_view.set_idle()

        if self.start_button.winfo_exists():
            self.start_button.config(text="Start guide",state="normal")

    def _update_trial_timer(self):
        elapsed=time.perf_counter()-self.trial_start_time
        remaining=max(0,TRIAL_DURATION_SECONDS-int(elapsed))

        minutes=remaining//60
        seconds=remaining%60

        self.timer_var.set(f"{minutes:02d}:{seconds:02d}")

        if elapsed>=TRIAL_DURATION_SECONDS:
            self.app.finish_condition()
            return

        self.timer_after_id=self.after(200,self._update_trial_timer)
    

class PeakForceFrame(tk.Frame):
    FRAME_MS = 33
    PAD = 70
 
    def __init__(self, parent, app):
        super().__init__(parent, bg=BACKGROUND)
 
        self.app = app
        self.family = pick_font(app)
        self.queue = app.data_queue
 
        # game state
        self.stroke_count = 0
        self.score = 0
        self.streak = 0
        self.best_streak = 0
        self.hits = 0
        self.history = deque(maxlen=12)   # True/False per stroke
 
        # animation state
        self.t = 0.0
        self.marker_pos = 0.0
        self.marker_target = 0.0
        self.marker_colour = MUTED
        self.trail = deque(maxlen=10)
        self.particles = []     # [x, y, vx, vy, life, colour]
        self.floaters = []      # [x, y, text, life, colour]
        self.shake = 0.0
        self.flash = 0.0
        self.flash_colour = OK
 
        rng = random.Random(7)
        self.stars = [(rng.random(), rng.random(), rng.choice([1, 1, 2]),
                       rng.random() * 6.28) for _ in range(45)]

        self.trial_start_time=time.perf_counter()
        self.timer_after_id=None
        self._build_layout()
        self.after(40, self.drain_queue)
        self._update_trial_timer()
        self.after(self.FRAME_MS, self._animate)
 
    # ------------------------------------------------------------------ layout
    def _build_layout(self):
        outer = tk.Frame(self, bg=BACKGROUND)
        outer.pack(fill="both", expand=True, padx=35, pady=25)
 
        top = tk.Frame(outer, bg=BACKGROUND)
        top.pack(fill="x")
        self.timer_var=tk.StringVar(value="00:15")

        tk.Label(top,textvariable=self.timer_var,bg=BACKGROUND,fg=OK,
         font=(self.family,16,"bold")).pack(side="right",padx=(0,20))
        tk.Label(top, text="PEAK FORCE CONTROL", bg=BACKGROUND, fg=TEXT,
                 font=(self.family, 26, "bold")).pack(side="left")
        tk.Label(top, text=f"{TARGET_SPM:.0f} spm", bg=BACKGROUND, fg=MUTED,
                 font=(self.family, 14, "bold")).pack(side="right")
 
        self.feedback_var = tk.StringVar(value="Take a stroke to start!")
        self.feedback_label = tk.Label(
            outer, textvariable=self.feedback_var, bg=BACKGROUND, fg=MUTED,
            font=(self.family, 26, "bold"))
        self.feedback_label.pack(pady=(25, 10))
 
        self.game_canvas = tk.Canvas(
            outer, bg=PANEL, highlightthickness=2,
            highlightbackground=PANEL_EDGE, height=360)
        self.game_canvas.pack(fill="both", expand=True, padx=60, pady=10)
 
        # stat cards
        cards = tk.Frame(outer, bg=BACKGROUND)
        cards.pack(pady=(10, 5))
 
        self.score_var = tk.StringVar(value="0")
        self.streak_var = tk.StringVar(value="0")
        self.best_var = tk.StringVar(value="0")
        self.acc_var = tk.StringVar(value="–")
        self.peak_var = tk.StringVar(value="–")
        self.position_var = tk.StringVar(value="–")
 
        for title,var,colour in (
            ("SCORE",self.score_var,CURVE),
            ("STREAK",self.streak_var,OK),
            ("BEST STREAK",self.best_var,WARN),
            ("ACCURACY",self.acc_var,TEXT),
            ("PEAK POSITION",self.position_var,TEXT),
        ):
            self._card(cards, title, var, colour)


 
    def _card(self, parent, title, var, colour):
        card = tk.Frame(parent, bg=PANEL, highlightthickness=1,
                        highlightbackground=PANEL_EDGE)
        card.pack(side="left", padx=8, ipadx=14, ipady=6)
        tk.Label(card, text=title, bg=PANEL, fg=MUTED,
                 font=(self.family, 9, "bold")).pack(pady=(4, 0))
        tk.Label(card, textvariable=var, bg=PANEL, fg=colour,
                 font=(self.family, 20, "bold")).pack()
 
    # --------------------------------------------------------------- geometry
    def _geom(self):
        w = max(self.game_canvas.winfo_width(), 400)
        h = max(self.game_canvas.winfo_height(), 300)
        left, right = self.PAD, w - self.PAD
        return w, h, left, right, h / 2
 
    # --------------------------------------------------------------- animation
    def _animate(self):
        if not self.winfo_exists():
            return
        self.t += self.FRAME_MS / 1000.0
 
        # ease marker toward target, keep a fading trail
        self.marker_pos += (self.marker_target - self.marker_pos) * 0.22
        self.trail.append(self.marker_pos)
 
        # particles / floaters
        for p in self.particles:
            p[0] += p[2]
            p[1] += p[3]
            p[3] += 0.25
            p[4] -= 0.035
        self.particles = [p for p in self.particles if p[4] > 0]
 
        for f in self.floaters:
            f[1] -= 1.6
            f[3] -= 0.025
        self.floaters = [f for f in self.floaters if f[3] > 0]
 
        self.shake *= 0.80
        self.flash *= 0.88
 
        self._draw_game()
        self.after(self.FRAME_MS, self._animate)
 
    def _draw_game(self, event=None):
        c = self.game_canvas
        c.delete("all")
 
        w, h, left, right, y = self._geom()
        sx = random.uniform(-1, 1) * self.shake
        sy = random.uniform(-1, 1) * self.shake
        span = right - left
 
        # --- twinkling background dots
        for fx, fy, size, ph in self.stars:
            tw = (math.sin(self.t * 1.6 + ph) + 1) / 2
            col = _mix(PANEL, GRID, 0.4 + 0.6 * tw)
            x, yy = fx * w, fy * h
            c.create_oval(x, yy, x + size, yy + size, fill=col, outline="")
 
        # --- soft lane glow
        for i in range(5, 0, -1):
            c.create_line(left + sx, y + sy, right + sx, y + sy,
                          fill=_mix(PANEL, GRID, 1 - i / 6), width=12 + i * 6,
                          capstyle=tk.ROUND)
        c.create_line(left + sx, y + sy, right + sx, y + sy, fill=GRID,
                      width=14, capstyle=tk.ROUND)
        c.create_line(left + sx, y + sy, right + sx, y + sy, fill=PANEL_EDGE,
                      width=4, capstyle=tk.ROUND)
 
        # --- ticks and labels (stroke timeline: catch -> finish)
        for i in range(11):
            tx = left + span * i / 10 + sx
            c.create_line(tx, y + 18 + sy, tx, y + 28 + sy, fill=PANEL_EDGE, width=2)
            if i % 5 == 0:
                c.create_text(tx, y + 42 + sy, text=f"{i * 10}%", fill=MUTED,
                              font=(self.family, 10, "bold"))
        c.create_text(left + sx, y - 95 + sy, text="CATCH", fill=MUTED, anchor="w",
                      font=(self.family, 10, "bold"))
        c.create_text(right + sx, y - 95 + sy, text="FINISH", fill=MUTED, anchor="e",
                      font=(self.family, 10, "bold"))
 
        # --- pulsing target zone
        tl = left + PEAK_POSITION_MIN * span + sx
        tr = left + PEAK_POSITION_MAX * span + sx
        pulse = (math.sin(self.t * 3.0) + 1) / 2
        for i in range(4, 0, -1):
            grow = i * 5 + pulse * 4
            c.create_rectangle(tl - grow, y - 55 - grow + sy, tr + grow, y + 55 + grow + sy,
                               outline=_mix(PANEL, OK, 0.08 + 0.05 * (5 - i) + 0.1 * pulse),
                               width=2)
        c.create_rectangle(tl, y - 55 + sy, tr, y + 55 + sy,
                           fill=_mix(PANEL, OK, 0.16 + 0.10 * pulse),
                           outline=OK, width=4)
        mid = (tl + tr) / 2
        c.create_line(mid, y - 55 + sy, mid, y + 55 + sy, fill=_mix(PANEL, OK, 0.5),
                      dash=(4, 4), width=2)
        c.create_text(mid, y - 78 + sy, text="★  TARGET  ★", fill=OK,
                      font=(self.family, 13, "bold"))
 
        # --- hit flash
        if self.flash > 0.05:
            c.create_rectangle(0, 0, w, h, fill=_mix(PANEL, self.flash_colour, self.flash * 0.35),
                               outline="", stipple="gray50")
 
        # --- marker trail + glow + ball
        mx = left + self.marker_pos * span + sx
        n = len(self.trail)
        for i, pos in enumerate(self.trail):
            tx = left + pos * span + sx
            frac = (i + 1) / max(n, 1)
            r = 4 + 10 * frac
            c.create_oval(tx - r, y - r + sy, tx + r, y + r + sy,
                          fill=_mix(PANEL, self.marker_colour, 0.12 * frac), outline="")
 
        c.create_line(mx, y - 100 + sy, mx, y - 22 + sy,
                      fill=_mix(PANEL, self.marker_colour, 0.6), dash=(3, 4), width=2)
 
        bob = math.sin(self.t * 5) * 1.5
        for i in range(4, 0, -1):
            r = 16 + i * 7
            c.create_oval(mx - r, y - r + sy + bob, mx + r, y + r + sy + bob,
                          fill=_mix(PANEL, self.marker_colour, 0.07 * (5 - i)), outline="")
        c.create_oval(mx - 18, y - 18 + sy + bob, mx + 18, y + 18 + sy + bob,
                      fill=self.marker_colour, outline=TEXT, width=2)
        c.create_oval(mx - 9, y - 11 + sy + bob, mx - 2, y - 4 + sy + bob,
                      fill=TEXT, outline="")
 
        # --- particles
        for px, py, _, _, life, col in self.particles:
            r = 2 + 4 * life
            c.create_oval(px - r, py - r, px + r, py + r,
                          fill=_mix(PANEL, col, min(1, life + 0.2)), outline="")
 
        # --- floating score text
        for fx, fy, text, life, col in self.floaters:
            c.create_text(fx, fy, text=text, fill=_mix(PANEL, col, min(1, life * 1.5)),
                          font=(self.family, 20, "bold"))
 
        # --- streak badge
        if self.streak >= 2:
            c.create_text(20, 22, anchor="w", text=f"★ x{self.streak}",
                          fill=WARN if self.streak < 5 else CURVE,
                          font=(self.family, 20 + min(self.streak, 8), "bold"))
 
        # --- history dots (last strokes)
        hist = list(self.history)
        start = w / 2 - (len(hist) - 1) * 14
        for i, ok in enumerate(hist):
            hx = start + i * 28
            col = OK if ok else BAD
            r = 8 if i == len(hist) - 1 else 6
            c.create_oval(hx - r, h - 28 - r, hx + r, h - 28 + r,
                          fill=col, outline=TEXT if i == len(hist) - 1 else "")
        if hist:
            c.create_text(w - 20, h - 28, anchor="e", text="LAST STROKES", fill=MUTED,
                          font=(self.family, 9, "bold"))
 
    def _burst(self, x, y, colour, count, speed):
        for _ in range(count):
            a = random.uniform(0, math.tau)
            s = random.uniform(speed * 0.4, speed)
            self.particles.append([x, y, math.cos(a) * s, math.sin(a) * s - 2, 1.0, colour])
 
    # -------------------------------------------------------------------- data
    def drain_queue(self):
        newest_stroke = None
 
        while True:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
 
            if message[0] == "stroke":
                newest_stroke = message[1]
                self.stroke_count += 1
 
        if newest_stroke is not None:
            self.show_stroke(newest_stroke)
 
        self.after(40, self.drain_queue)
 
    def show_stroke(self, packet):
        analysis = analyze_stroke(packet)
 
        peak_force = analysis.get("peak_force")
        peak_position = analysis.get("peak_position_fraction")
 
        if peak_force is not None:
            self.peak_var.set(f"{peak_force:.0f} N")
 
        if peak_position is None:
            return
 
        self.position_var.set(f"{peak_position * 100:.1f}%")
 
        w, h, left, right, y = self._geom()
        span = right - left
        x = left + peak_position * span
 
        self.marker_target = min(max(peak_position, 0.0), 1.0)
 
        centre=PEAK_POSITION_TARGET
        half=(PEAK_POSITION_MAX-PEAK_POSITION_MIN)/2

        hit=PEAK_POSITION_MIN<=peak_position<=PEAK_POSITION_MAX
        absolute_error=abs(peak_position-PEAK_POSITION_TARGET)
        self.history.append(hit)
 
        if hit:
            closeness = 1 - abs(peak_position - centre) / half   # 1 = dead centre
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            self.hits += 1
            points = 100 + int(50 * closeness) + 25 * min(self.streak - 1, 10)
            self.score += points
 
            if closeness > 0.8:
                msg = random.choice(["PERFECT!", "BULLSEYE!", "FLAWLESS!"])
            else:
                msg = random.choice(["IN TARGET", "NICE ONE!", "GOOD STROKE!"])
            if self.streak >= 5:
                msg += f"  🔥 x{self.streak}" if False else f"  x{self.streak}"
 
            colour = OK
            self.marker_colour = OK
            self.flash, self.flash_colour = 1.0, OK
            self._burst(x, y, OK, 26 + int(closeness * 20), 7)
            self._burst(x, y, CURVE, 10, 5)
            self.floaters.append([x, y - 40, f"+{points}", 1.0, CURVE])
        else:
            self.streak = 0
            msg = "PEAK TOO EARLY" if peak_position < PEAK_POSITION_MIN else "PEAK TOO LATE"
            colour = BAD
            self.marker_colour = BAD
            self.shake = 10.0
            self.flash, self.flash_colour = 0.6, BAD
            self._burst(x, y, BAD, 12, 4)
            self.floaters.append([x, y - 40, "MISS", 1.0, BAD])
 
        self.feedback_var.set(msg)
        self.feedback_label.config(fg=colour)
        self.score_var.set(f"{self.score}")
        self.streak_var.set(f"{self.streak}")
        self.best_var.set(f"{self.best_streak}")
        self.acc_var.set(f"{100 * self.hits / max(self.stroke_count, 1):.0f}%")

    def _update_trial_timer(self):
        elapsed=time.perf_counter()-self.trial_start_time
        remaining=max(0,TRIAL_DURATION_SECONDS-int(elapsed))

        minutes=remaining//60
        seconds=remaining%60

        self.timer_var.set(f"{minutes:02d}:{seconds:02d}")

        if elapsed>=TRIAL_DURATION_SECONDS:
            self.app.finish_condition()
            return

        self.timer_after_id=self.after(200,self._update_trial_timer)

class ForceCurveFrame(tk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,bg=BACKGROUND)

        self.app=app
        self.family=pick_font(app)
        self.queue=app.data_queue

        self.stroke_count=0
        self.trial_start_time=time.perf_counter()
        self.timer_after_id=None
        self._build_layout()
        self.after(40,self.drain_queue)
        self._update_trial_timer()

    def _build_layout(self):
        outer=tk.Frame(self,bg=BACKGROUND)
        outer.pack(fill="both",expand=True,padx=35,pady=25)

        top=tk.Frame(outer,bg=BACKGROUND)
        top.pack(fill="x")
        self.timer_var=tk.StringVar(value="00:15")

        tk.Label(top,textvariable=self.timer_var,bg=BACKGROUND,fg=OK,
         font=(self.family,16,"bold")).pack(side="right",padx=(0,20))

        tk.Label(top,text="FORCE CURVE",bg=BACKGROUND,fg=TEXT,
                font=(self.family,26,"bold")).pack(side="left")

        tk.Label(top,text=f"{TARGET_SPM:.0f} spm",
                bg=BACKGROUND,fg=MUTED,font=(self.family,14,"bold")).pack(side="right")

        self.feedback_var=tk.StringVar(value="KEEP YOUR CURVE INSIDE THE TARGET ZONE")

        tk.Label(outer,textvariable=self.feedback_var,bg=BACKGROUND,fg=TEXT,
                font=(self.family,20,"bold")).pack(pady=(20,10))

        panel=tk.Frame(outer,bg=PANEL,highlightbackground=PANEL_EDGE,highlightthickness=1)
        panel.pack(fill="both",expand=True,pady=(10,20))

        self.figure=Figure(figsize=(8,5),dpi=100,facecolor=PANEL)
        self.ax=self.figure.add_subplot(111)

        self.ax.set_facecolor(PANEL)
        self.ax.set_xlabel("Stroke length (%)",color=MUTED)
        self.ax.set_ylabel("Normalized force (%)",color=MUTED)

        self.ax.tick_params(colors=MUTED)

        for side in self.ax.spines.values():
            side.set_color(GRID)

        self.ax.grid(True,color=GRID,alpha=0.4)

        self.ax.set_xlim(0,100)
        self.ax.set_ylim(0,110)

        self.canvas=FigureCanvasTkAgg(self.figure,master=panel)
        self.canvas.get_tk_widget().pack(fill="both",expand=True,padx=15,pady=15)

        controls=tk.Frame(outer,bg=BACKGROUND)
        controls.pack(fill="x")

        tk.Button(controls,text="Finish Trial",command=self.app.finish_condition,
                bg=BAD,fg=BACKGROUND,font=(self.family,11,"bold"),
                relief="flat",padx=18,pady=9).pack(side="right")

    def _target_zone(self):
        x=[0,10,20,30,35,40,50,60,70,80,90,100]

        low=[ 0,15,45,75,88,87,80,68, 52,32,15,0]

        high=[ 5,35,70,100,105,105, 100,94,82,64,35, 5]

        return x,low,high

    def drain_queue(self):
        newest_stroke=None

        while True:
            try:
                message=self.queue.get_nowait()
            except queue.Empty:
                break

            if message[0]=="stroke":
                newest_stroke=message[1]
                self.stroke_count+=1

        if newest_stroke is not None:
            self.show_stroke(newest_stroke)

        self.after(40,self.drain_queue)

    def show_stroke(self,packet):
        stroke_length=float(packet.get("strokeLengthMeters") or 0)

        positions,forces=parse_force_curve(
            packet.get("forceCurve"),
            stroke_length
        )

        if not positions or not forces:
            return

        max_force=max(forces)

        if max_force<=0:
            return

        start=positions[0]
        end=positions[-1]

        if end<=start:
            return

        x_normalized=[
            (x-start)/(end-start)*100
            for x in positions
        ]

        y_normalized=[
            f/max_force*100
            for f in forces
        ]

        ref_x,ref_low,ref_high=self._target_zone()

        inside_count=0
        deviations=[]

        for x,y in zip(x_normalized,y_normalized):
            low=self._interpolate_reference(x,ref_x,ref_low)
            high=self._interpolate_reference(x,ref_x,ref_high)

            if low<=y<=high:
                inside_count+=1
                deviations.append(0)

            elif y<low:
                deviations.append(low-y)

            else:
                deviations.append(y-high)

        coverage=100*inside_count/len(y_normalized)

        mean_outside_error=sum(deviations)/len(deviations)

        self.ax.clear()

        self.ax.set_facecolor(PANEL)
        self.ax.set_xlim(0,100)
        self.ax.set_ylim(0,110)

        self.ax.set_xlabel("Stroke position (%)",color=MUTED)
        self.ax.set_ylabel("Force (% of peak)",color=MUTED)
        self.ax.tick_params(colors=MUTED)

        for side in self.ax.spines.values():
            side.set_color(GRID)

        self.ax.grid(True,color=GRID,alpha=0.4)

        self.ax.fill_between(
            ref_x,ref_low,ref_high,color=PEAK,alpha=0.50,label="Target zone" )

        self.ax.plot(
            x_normalized, y_normalized,color=CURVE,linewidth=3, label="Your stroke")

        self.ax.legend()

        self.canvas.draw_idle()

        if coverage>=80:
            message=f"{coverage:.0f}% IN TARGET — GREAT"

        elif coverage>=60:
            message=f"{coverage:.0f}% IN TARGET — CLOSE"

        else:
            message=f"{coverage:.0f}% IN TARGET — FOLLOW THE TARGET ZONE"

        self.feedback_var.set(message)

    def _update_trial_timer(self):
        elapsed=time.perf_counter()-self.trial_start_time
        remaining=max(0,TRIAL_DURATION_SECONDS-int(elapsed))

        minutes=remaining//60
        seconds=remaining%60

        self.timer_var.set(f"{minutes:02d}:{seconds:02d}")

        if elapsed>=TRIAL_DURATION_SECONDS:
            self.app.finish_condition()
            return

        self.timer_after_id=self.after(200,self._update_trial_timer)

    def _interpolate_reference(self,x,ref_x,ref_y):
        if x<=ref_x[0]:
            return ref_y[0]

        if x>=ref_x[-1]:
            return ref_y[-1]

        for i in range(1,len(ref_x)):
            if ref_x[i-1]<=x<=ref_x[i]:
                x1=ref_x[i-1]
                x2=ref_x[i]

                y1=ref_y[i-1]
                y2=ref_y[i]

                if x2==x1:
                    return y1

                return y1+(y2-y1)*(x-x1)/(x2-x1)

        return ref_y[-1]

def main():
    app=ExperimentApp()
    app.mainloop()


if __name__=="__main__":
    main()