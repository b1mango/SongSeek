import io
from datetime import time

import pytest
from openpyxl import Workbook

from songseek.errors import SongSeekError
from songseek.importers import parse_duration, parse_file, parse_text
from songseek.providers.netease import NeteaseProvider


@pytest.mark.parametrize("value, expected", [("3:45", 225), (225, 225), (225.3, 225.3), ("00:03:45", 225), (225300, 225.3), ("10001", 10.001), (10000, 10000), (None, None), ("", None), (time(0, 3, 45), 225)])
def test_duration(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["3:75", -1, "NaN", "inf", "1:2:3:4", "invalid", 0])
def test_invalid_duration(value):
    with pytest.raises(SongSeekError):
        parse_duration(value)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk"])
def test_csv(encoding):
    result = parse_file("歌曲,演唱,专辑,时长\n晴天,周杰伦,叶惠美,3:45\n,歌手,,\n后来,刘若英,,错\n".encode(encoding), "歌单.csv")
    assert result.source == "csv"
    assert result.mapping.title == "歌曲"
    assert result.rows[0].duration == 225
    assert len(result.rows) == 2
    assert len(result.warnings) == 2


def test_xlsx_mapping():
    book = Workbook()
    sheet = book.active
    sheet.append(["曲目字段", "人", "Album", "长度"])
    sheet.append(["晴天", "周杰伦", "叶惠美", "00:03:45"])
    stream = io.BytesIO()
    book.save(stream)
    result = parse_file(stream.getvalue(), "歌单.xlsx", {"title": "曲目字段", "artist": "人", "duration": "长度"})
    assert result.rows[0].model_dump() == {"title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration": 225}
    assert result.columns == ["曲目字段", "人", "Album", "长度"]


def test_markdown_table_selection():
    text = """| title | artist |
| --- | --- |
| A | B |

| # | 歌名 | 歌手 | 时长 |
| --- | --- | --- | --- |
| 1 | 晴天 | 周杰伦 | 225300 |
"""
    result = parse_file(text.encode(), "歌单.md")
    assert result.rows[0].title == "晴天"
    assert result.rows[0].duration == 225.3
    assert parse_file(text.encode(), "歌单.md", {"table": 0}).rows[0].title == "A"


def test_text_formats_and_ambiguity():
    result = parse_text("1. 晴天 - 周杰伦\n- 后来 / 刘若英\n3、 平凡之路\n* 歌手 - 歌名\n4) hello")
    assert [(r.title, r.artist) for r in result.rows] == [("晴天", "周杰伦"), ("后来", "刘若英"), ("平凡之路", ""), ("歌手", "歌名"), ("hello", "")]
    assert len(result.warnings) == 1 and "2 行" in result.warnings[0]


def test_text_tab_separated_paste():
    with_header = parse_text("歌名\t歌手\t时长\n晴天\t周杰伦\t4:29\n")
    assert (with_header.rows[0].artist, with_header.rows[0].duration) == ("周杰伦", 269)
    bare = parse_text("晴天\t周杰伦\t叶惠美\t4:29\n后来\t刘若英\t我等你\t5:41\n")
    assert [(r.title, r.artist, r.duration) for r in bare.rows] == [("晴天", "周杰伦", 269), ("后来", "刘若英", 341)]
    assert bare.mapping.duration == "第4列" and bare.warnings
    swapped = parse_text("周杰伦\t晴天\n", mapping={"title": "第2列", "artist": "第1列"})
    assert (swapped.rows[0].title, swapped.rows[0].artist) == ("晴天", "周杰伦")


@pytest.mark.parametrize("url", ["https://music.163.com/#/playlist?id=42", "https://music.163.com/playlist?id=42", "https://y.music.163.com/m/playlist?id=42"])
def test_netease_playlist_fetches_missing(config, monkeypatch, url):
    provider = NeteaseProvider(config)
    song = lambda identifier: {"id": identifier, "name": f"歌{identifier}", "ar": [{"name": "歌手"}], "al": {"name": "专辑"}, "dt": 225300}
    batches = []
    def call(path, **values):
        if "playlist/detail" in path:
            return {"code": 200, "playlist": {"name": "测试", "trackIds": [{"id": i} for i in range(1, 404)], "tracks": [song(1)]}}
        import json
        batch = json.loads(values["ids"])
        batches.append(batch)
        return {"code": 200, "songs": [song(i) for i in batch]}
    monkeypatch.setattr(provider, "call", call)
    result = provider.playlist(url)
    assert len(result.rows) == 403
    assert [len(b) for b in batches] == [200, 200, 2]
    assert result.rows[-1].title == "歌403"


def test_import_validation():
    with pytest.raises(SongSeekError, match="列映射"):
        parse_file(b"title,artist\na,b", "a.csv", {"title": "missing"})
    with pytest.raises(SongSeekError, match="列名重复"):
        parse_file(b"title,title\na,b", "a.csv")


def test_mapping_ignores_unrelated_markdown_tables():
    text = "| other | value |\n| --- | --- |\n| x | y |\n\n| song_column | person |\n| --- | --- |\n| 晴天 | 周杰伦 |"
    result = parse_file(text.encode(), "a.md", {"title": "song_column", "artist": "person"})
    assert result.rows[0].title == "晴天"


def test_unrecognized_columns_can_be_remapped():
    result = parse_file(b"custom,person\nA,B", "a.csv")
    assert result.columns == ["custom", "person"] and result.rows == [] and result.warnings
    assert parse_file(b"custom,person\nA,B", "a.csv", {"title": "custom", "artist": "person"}).rows[0].artist == "B"


def test_nested_list_prefix_and_slash_without_spaces():
    row = parse_text("- 1. 晴天/周杰伦").rows[0]
    assert (row.title, row.artist) == ("晴天", "周杰伦")
