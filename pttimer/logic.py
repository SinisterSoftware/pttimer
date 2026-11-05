#!/usr/bin/env python3
"""xefm_pttimer.py — pttimer:// 仮想フォルダ (~/.xefm/extensions/xefm_pttimer.py)

ptTimer.db の録画予約・自動予約・番組表を、読み取り専用の仮想フォルダとして見せる。

設定 (DB の場所など) は拡張パッケージの設定 (PttimerConfig クラス、既定値は pttimer/settings.py) から
使用時に読む。

URI 構造:
    pttimer://                                    カテゴリ一覧
    pttimer://予約/<日時>_<番組名>.txt             録画予約 (フラット)。delete_files (標準キー) で有効⇔無効のトグル
    pttimer://自動予約/<キーワード>_id<N>.txt      自動予約条件 (t_autoprog)
    pttimer://番組表/チャンネル/<種別>/<局>/<番組>.txt         局ごとの1週間分
    pttimer://番組表/日付/<日付>/<種別>/<局>/<番組>.txt        日付 → 種別 → 局
    pttimer://番組表/検索/<キーワード>/<番組>.txt              横断検索 (空白区切りで AND、
                                                  ローマ字は XeFM 内蔵 Migemo で日本語にも一致)

ファイルサイズは「録画/放送時間(分) × 1MB」の擬似値、更新日時は開始時刻。
無効な予約 (flg1==4) は隠しファイル扱い。
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path as _PyPath
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from xefm.path_base import ReadOnlyPathImpl, UriStatResult
from xefm.user_api import extension, is_desktop_mode

# ---------------------------------------------------------------------------
# 設定 (拡張の設定クラス PttimerConfig。使用時に読む)
# ---------------------------------------------------------------------------

_DEFAULTS: Dict[str, Any] = {
    "PTTIMER_DB_PATH": "",          # 空なら _default_db_path() の候補
    "CHANNEL_TXT_PATH": "",         # 局名・放送種別の解決に使う (更新は自動追従)
    "SERVICE_NAME_MAP": {},         # sid → 局名 の手動上書き (channel.txt より優先)
    "CHANNEL_TYPE_OVERRIDE": {},    # sid → "地デジ"/"BS"/"CS" の手動上書き
    "INCLUDE_PAST_RESERVATIONS": False,   # 終了済みの予約も出すか
    "EPG_DAYS_AHEAD": 7,            # 番組表に出す日数
    "EDITOR_COMMAND": ["vim"],      # 予約編集に使うエディタ (GUI なら --wait 相当を付ける)
}
OVERRIDES: Dict[str, Any] = {}     # テスト用。パッケージ外では拡張ハンドルが無いのでここが優先


def cfg(name: str) -> Any:
    if name in OVERRIDES:
        return OVERRIDES[name]
    ext = extension(__name__)
    if ext is not None:
        return getattr(ext.settings, name, _DEFAULTS[name])
    return _DEFAULTS[name]

CATEGORY_RESERVATIONS = "予約"
CATEGORY_AUTORESERVE = "自動予約"
CATEGORY_EPG = "番組表"
CATEGORIES = (CATEGORY_RESERVATIONS, CATEGORY_EPG, CATEGORY_AUTORESERVE)

EPG_VIEW_CHANNEL, EPG_VIEW_DATE, EPG_VIEW_SEARCH = "チャンネル", "日付", "検索"
EPG_VIEWS = (EPG_VIEW_CHANNEL, EPG_VIEW_DATE, EPG_VIEW_SEARCH)

BROADCAST_TYPES = ("地デジ", "BS", "CS")

_FLG1_STATUS = {0: "未処理", 1: "記録処理開始", 2: "録画中", 4: "無効"}
_FLG1_DISABLED = 4
_BYTES_PER_MINUTE = 1024 * 1024
_READ_ONLY_MSG = "pttimer 仮想フォルダは読み取り専用です"


def _default_db_path() -> Optional[str]:
    candidates = [
        os.environ.get("PTTIMER_DB_PATH"),
        os.path.expandvars(r"%ProgramFiles(x86)%\ptTimer\ptTimer.db"),
        os.path.expandvars(r"%ProgramFiles%\ptTimer\ptTimer.db"),
    ]
    return next((c for c in candidates if c and os.path.exists(c)), None)


def _db_path() -> Optional[str]:
    path = cfg("PTTIMER_DB_PATH") or _default_db_path()
    return path if path and os.path.exists(path) else None


def _query(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    """ptTimer.db を read-only (mode=ro) で引く。ptTimer が書き込み中でも読める。
    DB が無ければ []。ロック等で失敗したら例外 (キャッシュ側が直前のデータを使い続ける)。"""
    path = _db_path()
    if not path:
        return []
    uri = f"file:{quote(_PyPath(path).as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# チャンネル (channel.txt)
# ---------------------------------------------------------------------------

def classify_broadcast_type(onid_or_tsid: int) -> str:
    """channel.txt 2列目 (ONID/TSID の16進) から地デジ/BS/CS を推定する。

    公式な区別仕様が見つからないための推定で、誤判定は CHANNEL_TYPE_OVERRIDE で補正する。
      0x7D00 以上      地デジ (ONID。広域局 0x7FDx 台、中継局 0x7Dxx 台)
      0x4000-0x4FFF    BS     (TSID)
      0x6000-0x7CFF    110度CS (TSID)
    """
    if onid_or_tsid >= 0x7D00:
        return "地デジ"
    if 0x4000 <= onid_or_tsid <= 0x4FFF:
        return "BS"
    if 0x6000 <= onid_or_tsid <= 0x7CFF:
        return "CS"
    return "不明"


def _parse_channel_txt(path: str) -> Dict[int, Dict[str, Any]]:
    """channel.txt (CSV) → {sid: {name, onid_tsid, epg_enabled}}。
    列: [1]=ONID/TSID(16進) [4]=番組表取得 0=有効/1=無効 [5]=SID(16進) [6]=局名。
    文字コードは utf-8 → cp932 の順に試す。"""
    lines = None
    for encoding in ("utf-8", "cp932"):
        try:
            with open(path, encoding=encoding, newline="") as f:
                lines = f.read().splitlines()
            break
        except UnicodeDecodeError:
            continue
    result: Dict[int, Dict[str, Any]] = {}
    for row in csv.reader(lines or []):
        if len(row) < 7 or not row[6].strip():
            continue
        try:
            sid = int(row[5], 16)
        except ValueError:
            continue
        try:
            onid_tsid = int(row[1], 16)
        except ValueError:
            onid_tsid = None
        result[sid] = {"name": row[6].strip(), "onid_tsid": onid_tsid,
                       "epg_enabled": row[4].strip() == "0"}
    return result


class _ChannelMap:
    """channel.txt を mtime で自動再読込する。"""

    def __init__(self) -> None:
        self._map: Dict[int, Dict[str, Any]] = {}
        self._key: Optional[tuple] = None

    def reset(self) -> None:
        self._key = None

    def get(self) -> Dict[int, Dict[str, Any]]:
        path = cfg("CHANNEL_TXT_PATH")
        if not path or not os.path.exists(path):
            return {}
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            return self._map
        if key != self._key:
            self._map, self._key = _parse_channel_txt(path), key
        return self._map


_channels = _ChannelMap()


def channel_name(sid: int) -> str:
    names = cfg("SERVICE_NAME_MAP")
    if sid in names:
        return names[sid]
    info = _channels.get().get(sid)
    return info["name"] if info else f"SID:{sid}"


def _channel_type(sid: int) -> str:
    overrides = cfg("CHANNEL_TYPE_OVERRIDE")
    if sid in overrides:
        return overrides[sid]
    info = _channels.get().get(sid)
    if info is None or info["onid_tsid"] is None:
        return "不明"
    return classify_broadcast_type(info["onid_tsid"])


def reverse_channel_lookup(name: str) -> Optional[int]:
    """表示名 → sid (予約編集の保存用)。"SID:123" はそのまま数値。同名が複数なら None。"""
    name = name.strip()
    if m := re.fullmatch(r"SID:(\d+)", name):
        return int(m.group(1))
    for sid, mapped in cfg("SERVICE_NAME_MAP").items():
        if mapped == name:
            return sid
    hits = [sid for sid, info in _channels.get().items() if info["name"] == name]
    return hits[0] if len(hits) == 1 else None


def _channels_by_type() -> Dict[str, List[str]]:
    """放送種別ごとの局名 (重複除去・名前順)。局一覧の正は channel.txt。
    番組表取得が無効な sid (サブチャンネル等) は番組表フォルダに出さない。"""
    result: Dict[str, set] = {t: set() for t in BROADCAST_TYPES}
    for sid, info in _channels.get().items():
        btype = _channel_type(sid)
        if info["epg_enabled"] and btype in result:
            result[btype].add(channel_name(sid))
    return {t: sorted(names) for t, names in result.items()}


# ---------------------------------------------------------------------------
# 自動予約 (t_autoprog) のデコード — techknow.txt の定義
# ---------------------------------------------------------------------------

_GENRE_NAMES = {
    0: "ニュース/報道", 1: "スポーツ", 2: "情報/ワイドショー", 3: "ドラマ",
    4: "音楽", 5: "バラエティ", 6: "映画", 7: "アニメ/特撮",
    8: "ドキュメンタリー/教養", 9: "劇場/公演", 10: "趣味/教育", 11: "福祉",
    12: "予備", 13: "予備", 14: "拡張", 15: "その他",
}
_ZONE_NAMES = {
    -1: "すべて", 0: "午前", 1: "午後", 2: "5～11時", 3: "11～17時",
    4: "17～23時", 5: "23～5時", 6: "0～4時", 7: "4～8時", 8: "8～12時",
    9: "12～16時", 10: "16～20時", 12: "20～24時", 13: "日曜日",
    14: "月曜日", 15: "火曜日", 16: "水曜日", 17: "木曜日", 18: "金曜日",
    19: "土曜日", 20: "平日", 21: "土日",
}
_WEEKDAY_BITS = [(6, "日"), (5, "月"), (4, "火"), (3, "水"), (2, "木"), (1, "金"), (0, "土")]


def decode_genre(genre: int) -> str:
    return _GENRE_NAMES.get(genre, f"不明({genre})")


def decode_zone(zone: int) -> str:
    # 負値 (-1=すべて) は全ビット1扱いになるので、bit17 判定の前に除外する
    if zone >= 0 and zone & (1 << 17):
        days = "".join(n for bit, n in _WEEKDAY_BITS if zone & (1 << bit))
        return f"{days + '曜' if days else '曜日指定なし'} {(zone >> 12) & 31}時～{(zone >> 7) & 31}時"
    return _ZONE_NAMES.get(zone, f"不明({zone})")


def decode_autoprog_flags(flag: int) -> List[str]:
    parts = []
    if flag & 0b0001:
        parts.append("サービス指定あり")
    if flag & 0b0010:
        parts.append("タイトル以外も検索対象")
    if flag & 0b0100:
        parts.append("重複抑制あり")
    if postproc := (flag >> 3) & 0b11:
        parts.append(f"終了後処理:RECPOSTPROC{postproc}")
    return parts


# ---------------------------------------------------------------------------
# データモデル — 仮想ファイル1件 = 1オブジェクト
# ---------------------------------------------------------------------------

def safe_name(name: str) -> str:
    """ファイル名に使えない文字を置き換える。"""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "(無題)"


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _text_blocks(texts: str, items: str) -> List[Tuple[str, str]]:
    return [(label, body) for label, body in (("番組概要", texts), ("番組詳細", items)) if body]


class _Item:
    """仮想ファイルの共通部。サブクラスは file_name / fields() を持つ。
    本文は「fields」「blocks (概要・詳細)」「footer」から作り、
    get_extended_metadata (情報ダイアログ) も同じ部品を使う。"""

    file_name: str
    size_bytes = 0
    hidden = False

    @property
    def mtime(self) -> float:
        return time.time()

    def fields(self) -> List[Tuple[str, str]]:
        raise NotImplementedError

    def blocks(self) -> List[Tuple[str, str]]:
        return []

    def footer(self) -> List[Tuple[str, str]]:
        return []

    def details(self) -> List[Tuple[str, str]]:
        return self.fields() + self.blocks() + self.footer()

    def to_text(self) -> str:
        lines = [f"{k}: {v}" for k, v in self.fields()]
        for label, body in self.blocks():
            lines += ["", f"[{label}]", body]
        for k, v in self.footer():
            lines += ["", f"{k}: {v}"]
        return "\n".join(lines) + "\n"


class _Timed(_Item):
    """開始・終了時刻を持つもの (予約・番組)。サイズ = 分数 × 1MB、更新日時 = 開始時刻。"""

    start: datetime
    end: datetime

    @property
    def length_minutes(self) -> int:
        return max(0, round((self.end - self.start).total_seconds() / 60))

    @property
    def size_bytes(self) -> int:  # type: ignore[override]
        return self.length_minutes * _BYTES_PER_MINUTE

    @property
    def mtime(self) -> float:
        return self.start.timestamp()

    @property
    def _stamp(self) -> str:
        return self.start.strftime("%Y-%m-%d_%H-%M")


@dataclass
class PTTimerReservation(_Timed):
    reservation_id: str
    title: str
    channel: str
    start: datetime
    end: datetime
    status: str = ""
    note: str = ""
    disabled: bool = False       # flg1==4
    texts: str = ""
    items: str = ""

    @property
    def hidden(self) -> bool:  # type: ignore[override]
        return self.disabled

    @property
    def file_name(self) -> str:  # type: ignore[override]
        # 有効/無効は名前に含めない (トグルしてもパスが変わらないように)
        return f"{self._stamp}_{safe_name(self.title)}.txt"

    def fields(self):
        f = [("番組名", self.title), ("チャンネル", self.channel),
             ("開始", _ts(self.start)), ("終了", _ts(self.end)),
             ("録画時間", f"{self.length_minutes}分")]
        if self.status:
            f.append(("状態", self.status))
        if self.note:
            f.append(("備考", self.note))
        return f

    def blocks(self):
        return _text_blocks(self.texts, self.items)

    def footer(self):
        return [("予約ID", self.reservation_id)]


@dataclass
class PTTimerAutoReservation(_Item):
    """t_autoprog の1件。「条件に合う番組を探し続ける」検索条件なので時刻は無い。"""

    auto_id: str
    keyword: str
    sid: Optional[int]
    genre: int
    zone: int
    flag: int

    @property
    def file_name(self) -> str:  # type: ignore[override]
        kw = safe_name(self.keyword) if self.keyword.strip() else "(キーワード無し)"
        return f"{kw}_id{self.auto_id}.txt"

    @property
    def service_text(self) -> str:
        if (self.flag & 1) and self.sid is not None:
            return channel_name(self.sid)
        return "指定なし(全チャンネル)"

    def fields(self):
        f = [("キーワード", self.keyword or "(なし)"), ("対象チャンネル", self.service_text),
             ("ジャンル", decode_genre(self.genre)), ("時間帯", decode_zone(self.zone))]
        if notes := decode_autoprog_flags(self.flag):
            f.append(("フラグ", " / ".join(notes)))
        return f

    def footer(self):
        return [("検索番号", self.auto_id)]


@dataclass
class PTTimerEpgProgram(_Timed):
    sid: int
    eid: int
    start: datetime
    end: datetime
    title: str
    texts: str
    items: str
    channel_name: str
    broadcast_type: str

    @property
    def date_key(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def file_name(self) -> str:  # type: ignore[override]
        return f"{self._stamp}_{safe_name(self.title)}.txt"

    @property
    def search_file_name(self) -> str:
        """検索結果は複数局が混在するので、局名を入れて衝突を避ける。"""
        return f"{self._stamp}_{safe_name(self.channel_name)}_{safe_name(self.title)}.txt"

    def fields(self):
        return [("番組名", self.title), ("チャンネル", self.channel_name),
                ("放送種別", self.broadcast_type), ("開始", _ts(self.start)),
                ("終了", _ts(self.end)), ("放送時間", f"{self.length_minutes}分")]

    def blocks(self):
        return _text_blocks(self.texts, self.items)


# ---------------------------------------------------------------------------
# DB からの取得 (t_program / t_autoprog / t_event)
# ---------------------------------------------------------------------------

def get_reservations() -> List[PTTimerReservation]:
    """t_program (録画予約)。t_event を (sid, eid) で LEFT JOIN して概要・詳細も取る
    (時間指定予約は eid=0 で対応する t_event が無く、概要・詳細は空)。"""
    rows = _query(
        "SELECT p.id, p.sid, p.stime, p.length, p.title, p.repeat, p.flg1, p.flg2, "
        "e.texts AS event_texts, e.items AS event_items "
        "FROM t_program p LEFT JOIN t_event e ON e.sid = p.sid AND e.eid = p.eid "
        "ORDER BY p.stime ASC, p.flg3 DESC")
    now = time.time()
    result = []
    for r in rows:
        stime, length = r["stime"] or 0, r["length"] or 0
        if not cfg("INCLUDE_PAST_RESERVATIONS") and stime + length < now:
            continue
        flg2 = r["flg2"] or 0
        notes = []
        if r["repeat"]:
            notes.append("毎週記録")
        if flg2 & 0b0001:
            notes.append("サービス指定あり")
        if flg2 & 0b0010:
            notes.append("重複無効化")
        if postproc := (flg2 >> 2) & 0b11:
            notes.append(f"終了後処理:RECPOSTPROC{postproc}")
        result.append(PTTimerReservation(
            reservation_id=str(r["id"]),
            title=r["title"] or "(無題)",
            channel=channel_name(r["sid"]),
            start=datetime.fromtimestamp(stime),
            end=datetime.fromtimestamp(stime + length),
            status=_FLG1_STATUS.get(r["flg1"], f"不明({r['flg1']})"),
            note=" / ".join(notes),
            disabled=(r["flg1"] == _FLG1_DISABLED),
            texts=(r["event_texts"] or "").strip(),
            items=(r["event_items"] or "").strip(),
        ))
    return result


def get_auto_reservations() -> List[PTTimerAutoReservation]:
    rows = _query("SELECT id, sid, keyword, genre, zone, flag FROM t_autoprog ORDER BY id ASC")
    return [PTTimerAutoReservation(
        auto_id=str(r["id"]), keyword=r["keyword"] or "", sid=r["sid"],
        genre=-1 if r["genre"] is None else r["genre"],
        zone=-1 if r["zone"] is None else r["zone"],
        flag=r["flag"] or 0) for r in rows]


def get_epg_programs() -> List[PTTimerEpgProgram]:
    """t_event (番組表)。今日から EPG_DAYS_AHEAD 日分 (終了済み・遠い未来は除く)。"""
    now = int(time.time())
    rows = _query(
        "SELECT sid, eid, stime, length, title, texts, items FROM t_event "
        "WHERE (stime + length) >= ? AND stime < ? ORDER BY sid ASC, stime ASC",
        (now, now + cfg("EPG_DAYS_AHEAD") * 86400))
    result = []
    for r in rows:
        stime, length, sid = r["stime"] or 0, r["length"] or 0, r["sid"]
        result.append(PTTimerEpgProgram(
            sid=sid, eid=r["eid"],
            start=datetime.fromtimestamp(stime), end=datetime.fromtimestamp(stime + length),
            title=r["title"] or "(無題)",
            texts=(r["texts"] or "").strip(), items=(r["items"] or "").strip(),
            channel_name=channel_name(sid), broadcast_type=_channel_type(sid)))
    return result


class _Cache:
    """短寿命キャッシュ。ロック等で取得に失敗したら直前のデータを使い続ける。"""

    TTL = 5.0

    def __init__(self, fetch: Callable[[], Any], empty: Any) -> None:
        self._fetch, self._empty = fetch, empty
        self._data: Any = None
        self._at = 0.0

    def get(self) -> Any:
        now = time.monotonic()
        if self._data is None or now - self._at > self.TTL:
            try:
                self._data, self._at = self._fetch(), now
            except Exception:
                if self._data is None:
                    self._data = self._empty
        return self._data

    def invalidate(self) -> None:
        self._data = None


_reservations = _Cache(get_reservations, [])
_auto_reservations = _Cache(get_auto_reservations, [])
_epg = _Cache(get_epg_programs, [])
_epg_channels = _Cache(_channels_by_type, {})


def reset_caches() -> None:
    """DB・channel.txt・検索のキャッシュを全て捨てる。"""
    for cache in (_reservations, _auto_reservations, _epg, _epg_channels):
        cache.invalidate()
    _channels.reset()
    _search_cache.clear()


# ---------------------------------------------------------------------------
# 番組表検索 (Migemo 対応)
# ---------------------------------------------------------------------------

def _migemo_hit_func(term: str) -> Optional[Callable[[str], bool]]:
    """Migemo の公開 API が無いので常に None (通常の部分一致のみ)。"""
    return None


_search_cache: Dict[str, tuple] = {}   # keyword → (元の番組リストの参照, 結果)


def search_epg(keyword: str) -> List[PTTimerEpgProgram]:
    """空白 (半角/全角) 区切りで AND。番組名・概要・詳細のどれかに各語が部分一致 (大小無視) すればよい。"""
    terms = [t for t in re.split(r"[ \u3000]+", keyword) if t]
    if not terms:
        return []
    programs = _epg.get()
    cached = _search_cache.get(keyword)
    if cached and cached[0] is programs:
        return list(cached[1])

    hits = [_migemo_hit_func(t) for t in terms]      # Migemo には元の大小のまま渡す
    result = []
    for p in programs:
        text = f"{p.title} {p.texts} {p.items}"
        low = text.lower()
        if all(t.lower() in low or (h is not None and h(text)) for t, h in zip(terms, hits)):
            result.append(p)
    result.sort(key=lambda p: p.start)

    if len(_search_cache) >= 32:
        _search_cache.clear()
    _search_cache[keyword] = (programs, result)
    return list(result)


# ---------------------------------------------------------------------------
# 木構造 — 「このパスの中身は何か」を一箇所で決める
# ---------------------------------------------------------------------------

@dataclass
class _Node:
    """ディレクトリの子1件。item が None ならフォルダ。"""

    name: str
    item: Optional[_Item] = None

    def attrs(self, now: float) -> dict:
        if self.item is None:
            return {"is_dir": True, "is_link": False, "size": 0, "mtime": now,
                    "hidden": False, "ok": True}
        return {"is_dir": False, "is_link": False, "size": self.item.size_bytes,
                "mtime": self.item.mtime, "hidden": self.item.hidden, "ok": True}


def _dirs(names) -> List[_Node]:
    return [_Node(n) for n in names]


def _files(items, name=lambda i: i.file_name) -> List[_Node]:
    return [_Node(name(i), i) for i in items]


def _children_epg(rest: tuple) -> Optional[List[_Node]]:
    """番組表配下 (「番組表」を除いた残り) の子。存在しないフォルダは None。
      チャンネル: () / (種別) / (種別, 局)           ← 局の中が番組ファイル
      日付      : () / (日付) / (日付, 種別) / (日付, 種別, 局)
      検索      : () / (キーワード)                  ← キーワードは何でも「在る」(0件でも空フォルダ)
    """
    if not rest:
        return _dirs(EPG_VIEWS)
    view, path = rest[0], rest[1:]
    by_type = _epg_channels.get()

    def programs(btype, channel, date=None):
        return sorted((p for p in _epg.get()
                       if p.broadcast_type == btype and p.channel_name == channel
                       and (date is None or p.date_key == date)), key=lambda p: p.start)

    if view == EPG_VIEW_CHANNEL and len(path) <= 2:
        if not path:
            return _dirs(BROADCAST_TYPES)
        if path[0] not in BROADCAST_TYPES:
            return None
        if len(path) == 1:
            return _dirs(by_type.get(path[0], []))
        return _files(programs(*path)) if path[1] in by_type.get(path[0], []) else None

    if view == EPG_VIEW_DATE and len(path) <= 3:
        dates = {p.date_key for p in _epg.get()}
        if not path:
            return _dirs(sorted(dates))
        if path[0] not in dates:
            return None
        if len(path) == 1:
            return _dirs(BROADCAST_TYPES)
        if path[1] not in BROADCAST_TYPES:
            return None
        if len(path) == 2:
            return _dirs(by_type.get(path[1], []))
        return (_files(programs(path[1], path[2], path[0]))
                if path[2] in by_type.get(path[1], []) else None)

    if view == EPG_VIEW_SEARCH and len(path) <= 1:
        if not path:
            return []
        return _files(search_epg(path[0]), name=lambda p: p.search_file_name)
    return None


def _children(segments: tuple) -> Optional[List[_Node]]:
    """ディレクトリ segments の子の一覧。ディレクトリでない・存在しなければ None。"""
    if not segments:
        return _dirs(CATEGORIES)
    category, rest = segments[0], segments[1:]
    if category == CATEGORY_EPG:
        return _children_epg(rest)
    if rest:
        return None
    if category == CATEGORY_RESERVATIONS:
        return _files(sorted(_reservations.get(), key=lambda r: r.start))
    if category == CATEGORY_AUTORESERVE:
        return _files(_auto_reservations.get())
    return None


# ---------------------------------------------------------------------------
# PathImpl
# ---------------------------------------------------------------------------

class PTTimerPathImpl(ReadOnlyPathImpl):
    """pttimer:// 。パス計算は UriPathImpl、書き込み拒否は ReadOnlyPathImpl が持つ。
    ここに書くのは「中身の答え方」だけ。例外は unlink (予約の有効⇔無効トグル)。"""

    SCHEME = "pttimer"
    IS_REMOTE = False
    SEARCH_STRATEGY = "extracted"      # 本文は毎回組み立てるので、検索前に取り出す
    DISPLAY_PREFIX = "PTTIMER: "
    READ_ONLY_MESSAGE = _READ_ONLY_MSG

    def _nodes(self) -> Optional[List[_Node]]:
        return _children(tuple(self._segments))

    def _leaf(self) -> Optional[_Item]:
        segs = self._segments
        for node in (_children(tuple(segs[:-1])) or ()) if segs else ():
            if node.item is not None and node.name == segs[-1]:
                return node.item
        return None

    def reservation(self) -> Optional[PTTimerReservation]:
        """pttimer://予約/<file> ならその予約、それ以外は None。"""
        segs = self._segments
        if len(segs) == 2 and segs[0] == CATEGORY_RESERVATIONS:
            leaf = self._leaf()
            return leaf if isinstance(leaf, PTTimerReservation) else None
        return None

    # -- 5つの基本メソッド --

    def is_dir(self) -> bool:
        return self._nodes() is not None

    def exists(self) -> bool:
        return self.is_dir() or self._leaf() is not None

    def iterdir(self):
        nodes = self._nodes()
        if nodes is None:
            raise NotADirectoryError(str(self))
        for node in nodes:
            yield self.child(node.name)

    def stat(self):
        if self.is_dir():
            return UriStatResult(is_dir=True, mtime=time.time())
        leaf = self._leaf()
        if leaf is None:
            raise FileNotFoundError(str(self))
        return UriStatResult(size=leaf.size_bytes, mtime=leaf.mtime)

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if any(c in mode for c in "wax+"):
            self._refuse()
        leaf = self._leaf()
        if leaf is None:
            raise FileNotFoundError(str(self))
        text = leaf.to_text()
        if "b" in mode:
            return io.BytesIO(text.encode(encoding or "utf-8"))
        return io.StringIO(text)

    # -- 上書き --

    def listdir_attrs(self):
        """一覧表示用。名前と属性を一度に返す (無効な予約は hidden=True で出る)。"""
        nodes = self._nodes()
        if nodes is None:
            raise NotADirectoryError(str(self))
        now = time.time()
        return [(self.child(n.name), n.attrs(now)) for n in nodes]

    def unlink(self, missing_ok=False):
        """削除コマンドは、予約の有効(flg1=0)⇔無効(flg1=4)のトグルとして扱う。
        自動予約・番組表は読み取り専用のまま。"""
        segs = self._segments
        if len(segs) == 2 and segs[0] == CATEGORY_RESERVATIONS:
            entry = self.reservation()
            if entry is None:
                if missing_ok:
                    return
                raise FileNotFoundError(str(self))
            toggle_reservation_disabled(entry.reservation_id, entry.disabled)
            _reservations.invalidate()
            return
        self._refuse()

    def get_extended_metadata(self) -> Dict[str, Any]:
        leaf = None if self.is_dir() else self._leaf()
        details = leaf.details() if leaf else [("種別", "フォルダ"), ("パス", str(self))]
        return {"type": "pttimer", "details": details, "format_hint": "standard"}


