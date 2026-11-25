"""Detect footplants and bake two-bone IK into the original leg channels."""

from bisect import bisect_left, bisect_right
from math import sqrt
from statistics import median

from mathutils import Vector


SIDES = ("Left", "Right")


def leg_poses(bones, keys, side):
    names = ("Hips", side + "UpLeg", side + "Leg", side + "Foot")
    rest = [bones[name].bone.matrix_local for name in names]
    relative = [a.inverted() @ b for a, b in zip(rest, rest[1:])]
    poses = []
    for rotations in zip(*(keys[name] for name in names)):
        matrices = [rest[0] @ rotations[0].to_matrix().to_4x4()]
        for transform, rotation in zip(relative, rotations[1:]):
            matrices.append(matrices[-1] @ transform @ rotation.to_matrix().to_4x4())
        poses.append(matrices)
    return poses, [matrix.to_quaternion() for matrix in relative]


def contact_intervals(toes, visible, times, height, floor, mode):
    if mode == "BOTH":
        return [(0, len(times))]
    planted = []
    for index, time in enumerate(times):
        left = bisect_left(times, time - 0.08)
        right = bisect_right(times, time + 0.08) - 1
        elapsed = times[right] - times[left]
        speed = (toes[right] - toes[left]).length / elapsed if elapsed > 0 else 0
        planted.append(visible[index] and toes[index].z <= floor + height * 0.045
                       and speed <= height * 0.35)
    intervals = []
    start = None
    for index, contact in enumerate([*planted, False]):
        if contact and start is None:
            start = index
        elif not contact and start is not None:
            if times[index - 1] - times[start] >= 0.12:
                intervals.append((start, index))
            start = None
    return intervals


def foot_targets(toes, poses, times, intervals, floor):
    targets = [None] * len(times)
    for start, end in intervals:
        anchor = Vector((median(p.x for p in toes[start:end]),
                         median(p.y for p in toes[start:end]), floor))
        representative = min(range(start, end), key=lambda index: (toes[index].xy - anchor.xy).length_squared)
        orientation = poses[representative][-1].to_quaternion()
        for index in range(start, end):
            time = times[index]
            enter = (time - times[start - 1]) / 0.14 if start else 1.0
            leave = (times[end] - time) / 0.14 if end < len(times) else 1.0
            weight = min(enter, leave, 1.0)
            weight = weight * weight * (3 - 2 * weight)
            targets[index] = (anchor, orientation, weight)
    return targets


def solve_leg(pose, relative, toe, foot_rotation, toe_offset, hip_shift):
    pelvis, upper, lower, foot = pose
    hip = upper.translation + hip_shift
    knee = lower.translation + hip_shift
    ankle = foot.translation + hip_shift
    thigh_length = (knee - hip).length
    shin_length = (ankle - knee).length
    direction = toe - foot_rotation @ toe_offset - hip
    requested = direction.length
    if requested < 1e-8:
        direction = ankle - hip
    direction.normalize()

    # Keep the original knee bend plane and clamp unreachable targets to the leg's reach.
    margin = (thigh_length + shin_length) * 1e-5
    distance = max(abs(thigh_length - shin_length) + margin,
                   min(requested, thigh_length + shin_length - margin))
    bend = knee - hip
    pole = bend - direction * bend.dot(direction)
    if pole.length < margin:
        forward = pelvis.to_quaternion() @ Vector((0, 0, 1))
        pole = forward - direction * forward.dot(direction)
        if pole.length < margin:
            pole = direction.orthogonal()
    pole.normalize()
    along = (thigh_length ** 2 - shin_length ** 2 + distance ** 2) / (2 * distance)
    height = sqrt(max(0, thigh_length ** 2 - along ** 2))
    target_knee = hip + direction * along + pole * height
    target_ankle = hip + direction * distance

    upper_rotation = (knee - hip).rotation_difference(target_knee - hip) @ upper.to_quaternion()
    lower_rotation = (ankle - knee).rotation_difference(target_ankle - target_knee) @ lower.to_quaternion()
    return (
        (pelvis.to_quaternion() @ relative[0]).inverted() @ upper_rotation,
        (upper_rotation @ relative[1]).inverted() @ lower_rotation,
        (lower_rotation @ relative[2]).inverted() @ foot_rotation,
    )


def lock_feet(bones, keys, samples, confidence, mode):
    times = [time for time, _ in samples]
    rest_floor = min(bones[side + "Foot"].bone.tail_local.z for side in SIDES)
    height = bones["Head"].bone.tail_local.z - rest_floor
    legs = {}
    for offset, side in enumerate(SIDES):
        poses, relative = leg_poses(bones, keys, side)
        toe_offset = Vector((0, bones[side + "Foot"].bone.length, 0))
        toes = [pose[-1] @ toe_offset for pose in poses]
        visible = [points is not None and all(points[joint + offset][3] >= confidence
                                              for joint in (25, 27, 31)) for _, points in samples]
        legs[side] = (poses, relative, toe_offset, toes, visible)

    # Use the lower envelope of confidently observed foot heights to recognize contact in
    # the input motion. The rig's rest toe height defines the output ground reference.
    elevations = sorted(toe.z for _, _, _, toes, visible in legs.values()
                        for toe, seen in zip(toes, visible) if seen)
    observed_floor = elevations[len(elevations) // 10] if elevations else rest_floor
    targets = {}
    for side, (poses, _, _, toes, visible) in legs.items():
        intervals = contact_intervals(toes, visible, times, height, observed_floor, mode)
        targets[side] = foot_targets(toes, poses, times, intervals, rest_floor)

    root_inverse = bones["Hips"].bone.matrix_local.to_quaternion().inverted()
    locations = []
    for index in range(len(times)):
        # Lower or raise the hips with the contacting feet to preserve their reach.
        active = {side: targets[side][index] for side in SIDES if targets[side][index] is not None}
        total_weight = sum(weight for _, _, weight in active.values())
        lift = sum((anchor.z - legs[side][3][index].z) * weight
                   for side, (anchor, _, weight) in active.items()) / max(1.0, total_weight)
        hip_shift = Vector((0, 0, lift))
        locations.append(root_inverse @ hip_shift)
        for side, (anchor, orientation, weight) in active.items():
            poses, relative, toe_offset, toes, _ = legs[side]
            pose = poses[index]
            toe = (toes[index] + hip_shift).lerp(anchor, weight)
            rotation = pose[-1].to_quaternion().slerp(orientation, weight)
            solved = solve_leg(pose, relative, toe, rotation, toe_offset, hip_shift)
            for name, rotation in zip(("UpLeg", "Leg", "Foot"), solved):
                keys[side + name][index] = rotation
    # The corrected and uncorrected intervals must share the same quaternion hemisphere.
    for side in SIDES:
        for name in ("UpLeg", "Leg", "Foot"):
            rotations = keys[side + name]
            for index in range(1, len(rotations)):
                rotations[index].make_compatible(rotations[index - 1])
    return locations
