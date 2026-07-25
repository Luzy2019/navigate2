from .factory import build_perception_backend


def __getattr__(name):
    if name == "ManualCorrectedBackend":
        from .manual_corrected import ManualCorrectedBackend

        return ManualCorrectedBackend
    if name == "SAMJAMSAM2Backend":
        from .samjam_sam2 import SAMJAMSAM2Backend

        return SAMJAMSAM2Backend
    if name == "SAMJAMUniGoalBackend":
        from .samjam_unigoal import SAMJAMUniGoalBackend

        return SAMJAMUniGoalBackend
    if name == "UniGoalGroundedSAMBackend":
        from .unigoal_grounded_sam import UniGoalGroundedSAMBackend

        return UniGoalGroundedSAMBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ManualCorrectedBackend",
    "SAMJAMSAM2Backend",
    "SAMJAMUniGoalBackend",
    "UniGoalGroundedSAMBackend",
    "build_perception_backend",
]
