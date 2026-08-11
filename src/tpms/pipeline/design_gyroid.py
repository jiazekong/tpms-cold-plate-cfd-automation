"""design_gyroid.py — 文件 1/5：只设计 gyroid 单元（隐式场 -> 水密 solid STEP）。

流水线（5 文件，I/O 严格对齐）:
  1. design_gyroid.py   : {cid}_solid.step + {cid}_design_meta.json   （本文件）
  2. rebuild_fixture.py : {cid}_nowater_disks.step（3 solids + 2 disks）
  3. mesh_ft.py         : {cid}_ft_inlet_outlet.cas.h5 + {cid}_pipeline.json
  4. solve_ft.py        : {cid}_solved.cas/dat.h5 + {cid}_solve_result.json
  5. report_results.py  : comparison.json / .md

硬约束 A（2026-08-10 用户决策）: 最小壁厚必须 > 1mm。
  双校验，任一不过 -> REJECT（exit 2，不输出 solid.step）:
    * 理论: t = 2C / median(|grad phi|)，C = quantile(|phi|, 1-porosity)
    * 数值: 固体掩膜 EDT 距离场，t = 2 * percentile(edt, 0.5)
  依据: v2 (P90) 壁厚 ~0.57mm 导致 FT wrap 4 档尺寸全失败（结构不可行）;
        P80 ~1.15mm 勉强过线。所以 P 上限 ~P80 档。

输入（设计参数，命令行）:
  --cid <id>            候选名（默认 v6）
  --porosity <float>    孔隙率（默认 0.80；<=0.80 才能过壁厚 gate）
  --cell-x/--cell-y/--cell-z <mm>  晶胞尺寸（各向异性 = 不同值，默认 20/20/20）
  --fourier "k,amp,phase;k,amp,phase"  谱扰动（默认 x 向, 兼容 v4）; 新格式 "dir,k,amp,phase" 指定方向
  --hybrid <0..1>       gyroid+diamond 混合权重: w*g + (1-w)*d（归一化后）
  --res <mm>            网格分辨率（默认 0.15）
  --out <dir>           输出目录（默认 output/tpms/unit_cell_variants）
  --stl                 只输出 {cid}_solid.stl（秒级；跳过 OCC 缝合 ——
                        缝合对 >200k 面不可用：tol=0.3 Perform 卡死 /
                        tol=1e-6 产物 545MB。Fluent FT 原生支持 .stl）

用法示例:
  C:/anaconda3/envs/magnet2/python.exe design_gyroid.py --cid v6 --porosity 0.75
  C:/anaconda3/envs/magnet2/python.exe design_gyroid.py --cid c0 --porosity 0.7 --cell-x 24
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from skimage.measure import marching_cubes
from scipy.ndimage import distance_transform_edt
import trimesh

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "output" / "tpms" / "unit_cell_variants"

MIN_WALL_MM = 1.0      # 硬约束 A（理论 gate）
MIN_WALL_EDT_MM = 0.9  # EDT 受网格锯齿影响，略放宽
CELL_DEFAULT = 20.0
RES_DEFAULT = 0.15

# v1-v5 复现配置（porosity / cell / fourier / hybrid 用命令行覆盖）
REPRO = {
    "v0": {"porosity": 0.80},
    "v1": {"porosity": 0.50},
    "v2": {"porosity": 0.90},           # 应被壁厚 gate 拒绝
    "v3": {"cell_x": 30.0, "porosity": 0.80},
    "v4": {"porosity": 0.80,
           "fourier": "2,0.6,0.0;3,0.5,1.0", "fourier_amp": 0.25},
    "v5": {"porosity": 0.80, "hybrid": 0.5},
}


def parse_fourier(s):
    """谱扰动条目 -> (dir, k, amp, phase)。

    兼容旧格式 'k,amp,phase'（默认 x 向，v4 复现用）；新格式 'dir,k,amp,phase'
    指定方向（x/y/z）。多条用 ';' 分隔。
    """
    out = []
    for tok in (s or "").split(";"):
        tok = tok.strip()
        if not tok:
            continue
        p = [x.strip() for x in tok.split(",")]
        if p[0] in ("x", "y", "z"):
            d, p = p[0], p[1:]
        else:
            d = "x"  # 兼容 v4: 'k,amp,phase' = x 向
        p = [float(x) for x in p]
        if len(p) < 3:
            p += [0.0] * (3 - len(p))
        out.append((d, int(p[0]), p[1], p[2]))
    return out


def implicit_field(cell_x, cell_y, cell_z, res, fourier=None, fourier_amp=0.0,
                   hybrid=None):
    """隐式场 phi。gyroid 基础 + 可选 fourier 谱扰动 + 可选 diamond 混合。
    返回 (phi, xs, X, Y, Z, dx) —— phi 形状 (nx,ny,nz)。"""
    xs = np.arange(0.0, CELL_DEFAULT + res, res)
    # 各向异性: 只在一个轴上按 cell_x 缩放相位；体仍是 [0,20]^3（保持贴 case）
    X, Y, Z = np.meshgrid(2 * np.pi * xs / cell_x,
                          2 * np.pi * xs / cell_y,
                          2 * np.pi * xs / cell_z, indexing="ij")
    g = np.sin(X) * np.cos(Y) + np.sin(Y) * np.cos(Z) + np.sin(Z) * np.cos(X)
    phi = g.copy()
    if hybrid is not None:
        d = (np.sin(X) * np.sin(Y) * np.sin(Z) + np.sin(X) * np.cos(Y) * np.cos(Z)
             + np.cos(X) * np.sin(Y) * np.cos(Z) + np.cos(X) * np.cos(Y) * np.sin(Z))
        phi = hybrid * (g / np.max(np.abs(g))) + (1 - hybrid) * (d / np.max(np.abs(d)))
    for d, k, amp, phase in parse_fourier(fourier):
        coord = {"x": X, "y": Y, "z": Z}[d]
        phi += fourier_amp * amp * np.sin(k * coord + phase)
    return phi, xs, X, Y, Z, xs[1] - xs[0]


def wall_theory(phi, mask, C, xs, res):
    """理论壁厚: t = 2C / median(|grad phi|)（sheet 法向间距近似）。
    在固体掩膜内取样，避开边界过渡带。"""
    gy, gx, gz = np.gradient(phi, res)
    mag = np.sqrt(gx ** 2 + gy ** 2 + gz ** 2)
    inner = np.ones_like(phi, dtype=bool)
    inner[:, 0] = inner[:, -1] = inner[0, :] = inner[-1, :] = inner[:, :, 0] = inner[:, :, -1] = False
    vals = mag[inner & mask]
    if len(vals) == 0:
        return None
    return 2.0 * C / float(np.median(vals))


def wall_edt(mask, res):
    """数值壁厚（报告值）: 固体掩膜 EDT，t = 2 * percentile(edt, 50)。
    均匀 sheet 中 2*median(d) ~ t/2（低估），锯齿边缘不影响中位数。"""
    dt = distance_transform_edt(mask) * res
    vals = dt[mask]
    if len(vals) == 0:
        return None
    return 2.0 * float(np.percentile(vals, 50))


def to_stl(mask, xs, pad=True):
    vox = xs[1] - xs[0]
    if pad:
        # STEP 路线（旧链同款）: pad 一层 -> 网格越界 ~0.1mm，靠 rebuild 的
        # CAD 裁剪把底面切平到 z=0（旧 load_and_trim_design 语义）
        mp = np.pad(mask.astype(np.float32), 1, constant_values=0.0)
        v, f, _, _ = marching_cubes(mp, level=0.5, spacing=(vox, vox, vox))
        v -= vox
    else:
        # STL 路线: 不 pad -> isosurface 在 [0,20]^3 边界平面处封口，
        # bbox 精确 [0,20]^3、z_min=0 满足 NO-GAP（rebuild --stl 检查）
        v, f, _, _ = marching_cubes(mask.astype(np.float32), level=0.5,
                                    spacing=(vox, vox, vox))
    m = trimesh.Trimesh(vertices=v, faces=f, process=False)
    trimesh.repair.fix_normals(m)
    return m


def to_solid_step(m, out_path):
    """STL -> 水密 solid STEP（OCC 缝合；逻辑同 scripts/stl_to_step_solid.py）。"""
    import trimesh as _t
    from OCP.gp import gp_Pnt
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeFace,
                                    BRepBuilderAPI_Sewing)
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Solid
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    v0 = m.vertices[m.faces[:, 0]]
    v1 = m.vertices[m.faces[:, 1]]
    v2 = m.vertices[m.faces[:, 2]]
    area2 = np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    keep = area2 > 1e-8
    if not keep.all():
        m = _t.Trimesh(vertices=m.vertices, faces=m.faces[keep], process=True)
    # pad 伪影（2026-08-10 定案）: to_stl(pad=True) 的 pad 层在 z 两端
    # 产生微小独立碎片（~0.2mm3，贴地/顶面），缝合后 -> 多闭合 shell ->
    # MakeSolid 失败 -> COMPOUND。v5 链靠 rebuild 的 BRepAlgoAPI_Common
    # (cell_box [0,20]^3) 裁剪等效清除；design 缝合前 drop 非最大分量，
    # 同样保证输出单闭合壳（"与 v1-v5 一致"审计 §16）。
    if not m.is_watertight:
        m = _t.Trimesh(vertices=m.vertices, faces=m.faces, process=True)
    try:
        splits = m.split(only_watertight=True)
        if len(splits) > 1:
            keep_m = max(splits, key=lambda s: len(s.faces))
            print(f"  drop {len(splits)-1} pad fragments ({sum(len(s.faces) for s in splits if s is not keep_m)} tris), "
                  f"keep {len(keep_m.faces)} tris", flush=True)
            m = keep_m
    except Exception as e:
        print(f"  split check skipped: {str(e)[:120]}", flush=True)
    # 缝合容差（2026-08-10 实测）:
    #   tol=1e-6: Perform 33min 完成，但顶点未合并 -> STEP 545MB（不可用）
    #   tol=0.3 : 旧链参数（8/5 生成 38MB 紧凑 STEP），大网格上 Perform 时
    #             可能非常慢/卡死 —— 若连续失败请转 STL 直入路线
    #             （Fluent FT 原生支持 .stl，见 mesh_ft.py --stl）
    tol = 0.3
    t0 = time.time()
    sew = BRepBuilderAPI_Sewing(tol)
    n_ok = 0
    n_skip = 0
    nf = len(m.faces)
    for i, tri in enumerate(m.faces):
        p = m.vertices[tri]
        try:
            mk = BRepBuilderAPI_MakePolygon()
            mk.Add(gp_Pnt(float(p[0][0]), float(p[0][1]), float(p[0][2])))
            mk.Add(gp_Pnt(float(p[1][0]), float(p[1][1]), float(p[1][2])))
            mk.Add(gp_Pnt(float(p[2][0]), float(p[2][1]), float(p[2][2])))
            mk.Close()
            wire = mk.Wire()
            sew.Add(BRepBuilderAPI_MakeFace(wire).Shape())
            n_ok += 1
        except Exception:
            n_skip += 1
        if (i + 1) % 50000 == 0:
            print(f"  sew {i+1}/{nf} ok={n_ok} skip={n_skip} ({time.time()-t0:.0f}s)", flush=True)
    t_sew = time.time()
    sew.Perform()
    print(f"  sew.Perform done ({time.time()-t_sew:.0f}s), total {time.time()-t0:.0f}s", flush=True)
    shell = sew.SewedShape()
    b = BRep_Builder()
    solid = TopoDS_Solid()
    shape = shell
    try:
        b.MakeSolid(solid)
        b.Add(solid, shell)
        shape = solid
    except Exception:
        pass
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(out_path))
    return n_ok, shell.ShapeType()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", default="v6")
    ap.add_argument("--porosity", type=float)
    ap.add_argument("--cell-x", type=float)
    ap.add_argument("--cell-y", type=float)
    ap.add_argument("--cell-z", type=float)
    ap.add_argument("--fourier", default=None)
    ap.add_argument("--fourier-amp", type=float)
    ap.add_argument("--hybrid", type=float)
    ap.add_argument("--res", type=float, default=RES_DEFAULT)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--force", action="store_true",
                    help="壁厚 gate 不过时仍输出（仅排查用，勿用于正式候选）")
    ap.add_argument("--stl", action="store_true",
                    help="只输出 STL（跳过 OCC 缝合，秒级）")
    args = ap.parse_args()

    cid = args.cid
    rep = REPRO.get(cid, {})
    porosity = args.porosity if args.porosity is not None else rep.get("porosity", 0.80)
    cell_x = args.cell_x if args.cell_x is not None else rep.get("cell_x", CELL_DEFAULT)
    cell_y = args.cell_y if args.cell_y is not None else rep.get("cell_y", CELL_DEFAULT)
    cell_z = args.cell_z if args.cell_z is not None else rep.get("cell_z", CELL_DEFAULT)
    fourier = args.fourier if args.fourier is not None else rep.get("fourier")
    fourier_amp = args.fourier_amp if args.fourier_amp is not None else rep.get("fourier_amp", 0.0)
    hybrid = args.hybrid if args.hybrid is not None else rep.get("hybrid")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    step_path = out_dir / f"{cid}_solid.step"
    meta_path = out_dir / f"{cid}_design_meta.json"

    t0 = time.time()
    print(f"[{cid}] field: porosity={porosity} cell=({cell_x},{cell_y},{cell_z}) "
          f"fourier={fourier} amp={fourier_amp} hybrid={hybrid} res={args.res}", flush=True)
    phi, xs, X, Y, Z, dx = implicit_field(cell_x, cell_y, cell_z, args.res,
                                          fourier, fourier_amp, hybrid)
    C = float(np.quantile(np.abs(phi), 1 - porosity))
    mask = np.abs(phi) <= C
    print(f"[{cid}] C={C:.4f} solid_frac={mask.mean():.3f}", flush=True)

    t_th = wall_theory(phi, mask, C, xs, args.res)
    t_edt = wall_edt(mask, args.res)
    print(f"[{cid}] wall: theory={t_th:.3f}mm  edt50={t_edt:.3f}mm (gate {MIN_WALL_MM}mm)", flush=True)
    ok = t_th is not None and t_th > MIN_WALL_MM
    if not ok and not args.force:
        meta = {"cid": cid, "status": "REJECTED",
                "wall_theory_mm": t_th, "wall_edt_mm": t_edt,
                "min_wall_gate_mm": MIN_WALL_MM,
                "reason": f"wall {t_th if t_th is not None else 'NA'} <= {MIN_WALL_MM}mm (v2 P90 lesson)"}
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"[{cid}] REJECTED: wall {t_th:.3f}mm does not satisfy >{MIN_WALL_MM}mm "
              f"hard rule -> {meta_path.name}", flush=True)
        sys.exit(2)

    m = to_stl(mask, xs, pad=True)  # pad 版水密；--stl 时用布尔裁剪切平底面
    print(f"[{cid}] mesh: {len(m.faces)} faces watertight={m.is_watertight} "
          f"vol={m.volume:.1f}mm3 bbox=[{m.bounds[0][2]:.3f},{m.bounds[1][2]:.3f}]z", flush=True)
    # 水密 gate: 强 Fourier/hybrid 组合可使 sheet 拓扑断裂 -> marching cubes
    # 非水密（负体积）-> 缝合补洞后 wrap 仍泄漏（"leakage fluid-water to
    # solid-gyroid"）。水密是 wrap 的必要条件（成功设计的基线均 watertight=True）,
    # 非水密设计在 30s 内拒绝, 省 5min 无效 mesh 时间。gate 检查放在 drop 碎片
    # 之前（碎片是 pad 伪影, drop 后主块水密性由缝合闭合, 见审计 §14.5）。
    if not m.is_watertight and not args.force and not args.stl:
        meta = {"cid": cid, "status": "REJECTED",
                "wall_theory_mm": t_th, "wall_edt_mm": t_edt,
                "watertight": False,
                "reason": "marching cubes non-watertight (topology broken by "
                          "fourier/hybrid) -> wrap would leak"}
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"[{cid}] REJECTED: mesh not watertight (faces={len(m.faces)} "
              f"vol={m.volume:.1f}) -> wrap would leak", flush=True)
        sys.exit(2)
    if args.stl:
        # 边界裁剪到 [0,20]^3（等价旧链 CAD trim）: pad 版 isosurface 越界
        # ~0.1mm，与 cell box 求交（manifold3d 后端）切平底面 -> z_min=0
        # 精确、水密（NO-GAP 规则）。先修 winding（boolean 要求 volume）。
        trimesh.repair.fix_winding(m)
        # box 默认以原点为中心 ([-10,10]^3) —— 必须平移到 [0,20]^3，
        # 否则与 [0,20]^3 晶胞求交只留 [0,10]^3 一角（实测 v6 vol 257mm3）
        box = trimesh.creation.box(extents=(CELL_DEFAULT, CELL_DEFAULT, CELL_DEFAULT))
        box.apply_translation(np.ones(3) * CELL_DEFAULT / 2.0)
        m = m.intersection(box)
        stl_path = out_dir / f"{cid}_solid.stl"
        m.export(stl_path)
        size_mb = stl_path.stat().st_size / 1e6
        n_ok, shell_type = "STL_MODE", "STL"
    else:
        n_ok, shell_type = to_solid_step(m, step_path)
        size_mb = step_path.stat().st_size / 1e6

    meta = {
        "cid": cid,
        "status": "OK",
        "stl_mode": bool(args.stl),
        "design": {
            "porosity": porosity, "cell_x": cell_x, "cell_y": cell_y,
            "cell_z": cell_z, "fourier": fourier, "fourier_amp": fourier_amp,
            "hybrid": hybrid, "res_mm": args.res,
        },
        "wall_theory_mm": t_th,
        "wall_edt_mm": t_edt,
        "min_wall_gate_mm": MIN_WALL_MM,
        "C_quantile": C,
        "volume_mm3": round(float(m.volume), 2),
        "solid_frac": round(float(mask.mean()), 4),
        "faces": len(m.faces),
        "watertight": bool(m.is_watertight),
        "step": str(stl_path if args.stl else step_path),
        "step_mb": round(size_mb, 2),
        "sew_faces": n_ok, "shell_type": str(shell_type),
        "elapsed_s": round(time.time() - t0, 1),
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[{cid}] OK -> {(stl_path if args.stl else step_path).name} ({size_mb:.1f}MB) + "
          f"{meta_path.name} ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
