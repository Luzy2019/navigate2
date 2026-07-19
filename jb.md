/home/lzy/anaconda3/envs/isbench/bin/python scripts/test_all.py \
  --task store_apple_and_tissue_box_in_bottom_cabinet \
  --scene Wainscott_0_int \
  --scene-graph-backend samjam_sam2 \
  --scene-graph-step-interval 50 \
  --scene-graph-history-interval 1 \
  --nav-stuck-waypoint-tolerance 0.2 \
  --stop-on-error \
  --output-dir outputs/test_store_apple_and_tissue_box_in_bottom_cabinet_samjam_debug \
  --headless


/home/lzy/anaconda3/envs/isbench/bin/python scripts/test_all.py \
  --task store_apple_and_tissue_box_in_bottom_cabinet \
  --scene Wainscott_0_int \
  --plan-max-steps 30 \
  --scene-graph-backend samjam_sam2 \
  --scene-graph-step-interval 100 \
  --scene-graph-history-interval 50 \
  --nav-stuck-waypoint-tolerance 0.35 \
  --stop-on-error \
  --output-dir outputs/test_apple_tissue_online_plan


/home/lzy/anaconda3/envs/isbench/bin/python scripts/test_all.py \
  --task store_apple_and_tissue_box_in_bottom_cabinet \
  --scene Wainscott_0_int \
  --plan-max-steps 30 \
  --scene-graph-backend samjam_unigoal \
  --scene-graph-step-interval 30 \
  --scene-graph-history-interval 30 \
  --nav-stuck-waypoint-tolerance 0.25 \
  --stop-on-error \
  --output-dir outputs/test_apple_tissue_online_plan


/home/lzy/anaconda3/envs/isbench/bin/python scripts/test_all.py \
--task store_apple_and_tissue_box_in_bottom_cabinet \
--scene Wainscott_0_int \
--plan-max-steps 30 \
--scene-graph-backend samjam_unigoal \
--scene-graph-step-interval 150 \
--scene-graph-history-interval 30 \
--nav-stuck-waypoint-tolerance 0.25 \
--stop-on-error \
--output-dir outputs/test_apple_tissue_online_plan \
--save-topdown-scene \
--topdown-world-bounds -5.06 0.20 8.00 13.75 \
--topdown-output-size 1920x1080