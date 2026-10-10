"""System readings shared by the panel and the backend: CPU, memory, disk, temperature,
clock speed and the firmware's undervoltage/throttling flags."""
import re
import shutil
import subprocess
import time
from pathlib import Path


def command_output(args, timeout=.6):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def read_throttled(run=command_output):
    """Raw `vcgencmd get_throttled` flags, or None when unavailable."""
    match = re.fullmatch(r'throttled=(0x[0-9a-fA-F]+)', run(['vcgencmd', 'get_throttled']))
    return int(match[1], 16) if match else None


def decode_throttle(output):
    match = re.fullmatch(r'throttled=(0x[0-9a-fA-F]+)', output)
    return decode_flags(int(match[1], 16) if match else None)


def decode_flags(flags):
    """Firmware flag bits: 0 undervoltage now, 1 frequency capped, 2 throttled, 3 soft
    temperature limit; bits 16-19 are the same conditions 'since boot' (sticky)."""
    if flags is None:
        return {'power': 'Unknown', 'limits': 'Unknown', 'prior_alerts': 'Unknown',
                'flags': None}
    active = []
    if flags & 4:
        active.append('Throttled')
    if flags & 2:
        active.append('Capped')
    if flags & 8:
        active.append('Temp limit')
    return {'power': 'LOW VOLTAGE' if flags & 1 else 'No undervoltage',
            'limits': ', '.join(active) or 'None active',
            'prior_alerts': 'Yes, since boot' if flags & 0xf0000 else 'None since boot',
            'flags': flags}


def undervoltage_seen(previous, flags):
    """True if undervoltage is happening now, or the sticky 'since boot' bit turned on
    since the previous reading (a dip between polls). A sticky bit that was already set
    when we started proves nothing new."""
    if flags is None:
        return False
    new_sticky = previous is not None and flags & 0x10000 and not previous & 0x10000
    return bool(flags & 1 or new_sticky)


def health_alerts(rows, now=None):
    """Return (level, text) pairs for the banner, most serious first."""
    now = time.monotonic() if now is None else now
    alerts = []
    if rows.get('power') == 'LOW VOLTAGE':
        alerts.append(('critical', 'LOW VOLTAGE'))
    elif rows.get('undervolt_seen_at') is not None and now-rows['undervolt_seen_at'] < 300:
        # The sticky firmware bit flipped since launch: a dip happened between polls.
        alerts.append(('warning', 'VOLTAGE DIPPED'))
    temp = rows.get('temp_c')
    if temp is not None:
        if temp >= 80:
            alerts.append(('critical', f'HOT {temp:.0f} C'))
        elif temp >= 75:
            alerts.append(('warning', f'WARM {temp:.0f} C'))
    limits = rows.get('limits')
    if limits not in (None, 'Unknown', 'None active'):
        alerts.append(('warning', 'THROTTLED' if 'Throttled' in limits else
                       'CPU CAPPED' if 'Capped' in limits else 'TEMP LIMIT'))
    free = rows.get('disk_free_gib')
    if free is not None:
        if free < .25:
            alerts.append(('critical', f'STORAGE {free*1024:.0f} MB'))
        elif free < 1 or rows.get('disk_used_percent', 0) >= 95:
            alerts.append(('warning', f'LOW STORAGE {free:.1f} GB'))
    alerts.sort(key=lambda alert: alert[0] != 'critical')
    return alerts


def wireless_signal(proc=Path('/proc/net/wireless'), run=command_output):
    """Signal level from the kernel's table (no `iw` process); falls back to `iw`."""
    try:
        for line in proc.read_text().splitlines()[2:]:
            name, _, fields = line.partition(':')
            parts = fields.split()
            if len(parts) >= 3:
                level = float(parts[2].rstrip('.'))
                if level < 0:
                    return f'{name.strip()}: {level:.0f} dBm'
    except (OSError, ValueError):
        pass
    for path in Path('/sys/class/net').glob('*'):
        if (path/'wireless').exists():
            match = re.search(r'signal:\s*(-?\d+(?:\.\d+)?)\s*dBm',
                              run(['iw', 'dev', path.name, 'link']))
            if match:
                return f'{path.name}: {match[1]} dBm'
    return None


class SystemSampler:
    """Cheap numeric readings from /proc and /sys (no processes spawned). CPU use is the
    change since the previous read(), so the first reading has cpu=None."""
    def __init__(self, root=Path('/'), disk='/'):
        self.root, self.disk = Path(root), disk
        self.previous = None

    def text(self, path):
        return (self.root / path).read_text()

    def read(self):
        cpu = [int(v) for v in self.text('proc/stat').splitlines()[0].split()[1:9]]
        total, idle = sum(cpu), cpu[3] + cpu[4]
        usage = None
        if self.previous:
            dt, di = total-self.previous[0], idle-self.previous[1]
            if dt > 0:
                usage = max(0, min(100, 100*(1-di/dt)))
        self.previous = (total, idle)
        mem = {}
        for line in self.text('proc/meminfo').splitlines():
            parts = line.split()
            mem[parts[0].rstrip(':')] = int(parts[1])
        mem_total = mem['MemTotal'] / 1048576
        mem_used = (mem['MemTotal'] - mem['MemAvailable']) / 1048576
        disk = shutil.disk_usage(self.disk)
        values = {'cpu': usage, 'mem_used_gib': mem_used, 'mem_total_gib': mem_total,
                  'mem_percent': 100*mem_used/mem_total,
                  'disk_free_gib': disk.free/2**30, 'disk_used_percent': 100*disk.used/disk.total,
                  'uptime_s': int(float(self.text('proc/uptime').split()[0])),
                  'temp_c': None, 'freq_mhz': None, 'load1': None}
        for key, path, scale in (('temp_c', 'sys/class/thermal/thermal_zone0/temp', 1000),
                                 ('freq_mhz', 'sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 1000)):
            try:
                values[key] = int(self.text(path)) / scale
            except (OSError, ValueError):
                pass
        try:
            values['load1'] = float(self.text('proc/loadavg').split()[0])
        except (OSError, ValueError, IndexError):
            pass
        return values
