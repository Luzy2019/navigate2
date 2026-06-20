import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from og_ego_prim.scene_graph.backends.samjam_sam2 import (
    SAMJAMSAM2Backend,
    SAMJAMOutputWriter,
    _vlm_bbox_to_xyxy,
)
from og_ego_prim.scene_graph.perception import (
    FrameObservation,
    PerceivedObject,
    PerceivedRelation,
    PerceptionResult,
)


def _frame(frame_index=0):
    return FrameObservation(
        frame_index=frame_index,
        rgb=np.zeros((100, 200, 3), dtype=np.uint8),
        depth=None,
        intrinsics=None,
        camera_pose=None,
        robot_position=None,
        sensor_name="test_sensor",
    )


def _mask(x1, y1, x2, y2, score=0.9):
    seg = np.zeros((100, 200), dtype=bool)
    seg[y1:y2, x1:x2] = True
    return {
        "segmentation": seg,
        "bbox": [x1, y1, x2 - x1, y2 - y1],
        "area": int(seg.sum()),
        "predicted_iou": score,
        "stability_score": score,
    }


class SAMJAMSAM2BackendTest(unittest.TestCase):
    def setUp(self):
        self.backend = SAMJAMSAM2Backend()
        self.backend.room_lookup = lambda _point: "living_room_0"

    def test_vlm_bbox_to_xyxy_uses_samjam_normalized_yxyx_order(self):
        self.assertEqual(
            _vlm_bbox_to_xyxy([100, 200, 900, 800], (100, 200, 3)),
            [40.0, 10.0, 160.0, 90.0],
        )

    def test_vlm_objects_match_sam_masks_and_remap_relations(self):
        vlm_scene_graph = {
            "objects": [
                {
                    "id": 1,
                    "name": "bucket",
                    "bbox": [100, 200, 900, 800],
                    "is_hand": False,
                    "is_moving": False,
                },
                {
                    "id": 2,
                    "name": "countertop",
                    "bbox": [100, 0, 900, 300],
                    "is_hand": False,
                    "is_moving": False,
                },
            ],
            "relationships": [
                {"subj_id": 1, "obj_id": 2, "predicate": "on"},
            ],
        }
        masks = [
            _mask(35, 8, 165, 90, 0.95),
            _mask(0, 8, 62, 90, 0.9),
        ]

        objects, relations, summary, _ = self.backend._objects_from_vlm_and_masks(
            _frame(),
            vlm_scene_graph,
            masks,
        )

        self.assertEqual([obj.name for obj in objects], ["bucket", "kitchen_counter"])
        self.assertEqual(objects[0].category, "bucket")
        self.assertGreater(objects[0].attributes["match_iou"], 0.25)
        self.assertEqual(summary["matched_object_count"], 2)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0].source_id, "frame_vlm:0:1")
        self.assertEqual(relations[0].target_id, "frame_vlm:0:2")
        self.assertEqual(relations[0].relation, "on")

    def test_update_memory_marks_missing_objects_invisible_and_recomputes_edges(self):
        result = PerceptionResult(
            backend=self.backend.name,
            frame_index=0,
            objects=[
                PerceivedObject(
                    object_id="frame_vlm:0:1",
                    name="bucket",
                    category="bucket",
                    bbox=[10.0, 10.0, 60.0, 60.0],
                    room_id="living_room_0",
                    attributes={"currently_visible": True, "transient": False},
                ),
                PerceivedObject(
                    object_id="frame_vlm:0:2",
                    name="countertop",
                    category="countertop",
                    bbox=[0.0, 55.0, 90.0, 95.0],
                    room_id="living_room_0",
                    attributes={"currently_visible": True, "transient": False},
                ),
            ],
            relations=[
                PerceivedRelation(
                    source_id="frame_vlm:0:1",
                    target_id="frame_vlm:0:2",
                    relation="on",
                    source="test",
                )
            ],
            scene_graph={"nodes": [], "edges": []},
            metadata={},
        )

        updated = self.backend.update_memory(result)
        self.assertEqual(len(updated.objects), 2)
        self.assertEqual(len(updated.relations), 1)
        self.assertEqual(updated.relations[0].source_id, "samjam_object:0")

        empty = PerceptionResult(
            backend=self.backend.name,
            frame_index=1,
            objects=[],
            relations=[],
            scene_graph={"nodes": [], "edges": []},
            metadata={},
        )
        updated = self.backend.update_memory(empty)
        self.assertEqual(len(updated.objects), 0)
        self.assertEqual(len(updated.relations), 0)
        self.assertEqual(updated.metadata["memory_object_count"], 2)
        self.assertEqual(updated.metadata["visible_memory_object_count"], 0)
        self.assertFalse(updated.metadata["memory_objects"][0]["visible"])

    def test_robot_scene_canonicalizes_hand_names_to_robot_gripper(self):
        vlm_scene_graph = {
            "objects": [
                {
                    "id": 1,
                    "name": "hand_one",
                    "bbox": [100, 200, 900, 800],
                    "is_hand": True,
                    "is_moving": True,
                }
            ],
            "relationships": [],
        }
        objects, _, summary, _ = self.backend._objects_from_vlm_and_masks(
            _frame(),
            vlm_scene_graph,
            [_mask(35, 8, 165, 90, 0.95)],
        )

        self.assertEqual(summary["matched_object_count"], 1)
        self.assertEqual(objects[0].name, "robot_gripper")
        self.assertEqual(objects[0].category, "robot_gripper")
        self.assertFalse(objects[0].attributes["is_hand"])
        self.assertTrue(objects[0].attributes["vlm_raw_is_hand"])

    def test_low_iou_vlm_matches_are_rejected_by_default(self):
        vlm_scene_graph = {
            "objects": [
                {
                    "id": 1,
                    "name": "bucket",
                    "bbox": [100, 200, 900, 800],
                    "is_hand": False,
                    "is_moving": False,
                }
            ],
            "relationships": [],
        }
        objects, relations, summary, _ = self.backend._objects_from_vlm_and_masks(
            _frame(),
            vlm_scene_graph,
            [_mask(0, 0, 20, 20, 0.95)],
        )

        self.assertEqual(objects, [])
        self.assertEqual(relations, [])
        self.assertEqual(summary["matched_object_count"], 0)
        self.assertEqual(summary["rejected_vlm_objects"][0]["reason"], "low_iou")

    def test_transient_objects_are_pruned_after_ttl(self):
        with patch.dict(os.environ, {"ISBENCH_SAMJAM_MEMORY_TTL": "1"}):
            result = PerceptionResult(
                backend=self.backend.name,
                frame_index=0,
                objects=[
                    PerceivedObject(
                        object_id="frame_mask:0:1",
                        name="sam_mask_1",
                        category="sam_mask",
                        bbox=[10.0, 10.0, 60.0, 60.0],
                        room_id="living_room_0",
                        attributes={"currently_visible": True, "transient": True},
                    )
                ],
                scene_graph={"nodes": [], "edges": []},
                metadata={},
            )
            self.backend.update_memory(result)
            self.assertEqual(len(self.backend.memory), 1)

            for frame_index in (1, 2):
                empty = PerceptionResult(
                    backend=self.backend.name,
                    frame_index=frame_index,
                    objects=[],
                    relations=[],
                    scene_graph={"nodes": [], "edges": []},
                    metadata={},
                )
                self.backend.update_memory(empty)

            self.assertEqual(self.backend.memory, {})

    def test_samjam_output_writer_creates_expected_directories_and_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            writer = SAMJAMOutputWriter(Path(tmp_dir))
            frame = _frame()
            mask = np.zeros((100, 200), dtype=bool)
            mask[10:40, 20:80] = True
            obj = PerceivedObject(
                object_id="samjam_object:0",
                name="bucket",
                category="bucket",
                bbox=[20.0, 10.0, 80.0, 40.0],
                mask=mask,
                attributes={"is_hand": False, "is_moving": False, "is_moved": False, "mask_area": int(mask.sum())},
            )
            rel = PerceivedRelation(
                source_id="samjam_object:0",
                target_id="samjam_object:0",
                relation="near",
            )

            writer.write(
                frame=frame,
                vlm_scene_graph={
                    "objects": [
                        {"id": 1, "name": "bucket", "bbox": [100, 100, 400, 400]},
                    ],
                    "relationships": [],
                },
                candidates=[],
                objects=[obj],
                relations=[rel],
            )

            root = Path(tmp_dir)
            self.assertTrue((root / "resized_images" / "000000.jpg").exists())
            self.assertTrue((root / "scene_graph_output" / "0_objs.json").exists())
            self.assertTrue((root / "scene_graph_output" / "0_rels.json").exists())
            self.assertTrue((root / "vis_output" / "frame_0_vlm_bbox.jpg").exists())
            objs = json.loads((root / "scene_graph_output" / "0_objs.json").read_text())
            self.assertEqual(objs[0]["name"], "bucket")


if __name__ == "__main__":
    unittest.main()
