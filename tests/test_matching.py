import pytest

from songseek.matching import evaluate, evaluate_candidates, same_recording, sort_candidates
from songseek.models import RawCandidate, Track, now
from songseek.normalize import artist_set, artists_match, version_flags


def track(title="晴天", artist="周杰伦", duration=180):
    return Track(id=1, playlist_id=1, position=1, title=title, artist=artist, duration=duration, flags=version_flags(title), status="pending", updated_at=now())


def candidate(**changes):
    return RawCandidate(**({"provider": "mock", "source_id": "a", "title": "晴天", "artist": "周杰伦", "duration": 180, "bitrate": 128} | changes))


@pytest.mark.parametrize("expected, actual, verdict", [(180, 183, "match"), (180, 183.01, "review"), (180, 190, "review"), (180, 190.01, "reject"), (60, 62, "match"), (60, 62.01, "review"), (60.1, 63.1, "match"), (30, 19.9, "reject"), (30, 28, "match")])
def test_duration_boundaries(expected, actual, verdict):
    assert evaluate(track(duration=expected), candidate(duration=actual), "演示").verdict == verdict


@pytest.mark.parametrize("wanted, found, verdict", [("晴天", "晴天 Live", "reject"), ("晴天 Live", "晴天", "review"), ("晴天 Live", "晴天 (现场)", "match"), ("晴天 (伴奏)", "晴天 Off Vocal", "match"), ("晴天 DJ版", "晴天 Remix", "reject"), ("晴天", "晴天 (0.8x)", "reject"), ("晴天", "晴天 (翻唱)", "reject"), ("晴天", "晴天 试听", "reject")])
def test_versions_both_directions(wanted, found, verdict):
    assert evaluate(track(title=wanted), candidate(title=found), "演示").verdict == verdict


@pytest.mark.parametrize("label, flag", [("Live", "live"), ("演唱會", "live"), ("Inst.", "instrumental"), ("karaoke", "instrumental"), ("covered", "cover"), ("sped up", "speed"), ("降調", "speed"), ("8D", "speed"), ("1 hour loop", "speed"), ("preview", "clip"), ("片段", "clip"), ("DJ版", "dj"), ("混音", "remix")])
def test_version_dictionary(label, flag):
    assert flag in version_flags(f"晴天 ({label})")


def test_bracketed_versions_and_no_space_english_flags():
    assert version_flags("晴天Live版") == ["live"]
    assert evaluate(track(title="晴天 (DJ版)"), candidate(title="晴天 (DJ)"), "演示").verdict == "match"
    assert evaluate(track(title="晴天 (抖音DJ版)"), candidate(title="晴天 (DJ)"), "演示").verdict == "match"


def test_artist_splitting_and_traditional():
    assert artist_set("甲 & 乙, 丙、丁/戊 feat. 己 × 庚;辛") == set("甲乙丙丁戊己庚辛")
    result = evaluate(track(title="後來", artist="劉若英 & 伍佰"), candidate(title="后来", artist="刘若英"), "演示")
    assert result.verdict == "match"


@pytest.mark.parametrize("short, full", [("邓紫棋", "G.E.M.邓紫棋"), ("周杰伦", "Jay Chou 周杰倫"),
                                       ("邓紫棋", "邓紫棋 (G.E.M.)"), ("陈奕迅", "陈奕迅 Eason Chan"), ("GEM", "G.E.M.邓紫棋")])
def test_artist_aliases(short, full):
    assert artists_match(short, full) and artists_match(full, short)
    assert evaluate(track(artist=short), candidate(artist=full), "演示").verdict == "match"


@pytest.mark.parametrize("left, right", [("林", "林俊杰"), ("Jo", "John"), ("邓紫棋", "冯提莫"), ("", "周杰伦")])
def test_artist_aliases_do_not_match_short_or_other_names(left, right):
    assert not artists_match(left, right)
    assert not artists_match(right, left)


@pytest.mark.parametrize("duration, other, provider, artist, title, confirmed", [
    (234, 237, "other", "周杰伦", "晴天", True),
    (234, 237.01, "other", "周杰伦", "晴天", False),
    (60, 62, "other", "周杰伦", "晴天", True),
    (60, 62.01, "other", "周杰伦", "晴天", False),
    (234, 234, "mock", "周杰伦", "晴天", False),
    (234, None, "other", "周杰伦", "晴天", False),
    (234, 234, "other", "其他歌手", "晴天", False),
    (234, 234, "other", "周杰伦", "晴天 Live", False),
    (234, 234, "other", "周杰伦", "其他歌名", False),
])
def test_duration_consensus(duration, other, provider, artist, title, confirmed):
    raws = [candidate(duration=duration), candidate(source_id="b", provider=provider, duration=other, artist=artist, title=title)]
    results = evaluate_candidates(track(duration=None), raws, {"mock": "演示", "other": "其他"})
    assert (results[0].verdict == "match") == confirmed
    assert (results[0].checks[2].ok is True) == confirmed
    if confirmed:
        assert results[0].checks[2].label.startswith("多个音源时长一致 ")


