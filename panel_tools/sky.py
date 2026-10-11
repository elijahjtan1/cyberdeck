"""Sunrise, sunset, twilight and moon phase, computed offline from a date and a position.

The sun uses the standard sunrise equation (good to a minute or two away from the
poles); the moon uses the mean synodic month, so its phase can be up to about half a
day off. Both are plenty for planning daylight, and need no data files.
"""
import json
import math
from datetime import datetime, timedelta, timezone

J2000 = 2451545.0             # Julian date of 2000-01-01 12:00 UTC.
SYNODIC_MONTH = 29.530588853  # Mean days from new moon to new moon.
NEW_MOON = 2451550.1          # A mean new moon (2000-01-06), the phase reference.
SUNRISE_ALTITUDE = -0.833     # Degrees: sun's radius plus atmospheric refraction.
CIVIL_ALTITUDE = -6.0         # Civil twilight: enough light to work outdoors.
PHASE_NAMES = ('New moon', 'Waxing crescent', 'First quarter', 'Waxing gibbous',
               'Full moon', 'Waning gibbous', 'Last quarter', 'Waning crescent')


def julian(moment):
    return J2000 + (moment - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds()/86400


def from_julian(jd):
    return datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(days=jd-J2000)


def sun_events(day, lat, lon, altitude=SUNRISE_ALTITUDE):
    """(rise, set) in UTC for the calendar date `day` at lat/lon (degrees, east positive).

    Returns ('up', 'up') when the sun never sets that day and ('down', 'down') when it
    never rises, so the page can say so instead of showing a time.
    """
    n = day.toordinal() - datetime(2000, 1, 1).toordinal()
    mean = n - lon/360
    anomaly = math.radians((357.5291 + 0.98560028*mean) % 360)
    centre = (1.9148*math.sin(anomaly) + 0.02*math.sin(2*anomaly) + 0.0003*math.sin(3*anomaly))
    ecliptic = math.radians((math.degrees(anomaly) + centre + 180 + 102.9372) % 360)
    transit = J2000 + mean + 0.0053*math.sin(anomaly) - 0.0069*math.sin(2*ecliptic)
    declination = math.asin(math.sin(ecliptic)*math.sin(math.radians(23.4397)))
    phi = math.radians(lat)
    cos_hour = ((math.sin(math.radians(altitude)) - math.sin(phi)*math.sin(declination))
                / (math.cos(phi)*math.cos(declination)))
    if cos_hour < -1:
        return 'up', 'up'
    if cos_hour > 1:
        return 'down', 'down'
    hour = math.degrees(math.acos(cos_hour))/360
    return from_julian(transit-hour), from_julian(transit+hour)


def moon_phase(moment):
    """(age in days, fraction lit 0-1, phase name, days to next full, days to next new)."""
    age = (julian(moment) - NEW_MOON) % SYNODIC_MONTH
    lit = (1 - math.cos(2*math.pi*age/SYNODIC_MONTH))/2
    name = PHASE_NAMES[int((age/SYNODIC_MONTH)*8 + .5) % 8]
    to_full = (SYNODIC_MONTH/2 - age) % SYNODIC_MONTH
    return age, lit, name, to_full, SYNODIC_MONTH - age


def load_location(path):
    """Saved position from location.json, or None if it's missing or not a valid position."""
    try:
        saved = json.loads(path.read_text())
        lat, lon = float(saved['lat']), float(saved['lon'])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or math.isnan(lat) or math.isnan(lon):
        return None
    return lat, lon


def position_label(lat, lon):
    return f"{abs(lat):.1f}{'N' if lat >= 0 else 'S'} {abs(lon):.1f}{'E' if lon >= 0 else 'W'}"


def sky_lines(wall_time, location, sync='Unknown'):
    """Text rows for the SKY page; the moon needs no position, the sun does.

    sync is the health poller's NTP state. Without an RTC an offline Pi keeps the last
    saved time, so every answer here is for the wrong day unless the clock is synced.
    """
    if wall_time.tzinfo is None:
        wall_time = wall_time.astimezone()  # A naive time is the Pi's local time.
    clock_note = None if sync == 'Synchronized' else (
        'Clock not synced: check the date' if sync == 'Not synchronized'
        else 'Clock sync unknown: check the date')
    _, lit, name, to_full, to_new = moon_phase(wall_time)
    moon = f'{name}, {lit*100:.0f}% lit'
    nearer, days = ('Full', to_full) if to_full < to_new else ('New', to_new)
    days = round(days)
    upcoming = (f'{nearer} moon about now' if days == 0 else
                f"{nearer} moon in about {days} day{'s' if days > 1 else ''}")
    if location is None:
        return {'where': 'No saved location', 'sun': [], 'moon': moon, 'next': upcoming, 'lit': lit,
                'waxing': to_full < to_new, 'clock': clock_note}
    lat, lon = location
    zone = wall_time.tzinfo
    day = wall_time.date()

    def clock(value):
        if value == 'up':
            return 'sun up all day'
        if value == 'down':
            return 'sun down all day'
        return value.astimezone(zone).strftime('%H:%M')

    # Worked out for the local calendar date; where clock time is far from solar time
    # (Kiritimati, UTC+14 at 157W) the times can belong to the next day, a minute apart.
    rise, sunset = sun_events(day, lat, lon)
    dawn, dusk = sun_events(day, lat, lon, CIVIL_ALTITUDE)
    if isinstance(rise, datetime):
        minutes = round((sunset-rise).total_seconds()/60)
        daylight = f'Daylight {minutes//60}h {minutes%60:02}m'
        times = f'Sunrise {clock(rise)}   Sunset {clock(sunset)}'
    else:
        daylight = 'No sunrise or sunset today' if rise == 'up' else 'No daylight today'
        times = clock(rise).capitalize()
    sun = [times,
           f'Civil dawn {clock(dawn)}   dusk {clock(dusk)}' if isinstance(dawn, datetime)
           else 'Civil twilight: ' + ('no full dark' if dawn == 'up' else 'none'),
           daylight]
    return {'where': 'Saved location ' + position_label(lat, lon), 'sun': sun, 'moon': moon,
            'next': upcoming, 'lit': lit, 'waxing': to_full < to_new, 'clock': clock_note,
            'south': lat < 0}
