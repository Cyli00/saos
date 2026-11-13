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
        """Ranks which surfaces earn an asphere: ctx.AsphereExpert([1, 3, 5], terms=4, max_seconds=60).rank() ranks the
        surfaces and restores the lens; .run(nas=2) converts and optimises the best, reranking after each, and changes
        the structure. The residual comes from the merit rows (pass residual(ctx) when the positive-weight contributions
        do not add up to 100 %). engine="opticstudio" (the default, named for the method: merit rows on the current
        backend) or engine="rtc" (ranks by ray errors in an rtc copy; acceptance stays on the merit function). Other
        surface types need an AsphereProfile, finite slope limits a constraints(ctx, surface, limit_waves) provider.
        Options: uv run saos api saos.ctx.experts.asphere.AsphereExpert."""
        return AsphereExpert(self, surfaces, **options)

    def GlassExpert(self, surfaces, **options):
        """Searches glass by glass among in-production neighbours: ctx.GlassExpert([1, 3],
        catalogs=["SAOS_CDGM_INPROD"], neighbors=7, partial="working" or "gF", max_seconds=120).run(). surfaces="model"
        takes every model-glass surface, including an approved environment medium modelled that way, so before delivery
        pass the surfaces a round's model_glass lists instead. .candidates(surface) lists candidates without changing
        the lens. Cost, transmission, weight and expansion filters exist only through your feasible(ctx, surface);
        candidate_filter narrows the list. Options: uv run saos api saos.ctx.experts.glass.GlassExpert."""
        return GlassExpert(self, surfaces, **options)
