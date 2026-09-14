"""Captive Indonesia model adapter registration."""

from framework.registry import register_adapter
from adapters.captive.captive_adapter import CaptiveIndonesiaAdapter

register_adapter("captive", CaptiveIndonesiaAdapter)

__all__ = ["CaptiveIndonesiaAdapter"]
