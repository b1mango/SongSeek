import re
from collections.abc import Sequence

from .models import RawCandidate
from .normalize import artists_match, key, title_key

TRUSTED = {"netease", "kugou", "vkeys_qq"}


def parse_query(query: str, candidates: Sequence[RawCandidate] = ()) -> tuple[str, str]:
    query = query.strip()
    explicit = re.split(r"\s+[-–—]\s+|\s+/\s+", query, maxsplit=1)
    splits = [tuple(explicit)] if len(explicit) == 2 else [
        (query[:split.start()], query[split.end():]) for split in re.finditer(r"\s+", query)]
    votes = {}
    for left, right in splits:
        for title, artist in ((left.strip(), right.strip()), (right.strip(), left.strip())):
            agreement = {c.provider for c in candidates if c.provider in TRUSTED
                         and title_key(title) == title_key(c.title) and artists_match(artist, c.artist)}
            if agreement:
                votes[(title, artist)] = (len(agreement), -next(i for i, c in enumerate(candidates) if c.provider in agreement
                                                                          and title_key(title) == title_key(c.title)
                                                                          and artists_match(artist, c.artist)))
    if votes:
        return max(votes, key=votes.get)
    return (explicit[0].strip(), explicit[1].strip()) if len(explicit) == 2 else (query, "")


def detect_artist(query: str, candidates: Sequence[RawCandidate], lookups: list[dict]) -> dict | None:
    top = []
    for provider in sorted(TRUSTED):
        top.extend([c for c in candidates if c.provider == provider][:5])
    if any(title_key(c.title) == title_key(query) for c in top):
        return None
    for artist in lookups:
        if key(query) in {key(name) for name in [artist["name"], *artist["aliases"]]}:
            return artist
    matching = [c for c in top if key(query) == key(c.artist)]
    if top and len(matching) > len(top) / 2:
        return {"name": matching[0].artist, "aliases": [], "cover_url": None}
    return None
