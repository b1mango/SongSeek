from rapidfuzz.fuzz import ratio

from .models import Candidate, CheckItem, Quality, RawCandidate, Track
from .normalize import FLAG_LABELS, artists_match, title_key, tolerance, version_flags


def evaluate(track: Track, raw: RawCandidate, label: str, *, duration_confirmed: bool = False, netease_cookie_set: bool = False) -> Candidate:
    expected, actual = title_key(track.title), title_key(raw.title)
    similarity = ratio(expected, actual) if expected and actual else 0
    title_ok = similarity >= 90
    artist_ok = artists_match(track.artist, raw.artist) if track.artist else None
    delta = raw.duration - track.duration if raw.duration is not None and track.duration is not None else None
    duration_ok = abs(delta) <= tolerance(track.duration) if delta is not None else None
    duration_label = "缺少时长信息" if delta is None else f"时长差 {delta:+.1f}s"
    if duration_confirmed:
        duration_ok = True
        minutes, seconds = divmod(round(raw.duration), 60)
        duration_label = f"多个音源时长一致 {minutes}:{seconds:02d}"
    flags = version_flags(raw.title + " " + raw.extra.get("version_evidence", ""))
    extra = set(flags) - set(track.flags)
    missing = set(track.flags) - set(flags)
    version_ok = not extra and not missing
    version_label = "版本一致"
    if extra:
        version_label = "多了" + "、".join(FLAG_LABELS[f] for f in flags if f in extra) + "标记"
    elif missing:
        version_label = "缺少" + "、".join(FLAG_LABELS[f] for f in track.flags if f in missing) + "标记"
    checks = [
        CheckItem(key="title", ok=title_ok, label="歌名一致" if similarity == 100 else f"歌名相似度 {similarity:.0f}%"),
        CheckItem(key="artist", ok=artist_ok, label="清单未填歌手" if artist_ok is None else "歌手一致" if artist_ok else "歌手不同"),
        CheckItem(key="duration", ok=duration_ok, label=duration_label),
        CheckItem(key="version", ok=version_ok, label=version_label),
        CheckItem(key="quality", ok=True if raw.bitrate else None, label=f"{raw.bitrate}k {raw.format.upper()}" if raw.bitrate else "音质待确认"),
        CheckItem(key="popularity", ok=True if raw.popularity is not None else None, label=f"热度 {raw.popularity:.0%}" if raw.popularity is not None else "热度未知"),
    ]
    if not title_ok or extra or (delta is not None and abs(delta) > 10):
        verdict = "reject"
    elif artist_ok is not True or duration_ok is not True or missing:
        verdict = "review"
    else:
        verdict = "match"
    # 判定区间互不重叠；同类候选的最终排序另按音质、热度、音源顺序决定。
    detail = .45 * similarity / 100 + .25 * (artist_ok is True) + .2 * (duration_ok is True) + .1 * version_ok
    score = {"match": 90, "review": 50, "reject": 0}[verdict] + {"match": 9, "review": 39, "reject": 49}[verdict] * detail
    return Candidate(id=0, track_id=track.id, provider=raw.provider, provider_label=label,
                     source_id=raw.source_id, title=raw.title, artist=raw.artist, album=raw.album,
                     duration=raw.duration, quality=Quality(format=raw.format, bitrate=raw.bitrate,
                     label=f"{raw.bitrate}k" if raw.bitrate else "无损" if raw.format == "flac" else "未知音质"),
                     popularity=raw.popularity, score=round(score, 3), verdict=verdict, checks=checks,
                     flags=flags, preview_url="", web_url=raw.web_url, rank=0,
                     needs_member=raw.provider == "netease" and raw.extra.get("fee") in (1, 4) and not netease_cookie_set)


def evaluate_candidates(track: Track, raws: list[RawCandidate], labels: dict[str, str], *, netease_cookie_set: bool = False) -> list[Candidate]:
    candidates = [evaluate(track, raw, labels[raw.provider], netease_cookie_set=netease_cookie_set) for raw in raws]
    if track.duration is None:
        eligible = [c for c in candidates if c.duration is not None and all(c.checks[i].ok is True for i in (0, 1, 3))]
        for i, candidate in enumerate(candidates):
            if candidate in eligible and any(
                    other.provider != candidate.provider and title_key(other.title) == title_key(candidate.title)
                    and abs(other.duration - candidate.duration) <= tolerance(min(other.duration, candidate.duration))
                    for other in eligible):
                candidates[i] = evaluate(track, raws[i], labels[candidate.provider], duration_confirmed=True, netease_cookie_set=netease_cookie_set)
    return candidates


def same_recording(chosen: Candidate, candidate: Candidate) -> bool:
    return (title_key(candidate.title) == title_key(chosen.title)
            and artists_match(chosen.artist, candidate.artist)
            and set(candidate.flags) == set(chosen.flags)
            and (chosen.duration is None or candidate.duration is None
                 or abs(candidate.duration - chosen.duration) <= tolerance(chosen.duration)))


def sort_candidates(candidates: list[Candidate], orders: dict[str, int]) -> list[Candidate]:
    ranked = sorted(candidates, key=lambda c: (
        {"match": 0, "review": 1, "reject": 2}[c.verdict],
        -c.score if c.verdict != "match" else 0,
        c.duration is None if c.verdict == "review" else False,
        c.needs_member,
        -(10000 if c.quality.format in ("flac", "wav", "alac") else c.quality.bitrate or 0),
        -(c.popularity or 0), orders[c.provider], -c.score, c.source_id,
    ))
    for rank, candidate in enumerate(ranked, 1):
        candidate.rank = rank
    return ranked
