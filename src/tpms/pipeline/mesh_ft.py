"""mesh_ft.py — 文件 3/5：FT meshing（STEP -> cas.h5，参数化）。

输入: {cid}_nowater_disks.step（文件 2 输出，3 solids + 2 disks）
输出: {cid}_ft_inlet_outlet.cas.h5 + {cid}_pipeline.json（目录 output/sims/fluent_ft_variants/{cid}/）

继承自 src/tpms/fluent solver/ft_pipeline_variant.py（v1-v5 已验证的 FT 配方）:
  * FT import(One per part, mm) -> describe(close_caps=False)
  * 4 MPT: fluid/solid-fixture/solid-gyroid/solid-heatsource
  * flush-contact 变体: leakage=1mm 保持孔道开放 + mesh_controls_table
    globals（wrap 尺寸杠杆）+ gsm.merge_wrapper_at_solid_conacts=True
  * switch_to_solver -> sep_face_zone_region 切 disk 区 -> z 判定
    (z=+40 -> velocity-inlet, z=-20 -> pressure-outlet) -> TUI 改名 inlet/outlet
  * 0.38.1 坑: settings API zone_type/zone_name 坏，必须 TUI 双参数形式
  * RESUME: 已有 {cid}_sep.cas.h5 则跳过 meshing 只重做 apply-BC

配置:
  * --mp-g "x,y,z"    gyroid 材料点（默认 (9.95,9.95,9.95)；v3 anisotropic 用
                      (9.95,12.05,10.65)，见 MESH_REPRO）
  * --size-mm/--size-min   mesh_controls_table globals（v1/v3: 0.6/0.25；
                      薄壁候选需 < 最小壁厚；默认不设置 = table 默认）
  * --bare             v5 配方：完全不碰 leakage/size

用法:
  C:/anaconda3/envs/magnet2/python.exe mesh_ft.py v5
  C:/anaconda3/envs/magnet2/python.exe mesh_ft.py v6 --size-mm 0.6 --size-min 0.25
  C:/anaconda3/envs/magnet2/python.exe mesh_ft.py v6 --stl
    （STL 直入: 输入 {cid}_nowater.stl（rebuild_fixture.py --stl 输出）。
     跳过 OCC 缝合 —— Fluent FT 原生支持 .stl，ASCII STL 多 solid 命名。
     object 名 = 文件名无扩展 = {cid}_nowater，zone 名从其派生）
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import ansys.fluent.core as pyfluent

pyfluent.config.start_watchdog = False

ROOT = Path(r"C:\Users\jkong\Documents\power brain_new\heatsink_generation")
STEP_DIR = ROOT / "output" / "tpms" / "unit_cell_variants"
OUT_DIR = ROOT / "output" / "sims" / "fluent_ft_variants"

# v1-v5 已验证的 mesh 配置（新候选用命令行覆盖）
MESH_REPRO = {
    "v1": {"mp_g": (9.95, 9.95, 9.95), "size_mm": 0.6, "size_min": 0.25},
    "v2": {"mp_g": (9.95, 9.95, 9.95), "size_mm": 0.15, "size_min": 0.04},
    "v3": {"mp_g": (9.95, 12.05, 10.65), "size_mm": 0.6, "size_min": 0.25},
    "v4": {"mp_g": (10.30, 9.95, 9.95)},
    "v5": {"mp_g": (9.95, 9.95, 9.95)},
}

# shared material points（case/heatsource/流体 几何固定）
MP_F = (10.0, 10.0, -10.0)      # fluid: inside inlet pipe bore
MP_S = (5.0, 5.0, -2.5)         # solid-fixture: inside bottom plate
MP_H = (10.0, -7.5, 10.0)       # solid-heatsource: 5^3 block centre


def _regions(s):
    ftd = s.meshing.GlobalSettings.FTMRegionData
    return {"names": [str(x) for x in ftd.AllRegionNameList.get_state()],
            "types": [str(x) for x in ftd.AllRegionTypeList.get_state()]}


def _child(ft, idx, name, rtype, pt, obj):
    ir = ft.identify_regions
    ir.material_points_name = name
    ir.new_region_type = rtype
    ir.mpt_method_type = "Numerical Inputs"
    ir.selection_type = "object"
    ir.object_selection_list = [obj]
    ir.show_coordinates = True
    ir.x, ir.y, ir.z = pt
    ir.add_child = "yes"
    ir.insert_compound_child_task()
    return getattr(ft, f"identify_regions_child_{idx}")


def capture(solver, fn, *args, **kw):
    buf = []
    solver.transcript.register_callback(buf.append)
    solver.transcript.start()
    try:
        out = fn(*args, **kw)
    except Exception as e:
        out = f"EXC: {str(e)[:300]}"
    solver.transcript.stop()
    try:
        solver.transcript.unregister_callback(buf.append)
    except Exception:
        pass
    return out, "".join(buf)


def face_zone_zmap(case_path):
    """全部 face zone 的平均 z（mm）。STL 模式下盘面 zone 名不可预测，
    必须全扫描 h5 按 z 识别。"""
    import h5py
    import numpy as np
    out = {}
    with h5py.File(case_path, "r") as f:
        m = f["meshes/1"]
        fzt = m["faces/zoneTopology"]
        nz = int(fzt.attrs["nZones"][0])
        fmin = fzt["minId"][:]; fmax = fzt["maxId"][:]
        raw = fzt["name"][0]
        raw = raw.decode() if isinstance(raw, bytes) else raw
        names = [x for x in raw.split(";") if x]
        coords = m["nodes/coords/1"][:] * 1000.0
        fnodes = m["faces/nodes/1/nodes"][:]
        fnn = m["faces/nodes/1/nnodes"][:]
        for i in range(nz):
            lo, hi = int(fmin[i]), int(fmax[i])
            if lo <= 0:
                continue
            off = int(fnn[:lo - 1].sum())
            ids = fnodes[off:off + int(fnn[lo - 1:hi].sum())].astype(np.int64)
            out[names[i]] = float(coords[ids - 1, 2].mean())
    return out


def disk_z_h5(case_path, zone_name):
    return face_zone_zmap(case_path).get(zone_name)


def find_disk_zone(case_path, obj_l):
    """盘面 zone（h5 驱动，绕开 transcript 无换行拼接问题）:
    STEP 模式是 {OBJ}.N 独立 zone；STL 模式盘面被吸收进
    {合并对象名}:fluid-water wall，需要 sep 它再按 z 分离。返回 (sep_target, stl_mode)。"""
    names = list(face_zone_zmap(case_path).keys())
    for n in names:
        m = re.fullmatch(r"%s\.\d+" % re.escape(obj_l), n)
        if m:
            return n, False
    for n in names:
        if n.endswith(":fluid-water"):
            return n, True
    return None, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cid")
    ap.add_argument("--mp-g", default=None, help="gyroid 材料点 'x,y,z'")
    ap.add_argument("--size-mm", type=float, default=None)
    ap.add_argument("--size-min", type=float, default=None)
    ap.add_argument("--bare", action="store_true")
    ap.add_argument("--stl", action="store_true",
                    help="STL 直入模式（rebuild_fixture.py --stl 输出）")
    args = ap.parse_args()

    cid = args.cid
    rep = MESH_REPRO.get(cid, {})
    mp_g = tuple(float(x) for x in args.mp_g.split(",")) if args.mp_g else rep.get("mp_g", (9.95, 9.95, 9.95))
    size_mm = args.size_mm if args.size_mm is not None else rep.get("size_mm")
    size_min = args.size_min if args.size_min is not None else rep.get("size_min", 0.25)
    bare = args.bare or rep.get("bare", False)

    if args.stl:
        # Hybrid 导入（2026-08-10 定案）: case/heatsource/disks 走 STEP
        # （盘面 2D face 独立 zone，STEP 路线已验证），gyroid 走 STL
        # （跳过 OCC 缝合）。纯 STL 盘面路线实验失败（审计 §12）:
        #   合并 STL -> 盘面共边并入 fluid wall，sep 无法分离;
        #   分开导入 -> 盘面独立对象但 wrap 泄漏。
        # 对象名: STEP 对象 = 文件名（{cid}_nowater_disks，STEP 模式沿用），
        # STL 对象 = solid 名（gyroid）。
        OBJ = f"{cid}_nowater_disks"
        STEP = str(STEP_DIR / f"{cid}_nowater_disks.step")
        # gyroid 输入优先用 STEP（缝合/COMPOUND 版）：STL 网格对象 wrap 泄漏
        # （"leakage fluid-water to solid-gyroid"，审计 §13 三实验定案），
        # STEP 走 Discovery FMD 转换（需连接 RDP 会话）。
        _gstep = STEP_DIR / f"{cid}_gyroid.step"
        if _gstep.exists():
            DISKS = str(_gstep)
            GYROID_OBJ = f"{cid}_gyroid"
            gyroid_is_solid_step = True
        else:
            DISKS = str(STEP_DIR / f"{cid}_gyroid.stl")
            GYROID_OBJ = "gyroid"
            gyroid_is_solid_step = False
    else:
        OBJ = f"{cid}_nowater_disks"
        STEP = str(STEP_DIR / f"{cid}_nowater_disks.step")
        DISKS = None
        GYROID_OBJ = None
    if not Path(STEP).exists():
        print(f"[{cid}] FAIL: {STEP} not found (run rebuild_fixture.py first)", flush=True)
        sys.exit(2)
    OUT = OUT_DIR / cid
    OUT.mkdir(parents=True, exist_ok=True)
    OBJ_L = OBJ.lower()
    TYPE_MAP = {"fluid-water": "fluid", "solid-fixture": "solid",
                "solid-gyroid": "solid", "solid-heatsource": "solid", OBJ: "solid"}
    SEP = OUT / f"{cid}_sep.cas.h5"
    FINAL = OUT / f"{cid}_ft_inlet_outlet.cas.h5"
    log = {"cid": cid, "step": STEP, "obj": OBJ, "mp_g": mp_g,
           "size_mm": size_mm, "size_min": size_min, "bare": bare,
           "checkpoint": str(SEP), "resume": SEP.exists()}

    t0 = time.time()
    resume = SEP.exists()
    if resume:
        s = pyfluent.launch_fluent(mode="solver", dimension=3, precision="double",
                                   processor_count=4, cwd=str(OUT), ui_mode="no_gui",
                                   start_timeout=180)
    else:
        s = pyfluent.launch_fluent(mode="meshing", dimension=3, precision="double",
                                   processor_count=4, cwd=str(OUT), ui_mode="no_gui",
                                   start_timeout=180)
    try:
        if not resume:
            ft = s.fault_tolerant()
            imp = ft.import_cad_and_part_management
            imp.context = 0; imp.create_object_per = "One per part"
            imp.fmd_file_name = STEP; imp.file_loaded = False
            imp.length_unit = "mm"; imp.one_zone_per = "object"
            log["import"] = str(imp())
            if DISKS:
                imp.fmd_file_name = DISKS; imp.file_loaded = False
                log["import_gyroid_stl"] = str(imp())
            print(f"[{cid}] import done" + (" (+gyroid.stl)" if DISKS else ""), flush=True)
            desc = ft.describe_geometry_and_flow
            desc.add_enclosure = False; desc.close_caps = False
            desc.flow_type = "Internal flow through the object"
            log["describe"] = str(desc())

            urs = ft.update_region_settings
            gsm = ft.generate_surface_mesh
            ub = ft.update_boundaries
            cvm = ft.create_volume_mesh_ftm
            # flush-contact gyroids 需要 wrap 钻过孔道: leakage=1mm 保持
            # >=3.5mm 孔道开放（auto leakage ~4mm 会把孔道当泄漏封死，
            # run13b 教训: 水停在 z=0.05、+40 盘被吸收）；尺寸杠杆 =
            # mesh_controls_table globals（本 26.1 build 无
            # use_size_field_for_prime_wrap，wrap 直接吃 table 值）
            # hybrid 模式: gyroid MPT 选 STL 对象（solid 名），其余选 STEP 对象
            g_obj = GYROID_OBJ if args.stl else OBJ
            ch_f = _child(ft, 1, "fluid-water", "fluid", MP_F, OBJ)
            ch_s = _child(ft, 2, "solid-fixture", "solid", MP_S, OBJ)
            ch_g = _child(ft, 3, "solid-gyroid", "solid", mp_g, g_obj)
            ch_h = _child(ft, 4, "solid-heatsource", "solid", MP_H, OBJ)
            for t, ch in (("fluid", ch_f), ("solid", ch_s), ("gyroid", ch_g), ("heat", ch_h)):
                log[f"id_{t}"] = str(ch())
            seen = []
            for _ in range(3):
                reg = _regions(s)
                names = reg["names"] or ["fluid-water"]
                if names == seen:
                    break
                seen = names
                urs.all_region_name_list = names
                # STL 模式（v6 hybrid）: solid-gyroid 是水密网格对象，wrap 会
                # 在其上丢失薄壁结构 -> "leakage fluid-water to solid-gyroid"
                # （bare 复现，probe: leakage=1mm/size 无关）。FT 对封闭 STL
                # 的标准用法 = 不 wrap 直接 poly-hexcore 填充（体积填充行仍
                # 给 gyroid 开 poly-hexcore），其余区域保持 v4/v5 配方。
                # gyroid 以真 CAD SOLID STEP 导入（单 face Poly_Triangulation
                # tessellated SOLID，stl_to_solid_step_tri.py）时走 wrap
                # ——与 v4/v5 一致（v4/v5 gyroid 均为 CAD 对象参与 wrap）。
                g_stl_direct = args.stl and not gyroid_is_solid_step
                urs.all_region_mesh_method_list = [
                    ("none" if (n == "solid-gyroid" and g_stl_direct)
                     else ("wrap" if n in TYPE_MAP else "none")) for n in names]
                urs.all_region_volume_fill_list = [("poly-hexcore" if n in TYPE_MAP else "none") for n in names]
                urs.all_region_type_list = [TYPE_MAP.get(n, "void") for n in names]
                if not bare:
                    urs.all_region_leakage_size_list = ["1mm" for _ in names]
                log["update"] = str(urs())
            if size_mm is not None and not bare:
                try:
                    mct = ft.mesh_controls_table._task_object.arguments
                    mct.global_min = size_min
                    mct.global_max = size_mm
                    mct.target_growth_rate = 1.2
                    log["mesh_controls"] = f"set min={size_min} max={size_mm} (no execute)"
                    print(f"[{cid}] mesh_controls_table min={size_min} max={size_mm}", flush=True)
                except Exception as e:
                    log["mesh_controls"] = f"EXC: {str(e)[:300]}"
                try:
                    gsm.global_min = size_min
                    log["gsm_global_min"] = str(size_min)
                except Exception as e:
                    log["gsm_global_min"] = f"EXC: {str(e)[:150]}"
                # [v2 教训] 薄壁贴地 gyroid wrap "placed too close": 接触面
                # 距离 0 < local wrap size。merge_wrapper_at_solid_conacts
                # 让 wrap 层与贴地底面共存（v2 仍失败=结构问题，但此设置
                # 对 v1/v3 无害）
                try:
                    gsm.merge_wrapper_at_solid_conacts = True
                    log["gsm_merge_contacts"] = "True"
                except Exception as e:
                    log["gsm_merge_contacts"] = f"EXC: {str(e)[:150]}"
            try:
                # 体积填充不能吃未使用的 size field（run12 7.5M face 爆炸）
                cvm.fill_with_size_field = False
                log["cvm_fill_size_field"] = "False"
            except Exception as e:
                log["cvm_fill_size_field"] = f"EXC: {str(e)[:150]}"
            log["gsm"] = str(gsm())
            log["ub"] = str(ub())
            log["cvm"] = str(cvm())
            print(f"[{cid}] cvm done", flush=True)

            solver = s.switch_to_solver()
            mz = solver.settings.mesh.modify_zones
            _, zone_txt = capture(solver, mz.list_zones)
            log["zones_before"] = zone_txt[-2500:]
            # 先落盘 checkpoint，find_disk_zone 需要从 h5 读 zone 名
            solver.file.write_case(file_name=str(SEP))
            log["sep_written"] = SEP.exists()
            print(f"[{cid}] checkpoint -> {SEP.name}", flush=True)
            disk_zone, stl_mode = find_disk_zone(SEP, OBJ_L)
            if disk_zone is None:
                disk_zone = f"{OBJ_L}.2"
                print(f"[{cid}] WARN: disk zone not auto-detected, using {disk_zone}", flush=True)
            log["disk_zone"] = disk_zone
            log["stl_merge_wall"] = stl_mode
            try:
                out, txt = capture(solver, mz.sep_face_zone_region,
                                   face_zone_name=disk_zone, move_faces=True)
                log["sep"] = str(out)[:300]
                print(f"[{cid}] sep: {txt[-200:]}", flush=True)
            except Exception as e:
                log["sep"] = f"EXC: {str(e)[:300]}"
            solver.file.write_case(file_name=str(SEP))
            log["sep_written2"] = SEP.exists()
        else:
            s.file.read_case(file_name=str(SEP))
            solver = s
            mz = solver.settings.mesh.modify_zones
            _, zone_txt = capture(solver, mz.list_zones)
            log["zones_resume_before"] = zone_txt[-1200:]
            disk_zone, stl_mode = find_disk_zone(SEP, OBJ_L)
            if disk_zone is None:
                disk_zone = f"{OBJ_L}.2"
                print(f"[{cid}] WARN: disk zone not auto-detected (resume), using {disk_zone}", flush=True)
            log["disk_zone"] = disk_zone
            log["stl_merge_wall"] = stl_mode
            try:
                out, txt = capture(solver, mz.sep_face_zone_region,
                                   face_zone_name=disk_zone, move_faces=True)
                log["sep_resume"] = f"{str(out)[:120]} {txt[-120:]}"
            except Exception as e:
                log["sep_resume"] = f"EXC: {str(e)[:200]}"
            solver.file.write_case(file_name=str(SEP))
            log["sep_resume_written"] = SEP.exists()

        # apply-BC（公共段）: 0.38.1 settings API zone_type 坏、zone_name
        # 静默 no-op —— 必须 TUI 双参数
        mz_tui = solver.tui.mesh.modify_zones
        zmap = face_zone_zmap(SEP)
        # 盘面 = z 接近 +40（inlet）或 -20（outlet）的 face zone
        disk_zones = [(zn, z) for zn, z in zmap.items()
                      if zn not in ("fluid-water", "solid-fixture", "solid-gyroid",
                                    "solid-heatsource")
                      and (abs(z - 40.0) < 15 or abs(z + 20.0) < 15)]
        assigned = []
        typed = {}
        for zn, zavg in sorted(disk_zones, key=lambda x: -x[1]):
            log[f"z_{zn}"] = zavg
            print(f"[{cid}] z {zn} = {zavg}", flush=True)
            new_type = "velocity-inlet" if abs(zavg - 40.0) < 15 else "pressure-outlet"
            try:
                out, txt = capture(solver, mz_tui.zone_type, zn, new_type)
                ok = ("TUI ok" if ("EXC" not in str(out) and "Error" not in txt)
                      else f"TUI {str(out)[:80]} {txt[-150:]}")
            except Exception as e:
                ok = f"TUI EXC: {str(e)[:150]}"
            if not ok.startswith("TUI ok"):
                try:
                    mz.zone_type(zone_name=zn, new_type=new_type)
                    ok = "API ok"
                except Exception as e:
                    ok = f"API EXC: {str(e)[:150]}"
            typed[zn] = new_type
            log[f"type_{zn}"] = ok
            print(f"[{cid}] type {zn} -> {new_type}: {ok}", flush=True)
            assigned.append(zn)
        log["zmap"] = zmap
        log["disk_zones_found"] = len(disk_zones)
        if len(disk_zones) < 2:
            log["disk_warn"] = f"only {len(disk_zones)} disk zone(s) at +/-20/40 - sep may have failed"
            print(f"[{cid}] WARN: {len(disk_zones)} disk zones found (want 2)", flush=True)

        for zn in assigned:
            base = "inlet" if typed[zn] == "velocity-inlet" else "outlet"
            try:
                out, txt = capture(solver, mz_tui.zone_name, zn, base)
                log[f"rename_{zn}"] = ("TUI ok" if ("EXC" not in str(out) and "Error" not in txt)
                                       else f"TUI {str(out)[:80]} {txt[-150:]}")
                print(f"[{cid}] rename {zn} -> {base}: {log[f'rename_{zn}']}", flush=True)
            except Exception as e:
                log[f"rename_{zn}"] = f"EXC: {str(e)[:200]}"

        _, txt2 = capture(solver, mz.list_zones)
        log["zones_final"] = txt2[-3000:]
        print(f"[{cid}] zones-final:\n{txt2[-1500:]}", flush=True)

        solver.file.write_case(file_name=str(FINAL))
        log["case_written"] = FINAL.exists()
        if FINAL.exists():
            log["case_mb"] = round(FINAL.stat().st_size / 1e6, 2)
        log["status"] = "OK"
    except Exception as e:
        log["status"] = "FAIL"
        log["error"] = str(e)[:500]
        print(f"[{cid}] FAIL: {e}", flush=True)
    finally:
        log["elapsed_s"] = round(time.time() - t0, 1)
        (OUT / f"{cid}_pipeline.json").write_text(
            json.dumps(log, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        try:
            s.exit()
        except Exception:
            pass
    # wrap 失败（leakage / placed-too-close）被 except 捕获后必须 rc=1，
    # 否则调用方误判 mesh 成功、白白跑 solve_ft 的 assert 崩溃
    if log.get("status") == "FAIL":
        print(f"DONE {cid} FAILED (rc=1) -> {OUT}/{cid}_pipeline.json", flush=True)
        sys.exit(1)
    print(f"DONE {cid} -> {OUT}/{cid}_pipeline.json", flush=True)


if __name__ == "__main__":
    main()
