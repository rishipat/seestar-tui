"""LAN discovery, Alpaca requests, and coordinate handling."""

import csv
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import count
import json
import math
import re
import secrets
import socket
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler, Request, urlopen

TAP = "https://simbad.cfa.harvard.edu/simbad/sim-tap/sync"
POINTING = "rightascension declination azimuth altitude siderealtime slewing tracking athome atpark".split()
CLIENT = secrets.randbelow(65534) + 1
TRANSACTIONS = count(1)


class AlpacaError(Exception):
    def __init__(self, message, number=None):
        super().__init__(message)
        self.number = number


class Server:
    def __init__(self, address):
        self.address = address
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, values=None):
        params = {"ClientID": CLIENT, "ClientTransactionID": next(TRANSACTIONS)}
        url = f"http://{self.address}{path}"
        if values is None:
            request = Request(url + "?" + urlencode(params))
        else:
            request = Request(url, urlencode({**params, **values}).encode(), method="PUT")
        with self.opener.open(request, timeout=3) as response:
            result = json.load(response)
        if result.get("ErrorNumber", 0):
            raise AlpacaError(f"{path.rsplit('/', 1)[-1]}: {result.get('ErrorMessage') or result['ErrorNumber']}", result["ErrorNumber"])
        return result.get("Value")


@dataclass
class Device:
    server: Server
    name: str
    mount: int
    cameras: tuple
    wheel: int
    uid: str

    def read(self, member, kind="telescope", number=None):
        return self.server.request(self.path(member, kind, number))

    def write(self, member, values=None, kind="telescope", number=None):
        return self.server.request(self.path(member, kind, number), values or {})

    def path(self, member, kind, number):
        if number is None:
            number = self.mount if kind == "telescope" else self.wheel
        return f"/api/v1/{kind}/{number}/{member}"

    def telemetry(self):
        def measure(member):
            try:
                if member == "filter":
                    return member, self.read("position", "filterwheel")
                return member, self.read(member)
            except (OSError, ValueError, AlpacaError):
                return member, None

        with ThreadPoolExecutor(max_workers=6) as executor:
            return dict(executor.map(measure, POINTING + (["filter"] if self.wheel is not None else [])))

    def filters(self):
        return self.read("names", "filterwheel") if self.wheel is not None else []

    def connect(self, kind="telescope", number=None):
        if not self.read("connected", kind, number):
            self.write("connected", {"Connected": "true"}, kind, number)

    def select_filter(self, position):
        self.connect("filterwheel")
        self.write("position", {"Position": position}, "filterwheel")

    def home(self):
        self.connect()
        if not self.read("canfindhome"):
            raise AlpacaError("This driver does not advertise FindHome.")
        if self.read("atpark"):
            raise AlpacaError("Mount is parked. Unpark deliberately before requesting home.")
        errors = []
        for number in self.cameras:
            try:
                if self.read("connected", "camera", number):
                    state = self.read("camerastate", "camera", number)
                    if state in (1, 2):
                        if self.read("canabortexposure", "camera", number):
                            self.write("abortexposure", kind="camera", number=number)
                        else:
                            raise AlpacaError("Camera does not support AbortExposure.")
            except (OSError, AlpacaError, ValueError) as error:
                errors.append(f"camera {number}: {error}")
        if self.read("slewing"):
            self.write("abortslew")
            deadline = time.monotonic() + 8
            while self.read("slewing"):
                if time.monotonic() >= deadline:
                    raise AlpacaError("Slew did not stop. Home command was withheld.")
                time.sleep(0.2)
        if errors:
            raise AlpacaError("Home withheld because camera activity could not be stopped: " + "; ".join(errors))
        if not self.read("athome"):
            self.write("findhome")

    def goto(self, target):
        self.connect()
        if self.read("atpark") or self.read("slewing"):
            raise AlpacaError("Mount is parked or already slewing.")
        if not self.read("canslewasync"):
            raise AlpacaError("This driver does not advertise asynchronous RA/Dec slews.")
        ra, dec, altitude = self.coordinates(target)
        if altitude <= 0:
            raise AlpacaError(f"Target is below the horizon ({altitude:.1f}°).")
        if not self.read("tracking"):
            if not self.read("cansettracking"):
                raise AlpacaError("Tracking is off and cannot be enabled through this driver.")
            self.write("tracking", {"Tracking": "true"})
        self.write("slewtocoordinatesasync", {"RightAscension": ra, "Declination": dec})

    def site(self):
        lat, lon = (float(self.read(name)) for name in ("sitelatitude", "sitelongitude"))
        try:
            height = float(self.read("siteelevation"))
        except AlpacaError as error:
            if error.number != 1024:  # ASCOM NotImplemented
                raise
            height = 0.0
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and math.isfinite(height)):
            raise AlpacaError("Mount returned invalid observer coordinates.")
        return lat, lon, height

    def horizontal(self, target):
        _, horizontal = observe(target, self.site())
        return float(horizontal.az.deg), float(horizontal.alt.deg)

    def coordinates(self, target):
        return slew_coordinates(self, target)


