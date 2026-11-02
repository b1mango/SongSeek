import pytest

from songseek.matching import evaluate, sort_candidates
from songseek.models import RawCandidate, Track, now
from songseek.normalize import artist_set, version_flags


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
