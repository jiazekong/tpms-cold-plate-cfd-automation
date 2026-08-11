"""rebuild_fixture.py — 文件 2/5：把 gyroid 装进固定夹具（3 solids + 2 disks）。

输入: {cid}_solid.step（文件 1 输出，必须是 watertight solid）
输出: {cid}_nowater_disks.step（3 solids: gyroid/case/heatsource + 2 shells: inlet/outlet）
      + {cid}_rebuild_check.json（body 数/bbox/体积校验）

继承自:
  * scripts/build_all_variant_fixtures.py  (bf: load_and_trim_design/build_fixture/box/write_fluent_step)
  * src/fluent_ft_v0/code/build_v1_v5_nowater.py (build_nowater: 3 solids + 2 disks, no-gap)

关键规则（2026-08-10 决策）:
  * NO-GAP: gyroid 贴地 z_min=0，无 0.985 缩放、无缝隙
  * 约束 B 定案: 不做布尔融合（union 的 FMD 转换失败已放弃），保持 3 个独立
    solid 贴地接触 + FT mesh_interfaces 传热（v0-v5 已验证路线）
  * 输出名 = {cid}_nowater_disks.step —— 与 mesh_ft.py 的输入直接对齐
    （旧 build_v1_v5_nowater.py 输出 *_nowater.step 还需要手动复制改名为
    *_nowater_disks.step；本文件一步到位）

用法:
  C:/anaconda3/envs/magnet2/python.exe rebuild_fixture.py <cid> [--out DIR] [--stl]

  --stl  输出 {cid}_nowater.stl（多 solid ASCII STL: gyroid/case/heatsource/
         inlet/outlet）—— 与 design_gyroid.py --stl 配套，跳过 OCC 缝合
         （Fluent FT 原生支持 .stl 导入，ASCII STL 多 solid 命名）。
         默认（无 --stl）输出 {cid}_nowater_disks.step（3 solids + 2 disks，
         需 solid.step 输入）。

  注意: 输入 {cid}_solid.step 与输出 {cid}_nowater_disks.step 都在同一目录
  （默认 output/tpms/unit_cell_variants/）；--out 可指定独立输入/输出目录
  （验证用临时目录，避免覆盖正式产物）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import build_all_variant_fixtures as bf  # noqa: E402

INPUT_DIR = ROOT / "output" / "tpms" / "unit_cell_variants"
OUTPUT_DIR = INPUT_DIR


def write_hybrid_step(path, named_shapes):
    """Hybrid STEP 写入（2026-08-10 FMD 根因修复）: 只给非空名称 Set_s。

    背景: FMD 转换器（Discovery.exe）对自定义命名的 SOLID 崩溃
    （Direct3D9 无关 —— probe7 证明: v6 几何去掉 SOLID 名称立即导入成功;
    v5 旧链 SOLID 名称是翻译器默认名 "Open CASCADE STEP translator 7.8 NN",
    只有 disks 自定义命名 inlet/outlet）。所以 case/heatsource 必须传 ""。
    """
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for name, shape in named_shapes:
        label = shape_tool.AddShape(shape, False)
        if name:
            TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.Transfer(doc)
    if writer.Write(str(path).replace("\\", "/")) != 1:
        raise RuntimeError(f"Failed to write STEP: {path}")


def _body_report(shape, tag):
    """OCP: solid 数 + bbox（确认贴地 z_min=0）+ 体积。"""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE
    bb = Bnd_Box()
    BRepBndLib.Add_s(shape, bb)
    xmin, ymin, zmin, xmax, ymax, zmax = bb.Get()
    n_solid = 0
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        n_solid += 1
        exp.Next()
    from OCP.GProp import GProp_GProps
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)  # OCP 7.8: 显式 GProps 对象
    return {"n_solids": n_solid, "n_faces": 0, "vol_mm3": round(props.Mass(), 2),
            "bbox": [round(xmin, 3), round(ymin, 3), round(zmin, 3),
                     round(xmax, 3), round(ymax, 3), round(zmax, 3)]}


def disk(cx, cy, cz, r):
    """2D 圆盘面（inlet/outlet 边界体）。"""
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire,
                                    BRepBuilderAPI_MakeFace)
    from OCP.gce import gce_MakeCirc
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    ax = gp_Ax2(gp_Pnt(cx, cy, cz), gp_Dir(0, 0, 1))
    circ = gce_MakeCirc(ax, r).Value()
    edge = BRepBuilderAPI_MakeEdge(circ).Edge()
    wire = BRepBuilderAPI_MakeWire(edge).Wire()
    return BRepBuilderAPI_MakeFace(wire).Face()


def occ_to_triangles(shape, deflection=0.1):
    """OCC 实体 -> 三角形顶点列表 (9 floats each)。BRepMesh 三角化。
    规则几何（case/heatsource）面少、秒级 —— 替代 OCC 缝合（大网格不可用）。"""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRep import BRep_Tool
    BRepMesh_IncrementalMesh(shape, deflection, True)
    out = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = exp.Current()
        # OCP 7.8: explorer.Current() 返回 TopoDS_Shape，用 TopoDS.Face_s 转型
        from OCP.TopoDS import TopoDS
        face = TopoDS.Face_s(face)
        loc = face.Location()
        # OCP 新旧版本差异: 老版 BRep_Tool.Triangulation，新版 Triangulation_s
        tri_fn = getattr(BRep_Tool, "Triangulation", None) or BRep_Tool.Triangulation_s
        try:
            tri = tri_fn(face, loc)
        except TypeError:
            tri = tri_fn(face, loc, 0)  # 新版 OCP 需要 theMeshPurpose 参数
        if tri is not None:
            # OCP 7.8: Node(i)/Triangle(i) 单元素访问；旧版 Triangulations()
            # 数组写法做 getattr 兼容（Node(i) 两版都有）
            get_ids = (lambda i: tri_fn().Value(i).Get()) if (tri_fn := getattr(tri, "Nodes", None)) else (lambda i: tri.Triangle(i).Get())
            for i in range(1, tri.NbTriangles() + 1):
                ids = get_ids(i)
                p = [tri.Node(ids[j]) for j in range(3)]
                for q in p:
                    out.extend((q.X(), q.Y(), q.Z()))
        exp.Next()
    return out


def disk_to_triangles(cx, cy, cz, r, n=32):
    """圆盘面 -> 扇形三角形（开放面片，STL solid 块用）。"""
    import math
    out = []
    for i in range(n):
        a0 = 2 * math.pi * i / n
        a1 = 2 * math.pi * (i + 1) / n
        out.extend((cx, cy, cz,
                    cx + r * math.cos(a0), cy + r * math.sin(a0), cz,
                    cx + r * math.cos(a1), cy + r * math.sin(a1), cz))
    return out


def write_multi_solid_stl(path, parts):
    """ASCII STL 多 solid：[(name, [9 floats per triangle]), ...]。
    Fluent FT 按 solid 名创建 mesh objects（源码: ASCII STL one or more
    solids each with a solid name）。"""
    with open(path, "w") as f:
        for name, tris in parts:
            f.write(f"solid {name}\n")
            for i in range(0, len(tris), 9):
                x0, y0, z0 = tris[i], tris[i + 1], tris[i + 2]
                x1, y1, z1 = tris[i + 3], tris[i + 4], tris[i + 5]
                x2, y2, z2 = tris[i + 6], tris[i + 7], tris[i + 8]
                ux, uy, uz = (x1 - x0, y1 - y0, z1 - z0)
                vx, vy, vz = (x2 - x0, y2 - y0, z2 - z0)
                nx = uy * vz - uz * vy
                ny = uz * vx - ux * vz
                nz = ux * vy - uy * vx
                nl = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
                f.write(f"  facet normal {nx/nl:.6e} {ny/nl:.6e} {nz/nl:.6e}\n")
                f.write("    outer loop\n")
                for q in ((x0, y0, z0), (x1, y1, z1), (x2, y2, z2)):
                    f.write(f"      vertex {q[0]:.6f} {q[1]:.6f} {q[2]:.6f}\n")
                f.write("    endloop\n")
                f.write("  endfacet\n")
            f.write(f"endsolid {name}\n")


def build_nowater(cid, input_dir=None, output_dir=None):
    """读 {cid}_solid.step -> 组装 -> 写 {cid}_nowater_disks.step + check json。"""
    t0 = time.time()
    indir = Path(input_dir) if input_dir else INPUT_DIR
    outdir = Path(output_dir) if output_dir else OUTPUT_DIR
    design_path = indir / f"{cid}_solid.step"
    out_path = outdir / f"{cid}_nowater_disks.step"
    check_path = outdir / f"{cid}_rebuild_check.json"
    log = {"cid": cid, "input": str(design_path), "output": str(out_path),
           "status": "STARTED"}
    if not design_path.exists():
        log["status"] = "FAIL"
        log["error"] = f"{design_path.name} not found（先跑 design_gyroid.py）"
        check_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
        sys.exit(2)
    print(f"[{cid}] load {design_path.name}", flush=True)

    design = bf.load_and_trim_design(design_path)
    log["gyroid"] = _body_report(design, "gyroid")
    print(f"[{cid}] gyroid {log['gyroid']}", flush=True)

    # NO-GAP RULE: TPMS 必须贴地 z_min=0（无 0.985 缩放、无缝隙）
    zmin = log["gyroid"]["bbox"][2]
    log["z_min_mm"] = zmin
    if abs(zmin) > 0.05:
        log["status"] = "FAIL"
        log["error"] = (f"NO-GAP RULE: gyroid z_min={zmin:.3f}mm != 0 —— "
                        f"设计文件必须贴地（v4 0.985 悬浮缩放教训）")
        check_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
        sys.exit(2)
    print(f"[{cid}] NO-GAP OK: z_min={zmin}mm (flushed)", flush=True)

    fixture = bf.build_fixture()
    # Cut fix: 夹具伸入晶胞区 [0,20]^3 的材料切掉（水管凸台等）
    cell_box = bf.box(0, 0, 0, bf.S, bf.S, bf.S)
    fc = bf.BRepAlgoAPI_Cut(fixture, cell_box)
    fc.Build()
    if fc.IsDone():
        fixture = fc.Shape()
    log["case"] = _body_report(fixture, "case")
    print(f"[{cid}] case {log['case']} (pipe mouth OPEN)", flush=True)

    # 热源 5x5x5 贴 case 前壁（固定）
    heat_x = bf.S / 2 - bf.HEAT / 2
    heat_z = bf.S / 2 - bf.HEAT / 2
    heatsource = bf.box(heat_x, -bf.T - bf.HEAT, heat_z, bf.HEAT, bf.HEAT, bf.HEAT)
    log["heatsource"] = _body_report(heatsource, "heatsource")
    print(f"[{cid}] heatsource {log['heatsource']}", flush=True)

    # inlet/outlet 盘面（Ø8 @ (10,10)，inlet z=+40 / outlet z=-20 —— v0 验证配方）
    inlet_face = disk(bf.S / 2, bf.S / 2, -bf.T - bf.PIPE_LEN, bf.RID)      # z=-20
    outlet_face = disk(bf.S / 2, bf.S / 2, bf.S + bf.T + bf.PIPE_LEN, bf.RID)  # z=+40
    log["disks"] = {"inlet_z": -bf.T - bf.PIPE_LEN, "outlet_z": bf.S + bf.T + bf.PIPE_LEN,
                    "r_mm": bf.RID}

    named = [("", design), ("", fixture), ("", heatsource),
             ("inlet", inlet_face), ("outlet", outlet_face)]
    write_hybrid_step(out_path, named)
    size_mb = out_path.stat().st_size / 1e6

    log.update({
        "status": "OK",
        "bodies": [n for n, _ in named],
        "step_mb": round(size_mb, 2),
        "elapsed_s": round(time.time() - t0, 1),
    })
    check_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[{cid}] OK -> {out_path.name} ({size_mb:.1f}MB) + {check_path.name} "
          f"({time.time()-t0:.0f}s)", flush=True)


def build_nowater_stl(cid, input_dir=None, output_dir=None, no_disks=False):
    """Hybrid 装配（2026-08-10 定案）: 读 {cid}_solid.stl（gyroid，跳过
    OCC 缝合）-> case/heatsource/disks 用 OCC 直接写 STEP（规则几何秒级，
    disks 2D face 独立 zone）+ gyroid STL 原样复制。输出:
      * {cid}_nowater_disks.step（case/heatsource/inlet/outlet，无 gyroid）
      * {cid}_gyroid.stl（design --stl 的原样复制）

    纯 STL 盘面路线实验失败（审计 §12）: 合并 STL 盘面与 pipe 内壁共边
    并入 fluid wall（sep 无法分离）; 分开导入盘面独立对象但 wrap 泄漏。
    盘面必须走 STEP（FT 保留 2D face 独立 zone），gyroid 走 STL（秒级）。"""
    import numpy as np
    import trimesh
    t0 = time.time()
    indir = Path(input_dir) if input_dir else INPUT_DIR
    outdir = Path(output_dir) if output_dir else OUTPUT_DIR
    design_path = indir / f"{cid}_solid.stl"
    out_path = outdir / f"{cid}_nowater_disks.step"
    disk_path = outdir / f"{cid}_gyroid.stl"
    check_path = outdir / f"{cid}_rebuild_check.json"
    log = {"cid": cid, "input": str(design_path), "output": str(out_path),
           "mode": "stl", "status": "STARTED"}
    if not design_path.exists():
        log["status"] = "FAIL"
        log["error"] = f"{design_path.name} not found（先跑 design_gyroid.py --stl）"
        check_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
        sys.exit(2)
    print(f"[{cid}] load {design_path.name}", flush=True)

    m = trimesh.load(design_path)
    if not m.is_watertight:
        log["status"] = "FAIL"
        log["error"] = f"gyroid STL not watertight (faces={len(m.faces)})"
        check_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
        sys.exit(2)
    log["gyroid"] = {"n_tri": len(m.faces), "vol_mm3": round(float(m.volume), 2),
                     "bbox": [round(v, 3) for v in m.bounds.ravel().tolist()]}

    # NO-GAP RULE: 贴地 z_min=0
    zmin = float(m.bounds[0][2])
    log["z_min_mm"] = round(zmin, 4)
    if abs(zmin) > 0.05:
        log["status"] = "FAIL"
        log["error"] = f"NO-GAP RULE: gyroid z_min={zmin:.3f}mm != 0"
        check_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
        sys.exit(2)
    print(f"[{cid}] NO-GAP OK: z_min={zmin:.4f}mm (flushed)", flush=True)

    fixture = bf.build_fixture()
    cell_box = bf.box(0, 0, 0, bf.S, bf.S, bf.S)
    fc = bf.BRepAlgoAPI_Cut(fixture, cell_box)
    fc.Build()
    if fc.IsDone():
        fixture = fc.Shape()
    log["case"] = _body_report(fixture, "case")
    print(f"[{cid}] case {log['case']} (pipe mouth OPEN)", flush=True)

    heat_x = bf.S / 2 - bf.HEAT / 2
    heat_z = bf.S / 2 - bf.HEAT / 2
    hs = bf.box(heat_x, -bf.T - bf.HEAT, heat_z, bf.HEAT, bf.HEAT, bf.HEAT)
    log["heatsource"] = _body_report(hs, "heatsource")

    # inlet/outlet 盘面（2D face，STEP 路线已验证独立 zone）
    inlet_face = disk(bf.S / 2, bf.S / 2, -bf.T - bf.PIPE_LEN, bf.RID)      # z=-20
    outlet_face = disk(bf.S / 2, bf.S / 2, bf.S + bf.T + bf.PIPE_LEN, bf.RID)  # z=+40
    log["disks"] = {"inlet_z": -bf.T - bf.PIPE_LEN, "outlet_z": bf.S + bf.T + bf.PIPE_LEN,
                    "r_mm": bf.RID}

    # Hybrid 输出（2026-08-10 定案）: case/heatsource/disks 用 OCC 直接写
    # STEP（规则几何秒级，disks 2D face 独立 zone —— STEP 路线已验证）；
    # gyroid 用 STL（跳过 OCC 缝合）。纯 STL 盘面路线实验结论（审计 §12）:
    #   合并 STL -> 盘面与 pipe 内壁共边并入 fluid wall，sep 无法分离;
    #   分开导入 -> 盘面独立对象但 FT 不把它当封口，wrap 泄漏。
    # 所以盘面必须走 STEP，gyroid 走 STL，各取所长。
    # 命名（FMD 根因修复 2026-08-10）: case/heatsource 必须传 ""（不命名）。
    #   Discovery FMD 转换器对自定义命名的 SOLID 崩溃（probe7 铁证: v6 几何
    #   只命名 disks 立即导入成功; 8/9 v5 SOLID 名是翻译器默认名）。见
    #   write_hybrid_step docstring。
    named = [("", fixture), ("", hs),
             ("inlet", inlet_face), ("outlet", outlet_face)]
    write_hybrid_step(out_path, named)
    import shutil
    shutil.copy2(design_path, disk_path)
    size_mb = (out_path.stat().st_size + disk_path.stat().st_size) / 1e6
    log.update({
        "status": "OK",
        "bodies": [n for n, _ in named] + ["gyroid(stl)"],
        "body_file": out_path.name,
        "gyroid_file": disk_path.name,
        "step_mb": round(size_mb, 2),
        "elapsed_s": round(time.time() - t0, 1),
    })
    check_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[{cid}] OK -> {out_path.name} ({size_mb:.1f}MB) + {check_path.name} "
          f"({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cid")
    ap.add_argument("--in-dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--stl", action="store_true",
                    help="STL 直入模式（design_gyroid.py --stl 配套，跳过缝合）")
    args = ap.parse_args()
    try:
        if args.stl:
            build_nowater_stl(args.cid, input_dir=args.in_dir, output_dir=args.out)
        else:
            build_nowater(args.cid, input_dir=args.in_dir, output_dir=args.out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
