"""
Build fixture + water for all gyroid unit cell design variants.

For each *_solid.step design:
1. Load / trim / solidify design to [0,20]^3
2. Build 6-slab + 2-pipe fixture (fused into ONE body)
3. Extract water = (box - design) + pipe bores → SINGLE fused solid
4. Add heat_source block
5. Write named STEP with exactly 4 bodies: gyroid, fixture, water, heat_source

No inlet/outlet surface bodies — boundary faces are found on the water body at Z=-20 / Z=+40.

Usage:
    C:/anaconda3/envs/magnet2/python.exe scripts/build_all_variant_fixtures.py
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeSolid,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.Interface import Interface_Static
from OCP.STEPControl import STEPControl_Reader
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shell, TopoDS_Solid
from OCP.XCAFDoc import XCAFDoc_DocumentTool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
S = 20.0
T = 5.0
SL = 30.0
OFF = (S - SL) / 2.0          # -5
PIPE_LEN = 15.0
ROD, RID = 5.0, 4.0           # OD10 / ID8
HOLE_R = 4.0                  # through-hole radius
HEAT = 5.0                    # heat source side

INPUT_DIR  = ROOT / "output" / "tpms" / "unit_cell_variants"
OUTPUT_DIR = ROOT / "output" / "tpms" / "unit_cell_variants"

OVERLAP = 1.0  # extra overlap for bore-water fusion (avoids compound output)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def box(x0, y0, z0, dx, dy, dz):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def cyl(x, y, z0, dz, r):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1)), r, dz).Shape()


def count_solids(shape):
    """Return number of TopAbs_SOLID children in shape."""
    n = 0
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        n += 1
        exp.Next()
    return n


def fuse_pair(a, b):
    """Fuse two shapes with Build()+IsDone() check."""
    f = BRepAlgoAPI_Fuse(a, b)
    f.Build()
    if not f.IsDone():
        raise RuntimeError("Fuse failed")
    return f.Shape()


# ---------------------------------------------------------------------------
# Load & pre-process design
# ---------------------------------------------------------------------------
def load_and_trim_design(step_path):
    """Load design STEP, convert shells->solid if needed, trim to [0,20]^3."""
    reader = STEPControl_Reader()
    if reader.ReadFile(str(step_path)) != 1:
        raise RuntimeError(f"Cannot read: {step_path}")
    reader.TransferRoots()
    shape = reader.OneShape()

    solids = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        solids.append(TopoDS.Solid_s(exp.Current()))
        exp.Next()

    shells = []
    exp_shell = TopExp_Explorer(shape, TopAbs_SHELL)
    while exp_shell.More():
        shells.append(TopoDS.Shell_s(exp_shell.Current()))
        exp_shell.Next()

    if not solids and shells:
        valid_shells = []
        for sh in shells:
            face_count = 0
            fc = TopExp_Explorer(sh, TopAbs_FACE)
            while fc.More():
                face_count += 1
                fc.Next()
            if face_count >= 4:
                valid_shells.append(sh)
            else:
                print(f"    Skip {face_count}-face shell (<4 faces)")

        if not valid_shells:
            raise RuntimeError("No usable closed shells found")

        print(f"    Converting {len(valid_shells)}/{len(shells)} shells to solids")
        for sh in valid_shells:
            maker = BRepBuilderAPI_MakeSolid()
            maker.Add(sh)
            if maker.IsDone():
                solid = maker.Solid()
                if BRepCheck_Analyzer(solid).IsValid():
                    solids.append(solid)
                else:
                    print("    WARNING: invalid converted solid, using shell")
                    solids.append(sh)
            else:
                print("    WARNING: MakeSolid failed, using shell")
                solids.append(sh)

    if not solids:
        raise RuntimeError("No usable solids/shells in design")

    # Fuse all pieces into one design solid
    if len(solids) == 1:
        design = solids[0]
    else:
        print(f"    Fusing {len(solids)} design pieces...")
        design = solids[0]
        for s in solids[1:]:
            design = fuse_pair(design, s)

    # Trim to exactly [0,20]^3
    cell_box = box(0, 0, 0, S, S, S)
    common = BRepAlgoAPI_Common(design, cell_box)
    common.Build()
    if not common.IsDone():
        raise RuntimeError("Common(design, cell_box) failed")
    trimmed = common.Shape()
    n_solids = count_solids(trimmed)
    print(f"    Trimmed design -> {n_solids} solid(s)")
    return trimmed


# ---------------------------------------------------------------------------
# Build fixture
# ---------------------------------------------------------------------------
def build_fixture():
    """6 slabs + 2 pipes -> ONE fused fixture body."""
    E = 0.1
    sb  = box(OFF, OFF, -T, SL, SL, T)
    st  = box(OFF, OFF,  S, SL, SL, T)
    sf  = box(OFF, -T, OFF, SL, T + E, SL)
    sbk = box(OFF, S - E, OFF, SL, T + E, SL)
    s_left  = box(-T, OFF, OFF, T + E, SL, SL)
    s_right = box(S - E, OFF, OFF, T + E, SL, SL)

    # Through-holes
    sb = BRepAlgoAPI_Cut(sb, cyl(S/2, S/2, -T - 0.1, T + 0.2, HOLE_R)).Shape()
    st = BRepAlgoAPI_Cut(st, cyl(S/2, S/2, S - 0.1, T + 0.2, HOLE_R)).Shape()

    # Hollow pipes
    def hollow_pipe(z0, dz):
        return BRepAlgoAPI_Cut(cyl(S/2, S/2, z0, dz, ROD),
                                cyl(S/2, S/2, z0, dz, RID)).Shape()

    pipe_in  = hollow_pipe(-T - PIPE_LEN, PIPE_LEN)
    pipe_out = hollow_pipe(S + T, PIPE_LEN)

    print("    Fusing 8 fixture parts...")
    parts = [sb, st, sf, sbk, s_left, s_right, pipe_in, pipe_out]
    fixture = parts[0]
    for part in parts[1:]:
        fixture = fuse_pair(fixture, part)
    print(f"    Fixture: {count_solids(fixture)} solid(s)")
    # Trim side-slab E=0.1 intrusion into the cell [0,S]^3 — it overlaps
    # gyroid & water and triggers Icepak "Parts intersect". Keep E for clean
    # corner fusion, then cut the cell interior back out.
    cell_box = box(0, 0, 0, S, S, S)
    fcut = BRepAlgoAPI_Cut(fixture, cell_box)
    fcut.Build()
    if fcut.IsDone():
        fixture = fcut.Shape()
    print(f"    Fixture after cell-trim: {count_solids(fixture)} solid(s)")
    return fixture


# ---------------------------------------------------------------------------
# Extract water — FIXED: overlap bores with void for proper fusion
# ---------------------------------------------------------------------------
def extract_water(design):
    """Water = (cell_box - design) + bore_in + bore_out, fused into ONE solid.

    Key fix: bore cylinders extend INTO the cell region by OVERLAP (1mm)
    so they truly overlap with the gyroid void, producing a single fused
    solid instead of multiple touching-but-separate volumes.
    """
    print("    Extracting water volume...")
    cell_box = box(0, 0, 0, S, S, S)

    # gyroid_void = cell_box minus gyroid
    cut = BRepAlgoAPI_Cut(cell_box, design)
    cut.Build()
    gyroid_void = cut.Shape()

    # Bore cylinders WITH overlap into cell region
    bore_in  = cyl(S/2, S/2, -T - PIPE_LEN,          PIPE_LEN + T + OVERLAP, RID)  # Z: -20..+1
    bore_out = cyl(S/2, S/2, S - OVERLAP,             T + PIPE_LEN + OVERLAP, RID)  # Z: 19..40

    # Fuse in sequence
    water = fuse_pair(bore_in, gyroid_void)
    water = fuse_pair(water, bore_out)

    # Trim OVERLAP=1.0 bore poke into the gyroid — it overlaps the gyroid
    # (~24 mm3) and can trigger Icepak "Parts intersect". Keep OVERLAP for
    # bore-void fusion, then cut the gyroid back out of the fused water.
    wcut = BRepAlgoAPI_Cut(water, design)
    wcut.Build()
    if wcut.IsDone():
        water = wcut.Shape()

    n_solids = count_solids(water)
    print(f"    Water: {n_solids} solid(s)")
    if n_solids != 1:
        print(f"    WARNING: water has {n_solids} solids (expected 1) — may cause Icepak issues")
    return water


# ---------------------------------------------------------------------------
# Write named STEP (4 bodies only)
# ---------------------------------------------------------------------------
def write_fluent_step(path, named_shapes):
    """Write named STEP with exactly 4 body PRODUCTs: gyroid, fixture, water, heat_source."""
    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    for name, shape in named_shapes:
        label = shape_tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))

    Interface_Static.SetCVal_s("write.step.schema", "AP242")
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.Transfer(doc)
    if writer.Write(str(path).replace("\\", "/")) != 1:
        raise RuntimeError(f"Failed to write STEP: {path}")


# ---------------------------------------------------------------------------
# Build one variant
# ---------------------------------------------------------------------------
def build_variant(design_path, output_path, variant_label):
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"Building {variant_label}")
    print(f"  Design: {design_path.name}")
    print(f"  Output: {output_path.name}")

    # 1) Design
    design = load_and_trim_design(design_path)

    # 2) Fixture
    fixture = build_fixture()

    # 3) Water (fused single solid)
    water = extract_water(design)

    # 4) Heat source
    heat_x = S/2 - HEAT/2
    heat_z = S/2 - HEAT/2
    heat_source = box(heat_x, -T - HEAT, heat_z, HEAT, HEAT, HEAT)

    # 5) Write — 4 bodies ONLY (no inlet/outlet faces)
    named = [
        ("gyroid",      design),
        ("fixture",     fixture),
        ("water",       water),
        ("heat_source", heat_source),
    ]

    # Validate
    solid_counts = {name: count_solids(shape) for name, shape in named}
    print(f"  Body counts: {solid_counts}")
    total = sum(solid_counts.values())
    if total != 4:
        print(f"  WARNING: {total} total solids (expected 4)")

    write_fluent_step(output_path, named)

    size_mb = output_path.stat().st_size / 1e6
    print(f"  Done in {time.time()-t0:.1f}s — {size_mb:.1f} MB")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
VARIANTS = [
    ("v0_base_gyroid_P80_solid.step",       "v0_fixture_fluent.step"),
    ("v1_lowporosity_P50_solid.step",       "v1_fixture_fluent.step"),
    ("v2_highporosity_P90_solid.step",      "v2_fixture_fluent.step"),
    ("v3_anisotropic_1p5x_solid.step",      "v3_fixture_fluent.step"),
    ("v4_fourier_perturbed_solid.step",     "v4_fixture_fluent.step"),
    ("v5_hybrid_gyroid_diamond_solid.step", "v5_fixture_fluent.step"),
]


def main():
    # Optional CLI (backward compatible: no args -> historical VARIANTS list):
    #   python build_all_variant_fixtures.py --input-dir DIR --output-dir DIR \
    #       --variants A0_baseline A1_transverse
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", nargs="*", default=None,
                    help="variant names (design name without '_solid.step'); "
                         "default: historical VARIANTS list")
    ap.add_argument("--input-dir", default=None, help="dir with *_solid.step designs")
    ap.add_argument("--output-dir", default=None, help="dir for *_fixture_fluent.step")
    a = ap.parse_args()

    input_dir = Path(a.input_dir) if a.input_dir else INPUT_DIR
    output_dir = Path(a.output_dir) if a.output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if a.variants is not None:
        variants = [(f"{v}_solid.step", f"{v}_fixture_fluent.step") for v in a.variants]
    else:
        variants = VARIANTS

    for design_file, output_file in variants:
        design_path = input_dir / design_file
        output_path = output_dir / output_file
        if not design_path.exists():
            print(f"SKIP {design_file}: not found")
            continue
        variant_label = output_file.replace("_fixture_fluent.step", "")
        try:
            build_variant(design_path, output_path, variant_label)
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
