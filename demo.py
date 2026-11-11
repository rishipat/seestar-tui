"""In-memory Alpaca server for demos and command-path verification."""

import time

from alpaca import Device, Target

CATALOGUE = [
    Target("M  31", "NGC 224", "Galaxy", 10.6847, 41.2690),
    Target("M  42", "NGC 1976", "H II region", 83.8221, -5.3911),
    Target("M  57", "NGC 6720", "Planetary Nebula", 283.3962, 33.0292),
    Target("IC 1396", "Elephant's Trunk region", "H II region", 324.75, 57.5),
    Target("* alf Lyr", "Vega", "Star", 279.2347, 38.7837),
]


def search_demo(term):
    query = term.lower().replace(" ", "")
    return [obj for obj in CATALOGUE if any(
        query in value.lower().replace(" ", "") for value in (obj.name, obj.alias)
    )]


class DemoServer:
    address = "simulator"

    def __init__(self):
        self.started = time.monotonic()
        self.origin = self.destination = (20.0, 38.0)
        self.motion_start = 0
        self.home = True
        self.filter = 1
        self.exposure = 0
        self.tracking = True

    def pointing(self):
        fraction = min(1, (time.monotonic() - self.motion_start) / 4)
        ra, dec = self.origin
        target_ra, target_dec = self.destination
        return ((ra + ((target_ra - ra + 12) % 24 - 12) * fraction) % 24,
                dec + (target_dec - dec) * fraction, fraction < 1)

    def request(self, path, values=None):
        kind, _, member = path.split("/")[-3:]
        ra, dec, moving = self.pointing()
        if values is None:
            return {
                "rightascension": ra, "declination": dec, "azimuth": (ra * 15) % 360,
                "altitude": 45 + dec / 4, "siderealtime": 21 + (time.monotonic() - self.started) / 3590,
                "slewing": moving, "tracking": self.tracking, "athome": self.home and not moving,
                "atpark": False, "position": self.filter, "names": ["Dark", "IR", "LP"],
                "connected": True, "camerastate": self.exposure if kind == "camera" else 0,
                "canslewasync": True, "canfindhome": True, "cansettracking": True,
                "canabortexposure": True,
            }[member]
        if member == "abortslew":
            self.origin = self.destination = (ra, dec)
            self.motion_start = 0
        if member == "abortexposure":
            self.exposure = 0
        if member in ("findhome", "slewtocoordinatesasync"):
            self.origin = (ra, dec)
            self.home = member == "findhome"
            self.destination = (20, 38) if self.home else (values["RightAscension"], values["Declination"])
            self.motion_start = time.monotonic()
        if member == "position":
            self.filter = values["Position"]
        if member == "tracking":
            self.tracking = values["Tracking"] == "true"


class DemoDevice(Device):
    def site(self):
        return 37.87, -122.27, 0.0  # Fixed demo observing site in Berkeley.

    def coordinates(self, target):
        return target.ra / 15, target.dec, 45


def demo_devices():
    return [DemoDevice(DemoServer(), "Seestar S30 Pro · Simulator", 0, (0, 1), 0, "demo")]
