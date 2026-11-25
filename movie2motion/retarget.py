"""Convert MediaPipe landmarks to an in-place Action on a Pinnacle-Rigger armature."""

from bisect import bisect_left, bisect_right
from math import atan2, exp, isfinite

import bpy
from mathutils import Matrix, Quaternion, Vector

from .foot_contact import lock_feet


PARENTS = {
    "Hips": None,
    "Spine": "Hips",
    "Spine1": "Spine",
    "Spine2": "Spine1",
    "Neck": "Spine2",
    "Head": "Neck",
}
for side in ("Left", "Right"):
    for name, parent in (
        ("Shoulder", "Spine2"), ("Arm", side + "Shoulder"),
        ("ForeArm", side + "Arm"), ("Hand", side + "ForeArm"),
        ("UpLeg", "Hips"), ("Leg", side + "UpLeg"), ("Foot", side + "Leg"),
    ):
        PARENTS[side + name] = parent

INHERITED = {"Neck", "LeftShoulder", "RightShoulder"}

# Twist is solved from a reference direction measured in the rest pose. The rig faces
# armature -Y: elbows bend the forearm forward, knees bend the shin backward, and in the
# T-pose the index finger side of each palm points forward.
FRONT = Vector((0, -1, 0))
TWIST_LIMITS = {"Arm": 1.92, "UpLeg": 1.05, "Hand": 2.09}  # radians from the swing-only roll
BEND_RANGE = (0.14, 0.44)  # radians of elbow or knee bend over which twist fades in


def rig_bones(armature):
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Pinnacle-Rigger のアーマチュアを選択してください。")
    bones = {name: armature.pose.bones.get("mixamorig:" + name) for name in PARENTS}
    missing = [name for name, bone in bones.items() if bone is None]
    if missing:
        raise ValueError("必要なボーンがありません: " + ", ".join(missing))
    for name, parent in PARENTS.items():
        if bones[name].parent != (bones[parent] if parent else None):
            raise ValueError("対応していないボーン階層: " + bones[name].name)
    return bones


def body_basis(left, up):
    if left.length < 1e-5 or up.length < 1e-5:
        return None
    x = left.normalized()
    y = up.normalized().cross(x)
    if y.length < 0.05:
        return None
    y.normalize()
    return Matrix((x, y, x.cross(y))).transposed().to_quaternion()


def point_at(points, index, confidence):
    if points is None:
        return None
    x, y, z, visibility = points[index]
    if visibility >= confidence and all(isfinite(value) for value in (x, y, z)):
        return Vector((x, z, -y))
    return None


def difference(tip, base):
    if tip is not None and base is not None:
        return tip - base
    return None


def midpoint(a, b):
    if a is not None and b is not None:
        return (a + b) * 0.5
    return None


def observations(p):
    up = difference(midpoint(p[11], p[12]), midpoint(p[23], p[24]))
    pelvis = chest = None
    if up is not None:
        pelvis = body_basis(p[23] - p[24], up)
        chest = body_basis(p[11] - p[12], up)
    head = None
    if all(p[index] is not None for index in (0, 7, 8)):
        left = p[7] - p[8]
        head = body_basis(left, left.cross(midpoint(p[7], p[8]) - p[0]))
    directions = {}
    for side, offset in (("Left", 0), ("Right", 1)):
        for name, base, tip in (
            ("Arm", 11, 13), ("ForeArm", 13, 15), ("UpLeg", 23, 25),
            ("Leg", 25, 27), ("Foot", 27, 31),
        ):
            directions[side + name] = difference(p[tip + offset], p[base + offset])
        directions[side + "Hand"] = difference(midpoint(p[17 + offset], p[19 + offset]), p[15 + offset])
        # Index knuckle minus pinky knuckle gives the palm's roll around the hand axis.
        directions[side + "HandSide"] = difference(p[19 + offset], p[17 + offset])
    return pelvis, chest, head, directions


def smoothstep(value, low, high):
    t = min(1.0, max(0.0, (value - low) / (high - low)))
    return t * t * (3 - 2 * t)


def roll_toward(rotation, axis, local_reference, desired, limit, weight):
    """Rotate around ``axis`` so ``rotation @ local_reference`` points toward ``desired``."""
    if desired is None or weight <= 0:
        return rotation
    current = rotation @ local_reference
    current -= axis * current.dot(axis)
    wanted = desired - axis * desired.dot(axis)
    if current.length < 1e-5 or wanted.length < 1e-5:
        return rotation
    current.normalize()
    wanted.normalize()
    sine = axis.dot(current.cross(wanted))
    cosine = max(-1.0, min(1.0, current.dot(wanted)))
    angle = max(-limit, min(limit, atan2(sine, cosine)))
    return Quaternion(axis, angle * weight) @ rotation


