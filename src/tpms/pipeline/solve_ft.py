"""solve_ft.py — 文件 4/5：求解（cas.h5 -> solved + result json，参数化）。

输入: {cid}_ft_inlet_outlet.cas.h5（文件 3 输出）
输出: {cid}_ft_solved.cas.h5 / .dat.h5 + {cid}_solve_result.json
      （目录 output/sims/fluent_ft_variants/{cid}/）

继承自 src/tpms/fluent solver/fluent_ft_solve_variant.py（v0 验证配方）:
  * KEY: FT interface 壁是单侧的（无 Coupled 选项）-> 必须
    mesh_interfaces.create(si_name='intf', all_bnd=True) 自动配对共面
    （fluid<->case, fluid<->gyroid, heatsource<->case, gyroid<->case）
  * 物理（与 v0 一致）: V_INLET=0.0332 m/s (0.1 L/min), T_in=298.15K,
    30W / 125e-9 m3 = 2.4e8 W/m3, water-liquid / aluminum(k=200),
    SIMPLE+PRESTO, 1st 150 -> 2nd 200 iter, 16 核
  * gates: 质量守恒 < 1e-3、q_water/q_source、dP、Tmax

用法:
  C:/anaconda3/envs/magnet2/python.exe solve_ft.py v5
"""
import json
import sys
import time
from pathlib import Path

import ansys.fluent.core as pyfluent

pyfluent.config.start_watchdog = False

ROOT = Path(r"C:\Users\jkong\Documents\power brain_new\heatsink_generation")
OUT_DIR = ROOT / "output" / "sims" / "fluent_ft_variants"

V_INLET = 0.0332
T_INLET_K = 298.15
Q_HEAT_W = 30.0
HEAT_VOL_M3 = 125e-9
N1ST = 150
N2ND = 200

ZONE_FLUID = "fluid-water"
ZONES_SOLID = ["solid-fixture", "solid-gyroid", "solid-heatsource"]
ZONE_HEAT = "solid-heatsource"
ZONE_INLET = "inlet"
ZONE_OUTLET = "outlet"


def _names(container):
    for m in ("list", "get_object_names"):
        meth = getattr(container, m, None)
        if callable(meth):
            try:
                r = meth()
                if r is not None:
                    return list(r)
            except Exception:
                pass
    try:
        return list(container.keys())
    except Exception:
        return []


