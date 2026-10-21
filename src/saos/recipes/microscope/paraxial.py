"""兼容显微物镜配方的近轴预筛入口；实现上收到核心起点库。"""
from saos.starts.paraxial import (Surface, back_focal_distance, effective_focal_length,
                                 front_focal_distance, system_matrix)

__all__ = ["Surface", "system_matrix", "effective_focal_length", "front_focal_distance", "back_focal_distance"]
