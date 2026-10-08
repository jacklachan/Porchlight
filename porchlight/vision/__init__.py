from ..config import Settings
from .base import NoVision, Reading, VisionProvider
from .bedrock import BedrockVision
from .fixture import FixtureVision
from .openai_compat import OpenAICompatVision

__all__ = ["BedrockVision", "FixtureVision", "NoVision", "OpenAICompatVision", "Reading", "VisionProvider", "build_vision"]


def build_vision(settings: Settings) -> VisionProvider:
    if settings.vision_provider == "bedrock":
        return BedrockVision(settings.bedrock_model_id, settings.aws_region)
    if settings.vision_provider == "openai":
        return OpenAICompatVision(settings.vision_base_url, settings.vision_model, settings.vision_api_key)
    if settings.vision_provider == "fixture":
        return FixtureVision()
    return NoVision()
