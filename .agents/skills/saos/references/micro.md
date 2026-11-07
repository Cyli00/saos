# Micro-optics measurements

`from saos import micro` holds the layout-free measurements of fibre probes, endoscopes, fibre-tip printed lenses and miniature objectives: rays and focus, the medium before the image, part clearances, collection, diffraction and zoom travel. Build a job's checks from them instead of copying another job's helpers: a zoom-probe job that imported another job's private helper broke when it changed. `uv run saos api saos.micro` lists them, and each docstring has its exact definition.

- **Validity:** a physically invalid state raises `micro.DesignFailure`; wrap a check in `micro.guarded(fn)` so it returns inf. `micro.once(ctx, key, compute)` is `ctx.memo` that also remembers a failure; put your own namespace first in `key`.
- **Sample-side NA:** `marginal_na(ctx, wave, to)` is the smallest n·sin u of a ring of on-axis marginal rays, one of several definitions the jobs used (marginal height over depth, half the angle between opposite rays). Settle which one the demand means before writing it as a check.
- **Collection:** `collection_fraction` is the geometric solid angle over 4π, without Fresnel loss or absorption.
- **Zoom travel:** a `focus_travel` `span` is only a check value when `coverage_ok` is true.
- **Spare licences:** `micro.pooled` splits a slow per-field measurement over the worker pool when licences are free, leaving 2 for the user. It returns only the finished items, so compute the rest yourself.
