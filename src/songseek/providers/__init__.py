from ..config import Config
from .bilibili import BilibiliProvider
from .kugou import KugouProvider
from .mock import MockProvider
from .netease import NeteaseProvider
from .vkeys_qq import VKeysQQProvider
from .youtube import YouTubeProvider
from .zpod import ZpodProvider


def create_providers(config: Config) -> dict:
    classes = [MockProvider] if config.mock else [NeteaseProvider, VKeysQQProvider, KugouProvider, ZpodProvider, BilibiliProvider, YouTubeProvider]
    return {cls.id: cls(config) for cls in classes}