def test_consensus_preserves_explicit_duration_and_requires_artist_and_version():
    raws = [candidate(duration=234), candidate(provider="other", duration=234)]
    labels = {"mock": "演示", "other": "其他"}
    for target in [track(duration=180), track(artist="", duration=None), track(title="晴天 Live", duration=None)]:
        assert all(c.verdict != "match" for c in evaluate_candidates(target, raws, labels))


@pytest.mark.parametrize("changes, allowed", [
    ({"duration": 183}, True), ({"duration": 183.01}, False), ({"duration": None}, True),
    ({"artist": "Jay Chou 周杰伦"}, True), ({"artist": "其他"}, False),
    ({"title": "晴天 Live"}, False), ({"title": "其他"}, False),
])
def test_same_recording_uses_chosen_duration(changes, allowed):
    target = track(duration=None)
    chosen = evaluate(target, candidate(), "演示")
    other = evaluate(target, candidate(**changes), "演示")
    assert same_recording(chosen, other) == allowed


def test_same_recording_follows_selected_artist_and_version():
    target = track()
    chosen = evaluate(target, candidate(title="晴天 (Live)", artist="另一歌手", duration=200), "演示")
    alternate = evaluate(target, candidate(title="晴天 现场版", artist="另一歌手", duration=202), "其他")
    original = evaluate(target, candidate(), "演示")
    assert same_recording(chosen, alternate)
    assert not same_recording(chosen, original)


@pytest.mark.parametrize("artist, duration", [("", 180), ("周杰伦", None), ("", None)])
def test_unknown_fields_need_review(artist, duration):
    result = evaluate(track(artist=artist, duration=duration), candidate(), "演示")
    assert result.verdict == "review"
    assert any(check.ok is None for check in result.checks[:3])


def test_score_bands_and_ranking():
    target = track()
    raws = [candidate(source_id="a", bitrate=320, popularity=.1), candidate(provider="other", source_id="b", bitrate=128, popularity=1),
            candidate(source_id="c", bitrate=320, popularity=.8), candidate(provider="other", source_id="d", bitrate=320, popularity=.8),
            candidate(source_id="e", artist="其他", bitrate=9999), candidate(source_id="f", title="晴天 Live", bitrate=9999)]
    results = [evaluate(target, c, c.provider) for c in raws]
    assert min(c.score for c in results if c.verdict == "match") > max(c.score for c in results if c.verdict == "review")
    assert min(c.score for c in results if c.verdict == "review") > max(c.score for c in results if c.verdict == "reject")
    ranked = sort_candidates(results, {"mock": 0, "other": 1})
    assert [c.source_id for c in ranked] == ["c", "d", "a", "b", "e", "f"]


def test_title_similarity_threshold_and_annotation():
    assert evaluate(track(title="abcdefghij"), candidate(title="abcdefghik"), "演示").verdict == "match"
    assert evaluate(track(title="abcdefghij"), candidate(title="abcdefghxx"), "演示").verdict == "reject"
    assert evaluate(track(title="晴天 (原词)"), candidate(), "演示").verdict == "reject"


@pytest.mark.parametrize("annotation", [
    ' (电影《熊出没之雪岭熊风》插曲)', ' (正式版)', ' (鬼怪OST)',
    '（电视剧《某剧》主题曲）', '【动画片尾曲】', ' [游戏原声]',
    ' - 电影《某片》插曲', ' - 《Original Soundtrack》',
    ' (完整版)', ' (综艺推广曲)', ' (宣传曲)', ' (片头曲)',
    ' (Original Soundtrack)', ' (from "Some Film")',
])
def test_descriptive_title_annotations(annotation):
    from songseek.normalize import title_key
    title = "你从未离去" + annotation
    assert title_key(title) == title_key("你从未离去")
    assert evaluate(track(title=title), candidate(title="你从未离去"), "演示").verdict == "match"