def twist_references(bones):
    """Per-bone, rest-local vectors that should follow the observed bend or palm plane."""
    references = {}
    for side in ("Left", "Right"):
        for name, hint in (("Arm", FRONT), ("UpLeg", -FRONT)):
            bone = bones[side + name].bone
            direction = (bone.tail_local - bone.head_local).normalized()
            normal = direction.cross(hint)
            if normal.length > 1e-5:
                rest = bone.matrix_local.to_quaternion()
                references[side + name] = rest.inverted() @ normal.normalized()
        hand = bones[side + "Hand"].bone
        references[side + "Hand"] = hand.matrix_local.to_quaternion().inverted() @ FRONT
    return references


def filter_track(track, times, smoothing):
    # Fill short, bounded gaps only. Long occlusions split the filter's windows.
    track = list(track)
    observed = [index for index, point in enumerate(track) if point is not None]
    for left, right in zip(observed, observed[1:]):
        duration = times[right] - times[left]
        if right - left > 1 and duration <= 0.21:
            for index in range(left + 1, right):
                factor = (times[index] - times[left]) / duration
                track[index] = track[left].lerp(track[right], factor)

    result = [None] * len(track)
    start = 0
    while start < len(track):
        if track[start] is None:
            start += 1
            continue
        end = start + 1
        while end < len(track) and track[end] is not None:
            end += 1
        # A symmetric Gaussian uses frames on both sides of the current time.
        for index in range(start, end):
            time = times[index]
            left = bisect_left(times, time - 3 * smoothing, start, index)
            right = bisect_right(times, time + 3 * smoothing, index + 1, end)
            position = Vector((0, 0, 0))
            total = 0.0
            for neighbor in range(left, right):
                offset = (times[neighbor] - time) / smoothing
                weight = exp(-0.5 * offset * offset)
                position += weight * track[neighbor]
                total += weight
            result[index] = position / total
        start = end
    return result


def solve_rotations(bones, samples, confidence, smoothing, twist=True):
    references = twist_references(bones)
    rest = {name: bone.bone.matrix_local.to_quaternion() for name, bone in bones.items()}
    relative = {
        name: rest[parent].inverted() @ rest[name] if parent else rest[name]
        for name, parent in PARENTS.items()
    }

    def across(segment):
        return bones["Left" + segment].bone.head_local - bones["Right" + segment].bone.head_local

    up = midpoint(bones["LeftArm"].bone.head_local, bones["RightArm"].bone.head_local)
    up -= midpoint(bones["LeftUpLeg"].bone.head_local, bones["RightUpLeg"].bone.head_local)
    rest_pelvis = body_basis(across("UpLeg"), up)
    rest_chest = body_basis(across("Arm"), up)
    if rest_pelvis is None or rest_chest is None:
        raise ValueError("レスト姿勢から胴体の向きを計算できません。")

    times = [seconds for seconds, _ in samples]
    landmarks = [[point_at(points, index, confidence) for index in range(33)] for _, points in samples]
    raw_observations = [observations(points) for points in landmarks]
    valid_count = sum(pelvis is not None for pelvis, _, _, _ in raw_observations)
    if not valid_count:
        raise ValueError("腰と肩が同時に検出できませんでした。全身が映る動画でお試しください。")
    frames = raw_observations
    if smoothing > 0:
        tracks = [filter_track(track, times, smoothing) for track in zip(*landmarks)]
        frames = (observations(points) for points in zip(*tracks))

    keys = {name: [] for name in PARENTS}
    previous = {name: Quaternion() for name in PARENTS}
    for pelvis, chest, head, directions in frames:
        pelvis_delta = pelvis @ rest_pelvis.inverted() if pelvis is not None else None
        chest_delta = chest @ rest_chest.inverted() if chest is not None else None
        absolute = {}
        for name, parent in PARENTS.items():
            baseline = absolute[parent] @ relative[name] if parent else relative[name]
            target = None
            if name == "Hips" and pelvis_delta is not None:
                target = pelvis_delta @ rest[name]
            elif name in ("Spine", "Spine1", "Spine2") and chest_delta is not None:
                # Blend the torso from the solved pelvis rotation toward the shoulder frame.
                lower = absolute["Hips"] @ rest["Hips"].inverted()
                weight = {"Spine": 0.33, "Spine1": 0.67, "Spine2": 1.0}[name]
                target = lower.slerp(chest_delta, weight) @ rest[name]
            elif name == "Head" and head is not None:
                # Pinnacle-Rigger's neutral head is upright and faces armature -Y.
                target = head @ rest[name]
            elif name in INHERITED:
                target = baseline
            else:
                direction = directions.get(name)
                if direction is not None and direction.length > 1e-5:
                    axis = direction.normalized()
                    swing = (baseline @ Vector((0, 1, 0))).rotation_difference(axis)
                    target = swing @ baseline
                    kind = name[4:] if name.startswith("Left") else name[5:]
                    if twist and name in references:
                        side = name[:-len(kind)]
                        if kind == "Hand":
                            desired_side, weight = directions.get(side + "HandSide"), 1.0
                        else:
                            child = directions.get(side + ("ForeArm" if kind == "Arm" else "Leg"))
                            desired_side = weight = None
                            if child is not None and child.length > 1e-5:
                                # The plane spanned by both segments fixes the hinge's roll.
                                desired_side = axis.cross(child.normalized())
                                weight = smoothstep(axis.angle(child.normalized(), 0.0), *BEND_RANGE)
                        if desired_side is not None:
                            target = roll_toward(target, axis, references[name], desired_side,
                                                 TWIST_LIMITS[kind], weight)
            local = previous[name]
            if target is not None:
                desired = baseline.inverted() @ target
                desired.make_compatible(local)
                local = desired
            if twist and name.endswith("Hand"):
                # Share the wrist roll with the forearm, as a real radius and ulna do, so
                # skin around a twist-less wrist does not collapse.
                forearm = name[:-4] + "ForeArm"
                roll = Quaternion((local.w, 0, local.y, 0))
                if roll.magnitude > 1e-6:
                    roll.normalize()
                    half = Quaternion().slerp(roll, 0.5)
                    shared = relative[name] @ half @ relative[name].inverted()
                    keys[forearm][-1] = keys[forearm][-1] @ shared
                    previous[forearm] = keys[forearm][-1]
                    local = half.inverted() @ local
            absolute[name] = baseline @ local
            previous[name] = local
            keys[name].append(local)
    return keys, valid_count


