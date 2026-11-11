"""Keyboard-driven Seestar dashboard."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import curses
import time

from alpaca import discover, search_simbad
from demo import demo_devices, search_demo
from sky import LEFT, MIN_WIDTH, draw_sky

POLL_SECONDS = 0.35
SEARCH_DELAY = 0.4


def sexagesimal(value, signed=False):
    if value is None:
        return "—"
    seconds = round(abs(value) * 3600)
    if not signed:
        seconds %= 86400
    units, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    sign = ("−" if value < 0 else "+") if signed else ""
    return f"{sign}{units:02d}:{minutes:02d}:{seconds:02d}"


def degrees(value):
    return "—" if value is None else f"{value:07.2f}°"


def object_icon(kind):
    kind = kind.lower()
    if "galax" in kind:
        return "◎"
    if "nebula" in kind or "h ii" in kind or "hii" in kind:
        return "◇"
    if "cluster" in kind:
        return "⁙"
    if "star" in kind:
        return "✦"
    return "·"


class Dashboard:
    def __init__(self, screen, executor, options):
        self.screen = screen
        self.executor = executor
        self.options = options
        self.devices = []
        self.device = None
        self.device_index = self.result_index = 0
        self.results = []
        self.target = None
        self.status = {}
        self.filters = []
        self.query = ""
        self.search_at = self.poll_at = 0
        self.scan = self.poll = self.lookup = self.command = self.filter_names = None
        self.lookup_query = ""
        self.sky_job = self.sky_key = self.sky_position = None
        self.sky_at = 0
        self.sky_status = ""
        self.confirm = None
        self.pick_filter = False
        self.notice = ""
        self.search_status = ""
        self.choosing = True
        colors = (curses.COLOR_CYAN, curses.COLOR_WHITE, curses.COLOR_GREEN,
                  curses.COLOR_YELLOW, curses.COLOR_RED)
        self.palette = [0] * (len(colors) + 1)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for index, color in enumerate(colors, 1):
                curses.init_pair(index, color, -1)
                self.palette[index] = curses.color_pair(index)
        self.rescan()

    def rescan(self):
        if self.scan is None:
            self.choosing = True
            self.scan = self.executor.submit(demo_devices if self.options.demo else lambda: discover(self.options.host))

    def select_device(self):
        if not self.devices or self.scan:
            return
        self.device = self.devices[self.device_index]
        self.status = {}
        self.filters = []
        self.choosing = False
        self.poll = None
        self.poll_at = 0
        self.filter_names = self.executor.submit(self.device.filters)
        self.notice = ""

    def background(self, now):
        if self.scan and self.scan.done():
            try:
                self.devices = self.scan.result()
                self.device_index = 0
            except Exception as error:
                self.notice = str(error)
            self.scan = None

        for name in ("poll", "filter_names", "command"):
            future = getattr(self, name)
            if future is None or not future.done():
                continue
            try:
                result = future.result()
                if name == "poll":
                    self.status = result
                elif name == "filter_names":
                    self.filters = result or []
            except Exception as error:
                self.notice = str(error)
            setattr(self, name, None)

        if self.device and not self.choosing and self.poll is None and now >= self.poll_at:
            self.poll = self.executor.submit(self.device.telemetry)
            self.poll_at = now + POLL_SECONDS
        self.update_sky(now)

        if self.lookup and self.lookup.done():
            if self.lookup_query == self.query:
                try:
                    self.results = self.lookup.result()
                    self.result_index = 0
                    self.search_status = "" if self.results else "No matching objects."
                except Exception as error:
                    self.search_status = f"Lookup failed: {error}"
            self.lookup = None
        if self.search_at and now >= self.search_at and self.lookup is None:
            self.search_at = 0
            if len(self.query.strip()) >= 2:
                self.search_status = "Searching…"
                self.lookup_query = self.query
                finder = search_demo if self.options.demo else search_simbad
                self.lookup = self.executor.submit(finder, self.query.strip())

    def update_sky(self, now):
        active = self.device and self.target and not self.choosing and self.screen.getmaxyx()[1] >= MIN_WIDTH
        key = (id(self.device), self.target) if active else None
        if key != self.sky_key:
            self.sky_key = key
            self.sky_position = None
            self.sky_status = "Calculating target position…"
            self.sky_at = 0

        if self.sky_job and self.sky_job[1].done():
            job_key, future = self.sky_job
            if job_key == key:
                try:
                    self.sky_position = future.result()
                except Exception:
                    self.sky_position = None
                    self.sky_status = "Site or position unavailable"
            self.sky_job = None

        if key and self.sky_job is None and now >= self.sky_at:
            self.sky_at = now + 5
            self.sky_job = (key, self.executor.submit(self.device.horizontal, self.target))

    def line(self, y, x, value, style=0):
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and x < width - 1:
            try:
                clean = "".join(c if c.isprintable() else " " for c in str(value))
                self.screen.addnstr(y, x, clean, width - x - 1, style)
            except curses.error:
                pass

    def render(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 25 or width < 75:
            self.line(1, 2, "Resize terminal to at least 75 × 25.  Esc quits.")
        elif self.choosing:
            self.render_picker(height, width)
        else:
            self.render_dashboard(height, width)
        self.screen.noutrefresh()
        curses.doupdate()

    def render_picker(self, height, width):
        accent = self.palette[1] | curses.A_BOLD
        self.line(1, 3, "SEESTAR / DEVICE DISCOVERY", accent)
        self.line(3, 3, "Simulator" if self.options.demo else "LAN broadcast  /  ASCOM Alpaca")
        self.line(5, 3, "Searching…" if self.scan else "Select a Seestar" if self.devices else "No Seestar found. Enable Alpaca or supply --host IP.")
        if self.notice and not self.devices:
            self.line(6, 3, self.notice, self.palette[4])
        visible = height - 10
        start = max(0, self.device_index - visible + 1)
        for index, device in enumerate(self.devices[start:start + visible], start):
            selected = index == self.device_index
            self.line(7 + index - start, 3, f"{'›' if selected else ' '}  {device.name}    {device.server.address}", curses.A_REVERSE if selected else 0)
        self.line(height - 2, 3, "↑↓ Select     Enter Open     F1 Rescan     Esc Back / quit", accent)

    def render_dashboard(self, height, width):
        accent = self.palette[1] | curses.A_BOLD
        mode = "SIMULATION" if self.options.demo else "CONTROL ENABLED" if self.options.control else "READ-ONLY"
        s = self.status
        self.line(1, 3, f"SEESTAR   /   {mode}", accent)
        self.line(2, 3, f"{self.device.name}   ·   {self.device.server.address}", curses.A_DIM)
        self.line(3, 3, "-" * (width - 7), self.palette[1])
        self.line(4, 3, "EQUATORIAL", accent)
        self.line(4, 28, "HORIZONTAL", accent)
        self.line(4, 51, "MOUNT", accent)
        ra, dec, lst = (s.get(key) for key in ("rightascension", "declination", "siderealtime"))
        ha = (lst - ra + 12) % 24 - 12 if ra is not None and lst is not None else None
        motion = next((label for key, label in (("slewing", "SLEWING"), ("atpark", "PARKED"), ("athome", "AT HOME"), ("tracking", "TRACKING")) if s.get(key)), "IDLE" if s.get("slewing") is False else "NO TELEMETRY")
        self.line(5, 3, f"RA    {sexagesimal(ra)}")
        self.line(6, 3, f"Dec   {sexagesimal(dec, True)}")
        self.line(7, 3, f"HA    {sexagesimal(ha, True)}")
        self.line(5, 28, f"Az   {degrees(s.get('azimuth'))}")
        self.line(6, 28, f"El   {degrees(s.get('altitude'))}")
        self.line(7, 28, f"LST  {sexagesimal(lst)}")
        self.line(5, 51, motion, self.palette[3] | curses.A_BOLD)
        self.line(6, 51, f"Tracking  {('ON' if s['tracking'] else 'OFF') if s.get('tracking') is not None else '—'}")
        self.line(8, 3, "-" * ((LEFT if width >= MIN_WIDTH else width) - 7), self.palette[1])
        current = s.get("filter")
        filters = "   ".join(f"{'●' if current == i else '○'} {name}" for i, name in enumerate(self.filters))
        filter_status = "  ·  moving" if current == -1 else "  ·  position unavailable" if current is None else ""
        self.line(9, 3, "FILTER    " + (filters or "—") + filter_status + "     [F3 Select]", accent)
        self.line(11, 3, "SIMBAD  /  " + ("OFFLINE SAMPLE" if self.options.demo else "HARVARD CfA"), accent)
        text_width = LEFT - 2 if width >= MIN_WIDTH else width
        self.line(12, 3, "› " + self.query[-(text_width - 9):] + "▏", self.palette[2] | curses.A_BOLD)
        self.line(13, 3, self.search_status or ("↑↓ Browse results  ·  Enter Select" if self.results else "Type a catalogue ID or object name"), curses.A_DIM)
        visible = height - 20
        start = max(0, self.result_index - visible + 1)
        for index, obj in enumerate(self.results[start:start + visible], start):
            selected = index == self.result_index
            display_name = " ".join(obj.name.split())
            self.line(14 + index - start, 3, f"{'›' if selected else ' '} {object_icon(obj.kind)} {display_name[:20]:20} {obj.kind[:23]:23} {sexagesimal(obj.ra / 15)}  {sexagesimal(obj.dec, True)}", curses.A_REVERSE if selected else 0)
        if self.target:
            obj = self.target
            summary = f"TARGET   {obj.name}  ·  {obj.alias}  ·  {obj.kind}"
            self.line(height - 6, 3, summary[:text_width - 4], accent)
            self.line(height - 5, 3, f"J2000    RA {sexagesimal(obj.ra / 15)}    Dec {sexagesimal(obj.dec, True)}", accent)
        else:
            self.line(height - 6, 3, "TARGET   Select a SIMBAD result", curses.A_DIM)
        message = self.confirm[0] + "  [Y confirm / Esc cancel]" if self.confirm else self.notice
        if self.pick_filter:
            choices = "   ".join(f"{i + 1} {name}" for i, name in enumerate(self.filters[:9]))
            message = f"Select filter   {choices}   Esc cancel"
        self.line(height - 3, 3, message, self.palette[4])
        self.line(height - 2, 3, "F1 Devices    F2 GoTo    F3 Filter    F4 Stop + Home    Esc Quit", accent)
        if width >= MIN_WIDTH:
            status = self.sky_status if self.target else "Select a SIMBAD target"
            draw_sky(self.line, height, width, self.target, self.sky_position, status,
                     accent, curses.A_DIM, self.palette[3] | curses.A_BOLD,
                     self.palette[4] | curses.A_BOLD,
                     scope_position=(s.get("azimuth"), s.get("altitude")),
                     scope_style=self.palette[5] | curses.A_BOLD)

    def prepare(self, label, action):
        if self.command:
            self.notice = "A command is still being sent."
        elif not (self.options.control or self.options.demo):
            self.notice = "Read-only mode. Restart with --control to operate the hardware."
        else:
            self.confirm = (label, action)

    def key(self, key):
        if self.pick_filter:
            if key == "\x1b":
                self.pick_filter = False
            elif isinstance(key, str) and key in "123456789" and int(key) <= len(self.filters):
                index = int(key) - 1
                self.pick_filter = False
                if index != self.status.get("filter"):
                    self.prepare(f"Select {self.filters[index]} filter?", lambda: self.device.select_filter(index))
            return True
        if self.confirm:
            if key in ("y", "Y"):
                label, action = self.confirm
                self.command = self.executor.submit(action)
                self.notice = label + " requested. Watch live status."
                self.confirm = None
            elif key in ("\x1b", "n", "N"):
                self.confirm = None
            return True
        if key == "\x1b":
            if self.choosing and self.device:
                self.choosing = False
                return True
            return False
        if key in (curses.KEY_F1, "\x12") and not self.command:
            self.rescan()
        if self.choosing:
            if key in (curses.KEY_UP, curses.KEY_DOWN) and self.devices:
                delta = -1 if key == curses.KEY_UP else 1
                self.device_index = (self.device_index + delta) % len(self.devices)
            if key in ("\n", "\r", curses.KEY_ENTER):
                self.select_device()
            return True
        if key in (curses.KEY_UP, curses.KEY_DOWN) and self.results:
            delta = -1 if key == curses.KEY_UP else 1
            self.result_index = (self.result_index + delta) % len(self.results)
        elif key in ("\n", "\r", curses.KEY_ENTER) and self.results:
            self.target = self.results[self.result_index]
        elif key in (curses.KEY_F2, "\x07"):
            if self.target:
                target = self.target
                self.prepare(f"Slew to {target.name}?", lambda: self.device.goto(target))
            else:
                self.notice = "Select a SIMBAD result first."
        elif key in (curses.KEY_F3, "\x06"):
            if self.filters and self.status.get("filter") != -1:
                self.pick_filter = True
            else:
                self.notice = "Filter selection is currently unavailable."
        elif key in (curses.KEY_F4, "\x0f"):
            self.prepare("Abort Alpaca activity and return home?", self.device.home)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b", "\x15") or isinstance(key, str) and key.isprintable():
            if key == "\x15":
                self.query = ""
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.query = self.query[:-1]
            elif len(self.query) < 100:
                self.query += key
            self.results = []
            self.target = None
            self.search_status = ""
            self.search_at = time.monotonic() + SEARCH_DELAY
        return True


def run(screen, options):
    curses.raw()
    screen.keypad(True)
    screen.timeout(60)
    curses.set_escdelay(35)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    with ThreadPoolExecutor(max_workers=5) as executor:
        app = Dashboard(screen, executor, options)
        while True:
            app.background(time.monotonic())
            app.render()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key == "\x03" or not app.key(key):
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="append", default=[], metavar="IP[:PORT]", help="add a known device to LAN discovery")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--control", action="store_true", help="enable real hardware commands")
    modes.add_argument("--demo", action="store_true", help="simulate a Seestar and use a small offline catalogue")
    options = parser.parse_args()
    options.host = [address if ":" in address else address + ":32323" for address in options.host]
    try:
        curses.wrapper(run, options)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
