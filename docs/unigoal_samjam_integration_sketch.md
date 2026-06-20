# IS-Bench x UniGoal x SAMJAM Integration Sketch

## 1. Goal

Build the migration around `IS-Bench` as the host system:

- `IS-Bench` remains the owner of:
  - task definitions
  - primitive execution
  - safety evaluation
  - benchmark loop
- `UniGoal` contributes:
  - navigation backend ideas and reusable algorithms
  - frontier exploration / FMM planning logic
- `SAMJAM` contributes:
  - dynamic perception-driven scene graph signals
  - object visibility / motion / relation cues from image observations

The key design choice is:

> Do not migrate the full `UniGoal` runtime or the full `SAMJAM` pipeline as-is.
> Instead, expose both as pluggable backends behind `IS-Bench` interfaces.


## 2. Why Direct Porting Will Be Painful

### 2.1 IS-Bench today

`IS-Bench` is currently centered on semantic action execution, not continuous mobile navigation.

- Tasks are static JSON configs under `data/tasks/`
- Execution is driven by `Executor`
- Manipulation primitives live in `og_ego_prim/primitives/ego_primitives.py`
- Online execution currently skips `NAVIGATE*` actions in `og_ego_prim/benchmark/online_benchmark.py`
- Object references are resolved from `env.task.object_scope`

That means the current execution graph is:

`task json -> planner -> semantic primitive -> omnigibson object state change -> evaluator`

### 2.2 UniGoal today

`UniGoal` is a full navigation stack tightly coupled to its own environment assumptions:

- Habitat-style env wrapper
- continuous ego observations
- `gps`, `compass`, `depth`, `semantic`
- BEV map construction
- FMM local planner
- graph-guided exploration loop

Useful parts exist, but they are buried inside a Habitat-oriented control loop.

### 2.3 SAMJAM today

`SAMJAM` outputs a video scene graph built from:

- VLM-generated per-frame scene graph
- SAM2 masks
- video mask propagation / tracking
- object motion tags such as `is_moving`

Useful signals exist, but they are:

- 2D and image-centric
- not bound to simulator object handles
- not designed to drive primitive execution directly


## 3. Recommended Migration Principle

Use a three-layer split:

1. `Execution truth layer`
   - owned by `IS-Bench` / OmniGibson
   - authoritative for object identity and state
2. `Perception layer`
   - may be simulator-derived, VLM-derived, or SAMJAM-like
   - provides visibility / masks / motion / coarse relations
3. `Navigation layer`
   - produces robot base motion goals and path execution
   - initially OmniGibson-native
   - later replaceable by UniGoal-style backends

This keeps the benchmark stable while allowing iterative upgrades.


## 4. Target Architecture

### 4.1 High-level module diagram

```text
                    +----------------------+
                    |   data/tasks/*.json  |
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    |   OnlineBenchmark    |
                    | benchmark owner      |
                    +---+---------+--------+
                        |         |
                        |         +-----------------------------+
                        |                                       |
                        v                                       v
              +-------------------+                  +-------------------+
              |  PlanningAgent    |                  |    Evaluator      |
              | prompt + planning |                  | safety + goals    |
              +---------+---------+                  +-------------------+
                        |
                        v
              +-------------------+
              |     Executor      |
              +----+---------+----+
                   |         |
         primitive |         | scene context
                   v         ^
         +----------------+  |
         | Primitive Set  |  |
         +-------+--------+  |
                 |           |
        +--------+--------+  |
        |                 |  |
        v                 v  |
+---------------+   +-------------------+
| Navigation     |   | Hybrid Scene     |
| Backend        |   | Graph Builder    |
+-------+--------+   +--------+----------+
        ^                         ^
        |                         |
        |                         |
+-------+--------+      +--------+----------------------+
| OmniGibson /    |      | Perception Backend           |
| UniGoal-style   |      | simulator / SAMJAM-style     |
+-----------------+      +------------------------------+
```


## 5. New Interfaces To Introduce

Create three new packages under `og_ego_prim/`.

### 5.1 Navigation

Suggested files:

- `og_ego_prim/navigation/base.py`
- `og_ego_prim/navigation/omnigibson_nav.py`
- `og_ego_prim/navigation/unigoal_nav.py`

Suggested interface:

```python
class NavigationBackend:
    def reset(self, env): ...
    def navigate_to_object(self, env, target_obj): ...
    def navigate_to_pose(self, env, pose): ...
    def get_debug_state(self) -> dict: ...
```

Initial implementation:

- `OmniGibsonNavigationBackend`
- simple but real movement
- no UniGoal dependency yet