def write_curves(curves, path, frames, values):
    for index in range(len(values[0])):
        curve = curves.new(path, index=index)
        curve.keyframe_points.add(len(frames))
        coordinates = [value for frame, rotation in zip(frames, values)
                       for value in (frame, rotation[index])]
        curve.keyframe_points.foreach_set("co", coordinates)
        for key in curve.keyframe_points:
            key.interpolation = "LINEAR"
        curve.update()


def create_action(armature, samples, name, start_frame, scene_fps, confidence, smoothing, foot_contact="OFF"):
    bones = rig_bones(armature)
    keys, valid_count = solve_rotations(bones, samples, confidence, smoothing)
    locations = (lock_feet(bones, keys, samples, confidence, foot_contact)
                 if foot_contact != "OFF" else [(0, 0, 0)] * len(samples))
    frames = [start_frame + seconds * scene_fps for seconds, _ in samples]
    action = bpy.data.actions.new(name)
    slot = None
    try:
        if bpy.app.version >= (4, 4, 0):
            slot = action.slots.new(id_type="OBJECT", name=armature.name)
            strip = action.layers.new("Motion").strips.new(type="KEYFRAME")
            curves = strip.channelbag(slot, ensure=True).fcurves
        else:
            action.id_root = "OBJECT"
            curves = action.fcurves

        for bone_name, rotations in keys.items():
            bone = bones[bone_name]
            mode = bone.rotation_mode
            if mode == "QUATERNION":
                property_name = "rotation_quaternion"
                values = rotations
            elif mode == "AXIS_ANGLE":
                property_name = "rotation_axis_angle"
                values = [(rotation.angle, *rotation.axis) for rotation in rotations]
            else:
                property_name = "rotation_euler"
                values = []
                euler = bone.rotation_euler
                for rotation in rotations:
                    euler = rotation.to_euler(mode, euler)
                    values.append(euler)
            write_curves(curves, bone.path_from_id(property_name), frames, values)
        hips = bones["Hips"]
        write_curves(curves, hips.path_from_id("location"), frames, locations)
        action.use_fake_user = True
    except Exception:
        bpy.data.actions.remove(action)
        raise

    animation = armature.animation_data_create()
    previous = animation.action
    previous_slot = animation.action_slot if slot else None
    try:
        animation.action = action
        if slot:
            animation.action_slot = slot
    except Exception:
        animation.action = previous
        if previous_slot:
            animation.action_slot = previous_slot
        bpy.data.actions.remove(action)
        raise
    if previous:
        previous.use_fake_user = True
    return action, valid_count
