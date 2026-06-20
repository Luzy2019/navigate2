import argparse
import os
from pathlib import Path
import select
import sys
import termios
import time
import tty


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from og_ego_prim.utils.omnigibson_runtime import maybe_reexec_with_omnigibson_python

maybe_reexec_with_omnigibson_python()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Launch an OmniGibson IS-Bench task and control the robot base from the keyboard."
    )
    parser.add_argument("--task", default="store_a_tennis_ball")
    parser.add_argument("--scene", default="Rs_int")
    parser.add_argument(
        "--online-object-sampling",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable online object sampling from the task config.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without the viewer. Useful only for checking control logic.",
    )
    parser.add_argument(
        "--lin-vel",
        type=float,
        default=0.5,
        help="Linear base velocity command for W/S.",
    )
    parser.add_argument(
        "--ang-vel",
        type=float,
        default=0.8,
        help="Angular base velocity command for A/D.",
    )
    parser.add_argument(
        "--step-hz",
        type=float,
        default=20.0,
        help="Maximum control loop frequency.",
    )
    parser.add_argument(
        "--keymap",
        choices=("ijkl", "wasd", "arrows"),
        default="ijkl",
        help="Keyboard layout for robot base control. Default avoids common viewer WASD shortcuts.",
    )
    parser.add_argument(
        "--sticky",
        action="store_true",
        help="Keep the previous command until SPACE is pressed.",
    )
    parser.add_argument(
        "--viewer-camera-teleop",
        action="store_true",
        help="Enable OmniGibson viewer camera teleoperation.",
    )
    parser.add_argument(
        "--disable-gpu-dynamics",
        action="store_true",
        help="Disable OmniGibson GPU dynamics before environment creation.",
    )
    return parser.parse_args()


class RawKeyboard:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.fd = None
        self.old_settings = None

    def __enter__(self):
        if not self.enabled:
            return self
        if not sys.stdin.isatty():
            raise RuntimeError("Keyboard control requires an interactive terminal.")

        self.fd = sys.stdin.fileno()
        self.old_settings = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.enabled and self.old_settings is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)

    def read_key(self, timeout):
        if not self.enabled:
            time.sleep(timeout)
            return None

        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if not readable:
            return None

        key = sys.stdin.read(1)
        if key != "\x1b":
            return key

        # Best-effort arrow-key parsing: ESC [ A/B/C/D.
        suffix = ""
        while select.select([sys.stdin], [], [], 0)[0]:
            suffix += sys.stdin.read(1)
            if len(suffix) >= 2:
                break
        return key + suffix


def key_bindings_for(keymap):
    if keymap == "wasd":
        return {
            "forward": {"w", "W"},
            "backward": {"s", "S"},
            "turn_left": {"a", "A"},
            "turn_right": {"d", "D"},
            "stop": {" ", ";"},
        }
    if keymap == "arrows":
        return {
            "forward": {"\x1b[A"},
            "backward": {"\x1b[B"},
            "turn_left": {"\x1b[D"},
            "turn_right": {"\x1b[C"},
            "stop": {" ", ";"},
        }
    return {
        "forward": {"i", "I"},
        "backward": {"k", "K"},
        "turn_left": {"j", "J"},
        "turn_right": {"l", "L"},
        "stop": {" ", ";"},
    }


def key_label_for(keymap):
    if keymap == "wasd":
        return {
            "forward": "W",
            "backward": "S",
            "turn_left": "A",
            "turn_right": "D",
        }
    if keymap == "arrows":
        return {
            "forward": "Up",
            "backward": "Down",
            "turn_left": "Left",
            "turn_right": "Right",
        }
    return {
        "forward": "I",
        "backward": "K",
        "turn_left": "J",
        "turn_right": "L",
    }