Later implementation:

- `UniGoalStyleNavigationBackend`
- reuses FMM / frontier goal selection ideas
- adapts OmniGibson observations into the expected planner inputs

### 5.2 Perception

Suggested files:

- `og_ego_prim/perception/base.py`
- `og_ego_prim/perception/omnigibson_perception.py`
- `og_ego_prim/perception/samjam_perception.py`

Suggested interface:

```python
class PerceptionBackend:
    def reset(self): ...
    def observe(self, env, obs_bundle) -> "PerceptionFrame": ...
```

Suggested data classes:

```python
class PerceptionObject:
    object_id: str
    name: str
    bbox_2d: list[int] | None
    mask: object | None
    visible: bool
    is_moving: bool | None
    confidence: float
    source: str

class PerceptionRelation:
    source_id: str
    target_id: str
    relation: str
    confidence: float
    source: str

class PerceptionFrame:
    objects: list[PerceptionObject]
    relations: list[PerceptionRelation]
    metadata: dict
```

### 5.3 Scene Graph

Suggested files:

- `og_ego_prim/scene_graph/schema.py`
- `og_ego_prim/scene_graph/builder.py`
- `og_ego_prim/scene_graph/resolver.py`

Suggested interface:

```python
class SceneGraphBuilder:
    def reset(self, env): ...
    def update(self, env, perception_frame) -> "HybridSceneGraph": ...
```

Suggested hybrid graph semantics:

- node identity should map to `env.task.object_scope` whenever possible
- relation truth should prefer simulator states for:
  - `inside`
  - `on_top`
  - `covered`
  - `contains`
- perception relations should be supplemental, not authoritative


## 6. Recommended Data Ownership Rules

This is the most important rule set for avoiding integration drift.

### 6.1 Object identity

Use `IS-Bench` task object names as the canonical IDs.

Examples:

- `electric_kettle.n.01_1`
- `sink.n.01_1`
- `sponge.n.01_1`

Do not let UniGoal node IDs or SAMJAM track IDs become execution IDs.

Instead, maintain mappings:

```text
task object name  <->  wrapped_obj handle  <->  perception aliases / track ids
```

### 6.2 Object state truth

Use OmniGibson state as truth for execution/evaluation:

- open / closed
- toggled on / off
- inside / on top
- covered by stain / liquid / particles

Perception may be wrong; evaluator should not depend on it.

### 6.3 Scene graph truth

Use a hybrid graph:

- topology can include perception-driven edges
- execution-critical state must stay simulator-backed


## 7. Primitive Layer Changes

### 7.1 Add real navigation primitives

Suggested additions to `og_ego_prim/primitives/ego_primitives.py`:

- `NAVIGATE_TO`
- optionally `NAVIGATE_NEAR`
- optionally `LOOK_AT`

Example target primitive set:

```text
NAVIGATE_TO(target_obj)
GRASP(target_obj)
PLACE_ON_TOP(target_obj, placement_obj)
...
```

Note:

- the current system recognizes navigation-like actions in planning prompts
- but online execution bypasses them
- this shortcut should be removed after navigation backend exists

### 7.2 Executor integration

Update `og_ego_prim/primitives/executor.py` so navigation primitives call the navigation backend instead of being ignored upstream.

### 7.3 Benchmark integration

Update `og_ego_prim/benchmark/online_benchmark.py`:

- remove the early return for `NAVIGATE*`
- initialize:
  - navigation backend
  - perception backend
  - scene graph builder
- attach them to benchmark / executor lifecycle


## 8. Planner Layer Changes

### 8.1 Keep planner object vocabulary stable at first

Right now `PlanningAgent` validates action parameters against static `object_list`.

That is good for Phase 1.

Do not immediately let the planner invent new dynamic object names from perception.

Instead:

- feed scene graph as additional context
- keep action arguments constrained to task-relevant objects

### 8.2 Suggested prompt extension

Add a new optional planning context block:

```text
scene_graph_summary:
- visible objects: ...
- risky moving objects: ...
- current relations: ...
```

This lets SAMJAM-style signals affect planning without breaking execution naming.


## 9. How To Reuse UniGoal Safely

### 9.1 Reuse

Good candidates for reuse:

- FMM planner logic
- short-term goal selection
- frontier scoring
- map-based exploration abstractions

### 9.2 Do not directly reuse

Bad candidates for direct reuse:

- Habitat env wrapper
- episode / goal dataset assumptions
- category ID pipeline
- full graph exploration loop as the benchmark owner

