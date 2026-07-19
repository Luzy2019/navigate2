import json
import os
import re
import shutil
import subprocess
import time

import numpy as np
from PIL import Image, ImageDraw

from og_ego_prim.benchmark.tracker import EvalTracker
from og_ego_prim.domain import Action


class OnlineEvalTracker(EvalTracker):

    def __init__(self, scene_graph_history_interval: int = 10, video_fps: float = 1.0):
        super().__init__()

        self.plans = []
        self.raw_outputs = []
        self.awareness = None
        self.caption = None
        self.primitive_type = None

        self.goal_condition = {}
        self.termination = None

        self.error_stack = []
        self.execution_diagnostics = []
        self.risk_evaluations = []
        # Legacy report consumers expect this list; keep it as a projection of
        # the richer action-level evaluations rather than a second decision path.
        self.risk_predictions = []
        self.risk_predictor = None
        self.runtime_modules = None
        self.planner_episode = None
        self.latest_scene_graph = None
        self.scene_graph_history = []
        self._scene_graph_update_count = 0
        self.scene_graph_history_interval = int(scene_graph_history_interval)
        if self.scene_graph_history_interval <= 0:
            raise ValueError('scene_graph.history_interval must be greater than zero')
        self.video_cache = []
        self.video_fps = float(video_fps)
        if self.video_fps <= 0:
            raise ValueError('artifacts.video_fps must be greater than zero')
        # Latency is collected only by online runs.  Keeping raw samples makes
        # the per-task report useful while the batch metric can calculate a
        # mean across tasks without re-running the simulator.
        self._latency_started_at = time.perf_counter()
        self.latency = {
            'graph_construction': [],
            'risk_prediction': [],
            'planning': [],
            'action_execution': [],
            'total': [],
            'run_elapsed_seconds': None,
        }
        self._pending_planning_latency = 0.0
    
    def track_plan(self, **kwargs):
        self.plans.append(dict(**kwargs))

    @staticmethod
    def _action_key(action):
        text = str(action).strip()
        try:
            return Action.from_raw(text).to_legacy_plan().lower()
        except (TypeError, ValueError):
            return re.sub(r'\s+', '', text).lower()

    def mark_plan_runtime(self, action, **updates):
        normalized = self._action_key(action)
        for record in reversed(self.plans):
            planned = self._action_key(record.get('plan', {}).get('action', ''))
            if planned == normalized and record.get('runtime_finalized') is not True:
                record.update(updates)
                record['runtime_finalized'] = True
                return record
        return None
    
    def track_raw_output(self, **kwargs):
        self.raw_outputs.append(dict(**kwargs))

    def track_error(self, **kwargs):
        self.error_stack.append(dict(**kwargs))

    def track_execution_diagnostic(self, diagnostic):
        if diagnostic is not None:
            self.execution_diagnostics.append(diagnostic)

    def track_risk_evaluation(self, action, evaluation):
        payload = (
            evaluation.to_dict()
            if hasattr(evaluation, 'to_dict')
            else dict(evaluation)
        )
        self.risk_evaluations.append({
            'action': action,
            'evaluation': payload,
        })
        self.risk_predictions.append({
            'action': action,
            'predictions': list(payload.get('hazards') or payload.get('predictions') or []),
            'decision': payload.get('decision'),
        })

    def track_risk_prediction(self, action, prediction):
        """Compatibility wrapper for earlier tracker callers."""
        self.track_risk_evaluation(action, prediction)

    def track_process_safety_goal_condition(self, **kwargs):
        if 'process_safety_goal_condition' not in self.goal_condition:
            self.goal_condition['process_safety_goal_condition'] = []
        self.goal_condition['process_safety_goal_condition'].append(dict(**kwargs))
    
    def track_termination_safety_goal_condition(self, **kwargs):
        if 'termination_safety_goal_condition' not in self.goal_condition:
            self.goal_condition['termination_safety_goal_condition'] = []
        self.goal_condition['termination_safety_goal_condition'].append(dict(**kwargs))
    
    def track_execution_goal_condition(self, **kwargs):
        self.goal_condition['execution_goal_condition'] = dict(**kwargs)
    
    def track_awareness(self, **kwargs):
        self.awareness = dict(**kwargs)
    
    def track_caption(self, **kwargs):
        self.caption = dict(**kwargs)
        
    def track_termination(self, **kwargs):
        termination = dict(**kwargs)
        termination.setdefault('type', 'RuntimeTermination')
        termination.setdefault('msg', str(termination.get('reason') or 'terminated'))
        self.termination = termination

    def track_scene_graph(self, snapshot, force=False):
        if hasattr(snapshot, 'to_dict'):
            snapshot = snapshot.to_dict()

        self.latest_scene_graph = snapshot
        if force or self._scene_graph_update_count % self.scene_graph_history_interval == 0:
            self.scene_graph_history.append(snapshot)
        self._scene_graph_update_count += 1

    def track_latency(self, name: str, seconds: float) -> None:
        if name not in (
            'graph_construction',
            'risk_prediction',
            'planning',
            'action_execution',
            'total',
        ):
            raise ValueError(f'unknown latency metric: {name}')
        self.latency[name].append(max(float(seconds), 0.0))

    def track_planning_latency(self, seconds: float) -> None:
        seconds = max(float(seconds), 0.0)
        self.track_latency('planning', seconds)
        self._pending_planning_latency += seconds

    def consume_planning_latency(self) -> float:
        seconds = self._pending_planning_latency
        self._pending_planning_latency = 0.0
        return seconds

    @staticmethod
    def _latency_summary(samples):
        values = [float(value) for value in samples]
        return {
            'count': len(values),
            'total_seconds': sum(values),
            'average_seconds': sum(values) / len(values) if values else 0.0,
        }

    def finalize_latency(self) -> None:
        if self.latency['run_elapsed_seconds'] is None:
            self.latency['run_elapsed_seconds'] = max(
                time.perf_counter() - self._latency_started_at, 0.0
            )

    def latency_report(self):
        self.finalize_latency()
        report = {
            'graph_construction': self._latency_summary(self.latency['graph_construction']),
            'risk_prediction': self._latency_summary(self.latency['risk_prediction']),
            'planning': self._latency_summary(self.latency['planning']),
            'action_execution': self._latency_summary(self.latency['action_execution']),
            'total': self._latency_summary(self.latency['total']),
            # Starts after simulator environment creation and excludes artifact
            # encoding performed after evaluation.
            'run_elapsed_seconds': self.latency['run_elapsed_seconds'],
        }
        report['graph_construction_latency'] = report['graph_construction']['average_seconds']
        report['risk_prediction_latency'] = report['risk_prediction']['average_seconds']
        report['total_latency'] = report['total']['average_seconds']
        report['definitions'] = {
            'graph_construction': 'real scene graph builds; skipped backend refreshes are excluded',
            'risk_prediction': 'RiskPredictor.predict wall time per candidate action',
            'planning': 'time spent advancing the planner iterator, including online model calls',
            'action_execution': 'runtime gate, safety evaluation, primitive execution, and post-action graph refresh',
            'total': 'planning plus action_execution for one high-level plan',
            'run_elapsed_seconds': 'online benchmark time after environment creation and before artifact encoding',
        }
        return report

    def track_video_rgb(self, rgb):
        frame = np.asarray(rgb)
        if frame.ndim != 3 or frame.shape[2] not in (3, 4):
            raise ValueError(f'video frame must have shape HxWx3 or HxWx4, got {frame.shape}')
        if frame.dtype != np.uint8:
            frame = np.clip(frame, 0, 255).astype(np.uint8)
        self.video_cache.append(np.ascontiguousarray(frame[:, :, :3]))

    def track_video_observations(self, observations, label=None, columns=3, frame_width=1920):
        if not observations:
            return

        images = [Image.fromarray(np.asarray(obs)[:, :, :3]).convert('RGB') for obs in observations]
        columns = min(columns, len(images))
        rows = (len(images) + columns - 1) // columns
        tile_width = frame_width // columns
        tile_height = round(tile_width * images[0].height / images[0].width)
        header_height = 40 if label else 0
        frame = Image.new(
            'RGB',
            (tile_width * columns, tile_height * rows + header_height),
            color='black',
        )

        for i, image in enumerate(images):
            image = image.resize((tile_width, tile_height), Image.Resampling.LANCZOS)
            frame.paste(
                image,
                ((i % columns) * tile_width, (i // columns) * tile_height + header_height),
            )

        if label:
            ImageDraw.Draw(frame).text((12, 12), str(label), fill='white')

        self.track_video_rgb(np.asarray(frame))

    def save_video(self, save_path: str):
        if not self.video_cache:
            return None

        if os.path.isdir(save_path):
            save_path = os.path.join(save_path, 'video.mp4')
        else:
            assert save_path.endswith('.mp4')
            os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)

        ffmpeg = shutil.which('ffmpeg')
        if ffmpeg is None:
            raise RuntimeError('ffmpeg is required to save evaluation videos')

        height, width = self.video_cache[0].shape[:2]
        if any(frame.shape[:2] != (height, width) for frame in self.video_cache):
            raise ValueError('all evaluation video frames must have the same dimensions')

        command = [
            ffmpeg,
            '-y',
            '-loglevel', 'error',
            '-f', 'rawvideo',
            '-pix_fmt', 'rgb24',
            '-s', f'{width}x{height}',
            '-r', str(self.video_fps),
            '-i', '-',
            '-an',
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            save_path,
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            for frame in self.video_cache:
                process.stdin.write(frame.tobytes())
            process.stdin.close()
            stderr = process.stderr.read().decode('utf-8', errors='replace')
            return_code = process.wait()
        except Exception:
            process.kill()
            process.wait()
            raise

        if return_code != 0:
            raise RuntimeError(f'ffmpeg failed to save evaluation video: {stderr.strip()}')

        video_info = {
            'path': os.path.basename(save_path),
            'kind': 'step_observation',
            'fps': self.video_fps,
            'frames': len(self.video_cache),
            'width': width,
            'height': height,
        }
        self.video_cache.clear()
        return video_info

    def save_tracking(self, save_path: str):
        save_dir = os.path.dirname(save_path)
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

        planner_entries = ()
        if self.planner_episode is not None:
            snapshot = getattr(self.planner_episode, 'snapshot', None)
            planner_entries = snapshot() if callable(snapshot) else self.planner_episode

        report = {
            'task': self.task,
            'scene': self.scene,
            'model': self.model,
            'primitive_type': self.primitive_type,
            'awareness': self.awareness,
            'plans': [
                {
                    'step': plan.get('step'),
                    'action': plan.get('plan', {}).get('action'),
                    'caution': plan.get('plan', {}).get('caution'),
                    'executed': plan.get('executed'),
                    'succeeded': plan.get('succeeded'),
                    'runtime_decision': plan.get('runtime_decision'),
                    'runtime_reason': plan.get('runtime_reason'),
                }
                for plan in self.plans
            ],
            'termination': self.termination,
            'error_stack': self.error_stack,
            'execution_diagnostics': self.execution_diagnostics,
            'risk_evaluations': self.risk_evaluations,
            'risk_predictions': self.risk_predictions,
            'risk_predictor': self.risk_predictor,
            'runtime_modules': dict(self.runtime_modules or {}),
            'planner_episode': [
                entry.to_dict() if hasattr(entry, 'to_dict') else dict(entry)
                for entry in planner_entries
            ],
            'latency': self.latency_report(),
            'latest_scene_graph': self.latest_scene_graph,
            'scene_graph_history': self.scene_graph_history,
        }

        if 'process_safety_goal_condition' in self.goal_condition:
            report['process_safety_goal_condition'] = self.goal_condition['process_safety_goal_condition']
        if 'termination_safety_goal_condition' in self.goal_condition:
            report['termination_safety_goal_condition'] = self.goal_condition['termination_safety_goal_condition']
        if 'execution_goal_condition' in self.goal_condition:
            report['execution_goal_condition'] = self.goal_condition['execution_goal_condition']

        report['raw_outputs'] = self.raw_outputs

        try:
            video_info = self.save_video(save_dir or '.')
            if video_info is not None:
                report['video'] = video_info
        except Exception as e:
            self.track_error(
                action='save_video',
                err_type=e.__class__.__name__,
                msg=str(e),
            )
            report['error_stack'] = self.error_stack
        
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=4, ensure_ascii=False)
