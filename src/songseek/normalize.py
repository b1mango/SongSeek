import re
import unicodedata

from opencc import OpenCC

_converter = OpenCC("t2s")
VERSIONS = {
    "live": r"(?<![a-z])live(?![a-z])|现场|演唱会|音乐会",
    "dj": r"(?<![a-z])dj(?:版)?(?![a-z])",
    "remix": r"(?<![a-z])remix(?![a-z])|混音|重混",
    "instrumental": r"(?<![a-z])(?:instrumental|inst\.?|off[\s-]*vocal|karaoke|ktv)(?![a-z])|伴奏|纯音乐|无人声|卡拉ok",
    "cover": r"(?<![a-z])cover(?:ed)?(?![a-z])|翻唱",
    "speed": r"\bsped\b|speed\s*up|slowed|reverb|nightcore|加速|降速|慢速|变速|降调|升调|变调|全景|环绕|\b[38]d\b|\d+(?:\.\d+)?\s*[x倍]|循环|\bloop\b|\b\d+\s*hours?\b|一小时",
    "clip": r"片段|试听|节选|剪辑版|\bpreview\b|\bsnippet\b|\bshort(?:\s+version)?\b|\bclip\b",
}
FLAG_LABELS = {"live": "现场", "dj": "DJ", "remix": "混音", "instrumental": "伴奏", "cover": "翻唱", "speed": "变速变调", "clip": "片段试听"}
DESCRIPTION = r"电影|电视剧|动画|游戏|综艺|插曲|主题曲|片头曲|片尾曲|推广曲|宣传曲|(?<![a-z])ost(?![a-z])|原声|正式版|完整版|original\s+soundtrack|\bfrom\s+[\"“‘']"


def descriptive(value: str) -> bool:
    return bool(re.search(DESCRIPTION, value)) and not version_flags(value) and not re.search(r"[男女]声版", value)


def normalized(text: str) -> str:
    return _converter.convert(unicodedata.normalize("NFKC", text)).casefold()


def key(text: str) -> str:
    return "".join(c for c in normalized(text) if c.isalnum())


def version_flags(text: str) -> list[str]:
    value = normalized(text)
    return [flag for flag, pattern in VERSIONS.items() if re.search(pattern, value)]


def title_key(text: str) -> str:
    value = normalized(text)
    # 描述性附注不属于歌名；版本仍由原始标题提取，未知附注保留。
    value = re.sub(r"\s+[-–—]\s+(.+)$", lambda m: "" if descriptive(m[1]) else m[0], value)
    value = re.sub(r"\([^()]*\)|\[[^\[\]]*\]|【[^【】]*】", lambda m: "" if version_flags(m[0]) or descriptive(m[0]) else m[0], value)
    for pattern in VERSIONS.values():
        value = re.sub(pattern, " ", value)
    value = re.sub(r"(?:版|version)\s*$", "", value)
    return key(value)


def artist_set(text: str) -> set[str]:
    return {key(part) for part in re.split(r"\s*(?:&|,|，|、|/|;|；|×|\bfeat(?:uring)?\.?(?![a-z])|\bft\.?(?![a-z]))\s*", normalized(text)) if key(part)}


def tolerance(seconds: float) -> float:
    return 2 if seconds <= 60 else 3
