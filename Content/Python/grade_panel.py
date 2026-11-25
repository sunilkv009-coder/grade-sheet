"""Multi-camera grading contact sheet - Unreal side.
Open it from the menu: Grade Sheet > Open Grade Sheet (or the Grade Sheet toolbar button).

No window of its own: it starts grade_ui.py (separate process showing grade_ui.html)
and applies what that window asks for, through JSON files in Saved/GradeCaptures.

GRADE SETUP = one named grade for one camera:
    camera + the levels that are active for it + the ONE level the grade lives in.
In Unreal a setup is:
    PPV_Nuke_<Name>, PPV_Nuke_<code_code> or PPV_Nuke_<Name>_<code_code>
                     unbound post process volume, placed in the setup's target level (see ppv_label). The studio's
                     Tool Suite (Tag PPV Nuke actors) later renames them PPV_<Name> / PPV_Variant_<code-code>; a
                     volume that already exists under either name is used instead of making a new one.
    MI_Grade_<id>    instance of M_NukeGrade (whole frame + layers A, B, C)
    T_Mask_<id>      drawn-shape mask (R/G/B = layers A/B/C)
so the grade switches on and off with its level. Setups are listed in <project>/Config/GradeSheetSetups.json.
"""
import os, re, sys, json, glob, time, shutil, subprocess, traceback
import unreal
import __main__

PKG = "/Game/GradeSheet"                 # where the tool keeps its materials, grade instances and masks
CAP_RES = (1280, 720)
SETTLE_SEC, SHOT_SEC, ID_SEC = 6.0, 0.5, 1.0
# A high-resolution screenshot is rendered on its own, at its own size, so what has settled in the viewport does not
# carry over: Unreal renders the shot this many frames before saving it (its default is 4, which leaves lighting,
# reflections and anti-aliasing half-finished: a soft, noisy picture). Raise it if pictures still look unfinished.
SHOT_FRAMES = 64
SHOT_TIMEOUT = 90.0                # a heavy scene can take a long time to render one high-resolution shot
PARAM_NAMES = ["Blackpoint", "Whitepoint", "Lift", "Gain", "Multiply", "Offset", "Gamma",
               "Shadows", "Midtones", "Highlights", "Saturation"]
PREFIXES = ["", "L1_", "L2_", "L3_"]
MAX_ID, MAX_CAMS = 255, 8
WORDS = ["Mask"] + ["Mask%d" % j for j in range(1, 11)]   # 24 stencil IDs per scalar: IDs 1-24, 25-48, ... 241-255
CAP_DIR = os.path.abspath(os.path.join(unreal.Paths.convert_relative_path_to_full(
    unreal.Paths.project_saved_dir()), "GradeCaptures"))
CMD_DIR = os.path.join(CAP_DIR, "cmd")
os.makedirs(CMD_DIR, exist_ok=True)
PROJECT = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(PROJECT, "Scripts")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import grade_log as glog
glog.setup(CAP_DIR, "unreal", rotate=True)
import grade_diag as gdiag
HEARTBEAT_SEC = 5.0                # while capturing: one line every 5 s on what Unreal is doing
SLOW_FRAME_SEC = 1.5               # a frame longer than this is written to the log


def info(msg):
    unreal.log("GRADE_PANEL " + msg)
    glog.info(msg)


def warn(msg):
    unreal.log_warning("GRADE_PANEL " + msg)
    glog.warn(msg)


def fail(msg):
    unreal.log_error("GRADE_PANEL " + msg + "\n" + traceback.format_exc())
    glog.error(msg)


CFG = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_config_dir()), "GradeSheetSetups.json")
if not os.path.exists(CFG) and os.path.exists(os.path.join(PROJECT, "Scripts", "grade_setups.json")):
    shutil.copyfile(os.path.join(PROJECT, "Scripts", "grade_setups.json"), CFG)        # setups made before the plugin existed

SHOT_DIRS = [os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()), "Screenshots")]
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
mel = unreal.MaterialEditingLibrary
eal = unreal.EditorAssetLibrary
tools = unreal.AssetToolsHelpers.get_asset_tools()
if eal.does_asset_exist("/Game/GradeTest/M_NukeGrade"):
    PKG = "/Game/GradeTest"              # project that was set up before the plugin existed


def get_world():
    return unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()


def lname(obj):
    return obj.get_path_name().split("/")[-1].split(".")[0]


def world_actors():
    """Every actor of the editor world, INCLUDING those in hidden sublevels. Unreal's actor list skips hidden levels,
    and after a capture only the last camera's levels are visible, so cameras and grade volumes elsewhere would look
    missing (save then said 'no grade volume', and Done made duplicates). Cameras and volumes in hidden levels are
    added from the list of all loaded objects."""
    out = list(eas.get_all_level_actors())
    seen = set(id(a) for a in out)
    it = getattr(unreal, "ObjectIterator", None)
    world = get_world()
    if it is None or world is None:
        return out
    try:
        wpath = world.get_path_name()
    except Exception:
        wpath = None
    for cls in (unreal.CineCameraActor, unreal.PostProcessVolume):
        try:
            for a in it(cls):
                if id(a) in seen or a in out:
                    continue
                try:
                    p = a.get_path_name()
                    if "Default__" in p or not unreal.SystemLibrary.is_valid(a):
                        continue
                    lw = a.get_world() if hasattr(a, "get_world") else None
                    if wpath and lw is not None and lw.get_path_name() != wpath:
                        continue                                  # another world (a preview, play-in-editor...)
                    if lname(a.get_outer()) not in sublevels():
                        continue                                  # only actors of this world's sublevels
                except Exception:
                    continue
                out.append(a)
                seen.add(id(a))
        except Exception:
            glog.warn("could not list %s in hidden levels" % cls.__name__)
    return out


def sublevels():
    """name -> Level, without the persistent level."""
    levels = unreal.EditorLevelUtils.get_levels(get_world())
    return {lname(l): l for l in levels[1:]}


def level_file(level):
    pkg = level.get_outermost().get_name()                      # /Game/Folder/Name
    return os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir()),
                        pkg[len("/Game/"):] + ".umap")


def level_info():
    out = []
    for name, level in sorted(sublevels().items()):
        f = level_file(level)
        out.append({"name": name, "base": "_base" in name.lower(),
                    "readonly": os.path.exists(f) and not os.access(f, os.W_OK)})
    return out


_DONT_MODIFY = getattr(getattr(unreal, "LevelVisibilityDirtyMode", None), "DONT_MODIFY", None)


def perf_settings():
    """Editor Preferences > Performance (holds 'Use Less CPU when in Background'). Newer engines (5.6) no longer
    expose the class to Python by name, so the settings object is then looked up by its path."""
    cls = getattr(unreal, "EditorPerformanceSettings", None)
    if cls is not None:
        try:
            return unreal.get_default_object(cls)
        except Exception:
            pass
    for path in ("/Script/UnrealEd.Default__EditorPerformanceSettings",):
        for fn in ("find_object", "load_object"):
            f = getattr(unreal, fn, None)
            if f is None:
                continue
            try:
                obj = f(None, path)
                if obj is not None:
                    obj.get_editor_property("throttle_cpu_when_not_foreground")      # the property is reachable
                    return obj
            except Exception:
                pass
    return None


def set_visible(level, on):
    """Show or hide a level WITHOUT marking it as changed (so it never ends up in the save dialog)."""
    if _DONT_MODIFY is not None:
        try:
            return unreal.EditorLevelUtils.set_level_visibility(level, on, False, _DONT_MODIFY)
        except TypeError:
            pass
    unreal.EditorLevelUtils.set_level_visibility(level, on, False)


def is_visible(level):
    """True / False, or None when this engine version cannot tell (then visibility is always set)."""
    try:
        return bool(unreal.EditorLevelUtils.is_level_visible(level))
    except Exception:
        return None


def show_levels(names):
    """names = levels to show (None = all)."""
    for n, l in sublevels().items():
        on = names is None or n in names
        if is_visible(l) is not on:
            set_visible(l, on)


