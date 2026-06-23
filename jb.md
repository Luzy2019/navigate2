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
  --model gpt-4o-mini \
  --plan-max-steps 30 \
  --scene-graph-backend samjam_sam2 \
  --scene-graph-step-interval 50 \
  --scene-graph-history-interval 1 \
  --nav-stuck-waypoint-tolerance 0.2 \
  --stop-on-error \
  --output-dir outputs/test_apple_tissue_online_plan \
  --headless