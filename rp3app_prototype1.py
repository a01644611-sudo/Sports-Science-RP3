from __future__ import annotations

import argparse
import csv
import json
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
# the one loud thing on screen; everything else stays quiet so a rower
# glancing down mid-piece sees the shape first and the numbers second.

BACKGROUND = "#0B1F2E"
PANEL = "#12324A"
PANEL_EDGE = "#1E4763"
GRID = "#1B4059"
CURVE = "#F2B441"      # this stroke
GHOST = "#5B86A5"      # previous stroke, for comparison
PEAK = "#7EE0C6"       # peak force marker and its vertical line
TEXT = "#EDF4F9"
MUTED = "#8FB0C7"
OK = "#7EE0C6"
WARN = "#F2B441"
BAD = "#E8715A"

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
# CSV logging
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
# Background socket thread
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
# Formatting helpers
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
# Widgets
# --------------------------------------------------------------------------
class MetricTile(tk.Frame):
    """One number with its name underneath."""

    def __init__(self, parent, name: str, unit: str, family: str):
        super().__init__(parent, bg=PANEL, highlightbackground=PANEL_EDGE,
                         highlightthickness=1, bd=0)
        row = tk.Frame(self, bg=PANEL)
        row.pack(anchor="w", padx=12, pady=(8, 0))

        self.value = tk.Label(row, text="–", bg=PANEL, fg=TEXT,
                              font=(family, 20, "bold"))
        self.value.pack(side="left")
        if unit:
            tk.Label(row, text=" " + unit, bg=PANEL, fg=MUTED,
                     font=(family, 9)).pack(side="left", anchor="s", pady=(0, 4))

        tk.Label(self, text=name, bg=PANEL, fg=MUTED, font=(family, 9)).pack(
            anchor="w", padx=12, pady=(0, 8))

    def set_value(self, text: str) -> None:
        self.value.config(text=text)


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

        root.title("RP3 live force curve")
        root.geometry("1280x760")
        root.configure(bg=BACKGROUND)
        root.minsize(980, 620)
        self.family = pick_font(root)

        self._build_layout(source_description)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._running = True
        # The single point where background data enters the GUI thread.
        self.root.after(40, self.drain_queue)

    # -- construction -----------------------------------------------------
    def _build_layout(self, source_description: str) -> None:
        body = tk.Frame(self.root, bg=BACKGROUND)
        body.pack(fill="both", expand=True, padx=16, pady=(14, 6))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2, minsize=380)
        body.rowconfigure(0, weight=1)

        self._build_plot(body)
        self._build_metrics(body)

        self.status = tk.Label(self.root, text="● " + source_description, bg=BACKGROUND,
                               fg=MUTED, font=(self.family, 9), anchor="w")
        self.status.pack(fill="x", padx=18, pady=(0, 10))

        self.participant_label = tk.Label(self.root, text=f"Participant ID: {self.participant_id} | Session ID: {self.session_id}",
                                           bg=BACKGROUND, fg=BAD, font=(self.family, 9), anchor="w")
        self.participant_label.pack(fill="x", padx=18, pady=(0, 10))

    def _build_plot(self, parent) -> None:
        panel = tk.Frame(parent, bg=PANEL, highlightbackground=PANEL_EDGE,
                         highlightthickness=1, bd=0)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        header = tk.Frame(panel, bg=PANEL)
        header.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(header, text="Force through the drive", bg=PANEL, fg=TEXT,
                 font=(self.family, 13, "bold")).pack(side="left")
        self.stroke_label = tk.Label(header, text="waiting for your first stroke",
                                     bg=PANEL, fg=MUTED, font=(self.family, 9))
        self.stroke_label.pack(side="right")

        self.figure = Figure(figsize=(6.6, 4.6), dpi=100, facecolor=PANEL)
        self.ax = self.figure.add_subplot(111)
        self.ax.set_facecolor(PANEL)
        self.ax.set_xlabel("Stroke length (m)", color=MUTED, fontsize=11)
        self.ax.set_ylabel("Force (N)", color=MUTED, fontsize=11)
        self.ax.tick_params(colors=MUTED, labelsize=9)
        for side in self.ax.spines.values():
            side.set_color(GRID)
        self.ax.grid(True, color=GRID, alpha=0.5, linewidth=0.7)
        self.ax.set_xlim(0, 1.5)
        self.ax.set_ylim(0, 700)

        # Artists are created once and only have their data replaced, which is
        # what keeps redrawing cheap in matplotlib.
        (self.ghost_line,) = self.ax.plot([], [], color=GHOST, linewidth=1.6,
                                          linestyle="--", zorder=2)
        (self.curve_line,) = self.ax.plot([], [], color=CURVE, linewidth=2.4, zorder=3)
        self.peak_line = self.ax.axvline(0, color=PEAK, linewidth=1.0,
                                         linestyle=":", zorder=4, visible=False)
        (self.peak_point,) = self.ax.plot([], [], "o", color=PEAK, markersize=8,
                                          markeredgecolor=PANEL, markeredgewidth=1.5,
                                          zorder=5)
        self.peak_text = self.ax.text(0, 0, "", color=PEAK, fontsize=9,
                                      ha="center", va="bottom", zorder=6)
        self.figure.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.figure, master=panel)
        self.canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=6)
        self.canvas.draw()

        legend = tk.Frame(panel, bg=PANEL)
        legend.pack(fill="x", padx=16, pady=(0, 10))
        for colour, text in ((CURVE, "━ this stroke"), (GHOST, "╍ stroke before"),
                             (PEAK, "● peak force")):
            tk.Label(legend, text=text + "   ", bg=PANEL, fg=colour,
                     font=(self.family, 9)).pack(side="left")

    def _build_metrics(self, parent) -> None:
        container = tk.Frame(parent, bg=BACKGROUND)
        container.grid(row=0, column=1, sticky="nsew")
        container.columnconfigure(0, weight=1, uniform="tiles")
        container.columnconfigure(1, weight=1, uniform="tiles")

        definitions = [
            ("rate", "Stroke rate", "spm"),
            ("power", "Power", "W"),
            ("pace", "Pace per 500 m", ""),
            ("length", "Stroke length", "m"),
            ("peak", "Peak force", "N"),
            ("peak_pos", "Peak force position", "% of drive"),
            ("drive", "Drive time", "s"),
            ("recover", "Recovery time", "s"),
            ("ratio", "Drive to recovery", ""),
            ("energy", "Energy per stroke", "J"),
            ("drag", "Drag factor", ""),
            ("count", "Strokes logged", ""),
        ]
        self.tiles: dict[str, MetricTile] = {}
        for index, (key, name, unit) in enumerate(definitions):
            tile = MetricTile(container, name, unit, self.family)
            tile.grid(row=index // 2, column=index % 2, sticky="nsew", padx=4, pady=4)
            self.tiles[key] = tile

        rows = (len(definitions) + 1) // 2
        # A short reading guide. Not coaching yet, just naming what is on the
        # screen, because a beginner meets ten unfamiliar numbers at once.
        note = tk.Label(
            container,
            text=("The dotted line marks where your force peaked during the drive. "
                  "The dashed curve is the stroke before this one, so you can see "
                  "how alike two strokes in a row were."),
            bg=BACKGROUND, fg=MUTED, font=(self.family, 9),
            wraplength=360, justify="left",
        )
        note.grid(row=rows, column=0, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        container.rowconfigure(rows + 1, weight=1)

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
        stroke_length = float(packet.get("strokeLengthMeters") or 0.0)
        positions, forces = parse_force_curve(packet.get("forceCurve"), stroke_length)

        if positions and forces:
            # Fade the stroke that was on screen a moment ago into the ghost,
            # so a rower can see whether two strokes look alike.
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
            self.peak_text.set_position((peak_x, peak_y * 1.03))
            self.peak_text.set_text(f"{peak_y:.0f} N at {peak_x:.2f} m")

            self.recent_peaks.append(peak_y)
            self.recent_lengths.append(max(positions))
            self._rescale_axes()
            self.canvas.draw_idle()

        self._update_tiles(packet)

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

    def _update_tiles(self, packet: dict) -> None:
        drive = packet.get("driveTimeSeconds")
        recover = packet.get("recoverTimeSeconds")
        fraction = packet.get("relativePeakForcePositionFraction")

        self.tiles["rate"].set_value(number(packet.get("strokesPerMinute"), 1))
        self.tiles["power"].set_value(number(packet.get("powerWatts"), 0))
        self.tiles["pace"].set_value(format_pace(packet.get("paceSecondsPer500Meters")))
        self.tiles["length"].set_value(number(packet.get("strokeLengthMeters"), 2))
        self.tiles["peak"].set_value(number(packet.get("peakForceNewtons"), 0))
        self.tiles["peak_pos"].set_value(
            number(float(fraction) * 100, 0) if fraction is not None else "–")
        self.tiles["drive"].set_value(number(drive, 2))
        self.tiles["recover"].set_value(number(recover, 2))
        try:
            self.tiles["ratio"].set_value(f"1 : {float(recover) / float(drive):.1f}")
        except (TypeError, ValueError, ZeroDivisionError):
            self.tiles["ratio"].set_value("–")
        self.tiles["energy"].set_value(number(packet.get("energyJoules"), 0))
        self.tiles["drag"].set_value(number(packet.get("dragFactor"), 0))
        self.tiles["count"].set_value(str(self.stroke_count))

        self.stroke_label.config(text=f"stroke {self.stroke_count}")

    # -- shutdown ---------------------------------------------------------
    def on_close(self) -> None:
        self._running = False
        self.root.destroy()

#-------------------------------------------------
#Inicial window participant's information
#------------------------------------------------
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

    parser = argparse.ArgumentParser(description="Live RP3 force-curve dashboard (tkinter).")
    parser.add_argument("--host", default="localhost", help="RP3 IP address")
    parser.add_argument("--port", type=int, default=62321, help="RP3 TCP port")
    parser.add_argument("--csv", default=None,
                        help="CSV file to append to (default: a timestamped file in ./logs)")
    parser.add_argument("--no-csv", action="store_true", help="Do not log to CSV")
    parser.add_argument("--demo", action="store_true",
                        help="Generate strokes internally instead of connecting to anything")
    args = parser.parse_args()


    logger = None
    if not args.no_csv:
        path= args.csv
        if path is None:
            os.makedirs("logs", exist_ok=True)
            filename = f"{participant_id}_{session_id}.csv"
            path=os.path.join("logs", filename)
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
