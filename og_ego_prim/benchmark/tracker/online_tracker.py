import json
import os
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageDraw

from og_ego_prim.benchmark.tracker import EvalTracker


class OnlineEvalTracker(EvalTracker):

    def __init__(self):
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
        self.latest_scene_graph = None
        self.scene_graph_history = []
        self.scene_graph_history_interval = int(os.environ.get('ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL', '10'))
        if self.scene_graph_history_interval <= 0:
            raise ValueError('ISBENCH_SCENE_GRAPH_HISTORY_INTERVAL must be greater than zero')
        self.video_cache = []
        self.video_fps = float(os.environ.get('ISBENCH_VIDEO_FPS', '1'))
        if self.video_fps <= 0:
            raise ValueError('ISBENCH_VIDEO_FPS must be greater than zero')
    
    def track_plan(self, **kwargs):
        self.plans.append(dict(**kwargs))
    
    def track_raw_output(self, **kwargs):
        self.raw_outputs.append(dict(**kwargs))

    def track_error(self, **kwargs):
        self.error_stack.append(dict(**kwargs))

    def track_execution_diagnostic(self, diagnostic):
        if diagnostic is not None:
            self.execution_diagnostics.append(diagnostic)

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
        self.termination = dict(**kwargs)

    def track_scene_graph(self, snapshot, force=False):
        if hasattr(snapshot, 'to_dict'):
            snapshot = snapshot.to_dict()

        self.latest_scene_graph = snapshot
        global_step_index = snapshot.get('metadata', {}).get('global_step_index', 0)
        if force or global_step_index % self.scene_graph_history_interval == 0:
            self.scene_graph_history.append(snapshot)

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
        os.makedirs(save_dir, exist_ok=True)

        report = {
            'task': self.task,
            'scene': self.scene,
            'model': self.model,
            'primitive_type': self.primitive_type,
            'awareness': self.awareness,
            'plans': [
                {
                    'step': plan['step'], 
                    'action': plan['plan']['action'], 
                    'caution': plan['plan']['caution']
                }
                for plan in self.plans
            ],
            'termination': self.termination,
            'error_stack': self.error_stack,
            'execution_diagnostics': self.execution_diagnostics,
        }

        if self.latest_scene_graph is not None:
            report['latest_scene_graph'] = self.latest_scene_graph
        if self.scene_graph_history:
            report['scene_graph_history'] = self.scene_graph_history

        if 'process_safety_goal_condition' in self.goal_condition:
            report['process_safety_goal_condition'] = self.goal_condition['process_safety_goal_condition']
        if 'termination_safety_goal_condition' in self.goal_condition:
            report['termination_safety_goal_condition'] = self.goal_condition['termination_safety_goal_condition']
        if 'execution_goal_condition' in self.goal_condition:
            report['execution_goal_condition'] = self.goal_condition['execution_goal_condition']

        report['raw_outputs'] = self.raw_outputs

        try:
            video_info = self.save_video(save_dir)
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