### 9.3 Adaptation boundary

If UniGoal logic is brought in, adapt `IS-Bench` observations to a navigation-facing struct such as:

```python
NavObservation:
    rgb
    depth
    pose
    occupancy_map
    semantic_map
    target_spec
```

This is better than forcing `IS-Bench` to mimic Habitat APIs everywhere.


## 10. How To Reuse SAMJAM Safely

### 10.1 Reuse

Good candidates for reuse:

- mask generation / tracking ideas
- object motion tags
- per-frame relation prediction
- VLM prompt structure for scene graph extraction

### 10.2 Do not directly reuse

Bad candidates for direct reuse:

- raw track ID as execution ID
- pure 2D graph as world truth
- hardcoded model / key usage

### 10.3 Required rewrite

Before any direct reuse, rewrite the provider layer:

- remove hardcoded secrets
- isolate model client config
- expose a backend-friendly API


## 11. Phased Implementation Plan

### Phase 0: Make navigation real inside IS-Bench

Goal:

- `NAVIGATE_TO(obj)` becomes executable

Changes:

- add navigation primitive
- add navigation backend interface
- remove benchmark-side navigation skip

Success criterion:

- benchmark can execute a plan with navigation plus manipulation

### Phase 1: Introduce a minimal hybrid scene graph

Goal:

- represent visibility + canonical object IDs

Changes:

- add perception backend interface
- first backend uses OmniGibson viewer / bbox / object_scope only
- add scene graph builder with canonical task IDs

Success criterion:

- planner can consume dynamic scene summaries
- no change to evaluator truth source

### Phase 2: Add SAMJAM-style perception backend

Goal:

- dynamic visual object / motion / relation signals

Changes:

- add optional image-driven backend
- merge perception output into hybrid graph
- keep simulator-backed node resolution

Success criterion:

- scene graph captures motion and visibility changes that static initial setup misses

### Phase 3: Add UniGoal-style navigation backend

Goal:

- better long-horizon movement / exploration

Changes:

- adapt OmniGibson observations to UniGoal-style planner inputs
- port or wrap FMM / frontier exploration pieces

Success criterion:

- navigation backend can be swapped by config

### Phase 4: Planner and eval upgrades

Goal:

- make scene graph actually useful for safety-aware planning

Changes:

- add prompt context from graph
- log graph evolution
- optionally evaluate graph-grounded awareness


## 12. Suggested File-Level Roadmap

### First wave

- `og_ego_prim/primitives/ego_primitives.py`
- `og_ego_prim/primitives/executor.py`
- `og_ego_prim/benchmark/online_benchmark.py`
- new `og_ego_prim/navigation/`

### Second wave

- new `og_ego_prim/perception/`
- new `og_ego_prim/scene_graph/`
- `og_ego_prim/models/plan_agent.py`

### Third wave

- config plumbing
- logging / debug views
- optional adapters for UniGoal / SAMJAM components


## 13. Validation Strategy

Use a narrow validation ladder.

### 13.1 Navigation-only

Test plan:

```text
NAVIGATE_TO(countertop.n.01_1)
DONE
```

### 13.2 Navigation + manipulation

Test plan:

```text
NAVIGATE_TO(countertop.n.01_1)
PLACE_ON_TOP(electric_kettle.n.01_1, countertop.n.01_1)
DONE
```

### 13.3 Scene graph sanity

For a task like:

- `clean_a_kitchen_sink__with_electric_kettle`

check whether the graph can expose:

- kettle visible on sink
- sponge visible on countertop
- sink currently stained

### 13.4 Safety usefulness

Verify whether planner changes its order after dynamic scene context is injected:

- move electric device away first
- then wipe / soak / clean


## 14. Open Questions

These should be answered before deep implementation.

1. Should navigation be base-only, or must the arm posture be coordinated during movement?
2. What observation stream is available from OmniGibson during online benchmark for ego navigation?
3. Do we want scene graph updates from:
   - ego camera only
   - fixed viewer camera
   - multi-view sweep
4. Is the first target only benchmark evaluation, or also interactive demo / visualization?


## 15. Concrete Recommendation

The first implementation target should be:

> `Phase 0 + Phase 1`, and no more.

That means:

- make `NAVIGATE_TO` real
- add a minimal hybrid scene graph
- keep canonical IDs from `object_scope`
- keep evaluator fully simulator-backed

Only after that is stable should we pull in:

- UniGoal-style exploration logic
- SAMJAM-style dynamic perception

This keeps the migration incremental, debuggable, and aligned with the current `IS-Bench` codebase.
