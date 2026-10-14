"""查询项目材料数据，并生成仅包含折射率色散的 Zemax 目录。"""

import argparse
import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load():
    return json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))


def refractive_index(material, wavelength_nm):
    model = material['dispersion']
    if model is None:
        raise ValueError(f"{material['id']} 缺少可用色散模型，不能计算指定波长折射率")
    lo, hi = model['valid_range_nm']
    if not math.isfinite(wavelength_nm) or not lo <= wavelength_nm <= hi:
        raise ValueError(f"波长必须在 {lo}–{hi} nm 内，禁止外推")
    coefficients = model['coefficients']
    if model['formula'] == 'cauchy_nm':
        a, b, c = coefficients
        return a + b / wavelength_nm**2 + c / wavelength_nm**4
    if model['formula'] == 'sellmeier1_um':
        w2 = (wavelength_nm / 1000)**2
        return math.sqrt(1 + sum(coefficients[i] * w2 / (w2 - coefficients[i+1]) for i in (0, 2, 4)))
    raise ValueError(f"不支持的色散公式：{model['formula']}")


def export_agf(materials):
    lines = []
    for material in materials:
        model = material['dispersion']
        if model is None:
            continue
        nd = refractive_index(material, 587.5618)
        vd = (nd - 1) / (refractive_index(material, 486.1327) - refractive_index(material, 656.2725))
        if model['formula'] == 'sellmeier1_um':
            formula, coefficients = 2, model['coefficients']
        elif model['formula'] == 'cauchy_nm':
            # 将 n 的 Cauchy 式平方后精确转为 n² 的 Schott 式，且将 nm 换成 µm。
            a, b, c = model['coefficients']
            b, c = b / 1e6, c / 1e12
            formula, coefficients = 1, [a*a, 0, 2*a*b, b*b+2*a*c, 2*b*c, c*c]
        else:
            raise ValueError(f"不支持导出的色散公式：{model['formula']}")
        lines += [f"NM {material['agf_name']} {formula} 0 {nd:.10f} {vd:.8f} 1 3 1",
                  'CD ' + ' '.join(f'{v:.15g}' for v in coefficients),
                  'LD ' + ' '.join(f'{v / 1000:.8g}' for v in model['valid_range_nm']),
                  'OD -1 -1 -1 -1 -1 -1', '']
    return '\n'.join(lines)


def export_summary(materials, path):
    with path.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['材料ID', '名称', '厂商', '类别', '样品状态', '公布折射率', '公布波长nm',
                         'n488_模型计算', 'n532_模型计算', '色散公式', '有效范围nm', '来源ID'])
        for material in materials:
            model = material['dispersion']
            index = material['reported_index']
            values = [f'{refractive_index(material, w):.8f}' for w in (488, 532)] if model else ['', '']
            writer.writerow([material['id'], material['name'], material['manufacturer'], material['category'],
                             material['state'], index['value'], index['wavelength_nm'], *values,
                             model['formula'] if model else '缺少模型', model['valid_range_nm'] if model else '',
                             ','.join(material['source_ids'])])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('material', nargs='?', help='材料 ID；省略时列出全部材料')
    parser.add_argument('--wavelength', nargs='+', type=float, default=[488, 532], help='查询波长，单位 nm')
    parser.add_argument('--export', action='store_true', help='重新生成本目录的 CSV 与折射率 AGF')
    args = parser.parse_args()
    materials = load()['materials']
    if args.export:
        (ROOT / 'SAOS_MICRO_20261014.AGF').write_text(export_agf(materials), encoding='ascii')
        export_summary(materials, ROOT / 'summary.csv')
        print('已生成 summary.csv 和 SAOS_MICRO_20261014.AGF；AGF 不含吸收与热模型。')
        return
    if args.material:
        material = next((m for m in materials if m['id'] == args.material), None)
        if material is None:
            parser.error('材料 ID 不存在；省略参数可查看列表')
        try:
            values = {str(w): refractive_index(material, w) for w in args.wavelength}
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps({'id': material['id'], 'n_by_wavelength_nm': values,
                          'basis': material['dispersion'], 'notes': material['notes']}, ensure_ascii=False, indent=2))
    else:
        for material in materials:
            status = '可计算色散' if material['dispersion'] else '仅供材料初筛'
            print(f"{material['id']:24} {material['name']:14} {status}")


if __name__ == '__main__':
    main()