# ---------------------------------------------------------------------------
# ptTimer への書き込み (予約の無効化・編集)
# ---------------------------------------------------------------------------

# techknow.txt の通知プロトコル: ptTimer のウインドウ (クラス名 "ptTimer" / "ptTimer(2)") に
# WM_APP+0x1111 を WPARAM=0x8B36AFD1 で送ると、ptTimer.db を再読込する。
_RELOAD_MSG = 0x8000 + 0x1111
_RELOAD_WPARAM = 0x8B36AFD1


def _notify_pttimer(update_autoprog: bool = False) -> None:
    """実行中の ptTimer に DB の再読込を促す。失敗しても黙って無視する (DB 更新自体は成功済み)。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        for class_name in ("ptTimer", "ptTimer(2)"):
            if hwnd := user32.FindWindowW(class_name, None):
                user32.SendMessageW(hwnd, _RELOAD_MSG, _RELOAD_WPARAM, 1 if update_autoprog else 0)
    except Exception:
        pass


def _update_program(sql: str, params: tuple, reservation_id: str) -> None:
    """t_program を UPDATE して ptTimer に通知し、キャッシュを捨てる。"""
    path = _db_path()
    if not path:
        raise OSError("ptTimer.db が見つかりません")
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        if cur.rowcount == 0:
            raise FileNotFoundError(f"予約ID {reservation_id} が見つかりません")
    finally:
        conn.close()
    _notify_pttimer()
    _reservations.invalidate()


def toggle_reservation_disabled(reservation_id: str, currently_disabled: bool) -> int:
    """flg1 を 4(無効) ⇔ 0(未処理) に切り替える。切り替え後の flg1 を返す。"""
    new = 0 if currently_disabled else _FLG1_DISABLED
    _update_program("UPDATE t_program SET flg1 = ? WHERE id = ?", (new, reservation_id), reservation_id)
    return new


_EDIT_HEADER = (
    "# 編集可能な項目: 番組名 / チャンネル / 開始 / 終了\n"
    '# チャンネル欄は channel.txt 上の表示名、または "SID:12345" の形式で指定できます\n'
    "# (同名のチャンネルが複数あって特定できない場合は SID:12345 形式を使ってください)\n"
    "# 予約ID の行は変更・削除しないでください\n"
    "# 状態 / 備考 / 番組概要 / 番組詳細 は表示のみで、編集しても保存されません\n"
    "# この # で始まる行は保存時に無視されます\n"
    "#" + "-" * 60 + "\n"
)

_EDIT_LABELS = {
    "id": re.compile(r"^予約ID:\s*(\d+)\s*$"),
    "title": re.compile(r"^番組名:\s*(.*)$"),
    "channel": re.compile(r"^チャンネル:\s*(.*)$"),
    "start": re.compile(r"^開始:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*$"),
    "end": re.compile(r"^終了:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*$"),
}


class PTTimerEditError(Exception):
    """編集内容の検証エラー (メッセージはユーザー向け)。"""


def parse_edited_text(text: str) -> Dict[str, Any]:
    found: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        for key, pattern in _EDIT_LABELS.items():
            if key not in found and (m := pattern.match(line)):
                found[key] = m.group(1)
                break

    if missing := [k for k in _EDIT_LABELS if k not in found]:
        raise PTTimerEditError(f"必要な項目が見つかりません: {', '.join(missing)}")
    try:
        start = datetime.strptime(found["start"], "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(found["end"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise PTTimerEditError("開始/終了の日時形式が正しくありません(YYYY-MM-DD HH:MM:SS)")
    if end <= start:
        raise PTTimerEditError("終了時刻が開始時刻より前(または同じ)です")
    title = found["title"].strip()
    if not title:
        raise PTTimerEditError("番組名を空にすることはできません")
    channel = found["channel"].strip()
    sid = reverse_channel_lookup(channel)
    if sid is None:
        raise PTTimerEditError(f"チャンネル名 '{channel}' を特定できません。\"SID:12345\" の形式で指定してください")
    return {"id": found["id"], "title": title, "sid": sid, "start": start, "end": end}


def _run_editor(ctx, tmp_path: str) -> None:
    """エディタを起動して閉じるまで待つ。TUI は端末を貸す (ctx.run_program)。
    デスクトップ版は run_program が待たないため、直接起動して待つ。"""
    cmd = list(cfg("EDITOR_COMMAND")) + [tmp_path]
    try:
        desktop = is_desktop_mode()
    except Exception:
        desktop = False
    if ctx is not None and not desktop:
        ctx.run_program(cmd, terminal=True)
    else:
        subprocess.run(cmd, check=False)


def reservation_for_path(path) -> Optional[PTTimerReservation]:
    """pttimer://予約/<file> を表す path (または文字列) ならその予約、それ以外は None。"""
    text = str(path)
    prefix = f"{PTTimerPathImpl.SCHEME}://"
    if not text.startswith(prefix):
        return None
    return PTTimerPathImpl(text).reservation()