def discover(seeds=()):
    addresses = set(seeds)
    broadcasts = {"255.255.255.255"}
    try:
        interfaces = subprocess.check_output(["/sbin/ifconfig"], text=True, timeout=2)
        broadcasts.update(re.findall(r"\bbroadcast (\d+\.\d+\.\d+\.\d+)", interfaces))
    except (OSError, subprocess.SubprocessError):
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for broadcast in broadcasts:
            try:
                sock.sendto(b"alpacadiscovery1", (broadcast, 32227))
            except OSError:
                continue
        deadline = time.monotonic() + 1.3
        while time.monotonic() < deadline:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                data, sender = sock.recvfrom(2048)
                port = int(json.loads(data)["AlpacaPort"])
                if 1 <= port <= 65535:
                    addresses.add(f"{sender[0]}:{port}")
            except socket.timeout:
                break
            except (ValueError, KeyError, TypeError):
                continue

    def identify(address):
        server = Server(address)
        try:
            entries = server.request("/management/v1/configureddevices")
            cameras = tuple(item["DeviceNumber"] for item in entries if item["DeviceType"].lower() == "camera")
            wheel = next((item["DeviceNumber"] for item in entries if item["DeviceType"].lower() == "filterwheel"), None)
            return [
                Device(server, item["DeviceName"], item["DeviceNumber"], cameras, wheel, item["UniqueID"])
                for item in entries
                if item["DeviceType"].lower() == "telescope" and "seestar" in item["DeviceName"].lower()
            ]
        except (OSError, ValueError, AlpacaError, TypeError, KeyError):
            return []

    with ThreadPoolExecutor(max_workers=8) as executor:
        devices = {}
        for matches in executor.map(identify, sorted(addresses)):
            for device in matches:
                devices.setdefault(device.uid, device)
    return sorted(devices.values(), key=lambda device: device.name)


@dataclass(frozen=True)
class Target:
    name: str
    alias: str
    kind: str
    ra: float
    dec: float


