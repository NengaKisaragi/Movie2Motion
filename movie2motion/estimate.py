"""Run in a separate Python environment, outside Blender."""

import argparse
import json
from pathlib import Path
import sys


def estimate(video, model, sample_fps):
    import av
    import mediapipe as mp
    import numpy as np

    vision = mp.tasks.vision
    options = vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
    )
    samples = []
    with av.open(str(video)) as container, vision.PoseLandmarker.create_from_options(options) as detector:
        if not container.streams.video:
            raise ValueError("The file has no video stream.")
        stream = container.streams.video[0]
        source_fps = float(stream.average_rate or sample_fps)
        first_time = None
        last_ms = -1
        next_sample = 0.0
        for index, frame in enumerate(container.decode(stream)):
            if first_time is None:
                first_time = frame.time if frame.time is not None else 0.0
            seconds = frame.time - first_time if frame.time is not None else index / source_fps
            if seconds + 1e-6 < next_sample:
                continue
            # Keep the video clock, including for variable-frame-rate clips.
            timestamp = round(seconds * 1000)
            if timestamp <= last_ms:
                continue
            last_ms = timestamp
            next_sample = (int((seconds + 1e-6) * sample_fps) + 1) / sample_fps
            width = max(1, round(frame.width * min(1, 960 / max(frame.width, frame.height))))
            height = max(1, round(frame.height * min(1, 960 / max(frame.width, frame.height))))
            rgb = frame.reformat(width=width, height=height, format="rgb24").to_ndarray()
            turns = round(frame.rotation / 90) % 4
            if turns:
                rgb = np.ascontiguousarray(np.rot90(rgb, turns))
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            poses = detector.detect_for_video(image, timestamp).pose_world_landmarks
            points = None
            if poses:
                points = [
                    [p.x, p.y, p.z, min(p.visibility or 0, p.presence if p.presence is not None else 1)]
                    for p in poses[0]
                ]
            samples.append([seconds, points])

    if not any(points is not None for _, points in samples):
        raise ValueError("No person was detected in the video.")
    return samples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fps", type=int, default=24, choices=range(1, 61), metavar="1-60")
    args = parser.parse_args()
    try:
        samples = estimate(args.video, args.model, args.fps)
        with args.output.open("w", encoding="utf-8") as output:
            json.dump(samples, output, allow_nan=False, separators=(",", ":"))
    except ImportError as exc:
        print(f"Install Movie2Motion requirements in the configured Python environment: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Video analysis failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