@pytest.mark.parametrize("annotation", ["原词", "女声版", "男声版", "第二章", "Lost", "Ghost", "电影女声版"])
def test_unknown_and_voice_annotations_are_not_stripped(annotation):
    from songseek.normalize import title_key
    assert title_key(f"晴天 ({annotation})") != title_key("晴天")
    assert evaluate(track(), candidate(title=f"晴天 ({annotation})"), "演示").verdict == "reject"


@pytest.mark.parametrize("annotation, flag", [("Live", "live"), ("伴奏", "instrumental"), ("DJ", "dj"), ("Remix", "remix"), ("翻唱", "cover"), ("纯音乐", "instrumental"), ("电影插曲 Live", "live")])
def test_annotations_preserve_version_rejection(annotation, flag):
    result = evaluate(track(), candidate(title=f"晴天 ({annotation})"), "演示")
    assert flag in result.flags and result.verdict == "reject"


def test_review_tie_prefers_known_duration_before_quality_and_provider():
    known = evaluate(track(duration=None), candidate(provider="other", bitrate=128), "其他")
    unknown = evaluate(track(duration=None), candidate(duration=None, bitrate=320), "演示")
    assert known.verdict == unknown.verdict == "review" and known.score == unknown.score
    assert sort_candidates([unknown, known], {"mock": 0, "other": 1}) == [known, unknown]


@pytest.mark.parametrize("wanted, artist, found, singer", [
    ("そばにいるね (feat. SoulJa)", "青山テルマ feat. SoulJa", "そばにいるね", "青山テルマ"),
    ("Dusk Till Dawn (feat. Sia)", "ZAYN", "Dusk Til Dawn (Lyrics) ft. Sia", "Zayn"),
    ("你从未离去 (电影《熊出没》插曲)", "白挺", "你从未离去 (最终版)", "白挺"),
    ("我的楼兰", "云朵", "我的楼兰 百万豪装录音棚大声听【Hi-res】", "云朵"),
    ("そばにいるね", "青山テルマ", "そばにいるね 「Bass Tab 贝斯谱」", "青山テルマ"),
])
def test_real_title_noise(wanted, artist, found, singer):
    for left, right in [(wanted, found), (found, wanted)]:
        assert evaluate(track(title=left, artist=artist), candidate(title=right, artist=singer), "演示").verdict == "match"


def test_cover_credit_removed_but_missing_cover_still_needs_review():
    result = evaluate(track(title="Stay With Me(Cover 灿烈&PUNCH)(翻自 灿烈)", artist="XIANG"),
                      candidate(title="Stay With Me", artist="Xiang"), "演示")
    assert result.checks[0].label == "歌名一致"
    assert result.checks[1].ok is True
    assert result.verdict == "review" and result.checks[3].label == "缺少翻唱标记"


@pytest.mark.parametrize("noise", ["(Lyrics)", "(Lyric Video)", "(Official Video)", "(Official Audio)",
                                   "(Official MV)", "MV", "歌词版", "动态歌词", "高音质", "Hi-res", "无损", "4K", "最终版"])
def test_video_noise_keeps_real_versions(noise):
    from songseek.normalize import title_key
    assert title_key("晴天 " + noise) == title_key("晴天")
    assert evaluate(track(), candidate(title="晴天 Live " + noise), "演示").verdict == "reject"


def test_member_flag_survives_duration_consensus():
    raws = [candidate(provider="netease", extra={"fee": 4}), candidate(provider="other", extra={"fee": 1})]
    labels = {"netease": "网易云", "other": "其他"}
    results = evaluate_candidates(track(duration=None), raws, labels)
    assert [c.needs_member for c in results] == [True, False]
    assert all(c.verdict == "match" for c in results)
    authenticated = evaluate_candidates(track(duration=None), raws, labels, netease_cookie_set=True)
    assert all(not c.needs_member for c in authenticated)
    assert [c.score for c in results] == [c.score for c in authenticated]


def test_member_only_ranks_after_playable_equivalent():
    t = track(title="白鸽", artist="伍佰", duration=366)
    member = candidate(provider="netease", source_id="1", title="白鸽", artist="伍佰", duration=366, bitrate=320, popularity=1, extra={"fee": 1})
    free = candidate(provider="vkeys_qq", source_id="2", title="白鸽", artist="伍佰", duration=366, bitrate=320)
    ranked = sort_candidates([evaluate(t, member, "网易云"), evaluate(t, free, "QQ")], {"netease": 0, "vkeys_qq": 1})
    assert [c.provider for c in ranked] == ["vkeys_qq", "netease"] and ranked[1].needs_member
