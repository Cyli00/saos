"""当前 ZOS 镜头的非球面选面与材料搜索工具。"""
from .asphere import AsphereExpert, AsphereProfile
from .glass import GlassCandidate, GlassExpert

__all__ = ["AsphereExpert", "AsphereProfile", "ExpertMethods", "GlassCandidate", "GlassExpert"]


class ExpertMethods:
    """ctx.AsphereExpert / ctx.GlassExpert：脚本与 worker 上下文的专家入口。放在本包而非 kit.KitMethods，kit 就不必反向
    导入专家类；rtc 副本（fast.FastLens）没有这两个方法，专家类本来就要求权威镜头。
    The experts on script and worker contexts. They live here, not in kit.KitMethods, so kit never imports the experts;
    the rtc copy has neither, since the experts need the authoritative lens."""

    def AsphereExpert(self, surfaces, **options):
        return AsphereExpert(self, surfaces, **options)

    def GlassExpert(self, surfaces, **options):
        return GlassExpert(self, surfaces, **options)
