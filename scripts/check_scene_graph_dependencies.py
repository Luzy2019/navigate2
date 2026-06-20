#!/usr/bin/env python3
import importlib
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_UNIGOAL = REPO_ROOT / "og_ego_prim" / "scene_graph" / "vendor" / "unigoal"
VENDOR_GSA = VENDOR_UNIGOAL / "third_party" / "Grounded-Segment-Anything"
VENDOR_GROUNDINGDINO = VENDOR_GSA / "GroundingDINO"
VENDOR_SEGMENT_ANYTHING = VENDOR_GSA / "segment_anything"
VENDOR_SAMJAM = REPO_ROOT / "og_ego_prim" / "scene_graph" / "vendor" / "samjam"
MODEL_ROOT = Path(os.environ.get("ISBENCH_SCENE_GRAPH_MODEL_DIR", REPO_ROOT / "data" / "models"))


MODULES = [
    ("omegaconf", "omegaconf"),
    ("skimage", "scikit-image"),
    ("skfmm", "scikit-fmm"),
    ("sklearn", "scikit-learn"),
    ("open3d", "open3d"),
    ("faiss", "faiss-cpu"),
    ("supervision", "supervision==0.21.0"),
    ("grakel", "grakel"),
    ("openai", "openai"),
    ("lightglue", "lightglue"),
    ("cv2", "opencv-python"),
    ("torch", "torch"),
    ("iopath.common.file_io", "iopath"),
    ("grounded_sam_demo", "Grounded-Segment-Anything"),
    ("groundingdino.datasets.transforms", "GroundingDINO"),
    ("segment_anything", "segment-anything"),
    ("sam2.build_sam", "SAM2"),
]


PATHS = [
    (VENDOR_UNIGOAL / "src" / "graph" / "graph.py", "vendored UniGoal Graph"),
    (VENDOR_GSA / "GroundingDINO" / "groundingdino" / "config" / "GroundingDINO_SwinT_OGC.py", "GroundingDINO config"),
    (VENDOR_GSA / "segment_anything" / "segment_anything" / "__init__.py", "segment-anything package"),
    (VENDOR_SAMJAM / "sam2" / "build_sam.py", "vendored SAM2 package"),
    (MODEL_ROOT / "unigoal" / "groundingdino_swint_ogc.pth", "GroundingDINO checkpoint"),
    (MODEL_ROOT / "unigoal" / "sam_vit_h_4b8939.pth", "SAM checkpoint"),
    (MODEL_ROOT / "samjam" / "sam2.1_hiera_large.pt", "SAM2 checkpoint"),
]


def check_import(module_name, package_name):
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        return False, f"{package_name}: {exc.__class__.__name__}: {exc}"
    return True, package_name


def main():
    sys.path.insert(0, str(VENDOR_UNIGOAL))
    sys.path.insert(0, str(VENDOR_GSA))
    sys.path.insert(0, str(VENDOR_GROUNDINGDINO))
    sys.path.insert(0, str(VENDOR_SEGMENT_ANYTHING))
    sys.path.insert(0, str(VENDOR_SAMJAM))

    ok = True
    print("Scene graph dependency check")
    for path, label in PATHS:
        exists = path.exists()
        ok = ok and exists
        status = "OK" if exists else "MISSING"
        print(f"[{status}] {label}: {path}")

    for module_name, package_name in MODULES:
        imported, detail = check_import(module_name, package_name)
        ok = ok and imported
        status = "OK" if imported else "MISSING"
        print(f"[{status}] import {module_name} ({detail})")

    if not ok:
        print("\nSome scene graph dependencies are missing.")
        print("Install Python deps with: pip install -r requirements-scene-graph.txt")
        print("Install local packages from vendored GroundingDINO / segment-anything / SAM2 if needed.")
        return 1

    print("\nAll checked scene graph dependencies are available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