def restore_levels(vis):
    """Put level visibility back the way it was when the panel opened (vis: name -> visible)."""
    for n, l in sublevels().items():
        on = vis.get(n) is not False                    # unknown = visible
        if is_visible(l) is not on:
            set_visible(l, on)


def redraw():
    """Ask Unreal to draw its viewports now (a pending screenshot is taken on the next draw)."""
    for owner in (les, getattr(unreal, "EditorLevelLibrary", None)):
        fn = getattr(owner, "editor_invalidate_viewports", None) if owner else None
        if fn:
            try:
                return fn()
            except Exception:
                pass


def safe(s):
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s)).strip("_") or "x"


def variant_code(name):
    """Variant name -> the code in PPV names (studio syntax: codes joined with '-', e.g. 8WF-9WF)."""
    return re.sub(r"[^A-Za-z0-9\-]", "", re.sub(r"[\s_]+", "-", str(name).strip())).strip("-") or "x"


COLOR_GROUP = "PPV_CarPaint"       # colour volumes are attached to this actor (or put in this outliner folder)


VOL_TAG = "GradeSheet:"            # actor tag "GradeSheet:<setup id>" ties a volume to its setup (labels can repeat)


def _label_parts(s):
    name = safe(s.get("name", "Grade"))
    codes = [variant_code(v) for v in (s.get("variants") or {}).values()]
    if not codes and s.get("variant"):
        codes = [variant_code(name)]                             # older setups: the setup is named like its variant
    plain = bool(codes) and name.lower().replace("_", "-") in [c.lower() for c in codes] + ["-".join(codes).lower()]
    return name, codes, plain


def ppv_label(s):
    """Studio naming rule. The tool names volumes the way artists do before tagging:
         always-on grade            PPV_Nuke_<Name>                 PPV_Nuke_Badge
         variant grade              PPV_Nuke_<code_code>            PPV_Nuke_8WF_9WF
         named grade for a variant  PPV_Nuke_<Name>_<code_code>     PPV_Nuke_Badge_8WF_9WF
    Tool Suite > Tag PPV Nuke actors then renames them (tagged_label) and ties them to their variants."""
    name, codes, plain = _label_parts(s)
    code = "_".join(c.replace("-", "_") for c in codes)
    if not codes:
        return "PPV_Nuke_" + name
    return "PPV_Nuke_" + (code if plain else name + "_" + code)


def tagged_label(s):
    """The same volume after the Tool Suite tagged it: PPV_Badge, PPV_Variant_8WF-9WF, PPV_Badge_Variant_8WF-9WF."""
    name, codes, plain = _label_parts(s)
    if not codes:
        return "PPV_" + name
    return ("PPV_" if plain else "PPV_" + name + "_") + "Variant_" + "-".join(codes)


def setup_labels(s):
    """Every name the setup's volume may have (lower case): before and after tagging."""
    return {ppv_label(s).lower(), tagged_label(s).lower()}


ADOPTED_TAG = "GradeSheetAdopted"   # the volume existed before the tool used it: never deleted, only untied


def _ours(obj):
    try:
        return obj is not None and obj.get_path_name().startswith(PKG + "/")
    except Exception:
        return False


def drop_our_blendables(vol):
    """Remove the tool's materials from a volume's blendables, keeping everything else."""
    settings = vol.get_editor_property("settings")
    keep = [b for b in settings.get_editor_property("weighted_blendables").get_editor_property("array")
            if not _ours(b.get_editor_property("object"))]
    wbs = unreal.WeightedBlendables()
    wbs.set_editor_property("array", keep)
    settings.set_editor_property("weighted_blendables", wbs)
    vol.set_editor_property("settings", settings)


def set_blendables(vol, mi, grade_w, id_w):
    """The grade volume carries two materials: the grade (weight grade_w) and the stencil ID view (weight id_w).
    Anything else in its blendables (for example PPM_NukeGrade materials on an existing volume) is kept as it is."""
    out = []
    try:
        for b in vol.get_editor_property("settings").get_editor_property("weighted_blendables").get_editor_property("array"):
            if not _ours(b.get_editor_property("object")):
                out.append(b)
    except Exception:
        pass
    for obj, w in ((mi, grade_w), (eal.load_asset(PKG + "/M_StencilID"), id_w)):
        if obj:
            wb = unreal.WeightedBlendable()
            wb.set_editor_property("weight", float(w))
            wb.set_editor_property("object", obj)
            out.append(wb)
    wbs = unreal.WeightedBlendables()
    wbs.set_editor_property("array", out)
    settings = vol.get_editor_property("settings")
    old = [(b.get_editor_property("object"), round(b.get_editor_property("weight"), 3))
           for b in settings.get_editor_property("weighted_blendables").get_editor_property("array")]
    if old == [(b.get_editor_property("object"), round(b.get_editor_property("weight"), 3)) for b in out]:
        return                                                   # nothing to change
    settings.set_editor_property("weighted_blendables", wbs)
    vol.set_editor_property("settings", settings)


