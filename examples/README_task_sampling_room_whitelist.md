# Task Sampling Room / Whitelist Example

This example implements the room / whitelist part of the official BEHAVIOR task
sampling workflow:

1. Pick room types for the task, e.g. `kitchen`.
2. Pick a scene, e.g. `Wainscott_0_int`.
3. Build a `task_custom_lists.json` entry with per-synset category/model
   whitelists.
4. Run OmniGibson's official sampling scripts against that metadata.

Official reference:
https://behavior.stanford.edu/behavior_components/task_sampling.html

## Generate The Whitelist Entry

From the repository root:

```bash
/home/lzy/anaconda3/envs/isbench/bin/python examples/task_sampling_room_whitelist.py \
  --task cook_tofu_and_vegetables__with_lighter \
  --scene Wainscott_0_int \
  --room-type kitchen \
  --spec examples/task_sampling_room_whitelist_spec.json \
  --output examples/generated/task_custom_lists.json
```

The script reads:

- `data/bddl/<task>/problem0.bddl`
- `data/tasks/<task>.json`
- `bddl/bddl/generated_data/category_mapping.csv`
- `bddl/bddl/generated_data/object_inventory.json`
- cached scene templates under `data/scenes/<scene>/json/`, when available

It writes an official-style structure:

```json
{
  "cook_tofu_and_vegetables__with_lighter": {
    "room_types": ["kitchen"],
    "Wainscott_0_int": {
      "whitelist": {
        "stove.n.01": {
          "stove": {
            "igwqpj": null
          }
        }
      },
      "blacklist": {}
    }
  }
}
```

The generated file is safe to inspect and edit before installing it into the
OmniGibson challenge metadata directory.

## Install Into Challenge Metadata

After reviewing the generated whitelist, write or merge it into:

```text
<OmniGibson data root>/2026-challenge-task-instances/metadata/task_custom_lists.json
```

Use:

```bash
/home/lzy/anaconda3/envs/isbench/bin/python examples/task_sampling_room_whitelist.py \
  --task cook_tofu_and_vegetables__with_lighter \
  --scene Wainscott_0_int \
  --room-type kitchen \
  --spec examples/task_sampling_room_whitelist_spec.json \
  --install \
  --merge-existing
```

If your OmniGibson version does not expose `gm.DATA_PATH`, pass the data root
explicitly:

```bash
/home/lzy/anaconda3/envs/isbench/bin/python examples/task_sampling_room_whitelist.py \
  --task cook_tofu_and_vegetables__with_lighter \
  --scene Wainscott_0_int \
  --room-type kitchen \
  --spec examples/task_sampling_room_whitelist_spec.json \
  --install \
  --merge-existing \
  --data-path /path/to/omnigibson/data
```

The script also accepts these environment variables:

```text
BEHAVIOR_TASK_INSTANCES_PATH=/path/to/2026-challenge-task-instances
CHALLENGE_TASK_INSTANCES_PATH=/path/to/2026-challenge-task-instances
OMNIGIBSON_DATA_PATH=/path/to/omnigibson/data
```

## Run Official Sampling

Use the OmniGibson checkout that contains the official sampling scripts:

```bash
python OmniGibson/scripts/sampling/sample_b1k_tasks.py \
  -t cook_tofu_and_vegetables__with_lighter
```

Then generate more instances and robot poses:

```bash
python OmniGibson/scripts/sampling/multiply_b1k_tasks.py \
  --partial_save \
  --start_idx 1 \
  --end_idx 5 \
  -t cook_tofu_and_vegetables__with_lighter \
  -s Wainscott_0_int

python OmniGibson/scripts/sampling/sample_robot_pose.py \
  -t cook_tofu_and_vegetables__with_lighter
```

## Notes

- The script defaults to skipping `agent.n.01`, `floor.n.01`, `water.n.06`,
  and `liquid_soap.n.01` because these are not ordinary task object models to
  whitelist.
- Override model choices in `examples/task_sampling_room_whitelist_spec.json`
  when you want exact model IDs.
- Use `--max-models-per-category N` if you want to whitelist multiple models
  per synset.
- Use `--max-scene-templates 0` to disable model preference from cached scene
  templates.
