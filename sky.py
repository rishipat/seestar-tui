"""Terminal projection of the local celestial sphere."""

import curses
from functools import lru_cache
import math

MIN_WIDTH = 109
LEFT = 75


def sky_offset(azimuth, altitude, radius):
    """Stereographic projection centered at the zenith, with east to the right."""
    zenith_angle = math.radians(90 - max(0, altitude))
    distance = radius * math.tan(zenith_angle / 2)
    angle = math.radians(azimuth)
    return round(2 * distance * math.sin(angle)), round(-distance * math.cos(angle))


@lru_cache(maxsize=16)
def horizon(radius):
    return sorted({sky_offset(azimuth, 0, radius) for azimuth in range(360)})


@lru_cache(maxsize=16)
def grid(radius):
    rings = {sky_offset(azimuth, altitude, radius)
             for altitude in (30, 60) for azimuth in range(360)}
    spokes = {sky_offset(azimuth, altitude, radius)
              for azimuth in range(0, 360, 45) for altitude in range(1, 90)}
    return sorted(rings | spokes)


def compass_direction(azimuth):
    directions = ("North", "Northeast", "East", "Southeast", "South", "Southwest", "West", "Northwest")
    return directions[int((azimuth + 22.5) // 45) % len(directions)]


def draw_sky(line, height, width, target, position, status, accent, dim, visible, hidden,
             *, scope_position=(None, None), scope_style=0):
    panel_width = width - LEFT - 2
    radius = min(11, (panel_width - 4) // 4, (height - 14) // 2)
    center_x = LEFT + panel_width // 2
    center_y = 7 + radius

    line(4, LEFT, "LOCAL SKY", accent)
    for dx, dy in grid(radius):
        line(center_y + dy, center_x + dx, ".", dim)
    for dx, dy in horizon(radius):
        line(center_y + dy, center_x + dx, ".", accent)
    line(center_y - radius - 2, center_x, "N", accent)
    line(center_y + radius + 2, center_x, "S", accent)
    line(center_y, center_x - 2 * radius - 2, "W", accent)
    line(center_y, center_x + 2 * radius + 2, "E", accent)
    line(center_y, center_x, "+", dim)

    footer = center_y + radius + 3
    if position is not None:
        azimuth, altitude = position
        dx, dy = sky_offset(azimuth, altitude, radius)
        marker, style = ("*", visible) if altitude >= 0 else ("v", hidden)
        marker_x, marker_y = center_x + dx, center_y + dy
        name = " ".join(target.name.split())
        room = LEFT + panel_width - marker_x - 2 if dx <= 0 else marker_x - LEFT - 1
        label = name[:min(18, room)]
        if len(name) > len(label):
            label = label[:-3] + "..."
        label_x = marker_x + 2 if dx <= 0 else marker_x - len(label) - 1
        line(marker_y, label_x, label, style)
        line(footer, LEFT, f"{marker} {name}"[:panel_width], style)
        line(footer + 1, LEFT, f"Az {azimuth:6.1f}   El {altitude:+5.1f} deg")
        horizon_state = "above" if altitude >= 0 else "below"
        status = f"{compass_direction(azimuth)} / {horizon_state} horizon"

    scope_az, scope_alt = scope_position
    if scope_az is not None and scope_alt is not None and all(map(math.isfinite, scope_position)):
        dx, dy = sky_offset(scope_az, scope_alt, radius)
        scope_x, scope_y = center_x + dx, center_y + dy
        line(scope_y - 1, scope_x - 1, "+-+", scope_style)
        line(scope_y, scope_x - 1, "|", scope_style)
        line(scope_y, scope_x + 1, "|", scope_style)
        line(scope_y + 1, scope_x - 1, "+-+", scope_style)
        scope_label = "[ ] Scope below 0°" if scope_alt < 0 else "[ ] Scope"
        line(4, LEFT + 11, scope_label, scope_style)
    if position is not None:
        line(marker_y, marker_x, marker, style | curses.A_REVERSE)
    line(footer + 2, LEFT, status[:panel_width], dim)
    line(footer + 3, LEFT, "Rim 0°  Rings 30°/60°  + 90°", dim)