class Bridge(object):
    def __init__(self):
        self.alive = True
        self.handle = None
        self.proc = None
        self.t = self.next_poll = self.cap_until = 0.0
        self.cap_queue = []
        self.capture_id = 0
        self.status = ""
        self.dirty_masks = set()
        self.preview_shots, self.preview_mark, self.preview_id, self.preview_key = [], None, 0, ""
        self.shot_wait, self.missing, self.shot_size, self.shot_shown = None, [], {}, -1
        self.next_shot_check, self.throttle = 0.0, None
        self.slate_sleep, self.realtime = None, None       # editor settings changed for a capture (None = untouched)
        self.cap_ids, self.progress, self.gone_logged = [], [0, 0], False
        self.in_tick, self.reentered = False, 0                 # see tick()
        self.perf_note = ""                                # shown while capturing when Unreal may stop drawing
        self.shot_delay = None                             # r.HighResScreenshotDelay before a picture (None = untouched)
        self.last_levels = None                            # levels of the setup shown last (capture, or View in Unreal)
        self.result = None                                 # answer to the last one-off job (e.g. colour PPVs), for the window
        self.hb = {"next": 0.0, "ticks": 0, "max_dt": 0.0, "last_wall": None, "step": ""}   # capture heartbeat
        self.rest_variants, self.changed = {}, set()
        self.rest_vis = {n: is_visible(l) for n, l in sublevels().items()}
        try:
            cur = les.get_current_level()
            self.rest_current = lname(cur) if cur else None
        except Exception:
            self.rest_current = None
        self.cfg = {"cameras": [], "setups": []}
        if os.path.exists(CFG):
            try:
                with open(CFG) as f:
                    self.cfg = json.load(f)
            except Exception:
                warn("could not read " + CFG)
        self.scan()
        self.ensure_volumes("open")
        renamed = [ppv_label(s) for s in self.cfg["setups"] if self.name_volume(s)]
        if renamed:                                              # volumes from an older version get the studio names
            info("renamed volumes: " + ", ".join(renamed))

    def ensure_volumes(self, why):
        """Every setup must have its grade volume: one that does not exist (deleted, never saved, lost in a crash)
        is created again in the setup's level, with its grade instance. Returns the problems, if any."""
        made, problems = [], []
        for st in self.cfg["setups"]:
            if st["id"] in self.ppvs:
                continue
            try:
                self.ensure_setup(st)
                made.append(st["id"])
            except Exception as e:
                glog.error("%s: could not make the grade volume of %s: %s" % (why, st["id"], e))
                problems.append("%s / %s: %s" % (st["cam"], st["name"], e))
        if made:
            self.rest_state()
            self.restore_current()
            info("%s: made missing grade volumes: %s" % (why, ", ".join(made)))
        return problems

    def scan(self):
        """Cameras, grade volumes and stencil parts currently in the world."""
        t0 = time.time()
        self.cams, self.ppvs, parts, lv = {}, {}, {}, {}
        for a in world_actors():
            if isinstance(a, unreal.CineCameraActor):
                self.cams[a.get_actor_label()] = a
            elif isinstance(a, unreal.PostProcessVolume):
                sid = next((str(t)[len(VOL_TAG):] for t in a.tags if str(t).startswith(VOL_TAG)), None)
                if sid is None and a.get_actor_label().startswith("PPV_Grade_"):
                    sid = a.get_actor_label()[len("PPV_Grade_"):]      # made by an older version: named PPV_Grade_<id>
                if sid:
                    self.ppvs[sid] = a
            # every mesh component with a stencil ID counts, whatever actor it sits on (CAD / Datasmith imports
            # often put many mesh components on one actor, or use blueprints instead of static mesh actors)
            comps = a.get_components_by_class(unreal.PrimitiveComponent) if not isinstance(a, unreal.CineCameraActor) else []
            for comp in comps:
                try:
                    if not comp.get_editor_property("render_custom_depth"):
                        continue
                    sid = int(comp.get_editor_property("custom_depth_stencil_value"))
                except Exception:
                    continue
                if 0 < sid <= MAX_ID:
                    mat = comp.get_material(0) if isinstance(comp, unreal.MeshComponent) else None
                    parts.setdefault(sid, set()).add(mat.get_name() if mat else a.get_actor_label())
                    lv.setdefault(sid, set()).add(lname(a.get_outer()))
        self.parts = [{"id": k, "name": ", ".join(sorted(v)[:3]) + (" +%d more" % (len(v) - 3) if len(v) > 3 else ""),
                       "level": ", ".join(sorted(lv[k])), "levels": sorted(lv[k])}
                      for k, v in sorted(parts.items())]
        glog.info("scan: %d cameras, %d grade volumes, %d part IDs in %d levels (%.1f s)" % (
            len(self.cams), len(self.ppvs), len(self.parts), len(set(l for p in self.parts for l in p["levels"])), time.time() - t0))

    def setup(self, sid):
        for s in self.cfg["setups"]:
            if s["id"] == sid:
                return s
        return None

    def save_cfg(self):
        try:
            with open(CFG, "w") as f:
                json.dump(self.cfg, f, indent=1)
        except OSError:
            warn("could not write %s (read-only? check it out)" % CFG)

    # ------------------------------------------------------------ grade instances
    def mi(self, sid):
        path = PKG + "/MI_Grade_" + sid
        return eal.load_asset(path) if eal.does_asset_exist(path) else None

    def read_layers(self, sid):
        mi, layers, have = self.mi(sid), [], set()
        try:
            have = set(str(n) for n in mel.get_vector_parameter_names(mi)) if mi else set()
        except Exception:
            pass
        for k, prefix in enumerate(PREFIXES):
            params, mask = {}, []
            if mi:
                for p in PARAM_NAMES:
                    if have and prefix + p not in have:
                        continue                           # not in the material: the window uses the default
                    c = mel.get_material_instance_vector_parameter_value(mi, prefix + p)
                    params[p] = [c.r, c.g, c.b]
                if k:
                    for j, w in enumerate(WORDS):
                        bits = int(round(mel.get_material_instance_scalar_parameter_value(mi, prefix + w)))
                        mask += [j * 24 + b + 1 for b in range(24) if bits >> b & 1 and j * 24 + b + 1 <= MAX_ID]
                    if mel.get_material_instance_scalar_parameter_value(mi, prefix + "BG") > 0.5:
                        mask.insert(0, 0)                  # 0 = background (everything without a stencil ID)
            edge = {}
            if mi and k:
                for e in ("Grow", "Soft"):
                    try:
                        edge[e.lower()] = mel.get_material_instance_scalar_parameter_value(mi, prefix + e)
                    except Exception:
                        pass
            layers.append(dict({"params": params, "mask": mask}, **edge))
        return layers

    def apply(self, sid, layers):
        mi = self.mi(sid)
        if not mi:
            return None
        for k, prefix in enumerate(PREFIXES):
            if k >= len(layers):
                break
            for p in PARAM_NAMES:
                v = layers[k].get("params", {}).get(p)
                if v:
                    mel.set_material_instance_vector_parameter_value(
                        mi, prefix + p, unreal.LinearColor(v[0], v[1], v[2], 1.0))
            if k:
                ids = set(int(i) for i in layers[k].get("mask", []))
                ids = set(i for i in ids if 0 <= i <= MAX_ID)
                bits, bg = len(ids - {0}), 0 in ids
                for j, w in enumerate(WORDS):
                    word = sum(1 << ((i - 1) % 24) for i in ids if i and (i - 1) // 24 == j)
                    mel.set_material_instance_scalar_parameter_value(mi, prefix + w, float(word))
                mel.set_material_instance_scalar_parameter_value(mi, prefix + "BG", 1.0 if bg else 0.0)
                mel.set_material_instance_scalar_parameter_value(mi, prefix + "UseParts", 1.0 if (bits or bg) else 0.0)
                mel.set_material_instance_scalar_parameter_value(
                    mi, prefix + "UseShape", 1.0 if layers[k].get("shape") else 0.0)
                for e in ("Grow", "Soft"):                 # stencil edge: pixels at 1080 lines (Grow < 0 = erode)
                    mel.set_material_instance_scalar_parameter_value(mi, prefix + e, float(layers[k].get(e.lower(), 0) or 0))
        mel.update_material_instance(mi)
        return mi

    def import_mask(self, sid):
        """Saved/GradeCaptures/mask_<id>.png -> T_Mask_<id>, assigned to the setup's grade instance."""
        png = os.path.join(CAP_DIR, "mask_%s.png" % sid)
        mi = self.mi(sid)
        if not mi or not os.path.exists(png):
            return
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", png)
        task.set_editor_property("destination_path", PKG)
        task.set_editor_property("destination_name", "T_Mask_" + sid)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("automated", True)
        task.set_editor_property("save", False)
        task.set_editor_property("factory", unreal.TextureFactory())    # legacy importer: no content browser pop-up
        tools.import_asset_tasks([task])
        path = PKG + "/T_Mask_" + sid
        tex = eal.load_asset(path)
        if not tex:
            return
        if tex.get_editor_property("srgb"):
            tex.set_editor_property("srgb", False)
            tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_VECTOR_DISPLACEMENTMAP)
            tex.set_editor_property("mip_gen_settings", unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS)
            tex.set_editor_property("address_x", unreal.TextureAddress.TA_CLAMP)
            tex.set_editor_property("address_y", unreal.TextureAddress.TA_CLAMP)
        mel.set_material_instance_texture_parameter_value(mi, "ShapeMask", tex)
        mel.update_material_instance(mi)
        self.dirty_masks.add(path)

    # ------------------------------------------------------------ setups <-> Unreal
    def base_material(self, order):
        """Unreal merges instances of one material into a single pass (the last one wins), so each
        stacking position of a camera gets its own copy of M_NukeGrade."""
        if order <= 0:
            return eal.load_asset(PKG + "/M_NukeGrade")
        path = PKG + "/M_NukeGrade_S%d" % order
        if not eal.does_asset_exist(path):
            mat = eal.duplicate_asset(PKG + "/M_NukeGrade", path)
            mat.set_editor_property("blendable_priority", int(order))
        return eal.load_asset(path)

    def ensure_setup(self, s):
        """Create / move the instance and the volume for one setup."""
        sid = s["id"]
        mi = self.mi(sid)
        if not mi:
            mi = tools.create_asset("MI_Grade_" + sid, PKG, unreal.MaterialInstanceConstant,
                                    unreal.MaterialInstanceConstantFactoryNew())
        base = self.base_material(int(s.get("order", 0)))
        if mi.get_editor_property("parent") != base:
            mel.set_material_instance_parent(mi, base)
        vol = self.ppvs.get(sid)
        if vol and lname(vol.get_outer()) != s["target"]:
            self.release_volume(vol)                             # target level changed: rebuild it there
            vol = None
        if not vol:
            vol = self.existing_volume(s)
            if vol is not None:                                  # a volume with the setup's name is there: use it
                vol.modify()
                vol.tags = [str(t) for t in vol.tags if not str(t).startswith(VOL_TAG)] + [VOL_TAG + sid, ADOPTED_TAG]
                vol.set_editor_property("unbound", True)
                self.ppvs[sid] = vol
                glog.info("  using the existing volume %s in %s for %s" % (vol.get_actor_label(), s["target"], sid))
        if not vol:
            try:
                vol = self.spawn_volume(s["target"])
            except RuntimeError as e:
                raise RuntimeError("%s, then open Cameras and levels and press Done again" % e)
            vol.set_editor_property("unbound", True)
            self.ppvs[sid] = vol
        self.name_volume(s)
        adopted = ADOPTED_TAG in [str(t) for t in vol.tags]
        if not adopted and vol.get_editor_property("priority") != 10.0 + float(s.get("order", 0)):
            vol.set_editor_property("priority", 10.0 + float(s.get("order", 0)))
        set_blendables(vol, mi, 1.0, 0.0)

    def existing_volume(self, s):
        """A post process volume in the setup's level named like the setup (before or after tagging) that no other
        setup uses, or None."""
        names, used = setup_labels(s), set(id(v) for v in self.ppvs.values())
        for a in world_actors():
            if (isinstance(a, unreal.PostProcessVolume) and id(a) not in used and lname(a.get_outer()) == s["target"]
                    and a.get_actor_label().lower() in names):
                return a
        return None

    def release_volume(self, vol):
        """Let go of a setup's volume: one the tool made is deleted; one that existed before only loses the tool's
        tag and materials."""
        if ADOPTED_TAG in [str(t) for t in vol.tags]:
            vol.modify()
            vol.tags = [str(t) for t in vol.tags if not str(t).startswith(VOL_TAG) and str(t) != ADOPTED_TAG]
            drop_our_blendables(vol)
        else:
            eas.destroy_actor(vol)

    def spawn_volume(self, target):
        """A new post process volume in level `target` (made visible for it). RuntimeError when Unreal refuses."""
        level = sublevels().get(target)
        if not level:
            raise RuntimeError("Level %s is not loaded" % target)
        if is_visible(level) is not True:
            set_visible(level, True)
        les.set_current_level_by_name(target)
        vol = eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0))
        self.restore_current()
        if vol and lname(vol.get_outer()) != target:
            # Unreal could not make the level current (it locks read-only levels): try moving the volume
            glog.warn("volume was created in %s instead of %s; moving it" % (lname(vol.get_outer()), target))
            try:
                moved = unreal.EditorLevelUtils.move_actors_to_level([vol], level, False, False)
                vol = moved[0] if moved else vol
            except Exception:
                glog.error("moving the volume failed")
        if not vol or lname(vol.get_outer()) != target:
            if vol:
                eas.destroy_actor(vol)
            raise RuntimeError("%s is locked (Unreal locks levels that are read-only / not checked out), so the volume "
                               "could not be placed in it. Check the level out in Perforce or unlock it with the padlock "
                               "in the Levels panel" % target)
        return vol

    def make_color_ppvs(self, levels, codes):
        """Feature: Create PPVs for colours. In every chosen level, one PPV_Nuke_<code_code> per chosen colour:
        unbound, enabled, attached to the level's PPV_CarPaint actor (or in the PPV_CarPaint outliner folder).
        A volume that is already there (same label in that level) is left alone. Tagging the volumes to their
        variants is done afterwards with the studio's Tool Suite (Tag PPV Nuke actors)."""
        made, skipped, failed = [], [], []
        vis = {n: is_visible(l) for n, l in sublevels().items()}
        subs = sublevels()
        for lv in levels:                                        # Unreal does not list actors of hidden levels, so
            if lv in subs and vis.get(lv) is not True:           # show them first; put back at the end
                set_visible(subs[lv], True)
        actors = eas.get_all_level_actors()
        for lv in levels:
            have = {a.get_actor_label().lower(): a for a in actors
                    if isinstance(a, unreal.PostProcessVolume) and lname(a.get_outer()) == lv}
            group = next((a for a in actors if a.get_actor_label() == COLOR_GROUP and lname(a.get_outer()) == lv
                          and not isinstance(a, unreal.PostProcessVolume)), None)
            locked = None
            for code in codes:
                vc = variant_code(code)
                label = "PPV_Nuke_" + vc.replace("-", "_")           # the Tool Suite renames it PPV_Variant_<vc>
                if ("ppv_variant_" + vc).lower() in have:            # already tagged by the Tool Suite
                    skipped.append("%s in %s" % ("PPV_Variant_" + vc, lv))
                    continue
                if locked:                                       # one refusal is enough to know the level is locked
                    failed.append("%s in %s: level locked (see above)" % (label, lv))
                    continue
                if label.lower() in have:
                    skipped.append("%s in %s" % (label, lv))
                    continue
                try:
                    vol = self.spawn_volume(lv)
                    vol.set_actor_label(label)
                    vol.set_editor_property("unbound", True)
                    vol.set_editor_property("enabled", True)
                    vol.set_editor_property("blend_weight", 1.0)
                    placed = "folder " + COLOR_GROUP
                    if group is not None:
                        try:
                            vol.attach_to_actor(group, "", unreal.AttachmentRule.KEEP_WORLD,
                                                unreal.AttachmentRule.KEEP_WORLD, unreal.AttachmentRule.KEEP_WORLD, False)
                            placed = "under " + COLOR_GROUP
                        except Exception:
                            group = None
                    if group is None:
                        vol.set_folder_path(COLOR_GROUP)
                    have[label.lower()] = vol
                    made.append("%s in %s" % (label, lv))
                    glog.info("  made %s in %s (%s)" % (label, lv, placed))
                except Exception as e:
                    failed.append("%s in %s: %s" % (label, lv, e))
                    locked = "locked" in str(e)
                    glog.error("  could not make %s in %s" % (label, lv))
        restore_levels(vis)
        glog.info("colour PPVs: %d made, %d already there, %d failed" % (len(made), len(skipped), len(failed)))
        return {"made": made, "skipped": skipped, "failed": failed}

    def leave_levels(self):
        """When the tool is done looking through cameras, the levels of the setup it showed last stay visible and
        the others stay hidden, so nobody has to switch levels off by hand afterwards. Only when no setup has been
        shown yet (for example a wizard preview that was cancelled) do the levels go back to how the panel found them."""
        if self.last_levels is not None:
            show_levels(self.last_levels)
        else:
            restore_levels(self.rest_vis)

    def restore_current(self):
        """Give the user back the level that was current when the panel opened."""
        name = self.rest_current if self.rest_current in sublevels() else None
        les.set_current_level_by_name(name or lname(unreal.EditorLevelUtils.get_levels(get_world())[0]))

    def name_volume(self, s):
        """Give the setup's volume its studio name and its tag. Returns True if something changed."""
        vol = self.ppvs.get(s["id"])
        if not vol:
            return False
        label, tag = ppv_label(s), VOL_TAG + s["id"]
        tags = [str(t) for t in vol.tags]
        named = vol.get_actor_label().lower() in setup_labels(s)  # PPV_Nuke_... or already tagged by the Tool Suite
        if named and tag in tags:
            return False
        vol.modify()
        if tag not in tags:
            vol.tags = [t for t in tags if not t.startswith(VOL_TAG)] + [tag]
        if not named:
            vol.set_actor_label(label)
        return True

    def remove_setup(self, sid):
        glog.info("removing setup %s" % sid)
        vol = self.ppvs.pop(sid, None)
        if vol:
            self.release_volume(vol)
        for name in ("MI_Grade_", "T_Mask_"):
            if eal.does_asset_exist(PKG + "/" + name + sid):
                eal.delete_asset(PKG + "/" + name + sid)

    def configure(self, c):
        """Replace the whole list of cameras and setups (from the setup wizard)."""
        info = {l["name"]: l for l in level_info()}
        cams = [n for n in c.get("cameras", []) if n in self.cams][:MAX_CAMS]
        new, skipped, self.changed = [], [], set()
        for s in c.get("setups", []):
            cam, target = s.get("cam"), s.get("target")
            if cam not in cams or target not in info:
                continue
            li = info[target]
            if (li["base"] or li["readonly"]) and not s.get("override"):
                skipped.append(s.get("name", "?"))                # protected level, no override given
                continue
            sid = safe(cam) + "__" + safe(s.get("name", "Grade"))
            old = self.setup(sid) or {}
            levels = [l for l in s.get("levels", []) if l in info]
            if (not old or old.get("levels") != levels or old.get("variants", {}) != dict(s.get("variants") or {})
                    or not os.path.exists(os.path.join(CAP_DIR, sid + ".png"))):
                self.changed.add(sid)                             # new or different levels: needs a fresh capture
            new.append({"id": sid, "name": s.get("name", "Grade"), "cam": cam, "target": target,
                        "levels": levels, "order": 0,
                        "variants": dict(s.get("variants") or {}),          # variant table -> variant
                        "variant": bool(s.get("variant") or s.get("variants")),
                        "override": bool(s.get("override")), "shapes": old.get("shapes", [])})
        for cam in cams:                                    # stacking position of each setup
            mine = [s for s in new if s["cam"] == cam]
            always = [s for s in mine if not s["variant"]]
            for i, s in enumerate(always):
                s["order"] = i
            for s in mine:
                if s["variant"]:
                    s["order"] = len(always)                 # variants are never on together: they share one position
        keep = set(s["id"] for s in new)
        for s in self.cfg["setups"]:
            if s["id"] not in keep:
                self.remove_setup(s["id"])
        self.cfg = {"cameras": cams, "setups": new}
        errors = []
        for s in new:
            try:
                self.ensure_setup(s)
            except Exception as e:
                glog.error("could not set up %s" % s["id"])
                errors.append("%s: %s" % (s["name"], e))
        self.rest_state()
        self.save_cfg()
        for s in new:
            vol = self.ppvs.get(s["id"])
            glog.info("  volume %s in %s" % (vol.get_actor_label() if vol else "MISSING", s["target"]))
        msg = "%d cameras, %d grade setups" % (len(cams), len(new))
        if skipped:
            msg += ". Skipped (protected level): " + ", ".join(skipped)
        if errors:
            msg += ". Errors: " + "; ".join(errors)
        self.write_state(msg)

    # ------------------------------------------------------------ state for the window
    def write_state(self, status=None):
        if status is not None:
            if status != self.status and "waiting for Unreal" not in status and not status.startswith("Capturing"):
                glog.info("status: " + status) if status else None
            self.status = status
        setups = []
        for s in self.cfg["setups"]:
            d = dict(s)
            d["layers"] = self.read_layers(s["id"])
            setups.append(d)
        state = {"capture_id": self.capture_id, "status": self.status, "busy": bool(self.cap_queue or self.shot_wait),
                 "preview_id": self.preview_id, "progress": self.progress, "preview_key": self.preview_key, "variant_sets": self.variant_tables(),
                 "parts": self.parts, "levels": level_info(), "max_cams": MAX_CAMS, "result": self.result,
                 "all_cameras": [{"name": n, "level": lname(a.get_outer())} for n, a in sorted(self.cams.items())],
                 "cameras": self.cfg["cameras"], "setups": setups}
        tmp = os.path.join(CAP_DIR, "state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        for _ in range(25):
            try:
                return os.replace(tmp, os.path.join(CAP_DIR, "state.json"))
            except PermissionError:                              # Windows: the window is reading the file this instant
                time.sleep(0.01)
        glog.warn("state.json was busy; the window gets the next update")

    # ------------------------------------------------------------ commands
    def handle_cmd(self, c):
        op = c.get("op")
        if op == "config":
            glog.info("setup wizard sent %d setups for cameras %s" % (len(c.get("setups", [])), ", ".join(c.get("cameras", []))))
            for x in c.get("setups", []):
                glog.info("  %s / %s -> grade in %s%s, levels: %s" % (x.get("cam"), x.get("name"), x.get("target"),
                          " (override)" if x.get("override") else "", ", ".join(x.get("levels", []))))
        elif op == "preview":
            glog.info("preview %s with %s" % (c.get("cam"), "every level" if c.get("levels") is None
                                              else ", ".join(c.get("levels")) or "no levels"))
        elif op not in ("set", "shapes", "mask"):
            glog.info("command: " + json.dumps(c)[:300])
        if op == "set":
            self.apply(c["id"], c["layers"])
        elif op == "shapes":
            s = self.setup(c["id"])
            if s is not None:
                s["shapes"] = c.get("shapes", [])
                self.save_cfg()
        elif op == "mask":
            self.import_mask(c["cam"])                           # the window sends the setup id here
        elif op == "config":
            self.scan()
            self.configure(c)
            self.start_capture(self.changed)
        elif op == "levels":                                     # change the active levels of one setup
            s = self.setup(c["id"])
            if s is not None:
                have = sublevels()
                s["levels"] = [l for l in c.get("levels", []) if l in have]
                if s["target"] not in s["levels"]:
                    s["levels"].append(s["target"])
                self.save_cfg()
                self.write_state("Levels of %s / %s changed" % (s["cam"], s["name"]))
                self.start_capture([s["id"]])
        elif op == "new_level":
            name = safe(c.get("name", ""))
            if name in sublevels():
                return self.write_state("Level %s already exists" % name)
            unreal.EditorLevelUtils.create_new_streaming_level(unreal.LevelStreamingDynamic, PKG + "/" + name, False)
            les.set_current_level_by_name(lname(unreal.EditorLevelUtils.get_levels(get_world())[0]))
            self.write_state("Created level " + name)
        elif op == "save":
            self.scan()
            self.ensure_volumes("save")
            for sid, layers in c.get("grades", {}).items():
                self.apply(sid, layers)
            self.rest_state()
            eal.save_directory(PKG, True, True)                  # the tool's own folder, changed assets only
            self.dirty_masks = set()
            # Levels are NOT saved or checked out here: the volumes stay in the open levels and the artist saves the
            # levels when ready (Unreal lists them as unsaved).
            problems = []
            for st in self.cfg["setups"]:                        # every grade volume must be in its own level
                vol = self.ppvs.get(st["id"])
                where = lname(vol.get_outer()) if vol else None
                if where != st["target"]:
                    problems.append("%s / %s: %s" % (st["cam"], st["name"], "no grade volume, and it could not be made (see Log)" if not where else "volume is in %s, not %s" % (where, st["target"])))
            glog.info("save grades: %s (levels are not saved by the tool)" % PKG)
            with open(os.path.join(CAP_DIR, "grades.json"), "w") as f:
                json.dump(c.get("grades", {}), f, indent=1)
            self.save_cfg()
            for x in problems:
                warn("save: " + x)
            self.write_state(("Saved %d grade setups (levels not saved: save them yourself when ready)" % len(self.cfg["setups"])) if not problems else
                             "NOT everything was saved. " + "; ".join(problems[:3]) + (" (+%d more, see Log)" % (len(problems) - 3)
                                                                                     if len(problems) > 3 else ""))
        elif op == "preview":                                    # setup wizard: show this camera with these levels
            if self.cap_queue:
                return
            show_levels(c.get("levels"))                         # None = every level
            cam = self.cams.get(c.get("cam"))
            if cam:
                les.pilot_level_actor(cam)
            key = c.get("key", "")                               # a quick picture, then one after the lighting settles
            self.preview_shots = [(self.t + 0.8, key), (self.t + 3.2, key)]
        elif op == "pilot":
            s = self.setup(c["id"])
            if s and s["cam"] in self.cams:
                show_levels(s["levels"])
                self.last_levels = list(s["levels"])
                self.rest_state(s["id"])
                self.switch_variant(s)
                les.pilot_level_actor(self.cams[s["cam"]])
        elif op == "eject":
            les.eject_pilot_level_actor()
            self.rest_state()
            self.switch_variant(None)
            self.leave_levels()
        elif op == "color_ppvs":                                 # feature: create PPVs for colours
            glog.info("create colour PPVs: colours %s in levels %s" % (", ".join(c.get("colors", [])), ", ".join(c.get("levels", []))))
            res = self.make_color_ppvs(c.get("levels", []), c.get("colors", []))
            self.result = dict(res, id=c.get("req"), kind="color_ppvs")
            self.write_state("Colour PPVs: %d made, %d already there, %d failed" % (len(res["made"]), len(res["skipped"]), len(res["failed"])))
        elif op == "cancel":                                     # Stop button while capturing
            if self.cap_queue or self.shot_wait:
                self.end_capture()
                self.capture_id += 1
                self.write_state("Capture stopped")
        elif op == "capture":
            self.scan()                                          # pick up newly assigned part IDs
            self.ensure_volumes("capture")
            self.start_capture(c.get("ids"))
        elif op == "py":                                         # run another script from the Scripts folder
            path = os.path.abspath(os.path.join(HERE, os.path.basename(c.get("file", ""))))
            with open(path) as f:
                exec(compile(f.read(), path, "exec"), {"__name__": "__main__", "__file__": path})
            self.scan()
            self.write_state("Ran " + os.path.basename(path))

    # ------------------------------------------------------------ capture: one still per setup, grades off
    def enable_grades(self, on, id_for=None):
        """on=False: every grade off (volumes stay enabled, grade weight 0). id_for: that setup shows stencil IDs."""
        for sid, vol in self.ppvs.items():
            if not on and not vol.get_editor_property("enabled"):
                vol.set_editor_property("enabled", True)
            set_blendables(vol, self.mi(sid), 1.0 if on else 0.0, 1.0 if sid == id_for else 0.0)

    def rest_state(self, active=None):
        """Always-on setups enabled. A variant setup (for example a paint code) is enabled only while it is
        the one being viewed; at rest it is off and the production script enables it by its name."""
        for s in self.cfg["setups"]:
            vol = self.ppvs.get(s["id"])
            if vol:
                set_blendables(vol, self.mi(s["id"]), 1.0, 0.0)
                on = (not s.get("variant")) or s["id"] == active
                if vol.get_editor_property("enabled") != on:
                    vol.set_editor_property("enabled", on)

    def variant_sets(self):
        """Every Variant Manager table in the world: [(table name, [variants])]."""
        out = []
        for a in eas.get_all_level_actors():
            if isinstance(a, unreal.LevelVariantSetsActor):
                lvs = a.get_level_variant_sets(True)
                if not lvs:
                    continue
                for i in range(lvs.get_num_variant_sets()):
                    vs = lvs.get_variant_set(i)
                    out.append((str(vs.get_display_text()), [vs.get_variant(j) for j in range(vs.get_num_variants())]))
        return out

    def variant_tables(self):
        try:
            return [{"name": name, "variants": [str(v.get_display_text()) for v in vs]} for name, vs in self.variant_sets()]
        except Exception:
            return []

    def switch_variant(self, s=None):
        """Per variant table: the variant the setup is tied to; otherwise the one that was on when the panel opened."""
        try:
            chosen = (s or {}).get("variants") or {}
            for table, vs in self.variant_sets():
                want = chosen.get(table)
                if want is None and s is not None and s.get("variant") and not chosen:
                    want = s["name"]                             # older setups: variant named like the setup
                names = [str(v.get_display_text()).strip().lower() for v in vs]
                if want is None or want.strip().lower() not in names:
                    want = self.rest_variants.get(table)
                if want and want.strip().lower() in names:
                    vs[names.index(want.strip().lower())].switch_on()
        except Exception:
            fail("variant switch failed")

    def start_capture(self, ids=None):
        """Capture the given setups (None = all of them)."""
        todo = [s for s in self.cfg["setups"] if ids is None or s["id"] in ids]
        if self.cap_queue or not todo:
            return
        self.enable_grades(False)
        self.progress = [0, len(todo)]                           # setups captured so far, setups to capture
        self.cap_ids = [s["id"] for s in todo]
        q = []
        for s in todo:
            sid = s["id"]
            q += [("state", sid), ("wait", SETTLE_SEC), ("shot", sid, ""), ("wait", SHOT_SEC),
                  ("id", sid, True), ("wait", ID_SEC), ("shot", sid, "_id"), ("wait", SHOT_SEC), ("id", sid, False)]
        self.cap_queue, self.cap_until = q + [("done",)], 0.0
        self.shot_wait, self.missing = None, []
        glog.info("editor before the capture: " + self.editor_text())
        self.keep_rendering(True)
        self.cap_started = time.time()
        glog.info("capture started: " + ", ".join(self.cap_ids))
        self.hb.update(next=0.0, ticks=0, max_dt=0.0, last_wall=None, step="")
        self.write_state("Capturing")

    def shot_snapshot(self):
        """Every screenshot file that exists right now (so a new one can be told apart without comparing clocks)."""
        out = set()
        for d in [CAP_DIR] + SHOT_DIRS:
            out.update(os.path.normcase(f) for f in glob.glob(os.path.join(d, "**", "*.png"), recursive=True))
        return out

    def find_shot(self, path, before):
        """The screenshot just asked for: at `path`, or a new file Unreal wrote under another name or folder
        (then it is moved to `path`). Returns True when the file is there and finished."""
        if os.path.exists(path):                                 # deleted before the request, so it is new
            found = path
        else:
            skip = {os.path.normcase(os.path.join(CAP_DIR, n)) for n in ("preview.png",)}
            new = [f for f in self.shot_snapshot() - before - skip
                   if not os.path.basename(f).startswith(("mask_", "ref_"))]
            if not new:
                return False
            found = max(new, key=os.path.getmtime)
        try:
            size = os.path.getsize(found)
            if size < 1000 or size != self.shot_size.get(found):
                self.shot_size = {found: size}                   # still being written: check again next time
                return False
            if os.path.normcase(found) != os.path.normcase(path):
                shutil.move(found, path)
                info("screenshot was written to %s, moved to %s" % (found, path))
            return True
        except OSError:
            return False

    def keep_rendering(self, on):
        """While its window is not in front (the Grade Sheet window usually is), Unreal stops drawing: it uses
        less CPU, its interface goes to sleep, and a viewport that is not Realtime is never drawn again, so a
        requested screenshot is only taken once somebody clicks on Unreal. For the length of a capture all three
        are switched off, and the user's own settings are put back afterwards."""
        sl = unreal.SystemLibrary
        try:
            perf = perf_settings()
            if perf is None:
                # UE 5.6: the settings object is not reachable from Python. The editor's own "set" console command
                # changes it on the class defaults, which is what the editor reads every frame.
                # The value cannot be read back, so it is not put back afterwards either (that could switch on a
                # preference the user had switched off); it stays off until Unreal restarts.
                if on:
                    sl.execute_console_command(None, "set EditorPerformanceSettings bThrottleCPUWhenNotForeground 0")
                    glog.info("'use less CPU in background' switched off with the console command 'set'")
                    self.perf_note = ("If this keeps counting, turn off Editor Preferences > General > Performance > "
                                      "Use Less CPU when in Background")
            elif on:
                if self.throttle is None:
                    self.throttle = perf.get_editor_property("throttle_cpu_when_not_foreground")
                perf.set_editor_property("throttle_cpu_when_not_foreground", False)
            elif perf is not None and self.throttle is not None:
                perf.set_editor_property("throttle_cpu_when_not_foreground", self.throttle)
                self.throttle = None
        except Exception:
            glog.error("could not change 'use less CPU in background'")
            if on:
                self.perf_note = ("If this keeps counting, turn off Editor Preferences > General > Performance > "
                                  "Use Less CPU when in Background")
        try:                                                     # the editor interface must keep ticking
            if on and self.slate_sleep is None:
                try:
                    self.slate_sleep = bool(sl.get_console_variable_bool_value("Slate.AllowSlateToSleep"))
                except Exception:
                    self.slate_sleep = True                      # the engine default
                sl.execute_console_command(None, "Slate.AllowSlateToSleep 0")
            elif not on and self.slate_sleep is not None:
                sl.execute_console_command(None, "Slate.AllowSlateToSleep %d" % int(self.slate_sleep))
                self.slate_sleep = None
        except Exception:
            glog.error("could not change Slate.AllowSlateToSleep")
        try:                                                     # the viewport draws every frame (newer engines only)
            get = getattr(les, "editor_get_viewport_realtime", None)
            put = getattr(les, "editor_set_viewport_realtime", None)
            if get and put:
                if on and self.realtime is None:
                    self.realtime = bool(get())
                    if not self.realtime:
                        put(True)
                elif not on and self.realtime is not None:
                    if not self.realtime:
                        put(False)
                    self.realtime = None
        except Exception:
            glog.error("could not change the viewport's Realtime setting")
        if on:
            glog.info("  editor during the capture: " + self.editor_text())
            glog.info("drawing in the background: on (use less CPU was %s, interface sleep was %s, Realtime was %s)" % (
                self.throttle, self.slate_sleep, self.realtime))

    def shot_frames(self, on):
        """on: the next screenshot is rendered SHOT_FRAMES frames so it comes out finished. off: the user's value back."""
        sl = unreal.SystemLibrary
        try:
            if on and self.shot_delay is None:
                try:
                    self.shot_delay = int(sl.get_console_variable_int_value("r.HighResScreenshotDelay"))
                except Exception:
                    self.shot_delay = 4                          # the engine default
                sl.execute_console_command(None, "r.HighResScreenshotDelay %d" % max(SHOT_FRAMES, self.shot_delay))
            elif not on and self.shot_delay is not None:
                sl.execute_console_command(None, "r.HighResScreenshotDelay %d" % self.shot_delay)
                self.shot_delay = None
        except Exception:
            glog.error("could not change r.HighResScreenshotDelay")

    def end_capture(self):
        """Give Unreal back the way it was. Each step on its own, so one failure never leaves the rest undone."""
        self.cap_queue, self.shot_wait = [], None
        for step in (lambda: self.shot_frames(False), self.rest_state, lambda: self.switch_variant(None), self.leave_levels,
                     les.eject_pilot_level_actor, lambda: self.keep_rendering(False)):
            try:
                step()
            except Exception:
                glog.error("could not put everything back after the capture")

    def stop(self):
        """Stop for good: the window was closed, or Grade Sheet was opened again. A capture that is still running
        is ended first, so levels, grades and editor settings are never left the way the capture had them.
        Returns the ids of the setups that were not captured yet."""
        left = []
        if self.cap_queue or self.shot_wait:
            left = self.cap_ids[self.progress[0]:]
            glog.warn("capture ended early, not captured: " + (", ".join(left) or "none"))
            self.end_capture()
        self.alive = False
        if self.handle is not None:
            unreal.unregister_slate_post_tick_callback(self.handle)
            self.handle = None
        return left

    def capture_step(self):
        if self.cap_queue or self.shot_wait:
            redraw()                                             # every frame: the lighting settles, pending shots get taken
        if self.shot_wait:                                       # a screenshot was asked for: wait for its file
            path, before, deadline, label = self.shot_wait
            if self.t < self.next_shot_check:
                return
            self.next_shot_check = self.t + 0.5
            if self.find_shot(path, before):
                glog.info("  picture written after %.1f s: %s" % (self.t - (deadline - SHOT_TIMEOUT), os.path.basename(path)))
                self.shot_wait = None
                self.shot_frames(False)
            elif self.t > deadline:
                warn("no screenshot file after %ds: %s (also looked in %s)" % (SHOT_TIMEOUT, path, ", ".join(SHOT_DIRS)))
                glog.warn("  Unreal's own log, last lines:\n" + gdiag.unreal_log_tail(CAP_DIR, 80))
                self.missing.append(os.path.basename(path))
                self.shot_wait = None
                self.shot_frames(False)
            else:
                waited = int(self.t - (deadline - SHOT_TIMEOUT))
                if waited >= 3 and waited != self.shot_shown:
                    self.shot_shown = waited
                    self.write_state("%s: waiting for Unreal to write the picture (%d s)%s" % (
                        label, waited, (". " + self.perf_note if self.perf_note else
                                        ". If this keeps counting, click once on the Unreal window") if waited >= 15 else ""))
                    if waited in (15, 45):
                        warn("still waiting for %s after %d s" % (os.path.basename(path), waited))
                        glog.warn("  " + self.editor_text())
                        glog.warn("  Unreal's own log, last matching lines:\n" + gdiag.unreal_log_tail(CAP_DIR, 40, gdiag.STALL_WORDS))
                return
        if not self.cap_queue or self.t < self.cap_until:
            return
        step = self.cap_queue.pop(0)
        kind = step[0]
        self.hb["step"] = "%s %s%s" % (kind, step[1] if len(step) > 1 else "", " (part IDs)" if len(step) > 2 and step[2] else "")
        if kind == "wait":
            self.cap_until = self.t + step[1]
            return
        if kind == "done":
            self.end_capture()
            glog.info("capture finished in %.0f s, missing pictures: %s" % (
                time.time() - self.cap_started, ", ".join(self.missing) or "none"))
            glog.info("  editor after the capture: " + self.editor_text())
            self.capture_id += 1
            if self.missing:
                return self.write_state("Capture finished, but Unreal wrote no picture for %d shots (%s). "
                                        "See the Output Log (GRADE_PANEL)." % (len(self.missing), ", ".join(self.missing[:4])))
            return self.write_state("Capture finished")
        s = self.setup(step[1])
        cam = self.cams.get(s["cam"]) if s else None
        if not cam:
            return
        if kind == "state":
            show_levels(s["levels"])
            self.last_levels = list(s["levels"])
            self.switch_variant(s)
            les.pilot_level_actor(cam)                           # look through the camera while the lighting settles
            glog.info("  %s / %s: levels %s, variants %s" % (s["cam"], s["name"], ", ".join(s["levels"]),
                                                             s.get("variants") or ("by name" if s.get("variant") else "none")))
            self.write_state("Capturing %s / %s" % (s["cam"], s["name"]))
        elif kind == "id":
            self.enable_grades(False, s["id"] if step[2] else None)
            if not step[2]:
                self.progress[0] += 1                            # this setup is finished
        elif kind == "shot":
            path = os.path.join(CAP_DIR, step[1] + step[2] + ".png")
            if os.path.exists(path):
                os.remove(path)                                  # so an old picture is never taken for the new one
            label = "Capturing %s / %s%s" % (s["cam"], s["name"], " (part IDs)" if step[2] else "")
            before = self.shot_snapshot()
            self.shot_frames(not step[2])                        # the picture itself: many frames. Part IDs: as before
            glog.info("  screenshot requested: %s%s" % (os.path.basename(path), "" if step[2] else " (%d frames)" % SHOT_FRAMES))
            unreal.AutomationLibrary.take_high_res_screenshot(CAP_RES[0], CAP_RES[1], path)
            self.shot_wait, self.shot_size, self.shot_shown = (path, before, self.t + SHOT_TIMEOUT, label), {}, -1
            self.next_shot_check = self.t + 0.5
            self.write_state(label)

    def cvar(self, name):
        sl = unreal.SystemLibrary
        for get in ("get_console_variable_int_value", "get_console_variable_float_value", "get_console_variable_bool_value"):
            try:
                return getattr(sl, get)(name)
            except Exception:
                continue
        return "?"

    def editor_text(self):
        """The editor settings that decide whether Unreal keeps drawing, as one line."""
        rt = "?"
        try:
            get = getattr(les, "editor_get_viewport_realtime", None)
            rt = get() if get else "n/a"
        except Exception:
            pass
        try:
            thr = perf_settings().get_editor_property("throttle_cpu_when_not_foreground")
        except Exception:
            thr = "?"
        return "viewport Realtime %s | use-less-CPU-in-background %s | Slate.AllowSlateToSleep %s | r.HighResScreenshotDelay %s | t.MaxFPS %s | r.ScreenPercentage %s" % (
            rt, thr, self.cvar("Slate.AllowSlateToSleep"), self.cvar("r.HighResScreenshotDelay"), self.cvar("t.MaxFPS"),
            self.cvar("r.ScreenPercentage"))

    def heartbeat(self, dt):
        """While capturing: count frames, note slow ones, and every HEARTBEAT_SEC write where the capture is.
        A gap between heartbeats in the log means Unreal itself stopped running (its frames stopped)."""
        if not (self.cap_queue or self.shot_wait):
            self.hb.update(ticks=0, max_dt=0.0, last_wall=None)
            return
        now = time.time()
        hb = self.hb
        if hb["last_wall"] is not None and now - hb["last_wall"] > SLOW_FRAME_SEC:
            glog.warn("  Unreal did not run for %.1f s (one frame; step: %s, window in front: %s)" % (
                now - hb["last_wall"], hb["step"], gdiag.foreground_text(os.getpid())))
        hb["last_wall"] = now
        hb["ticks"] += 1
        hb["max_dt"] = max(hb["max_dt"], dt)
        if now < hb["next"]:
            return
        if hb["next"]:
            wait = ""
            if self.shot_wait:
                path, before, deadline, label = self.shot_wait
                wait = " | waiting %.0f s for %s" % (self.t - (deadline - SHOT_TIMEOUT), os.path.basename(path))
            elif self.t < self.cap_until:
                wait = " | settling, %.1f s left" % (self.cap_until - self.t)
            glog.info("  heartbeat: %d/%d done, step %s%s | %d frames in %.0f s (slowest %.2f s) | re-entered %d | in front: %s" % (
                self.progress[0], self.progress[1], hb["step"] or "-", wait, hb["ticks"], HEARTBEAT_SEC,
                hb["max_dt"], self.reentered, gdiag.foreground_text(os.getpid())))
        hb.update(next=now + HEARTBEAT_SEC, ticks=0, max_dt=0.0)
        try:                                                     # for the window's observer: Unreal is still running
            with open(os.path.join(CAP_DIR, "alive.txt"), "w") as f:
                f.write("%.1f %s" % (now, hb["step"]))
        except OSError:
            pass

    def preview_step(self):
        if self.preview_mark is not None and self.t >= self.preview_mark[0]:
            self.preview_key = self.preview_mark[1]              # which request this picture answers
            self.preview_mark = None
            self.preview_id += 1
            self.write_state()
        if self.preview_shots and self.t >= self.preview_shots[0][0] and not self.cap_queue and self.preview_mark is None:
            key = self.preview_shots.pop(0)[1]
            unreal.AutomationLibrary.take_high_res_screenshot(640, 360, os.path.join(CAP_DIR, "preview.png"))
            self.preview_mark = (self.t + 0.7, key)

    # ------------------------------------------------------------ lifetime
    def tick(self, dt):
        # Unreal can run this callback again from INSIDE a call it makes (a screenshot request or a level visibility
        # change pumps the interface). Working in that inner call nests level changes, which crashes Unreal with
        # "Assertion failed: Level->bIsVisible == bShouldBeVisible". So the inner call does nothing.
        if self.in_tick:
            self.reentered += 1
            return
        self.in_tick = True
        try:
            self.tick_once(dt)
        finally:
            self.in_tick = False

    def tick_once(self, dt):
        try:
            gone = self.proc is not None and self.proc.poll() is not None      # the window was closed
            if not self.alive or (gone and not (self.cap_queue or self.shot_wait)):
                self.stop()
                return
            if gone and not self.gone_logged:                    # closed in the middle of a capture: finish it first
                self.gone_logged = True
                glog.info("the window was closed during a capture; Unreal finishes the capture on its own")
            self.t += dt
            self.heartbeat(dt)
            self.capture_step()
            self.preview_step()
            if self.t >= self.next_poll:
                self.next_poll = self.t + 0.15
                for path in sorted(glob.glob(os.path.join(CMD_DIR, "*.json"))):
                    try:
                        cmd = None
                        with open(path) as f:
                            cmd = json.load(f)
                        os.remove(path)
                        self.handle_cmd(cmd)
                    except Exception:
                        fail("command failed: %s" % (json.dumps(cmd)[:300] if cmd else path))
                        self.write_state("Error: see Grade Sheet > Open log")
                        try:
                            os.remove(path)
                        except Exception:
                            pass
        except Exception:
            fail("error in the update loop; capture stopped")
            try:
                if self.cap_queue or self.shot_wait:             # tell the window, and give Unreal back as it was
                    self.end_capture()
                    self.capture_id += 1
                    self.write_state("Error: the capture stopped. See Grade Sheet > Open log")
            except Exception:
                self.cap_queue, self.shot_wait = [], None

    def start(self):
        try:
            world = get_world()
            perf = perf_settings()
            glog.info("=" * 70)
            glog.info("Grade Sheet opened | Unreal %s | project %s | map %s" % (
                unreal.SystemLibrary.get_engine_version(), PROJECT, world.get_name() if world else "?"))
            glog.info("%d sublevels, %d cine cameras, %d setups | assets in %s | captures in %s" % (
                len(sublevels()), len(self.cams), len(self.cfg["setups"]), PKG, CAP_DIR))
            glog.info("read-only levels: %s" % (", ".join(l["name"] for l in level_info() if l["readonly"]) or "none"))
            glog.info("editor 'use less CPU in background': %s" % (
                perf.get_editor_property("throttle_cpu_when_not_foreground") if perf else "not reachable from Python"))
        except Exception:
            glog.error("could not write the session header")
        unreal.SystemLibrary.execute_console_command(None, "r.CustomDepth 3")   # stencil on
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import importlib, grade_assets
        importlib.reload(grade_assets)
        built = grade_assets.ensure(PKG, CAP_DIR)                # builds the materials on first use
        if built:
            info("built materials: " + ", ".join(built))
        for path in glob.glob(os.path.join(CMD_DIR, "*.json")):      # drop stale commands
            os.remove(path)
        try:                                                     # what is switched on right now, per variant table
            self.rest_variants = {name: next((str(v.get_display_text()) for v in vs if v.is_active()), None)
                                  for name, vs in self.variant_sets()}
        except Exception:
            self.rest_variants = {}
        self.write_state("")
        # the window runs in the Python that ships with the engine (works the same on every 5.x install)
        base = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir()),
                            "Binaries", "ThirdParty", "Python3")
        exe = next((p for p in (os.path.join(base, "Win64", "pythonw.exe"), os.path.join(base, "Win64", "python.exe"),
                                os.path.join(base, "Mac", "bin", "python3"), os.path.join(base, "Linux", "bin", "python3"))
                    if os.path.exists(p)), None)
        if exe is None:
            raise RuntimeError("Could not find the engine's Python under " + base)
        self.proc = subprocess.Popen([exe, os.path.join(HERE, "grade_ui.py"), CAP_DIR])
        self.handle = unreal.register_slate_post_tick_callback(self.tick)


# The menu runs this file with runpy, which gives every run its own throw-away __main__ module, so the running
# panel cannot be kept there: it would never be found again, and opening Grade Sheet a second time would leave the
# first panel running next to the new one. This holder stays for the whole editor session.
SESSION = sys.modules.setdefault("_grade_sheet_session", type(sys)("_grade_sheet_session"))
try:
    old, left = getattr(SESSION, "bridge", None) or getattr(__main__, "_grade_bridge", None), []
    if old is not None:
        if hasattr(old, "stop"):
            left = old.stop()                  # ends a capture that is still running and puts Unreal back first
        old.alive = False
        if old.proc is not None and old.proc.poll() is None:
            old.proc.terminate()
    SESSION.bridge = Bridge()
    SESSION.bridge.start()
    if left:                                   # reopened in the middle of a capture: carry on with the rest
        SESSION.bridge.start_capture(left)
    info("started: %d cameras in the level, %d grade setups" % (len(SESSION.bridge.cams), len(SESSION.bridge.cfg["setups"])))
except Exception:
    fail("could not start")
