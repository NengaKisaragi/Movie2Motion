bl_info = {
    "name": "Movie2Motion",
    "author": "Movie2Motion",
    "version": (0, 4, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Movie2Motion",
    "description": "Create an in-place Action from a video on a Pinnacle-Rigger humanoid",
    "category": "Animation",
}

from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile, TemporaryDirectory

import bpy
from bpy.app.handlers import persistent

from .retarget import create_action, rig_bones


_job = None


def runtime_config_path():
    return Path(bpy.utils.user_resource("CONFIG")) / "movie2motion-runtime.json"


@lru_cache(maxsize=1)
def saved_runtime_paths():
    try:
        data = json.loads(runtime_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in ("python_path", "model_path") if isinstance(data.get(key), str)}


def runtime_path_getter(key):
    def get(preferences):
        paths = saved_runtime_paths()
        if key in paths:
            return paths[key]
        legacy = (preferences.bl_system_properties_get()
                  if hasattr(preferences, "bl_system_properties_get") else preferences)
        return legacy.get(key, "") if legacy is not None else ""

    return get


def runtime_path_setter(key):
    def set_path(preferences, value):
        saved_runtime_paths.cache_clear()
        paths = {
            field: runtime_path_getter(field)(preferences)
            for field in ("python_path", "model_path")
        }
        paths[key] = value
        config = runtime_config_path()
        config.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=config.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(paths, file, ensure_ascii=False, indent=2)
                file.write("\n")
            temporary.replace(config)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        saved_runtime_paths.cache_clear()

    return set_path


class M2MPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    python_path: bpy.props.StringProperty(
        name="外部 Python の実行ファイル", subtype="FILE_PATH",
        get=runtime_path_getter("python_path"), set=runtime_path_setter("python_path"),
    )
    model_path: bpy.props.StringProperty(
        name="MediaPipe の .task モデル", subtype="FILE_PATH",
        get=runtime_path_getter("model_path"), set=runtime_path_setter("model_path"),
    )

    def draw(self, context):
        self.layout.prop(self, "python_path")
        self.layout.prop(self, "model_path")


class M2MSettings(bpy.types.PropertyGroup):
    video: bpy.props.StringProperty(name="動画 (.mp4 / .mov)", subtype="FILE_PATH")
    fps: bpy.props.IntProperty(name="解析 FPS", default=24, min=1, max=60)
    confidence: bpy.props.FloatProperty(name="関節の信頼度", default=0.5, min=0, max=1)
    smoothing: bpy.props.FloatProperty(name="平滑化 (秒)", default=0.08, min=0, max=1,
        description="前後のフレームから関節の揺れを抑え、短い検出抜けを補間。0で無効",)

    foot_contact: bpy.props.EnumProperty(
        name="足の接地補正",
        items=(
            ("OFF", "なし", "元の足の動きを使う"),
            ("AUTO", "自動", "低くゆっくり動く足の接地を推定して補正"),
            ("BOTH", "両足固定", "構えなど、動画全体で両足をつく動き向け"),
        ),
        default="OFF",
    )


def existing_file(value, label):
    path = Path(bpy.path.abspath(value)).expanduser()
    if not value or not path.is_file():
        raise ValueError(f"{label}を指定してください。")
    return path


class M2M_OT_generate(bpy.types.Operator):
    bl_idname = "movie2motion.generate"
    bl_label = "動画から Action を生成"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        active = context.active_object
        return (_job is None and context.window is not None and active is not None
                and active.type == "ARMATURE" and active.mode in {"OBJECT", "POSE"})

    def execute(self, context):
        global _job
        settings = context.scene.movie2motion
        preferences = context.preferences.addons[__package__].preferences
        self._process = self._timer = self._directory = None
        try:
            rig_bones(context.active_object)
            video = existing_file(settings.video, "動画 (.mp4 / .mov)")
            if video.suffix.lower() not in {".mp4", ".mov"}:
                raise ValueError(".mp4 または .mov の動画を選択してください。")
            python = existing_file(preferences.python_path, "アドオン設定で外部 Python")
            model = existing_file(preferences.model_path, "アドオン設定で .task モデル")
            self._directory = TemporaryDirectory(prefix="movie2motion-")
            directory = Path(self._directory.name)
            self._output = directory / "landmarks.json"
            self._log = directory / "worker.log"
            worker = Path(__file__).with_name("estimate.py")
            with self._log.open("wb") as log:
                self._process = subprocess.Popen(
                    [str(python), "-I", "-B", str(worker), str(video), str(model),
                     str(self._output), "--fps", str(settings.fps)],
                    stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, "MPLCONFIGDIR": str(directory / "matplotlib")},
                )
            self._armature = context.active_object
            self._scene = context.scene
            self._start = 1
            context.scene.frame_set(self._start)
            self._fps = context.scene.render.fps / context.scene.render.fps_base
            self._confidence = settings.confidence
            self._smoothing = settings.smoothing
            self._foot_contact = settings.foot_contact
            self._name = f"Movie2Motion · {video.stem}"
            if self._foot_contact != "OFF":
                self._name += " · 接地補正"
            self._wm = context.window_manager
            self._timer = self._wm.event_timer_add(0.25, window=context.window)
            self._wm.modal_handler_add(self)
            _job = self
            if context.area:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        except (OSError, RuntimeError, ValueError) as exc:
            self._close()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def modal(self, context, event):
        if self._process is None:
            return {"CANCELLED"}
        if event.type == "ESC":
            self.cancel(context)
            return {"CANCELLED"}
        if event.type != "TIMER" or self._process.poll() is None:
            return {"PASS_THROUGH"}
        try:
            if self._process.returncode:
                message = self._log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                raise ValueError(message[-1] if message else "動画解析に失敗しました。")
            if context.scene != self._scene or self._armature.name not in self._scene.objects:
                raise ValueError("解析開始時のシーンまたはアーマチュアを確認してください。")
            if self._armature.mode not in {"OBJECT", "POSE"}:
                raise ValueError("アーマチュアを Object または Pose モードに切り替えてください。")
            with self._output.open(encoding="utf-8") as output:
                samples = json.load(output)
            action, detected = create_action(
                self._armature, samples, self._name, self._start, self._fps,
                self._confidence, self._smoothing, self._foot_contact,
            )
            self.report({"INFO"}, f"{action.name} を生成しました（胴体検出 {detected}/{len(samples)}）")
            return {"FINISHED"}
        except (OSError, ReferenceError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            self._close()
            if context.area:
                context.area.tag_redraw()

    def cancel(self, context):
        self._close()
        if context.area:
            context.area.tag_redraw()

    def _close(self):
        global _job
        if self._timer is not None:
            self._wm.event_timer_remove(self._timer)
            self._timer = None
        if self._process is not None:
            if self._process.poll() is None:
                self._process.kill()
                self._process.wait()
            self._process = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
        if _job is self:
            _job = None


class M2M_PT_panel(bpy.types.Panel):
    bl_label = "Movie2Motion"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Movie2Motion"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.movie2motion
        layout.prop(settings, "video")
        layout.prop(settings, "fps")
        layout.prop(settings, "confidence")
        layout.prop(settings, "smoothing")
        layout.prop(settings, "foot_contact")
        if _job is not None:
            layout.label(text="動画を解析中… Esc で中止")
        else:
            layout.operator(M2M_OT_generate.bl_idname)


classes = (M2MPreferences, M2MSettings, M2M_OT_generate, M2M_PT_panel)


@persistent
def cancel_before_load(_):
    if _job is not None:
        _job._close()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.movie2motion = bpy.props.PointerProperty(type=M2MSettings)
    bpy.app.handlers.load_pre.append(cancel_before_load)


def unregister():
    cancel_before_load(None)
    bpy.app.handlers.load_pre.remove(cancel_before_load)
    del bpy.types.Scene.movie2motion
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