def edit_reservation(path, ctx=None) -> None:
    """pttimer://予約/... を外部エディタで開き、保存内容を ptTimer.db に書き戻す。"""
    def say(msg: str) -> None:
        if ctx is not None:
            ctx.message(msg)

    entry = reservation_for_path(path)
    if entry is None:
        say("これは編集可能な pttimer 予約ファイルではありません")
        return

    original = _EDIT_HEADER + entry.to_text()
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="pttimer_edit_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(original)
        try:
            _run_editor(ctx, tmp)
        except OSError as e:
            say(f"エディタを起動できませんでした: {e}")
            return
        with open(tmp, encoding="utf-8") as f:
            edited = f.read()
        if edited == original:
            say("変更はありませんでした")
            return
        try:
            c = parse_edited_text(edited)
        except PTTimerEditError as e:
            say(f"保存を中止しました: {e}")
            return
        _update_program(
            "UPDATE t_program SET title=?, sid=?, stime=?, length=? WHERE id=?",
            (c["title"], c["sid"], int(c["start"].timestamp()),
             int((c["end"] - c["start"]).total_seconds()), c["id"]), c["id"])
        say(f"予約(ID {c['id']})を更新しました")
        if ctx is not None:
            ctx.pane.refresh()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# XeFM アクション
# ---------------------------------------------------------------------------

def pttimer_edit(ctx):
    entry = ctx.pane.focused
    if entry is None:
        ctx.message("カーソル位置に対象がありません")
        return
    edit_reservation(entry.path, ctx)


def pttimer_epg_search(ctx):
    def on_keyword(keyword):
        keyword = keyword.replace("/", " ").strip()      # "/" はパス区切りになってしまう
        if keyword:
            ctx.pane.cd(f"pttimer://番組表/検索/{keyword}/")
    ctx.input("番組表を検索:", on_accept=on_keyword)


PTTIMER_SCHEME = {"class": PTTimerPathImpl, "override": True}