def search_simbad(term):
    def literal(value):
        return "'" + value.replace("'", "''") + "'"

    # Escape LIKE metacharacters by excluding them from prefix searches.
    if any(character in term for character in "%_"):
        raise ValueError("Enter a catalogue name without wildcard characters.")
    term = " ".join(term.split())
    variable = re.fullmatch(r"(?:V\* )?([A-Za-z]{1,2}|[Vv]\d+) ([A-Za-z]{1,3})", term, re.IGNORECASE)
    catalogue = re.fullmatch(r"([A-Za-z]+) *([0-9].*)", term)
    if variable:
        designation, constellation = variable.groups()
        # SIMBAD indexes GCVS names under V*, with IAU constellation capitalization.
        mixed_case = ("CMa", "CMi", "CVn", "CrA", "CrB", "LMi", "PsA", "TrA", "UMa", "UMi")
        abbreviations = {constellation.title()}
        abbreviations.update(
            name[:len(constellation)] for name in mixed_case
            if name.lower().startswith(constellation.lower())
        )
        patterns = [f"V* {designation.upper()} {name}" for name in sorted(abbreviations)]
        if not term.upper().startswith("V* "):
            patterns.extend((term, term.upper(), "NAME " + term.title()))
    elif catalogue:
        prefix, identifier = catalogue.groups()
        prefix = prefix.upper()
        patterns = [f"{prefix} {identifier}"]
        widths = {"M": (2, 3), "NGC": (4, 5), "IC": (4,)}
        if prefix in widths and identifier.isdigit():
            # Query both the exact numeric field and longer numbers with this prefix.
            patterns = list(dict.fromkeys(
                f"{prefix} {' ' * max(0, width - length)}{identifier}"
                for width in widths[prefix]
                for length in range(len(identifier), min(width, 4) + 1)
            )) or patterns
    elif term.upper().startswith("NAME "):
        patterns = [term, "NAME " + term[5:].title()]
    else:
        patterns = [term, term.upper(), "NAME " + term.title()]
    patterns = list(dict.fromkeys(patterns))

    def lookup(pattern):
        columns = "b.main_id,b.ra,b.dec,t.otype_longname,i.id AS matched_id"
        query = (
            f"SELECT TOP 20 {columns} FROM ident AS i JOIN basic AS b ON i.oidref=b.oid "
            f"LEFT JOIN otypedef AS t ON b.otype=t.otype WHERE i.id LIKE {literal(pattern + '%')}"
        )
        params = urlencode({"request": "doQuery", "lang": "adql", "format": "csv", "query": query})
        with urlopen(TAP + "?" + params, timeout=7) as response:
            rows = csv.DictReader(response.read(512_000).decode("utf-8-sig").splitlines())
            if not {"main_id", "ra", "dec", "matched_id"}.issubset(rows.fieldnames or []):
                raise ValueError("Harvard SIMBAD did not return the expected TAP table.")
            return [
                Target(row["main_id"], row["matched_id"], row["otype_longname"] or "Object",
                       float(row["ra"]), float(row["dec"]))
                for row in rows if row["ra"] and row["dec"]
            ]

    exact = {" ".join(pattern.casefold().split()) for pattern in patterns}

    def rank(obj):
        return " ".join(obj.alias.casefold().split()) not in exact

    with ThreadPoolExecutor(max_workers=3) as executor:
        matches = [obj for rows in executor.map(lookup, patterns) for obj in rows
                   if 0 <= obj.ra < 360 and -90 <= obj.dec <= 90]
    targets = {}
    for obj in sorted(matches, key=rank):
        targets.setdefault(obj.name, obj)
    return list(targets.values())[:10]


def observe(target, site):
    try:
        from astropy import units as u
        from astropy.coordinates import AltAz, EarthLocation, SkyCoord
        from astropy.time import Time
        from astropy.utils import iers
    except ImportError as error:
        raise AlpacaError("Coordinate conversion needs Astropy. See README for installation.") from error

    iers.conf.auto_download = False
    lat, lon, height = site
    location = EarthLocation.from_geodetic(lon * u.deg, lat * u.deg, height * u.m)
    icrs = SkyCoord(target.ra * u.deg, target.dec * u.deg, frame="icrs")
    return icrs, icrs.transform_to(AltAz(obstime=Time.now(), location=location))


def slew_coordinates(device, target):
    frame = device.read("equatorialsystem")
    if frame not in (1, 2):
        raise AlpacaError(f"Unsupported mount coordinate frame ({frame}).")
    icrs, horizontal = observe(target, device.site())
    altitude = horizontal.alt.deg
    pointing = icrs
    if frame == 1:
        from astropy.coordinates import TETE

        pointing = icrs.transform_to(TETE(obstime=horizontal.obstime, location=horizontal.location))
    if not all(math.isfinite(value) for value in (pointing.ra.hour, pointing.dec.deg, altitude)):
        raise AlpacaError("Coordinate conversion produced an invalid value.")
    return float(pointing.ra.hour), float(pointing.dec.deg), float(altitude)