def print_help(robot, keymap):
    labels = key_label_for(keymap)
    base_idx = robot.controller_action_idx.get("base")
    print()
    print("Keyboard control is ready. Keep this terminal focused.")
    print(f"  {labels['forward']:<11}: forward")
    print(f"  {labels['backward']:<11}: backward")
    print(f"  {labels['turn_left']:<11}: turn left")
    print(f"  {labels['turn_right']:<11}: turn right")
    print("  ; / SPACE   : stop")
    print("  H           : help")
    print("  X or Ctrl-C : quit")
    print()
    print(f"keymap={keymap}, robot={robot.name}, action_dim={robot.action_dim}, base_idx={base_idx}")
    print()
    sys.stdout.flush()


def command_from_key(key, keymap, lin_vel, ang_vel):
    bindings = key_bindings_for(keymap)
    if key in bindings["forward"]:
        return lin_vel, 0.0
    if key in bindings["backward"]:
        return -lin_vel, 0.0
    if key in bindings["turn_left"]:
        return 0.0, ang_vel
    if key in bindings["turn_right"]:
        return 0.0, -ang_vel
    if key in bindings["stop"]:
        return 0.0, 0.0
    return None


def make_base_action(robot, linear_velocity, angular_velocity, torch_module):
    action = torch_module.zeros(robot.action_dim)
    base_idx = robot.controller_action_idx.get("base")
    if base_idx is None:
        raise RuntimeError(f'Robot "{robot.name}" does not expose a base controller.')

    base_action = action[base_idx]
    if base_action.numel() == 2:
        base_action[0] = linear_velocity
        base_action[1] = angular_velocity
    elif base_action.numel() == 3:
        base_action[0] = linear_velocity
        base_action[1] = 0.0
        base_action[2] = angular_velocity
    else:
        raise RuntimeError(
            f"Unsupported base action size {int(base_action.numel())}; expected 2 or 3."
        )

    action[base_idx] = base_action
    return action


def main():
    args = parse_args()

    if args.headless:
        os.environ["OMNIGIBSON_HEADLESS"] = "1"

    # Isaac Sim / Kit may try to parse this script's CLI flags. Keep our flags
    # out of sys.argv before importing OmniGibson.
    sys.argv = [sys.argv[0]]

    from og_ego_prim.utils.monkey_patch import add_monkey_patch

    add_monkey_patch()

    import omnigibson as og
    from omnigibson.macros import gm
    import torch

    from og_ego_prim.benchmark import build_benchmark

    gm.ENABLE_OBJECT_STATES = True
    gm.USE_GPU_DYNAMICS = not args.disable_gpu_dynamics

    benchmark = None
    try:
        benchmark = build_benchmark(
            task=args.task,
            scene=args.scene,
            ego_view=False,
            draw_bbox_2d=False,
            use_initial_setup=False,
            use_self_caption=False,
            online_object_sampling=args.online_object_sampling,
            debug=False,
            eval_process_safety=False,
            eval_termination_safety=False,
            eval_awareness=False,
            eval_execution=False,
        )

        env = benchmark.env
        if not env.robots:
            raise RuntimeError("The loaded environment has no robot to control.")

        robot = env.robots[0]
        if args.viewer_camera_teleop and not gm.HEADLESS:
            og.sim.enable_viewer_camera_teleoperation()

        print_help(robot, args.keymap)

        linear_velocity = 0.0
        angular_velocity = 0.0
        loop_dt = 1.0 / max(args.step_hz, 1e-6)

        with RawKeyboard() as keyboard:
            while True:
                loop_start = time.time()
                key = keyboard.read_key(timeout=0.0)

                if key in {"x", "X", "\x03"}:
                    print("Exiting keyboard control.")
                    break
                if key in {"h", "H"}:
                    print_help(robot, args.keymap)

                command = command_from_key(key, args.keymap, args.lin_vel, args.ang_vel) if key else None
                if command is not None:
                    linear_velocity, angular_velocity = command
                elif not args.sticky:
                    linear_velocity, angular_velocity = 0.0, 0.0

                action = make_base_action(robot, linear_velocity, angular_velocity, torch)
                env.step(action)

                elapsed = time.time() - loop_start
                if elapsed < loop_dt:
                    time.sleep(loop_dt - elapsed)
    finally:
        if benchmark is not None:
            try:
                og.clear()
            except Exception:
                pass


if __name__ == "__main__":
    main()