def _scalar(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in sorted(v):
            r = _scalar(v[k])
            if r is not None:
                return r
    if isinstance(v, (list, tuple)):
        for x in v:
            r = _scalar(x)
            if r is not None:
                return r
    return None


def _mk(container, name):
    if name not in _names(container):
        container.create(name=name)
    return container[name]


def main():
    cid = sys.argv[1] if len(sys.argv) > 1 else "v5"
    OBJ = f"{cid}_nowater_disks"
    OUT = OUT_DIR / cid
    OUT.mkdir(parents=True, exist_ok=True)
    CASE = OUT / f"{cid}_ft_inlet_outlet.cas.h5"
    # Fluent lowercases zone names；zone 匹配必须用小写对象名（pipeline 同样）
    WALL_HEAT = f"{OBJ.lower()}.1:solid-heatsource"
    log = {"cid": cid, "case": str(CASE), "stages": {}, "status": "STARTED"}
    assert CASE.exists(), CASE

    t0 = time.time()
    s = pyfluent.launch_fluent(mode="solver", dimension=3, precision="double",
                               processor_count=16, ui_mode="no_gui", cwd=str(OUT),
                               start_timeout=180)
    try:
        setup = s.settings.setup
        sol = s.settings.solution

        s.settings.file.read_case(file_name=str(CASE))
        s.settings.mesh.check()
        print(f"[{cid}] mesh read + check", flush=True)

        setup.general.solver.type = "pressure-based"
        setup.general.solver.velocity_formulation = "absolute"
        setup.models.energy.enabled = True
        setup.models.viscous.model = "laminar"

        # [KEY] 自动创建 mesh interfaces: 配对全部共面壁
        buf = []
        s.transcript.register_callback(buf.append)
        s.transcript.start()
        try:
            r = setup.mesh_interfaces.create(si_name="intf", all_bnd=True)
            log["mesh_interfaces"] = {"call": str(r)[:200], "created": True}
            print(f"[{cid}] interfaces create -> {str(r)[:150]}", flush=True)
        except Exception as e:
            log["mesh_interfaces"] = f"EXC: {str(e)[:300]}"
            print(f"[{cid}] interfaces EXC: {str(e)[:250]}", flush=True)
        s.transcript.stop()
        log["interfaces_transcript"] = "\n".join(buf)[-2500:]
        try:
            log["iface_names"] = _names(setup.mesh_interfaces)
        except Exception as e:
            log["iface_names"] = f"EXC {str(e)[:150]}"

        # materials
        try:
            mats = setup.materials
            if "water-liquid" not in _names(mats.fluid):
                mats.database.copy_by_name(type="fluid", name="water-liquid")
            if "aluminum" not in _names(mats.solid):
                mats.database.copy_by_name(type="solid", name="aluminum")
            al = mats.solid["aluminum"]
            al.set_state({"density": {"value": 2719.0},
                          "specific_heat": {"value": 871.0},
                          "thermal_conductivity": {"value": 200.0}})
        except Exception as e:
            log["stages"]["materials_warn"] = str(e)[:200]

        # cell zones
        czc = setup.cell_zone_conditions
        czc.fluid[ZONE_FLUID].general.material = "water-liquid"
        for z in ZONES_SOLID:
            czc.solid[z].general.material = "aluminum"
        q_vol = Q_HEAT_W / HEAT_VOL_M3
        hs = czc.solid[ZONE_HEAT]
        hs.sources.enable = True
        terms = hs.sources.terms
        if "energy" not in _names(terms):
            terms.create(name="energy")
        term = terms["energy"]
        try:
            term.resize(1)
        except Exception:
            pass
        term[0].option = "value"
        term[0].value = q_vol
        log["stages"]["heat_source"] = f"{Q_HEAT_W}W -> {q_vol:.3e} W/m^3"
        print(f"[{cid}] zones: materials + heat source {q_vol:.2e} W/m3", flush=True)

        # BCs: inlet / outlet（interface 处理耦合传热）
        bc = setup.boundary_conditions
        vi = bc.velocity_inlet[ZONE_INLET]
        vi.momentum.velocity_magnitude = V_INLET
        vi.thermal.temperature = T_INLET_K
        po = bc.pressure_outlet[ZONE_OUTLET]
        po.momentum.gauge_pressure = 0.0
        po.thermal.backflow_total_temperature = T_INLET_K
        log["stages"]["bcs"] = f"inlet={V_INLET}m/s {T_INLET_K}K; outlet=0Pa"
        print(f"[{cid}] bcs set", flush=True)

        # reports
        rd = sol.report_definitions
        v = _mk(rd.volume, "tmax_heatsource")
        v.report_type = "volume-max"
        v.field = "temperature"
        v.cell_zones = [ZONE_HEAT]
        g = _mk(rd.volume, "tmax_gyroid")
        g.report_type = "volume-max"
        g.field = "temperature"
        g.cell_zones = ["solid-gyroid"]
        for nm, surf in [("T_in_massavg", ZONE_INLET), ("T_out_massavg", ZONE_OUTLET)]:
            sv = _mk(rd.surface, nm)
            sv.report_type = "surface-massavg"
            sv.field = "temperature"
            sv.surface_names = [surf]
        for nm, surf in [("P_in_areaavg", ZONE_INLET), ("P_out_areaavg", ZONE_OUTLET)]:
            sv = _mk(rd.surface, nm)
            sv.report_type = "surface-areaavg"
            sv.field = "pressure"
            sv.surface_names = [surf]
        for nm, surf in [("mdot_in", ZONE_INLET), ("mdot_out", ZONE_OUTLET)]:
            f = _mk(rd.flux, nm)
            f.report_type = "flux-massflow"
            f.boundaries = [surf]
        qf = _mk(rd.flux, "q_heatsource_walls")
        qf.report_type = "flux-heattransfer"
        qf.boundaries = [WALL_HEAT]
        log["stages"]["reports"] = "defined"

        # methods
        methods = sol.methods
        try:
            methods.p_v_coupling.flow_scheme = "SIMPLE"
        except Exception as e:
            log["stages"]["scheme_warn"] = str(e)[:150]
        try:
            methods.spatial_discretization.set_state({
                "gradient_scheme": "least-square-cell-based",
                "discretization_scheme": {
                    "pressure": "presto!",
                    "mom": "first-order-upwind",
                    "temperature": "first-order-upwind",
                },
            })
        except Exception as e:
            log["stages"]["method1_warn"] = str(e)[:200]

        # init（0.38.1 里 TUI 命令是 hyb_initialization）
        try:
            s.tui.solve.initialize.hyb_initialization()
            log["stages"]["init"] = "hybrid(TUI) OK"
            print(f"[{cid}] hybrid init OK", flush=True)
        except Exception as e:
            log["stages"]["init_hybrid_tui"] = f"EXC: {str(e)[:200]}"
        try:
            s.tui.solve.initialize.patch("temperature", "298.15", "all-zones")
            log["stages"]["patch"] = "OK"
        except Exception as e:
            log["stages"]["patch"] = f"skipped: {str(e)[:120]}"

        # 1st order
        t1 = time.time()
        sol.run_calculation.iterate(iter_count=N1ST)
        log["stages"]["iter1"] = f"{N1ST} iters in {time.time()-t1:.0f}s"
        print(f"[{cid}] 1st order {N1ST} done", flush=True)

        # 2nd order
        try:
            methods.spatial_discretization.set_state({
                "gradient_scheme": "least-square-cell-based",
                "discretization_scheme": {
                    "pressure": "presto!",
                    "mom": "second-order-upwind",
                    "temperature": "second-order-upwind",
                },
            })
        except Exception as e:
            log["stages"]["method2_warn"] = str(e)[:200]
        t2 = time.time()
        sol.run_calculation.iterate(iter_count=N2ND)
        log["stages"]["iter2"] = f"{N2ND} iters in {time.time()-t2:.0f}s"
        print(f"[{cid}] 2nd order {N2ND} done", flush=True)

        # extract
        report_names = ["tmax_heatsource", "tmax_gyroid", "T_in_massavg",
                        "T_out_massavg", "P_in_areaavg", "P_out_areaavg",
                        "mdot_in", "mdot_out", "q_heatsource_walls"]
        try:
            raw = rd.compute(report_defs=report_names)
        except Exception as e:
            log["stages"]["compute_err"] = str(e)[:300]
            raw = {}
        vals = {}
        if isinstance(raw, dict):
            vals = {str(k): v for k, v in raw.items()}
        elif isinstance(raw, (list, tuple)):
            vals = {nm: (raw[i] if i < len(raw) else None) for i, nm in enumerate(report_names)}
        else:
            vals = {"raw": raw}
        log["reports"] = {k: str(v) for k, v in vals.items()}
        print(f"[{cid}] reports:", json.dumps(vals, default=str), flush=True)

        out_case = OUT / f"{cid}_ft_solved.cas.h5"
        out_dat = OUT / f"{cid}_ft_solved.dat.h5"
        s.settings.file.write_case(file_name=str(out_case))
        s.settings.file.write_data(file_name=str(out_dat))
        log["saved"] = [str(out_case), str(out_dat)]

        # gates
        try:
            m_in = _scalar(vals.get("mdot_in"))
            m_out = _scalar(vals.get("mdot_out"))
            t_in = _scalar(vals.get("T_in_massavg"))
            t_out = _scalar(vals.get("T_out_massavg"))
            p_in = _scalar(vals.get("P_in_areaavg"))
            p_out = _scalar(vals.get("P_out_areaavg"))
            q_iface = _scalar(vals.get("q_heatsource_walls"))
            tmax = _scalar(vals.get("tmax_heatsource"))
            mass_bal = abs(m_in + m_out) / abs(m_in) if m_in else None
            dT = (t_out - t_in) if (t_out is not None and t_in is not None) else None
            q_water = (m_in * 4186.0 * dT) if (m_in and dT) else None
            log["gates"] = {
                "mass_balance_frac": mass_bal,
                "mass_balance_gate_ok": mass_bal is not None and mass_bal < 0.001,
                "dT_out_in_K": dT,
                "q_water_calc_W": q_water,
                "q_water_vs_source_frac": (q_water / Q_HEAT_W) if q_water else None,
                "q_heatsource_walls_W": q_iface,
                "dP_Pa": (p_in - p_out) if (p_in is not None and p_out is not None) else None,
                "tmax_heatsource_K": tmax,
                "tmax_gyroid_K": _scalar(vals.get("tmax_gyroid")),
            }
            print(f"[{cid}] gates:", json.dumps(log["gates"], default=str), flush=True)
        except Exception as e:
            log["gates"] = f"err {e}"
        log["status"] = "PASS"
    except Exception as e:
        log["status"] = "FAIL"
        log["error"] = str(e)[:700]
        print(f"[{cid}] FAIL:", e, flush=True)
    finally:
        log["elapsed_s"] = round(time.time() - t0, 1)
        try:
            s.exit()
        except Exception:
            pass
    (OUT / f"{cid}_solve_result.json").write_text(
        json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[{cid}] STATUS={log['status']} elapsed={log['elapsed_s']}s", flush=True)


if __name__ == "__main__":
    main()
