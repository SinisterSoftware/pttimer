"""ファイル一覧に出す pttimer 用の列 (チャンネル・録画分数・状態・開始時刻)。"""

from urllib.parse import unquote

from xefm.user_api import Cell

from .logic import PTTimerAutoReservation, PTTimerReservation, _children

_WEEKDAYS = "月火水木金土日"


def in_pttimer(path):
    return str(path).startswith("pttimer://")


def _items(directory):
    text = str(directory)
    text = text.split("://", 1)[-1]
    segments = tuple(unquote(s) for s in text.replace("\\", "/").split("/") if s)
    nodes = _children(segments) or []
    return {n.name: n.item for n in nodes if n.item is not None}


def _read(directory, entries, getter):
    try:
        items = _items(directory)
    except Exception:
        return [None] * len(entries)
    out = []
    for e in entries:
        item = items.get(e.path.name)
        out.append(getter(item) if item is not None else None)
    return out


def channel(directory, entries):
    def get(item):
        if isinstance(item, PTTimerAutoReservation):
            return item.service_text
        return getattr(item, "channel", None) or getattr(item, "channel_name", None)
    return _read(directory, entries, get)


def minutes(directory, entries):
    return _read(directory, entries, lambda i: getattr(i, "length_minutes", None))


def state(directory, entries):
    def get(item):
        if isinstance(item, PTTimerReservation):
            return "off" if item.disabled else "on"
        return None
    return _read(directory, entries, get)


def start(directory, entries):
    return _read(directory, entries, lambda i: i.mtime if hasattr(i, "start") else None)


def fmt_start(value, entry):
    if value is None:
        return ""
    import time
    t = time.localtime(value)
    return f"{t.tm_mon:02d}/{t.tm_mday:02d}({_WEEKDAYS[t.tm_wday]}) {t.tm_hour:02d}:{t.tm_min:02d}"


def fmt_minutes(value, entry):
    if value is None:
        return ""
    return f"{value // 60}h{value % 60:02d}m" if value >= 60 else f"{value}m"


def fmt_state(value, entry):
    if value == "off":
        return Cell("×", color="red")
    return ""
