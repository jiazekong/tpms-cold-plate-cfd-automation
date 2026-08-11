"""report_results.py — 文件 5/5：汇总全部候选结果 -> comparison json + md。

输入: output/sims/fluent_ft_variants/{cid}/{cid}_solve_result.json（文件 4 输出）
      + output/tpms/unit_cell_variants/{cid}_design_meta.json（文件 1 输出，可选）
输出: <out>/comparison.json + <out>/comparison.md

统一 schema（每候选一行）:
  variant / desc / Tmax_heatsource_C / Tmax_gyroid_C / dP_Pa /
  q_water_W / q_frac / mass_bal / volume_mm3 / wall_mm（设计元数据）/
  status（OK / FAILED）

用法:
  C:/anaconda3/envs/magnet2/python.exe report_results.py [--out DIR]
  （默认输出 output/sims/fluent_ft_variants/）
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = ROOT / "output" / "sims" / "fluent_ft_variants"
DESIGN_DIR = ROOT / "output" / "tpms" / "unit_cell_variants"

DESC = {
    "v0": "P80 base gyroid", "v1": "P50 low porosity", "v2": "P90 high porosity",
    "v3": "anisotropic 1.5x", "v4": "fourier perturbed", "v5": "gyroid+diamond hybrid",
}


def collect(sim_dir, design_dir):
    rows = []
    for p in sorted(sim_dir.glob("*/{}_solve_result.json".format("*"))):
        cid = p.parent.name
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            rows.append({"variant": cid, "status": "JSON_ERR", "error": str(e)[:120]})
            continue
        gates = r.get("gates") if isinstance(r.get("gates"), dict) else {}
        row = {
            "variant": cid,
            "desc": DESC.get(cid, ""),
            "status": r.get("status", "UNKNOWN"),
            "tmax_heatsource_K": gates.get("tmax_heatsource_K"),
            "tmax_heatsource_C": (gates.get("tmax_heatsource_K") - 273.15
                                  if gates.get("tmax_heatsource_K") is not None else None),
            "tmax_gyroid_K": gates.get("tmax_gyroid_K"),
            "dP_Pa": gates.get("dP_Pa"),
            "q_water_W": gates.get("q_water_calc_W"),
            "q_frac": gates.get("q_water_vs_source_frac"),
            "mass_bal": gates.get("mass_balance_frac"),
            "elapsed_s": r.get("elapsed_s"),
        }
        dmeta = design_dir / f"{cid}_design_meta.json"
        if dmeta.exists():
            try:
                dm = json.loads(dmeta.read_text(encoding="utf-8"))
                row["volume_mm3"] = dm.get("volume_mm3")
                row["wall_theory_mm"] = dm.get("wall_theory_mm")
                row["wall_edt_mm"] = dm.get("wall_edt_mm")
                row["design"] = dm.get("design")
            except Exception:
                pass
        if r.get("error"):
            row["error"] = str(r["error"])[:150]
        rows.append(row)
    rows.sort(key=lambda x: (x.get("tmax_heatsource_C") is None, x.get("tmax_heatsource_C") or 1e9))
    return rows


def render_md(rows):
    lines = [
        "# Fluent FT 仿真对比（自动生成，report_results.py）",
        "",
        "**工况**: 0.1 L/min (0.0332 m/s), T_in=298.15K, 30W (heatsource 5³, 2.4e8 W/m³), "
        "water-liquid + aluminum(k=200), SIMPLE+PRESTO, 1st 150 → 2nd 200 iter, 16 核",
        "",
        "| 指标 | " + " | ".join(r["variant"] for r in rows) + " |",
        "|---|" + "---|" * len(rows),
    ]
    def col(key, fmt=lambda v: f"{v:.1f}" if v is not None else "—"):
        return " | ".join(fmt(r.get(key)) if isinstance(r.get(key), (int, float)) else
                          (r.get(key) if r.get(key) else "—") for r in rows)
    lines.append(f"| **Tmax_heatsource (°C)** | {col('tmax_heatsource_C')} |")
    lines.append(f"| **Tmax_gyroid (°C)** | {col('tmax_gyroid_K', lambda v: f'{v-273.15:.2f}')} |")
    lines.append(f"| **dP (Pa)** | {col('dP_Pa')} |")
    lines.append(f"| **q_water (W)** | {col('q_water_W')} |")
    lines.append(f"| **q/q_source** | {col('q_frac')} |")
    lines.append(f"| **质量守恒偏差** | {col('mass_bal', lambda v: f'{v:.2e}')} |")
    lines.append(f"| **体积 (mm³)** | {col('volume_mm3')} |")
    lines.append(f"| **壁厚理论 (mm)** | {col('wall_theory_mm')} |")
    lines.append(f"| **状态** | {col('status', lambda v: str(v))} |")
    lines += [
        "",
        "## 传热排序（Tmax 越低越好）",
        "",
        ", ".join(
            f"**{r['variant']} ({r.get('tmax_heatsource_C', '—') if r.get('tmax_heatsource_C') is not None else '—'})**"
            for r in rows if r.get("status") != "FAILED") or "（无可用结果）",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(SIM_DIR))
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = collect(SIM_DIR, DESIGN_DIR)
    if not rows:
        print("no solve_result.json found under", SIM_DIR)
        sys.exit(1)
    (out_dir / "comparison.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "comparison.md").write_text(render_md(rows), encoding="utf-8")
    print(f"OK: {len(rows)} candidates -> {out_dir}/comparison.json + comparison.md")
    for r in rows:
        t = r.get("tmax_heatsource_C")
        print(f"  {r['variant']:6s} {r['status']:7s} Tmax={t if t is None else round(t,2)}°C "
              f"dP={r.get('dP_Pa')} q_frac={r.get('q_frac')}")


if __name__ == "__main__":
    main()
