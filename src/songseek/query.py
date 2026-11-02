import re
from collections.abc import Sequence

from .models import RawCandidate
from .normalize import artists_match, title_key


def parse_query(query: str, candidates: Sequence[RawCandidate] = ()) -> tuple[str, str]:
    query = query.strip()
    parts = re.split(r"\s+[-–—]\s+|\s+/\s+", query, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    for candidate in candidates:
        for split in re.finditer(r"\s+", query):
            title, artist = query[:split.start()], query[split.end():]
            if title_key(title) == title_key(candidate.title) and artists_match(artist, candidate.artist):
                return title, artist
    return query, ""
