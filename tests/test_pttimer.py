import sqlite3
import time

import pytest

from pttimer import logic


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ptTimer.db"
    conn = sqlite3.connect(path)
    now = int(time.time())
    conn.executescript(
        """
        CREATE TABLE t_program (id INTEGER, sid INTEGER, eid INTEGER, stime INTEGER, length INTEGER,
            title TEXT, repeat INTEGER, flg1 INTEGER, flg2 INTEGER, flg3 INTEGER);
        CREATE TABLE t_event (sid INTEGER, eid INTEGER, stime INTEGER, length INTEGER,
            title TEXT, texts TEXT, items TEXT);
        CREATE TABLE t_autoprog (id INTEGER, sid INTEGER, keyword TEXT, genre INTEGER, zone INTEGER, flag INTEGER);
        """
    )
    conn.execute("INSERT INTO t_program VALUES (1, 100, 5, ?, 1800, 'News: Late', 0, 0, 0, 0)", (now + 3600,))
    conn.execute("INSERT INTO t_program VALUES (2, 100, 0, ?, 600, 'Off', 1, 4, 0, 0)", (now + 7200,))
    conn.execute("INSERT INTO t_event VALUES (100, 5, ?, 1800, 'News: Late', 'summary text', 'detail')", (now + 3600,))
    conn.execute("INSERT INTO t_event VALUES (100, 6, ?, 3600, 'Baseball', 'yakyu game', '')", (now + 100,))
    conn.execute("INSERT INTO t_autoprog VALUES (9, 100, 'drama', 3, -1, 1)")
    conn.commit()
    conn.close()
    channel = tmp_path / "channel.txt"
    channel.write_text("x,7FE0,0,0,0,64,Test TV\n", encoding="utf-8")
    logic.OVERRIDES.update(PTTIMER_DB_PATH=str(path), CHANNEL_TXT_PATH=str(channel))
    logic.reset_caches()
    yield path
    logic.OVERRIDES.clear()


def test_settings_defaults():
    from pttimer.settings import Settings

    assert Settings.PTTIMER_DB_PATH == r"D:\DTV\ptTimer\ptTimer.db"
    assert Settings.CHANNEL_TXT_PATH == r"D:\DTV\ptTimer\channel.txt"
    assert logic.cfg("EPG_DAYS_AHEAD") == 7


def test_classify_and_decode():
    assert logic.classify_broadcast_type(0x7FE0) == "地デジ"
    assert logic.classify_broadcast_type(0x4010) == "BS"
    assert logic.classify_broadcast_type(0x6020) == "CS"
    assert logic.decode_zone(-1) == "すべて"
    assert logic.decode_genre(3) == "ドラマ"
    assert logic.decode_autoprog_flags(0b0101) == ["サービス指定あり", "重複抑制あり"]
    assert logic.safe_name('a/b:c') == "a_b_c"


def test_parse_edited_text_roundtrip_and_errors(db):
    text = "予約ID: 7\n番組名: X\nチャンネル: SID:100\n開始: 2030-01-01 10:00:00\n終了: 2030-01-01 11:00:00\n"
    c = logic.parse_edited_text("# comment\n" + text)
    assert (c["id"], c["sid"], c["title"]) == ("7", 100, "X")
    with pytest.raises(logic.PTTimerEditError):
        logic.parse_edited_text(text.replace("11:00", "09:00"))
    with pytest.raises(logic.PTTimerEditError):
        logic.parse_edited_text("番組名: X\n")


def test_channel_lookup(db):
    assert logic.channel_name(100) == "Test TV"
    assert logic.channel_name(5) == "SID:5"
    assert logic.reverse_channel_lookup("Test TV") == 100


def test_reservations_and_tree(db):
    res = logic.get_reservations()
    assert [r.reservation_id for r in res] == ["1", "2"]
    assert res[1].disabled and res[1].hidden
    assert res[0].texts == "summary text"
    root = logic.PTTimerPathImpl("pttimer://")
    assert sorted(p.name for p in root.iterdir()) == sorted(logic.CATEGORIES)
    folder = logic.PTTimerPathImpl("pttimer://予約")
    names = [p.name for p in folder.iterdir()]
    assert len(names) == 2 and all(n.endswith(".txt") for n in names)
    leaf = logic.PTTimerPathImpl("pttimer://予約/" + names[0])
    assert "番組名: News_ Late" not in leaf.open().read()
    assert "予約ID: 1" in leaf.open().read()
    assert logic.reservation_for_path("pttimer://予約/" + names[0]).reservation_id == "1"
    assert logic.reservation_for_path("C:\\x.txt") is None


def test_toggle_disabled(db):
    assert logic.toggle_reservation_disabled("1", False) == 4
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT flg1 FROM t_program WHERE id=1").fetchone()[0] == 4
    conn.close()


def test_auto_reservations(db):
    (auto,) = logic.get_auto_reservations()
    assert auto.file_name == "drama_id9.txt"
    assert auto.service_text == "Test TV"
    assert "ジャンル: ドラマ" in auto.to_text()


def test_epg_search_and_tree(db):
    assert [p.title for p in logic.search_epg("yakyu")] == ["Baseball"]
    assert logic.search_epg("zzz") == []
    assert {p.title for p in logic.search_epg("summary detail")} == {"News: Late"}
    node = logic.PTTimerPathImpl("pttimer://番組表/検索/yakyu")
    assert node.is_dir() and len(list(node.iterdir())) == 1
    ch = logic.PTTimerPathImpl("pttimer://番組表/チャンネル/地デジ/Test TV")
    assert len(list(ch.iterdir())) == 2
