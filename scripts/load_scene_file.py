import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

maybe_reexec_with_omnigibson_python()

import omnigibson as og
from omnigibson.macros import gm
from omnigibson.utils.ui_utils import CameraMover
from PIL import Image
import datetime

gm.ENABLE_OBJECT_STATES = True
gm.USE_GPU_DYNAMICS = True


def _record_image_numpy_safe(self, fpath=None):
    og.log.info("Recording image...")
    if fpath is None:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        fpath = f"{self.save_dir}/og_{timestamp}.png"

    img = self.get_image()
    if hasattr(img, "detach"):
        img = img.detach().cpu().numpy()

    Path(Path(fpath).parent).mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(fpath)
    og.log.info(f"Saved current viewer camera image to {fpath}.")


CameraMover.record_image = _record_image_numpy_safe

scene_file = "/home/lzy/code/IS-Bench/data/scenes/Rs_garden/json/Rs_garden_task_boil_water_in_the_microwave__with_beer_glass_0_0_template.json"

cfg = {
    "scene": {
        "type": "InteractiveTraversableScene",
        "scene_model": "Rs_garden",
        "scene_file": scene_file,
        "trav_map_resolution": 0.1,
        "default_erosion_radius": 0.0,
        "trav_map_with_objects": True,
        "num_waypoints": 1,
        "waypoint_resolution": 0.2,
    },
    "robots": [],
    "task": {
        "type": "DummyTask",
    },
}

env = og.Environment(configs=cfg)
og.sim.enable_viewer_camera_teleoperation()

while True:
    og.sim.step()
