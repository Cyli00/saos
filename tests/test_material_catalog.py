import csv
import hashlib
import importlib.util
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'library' / 'materials'
spec = importlib.util.spec_from_file_location('material_catalog', ROOT / 'catalog.py')
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)
DATA = catalog.load()
MODELS = [m for m in DATA['materials'] if m['dispersion']]


@pytest.mark.parametrize('material', MODELS, ids=lambda m: m['id'])
def test_models_against_published_measurements(material):
    errors = [abs(catalog.refractive_index(material, w) - n) for w, n in material['index_samples']]
    assert max(errors) < (0.0012 if material['id'] == 'ipdip2-darus2026' else 0.0005)


@pytest.mark.parametrize('material', [m for m in MODELS if 'spectral_column_suffix' in m], ids=lambda m: m['id'])
def test_models_against_authors_separate_spectral_table(material):
    rows = csv.DictReader((ROOT / 'sources/darus-6471/RefractiveIndexSellmeierData.txt').read_text().splitlines(), delimiter='\t')
    errors = [abs(catalog.refractive_index(material, float(r['lambda_nanometre'])) -
                  float(r['n_' + material['spectral_column_suffix'] + '_Sellmeier']))
              for r in rows if 405 <= float(r['lambda_nanometre']) <= 1940]
    # 作者提供的 IPX-Clear 系数与独立拟合表有差异；库以 AGF 系数为准，并在说明中记录差异。
    assert max(errors) < (3.4e-5 if material['id'] == 'ipxclear-darus2026' else 1.1e-8)


@pytest.mark.parametrize('material', [m for m in DATA['materials'] if not m['dispersion']], ids=lambda m: m['id'])
def test_single_point_materials_do_not_invent_dispersion(material):
    with pytest.raises(ValueError, match='缺少可用色散模型'):
        catalog.refractive_index(material, 532)


@pytest.mark.parametrize('wavelength', [0, 404, 1941, float('nan'), float('inf')])
def test_query_rejects_extrapolation(wavelength):
    material = next(m for m in MODELS if m['id'] == 'ips-darus2026')
    with pytest.raises(ValueError, match='禁止外推'):
        catalog.refractive_index(material, wavelength)


def test_exported_agf_matches_queries_and_excludes_unverified_transmission():
    text = (ROOT / 'SAOS_MICRO_20261014.AGF').read_text(encoding='ascii')
    assert text == catalog.export_agf(DATA['materials'])
    assert not any(line.startswith('IT ') for line in text.splitlines())
    mapping = {m['agf_name']: m for m in MODELS}
    seen = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == 'NM':
            material, formula = mapping[fields[1]], int(fields[2])
            seen.append(fields[1])
        elif fields[0] == 'CD':
            c = list(map(float, fields[1:]))
            for w in (488, 532, 587.5618):
                l = w / 1000
                if formula == 1:
                    n = math.sqrt(c[0]+c[1]*l*l+c[2]/l**2+c[3]/l**4+c[4]/l**6+c[5]/l**8)
                else:
                    n = math.sqrt(1+c[0]/(1-c[1]/l**2)+c[2]/(1-c[3]/l**2)+c[4]/(1-c[5]/l**2))
                assert n == pytest.approx(catalog.refractive_index(material, w), abs=1e-12)
    assert set(seen) == set(mapping)


def test_archived_sources_and_references():
    manifest = json_load(ROOT / 'sources/darus-6471/manifest.json')
    for file in manifest['files']:
        assert hashlib.sha256((ROOT / 'sources/darus-6471' / file['file']).read_bytes()).hexdigest() == file['sha256']
    ids = [m['id'] for m in DATA['materials']]
    assert len(ids) == len(set(ids)) == 22
    for material in DATA['materials']:
        assert all(s in DATA['sources'] for s in material['source_ids'])


def json_load(path):
    import json
    return json.loads(path.read_text(encoding='utf-8'))
