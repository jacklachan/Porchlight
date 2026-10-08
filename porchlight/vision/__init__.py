from ..config import Settings
from .base import NoVision, Reading, VisionProvider
from .bedrock import BedrockVision
from .fixture import FixtureVision

__all__ = ["BedrockVision", "FixtureVision", "NoVision", "Reading", "VisionProvider", "build_vision"]


def build_vision(settings: Settings) -> VisionProvider:
    if settings.vision_provider == "bedrock":
        return BedrockVision(settings.bedrock_model_id, settings.aws_region)
    if settings.vision_provider == "fixture":
        return FixtureVision()
    return NoVision()
