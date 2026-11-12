"""Multi-camera grading contact sheet - Unreal side.
Open it from the menu: Grade Sheet > Open Grade Sheet (or the Grade Sheet toolbar button).

No window of its own: it starts grade_ui.py (separate process showing grade_ui.html)
and applies what that window asks for, through JSON files in Saved/GradeCaptures.

GRADE SETUP = one named grade for one camera:
    camera + the levels that are active for it + the ONE level the grade lives in.
In Unreal a setup is:
    PPV_Grade_<id>   unbound post process volume, placed in the setup's target level
    MI_Grade_<id>    instance of M_NukeGrade (whole frame + layers A, B, C)
    T_Mask_<id>      drawn-shape mask (R/G/B = layers A/B/C)
so the grade switches on and off with its level. Setups are listed in <project>/Config/GradeSheetSetups.json.
"""
import os, re, sys, json, glob, shutil, subprocess, traceback
import unreal
import __main__

PKG = "/Game/GradeSheet"                 # where the tool keeps its materials, grade instances and masks
CAP_RES = (1280, 720)
SETTLE_SEC, SHOT_SEC, ID_SEC = 4.0, 2.5, 1.0
PARAM_NAMES = ["Blackpoint", "Whitepoint", "Lift", "Gain", "Multiply", "Offset", "Gamma",
               "Shadows", "Midtones", "Highlights"]
PREFIXES = ["", "L1_", "L2_", "L3_"]
MAX_ID, MAX_CAMS = 24, 8
CAP_DIR = os.path.abspath(os.path.join(unreal.Paths.convert_relative_path_to_full(
    unreal.Paths.project_saved_dir()), "GradeCaptures"))
CMD_DIR = os.path.join(CAP_DIR, "cmd")
os.makedirs(CMD_DIR, exist_ok=True)
PROJECT = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(PROJECT, "Scripts")
CFG = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_config_dir()), "GradeSheetSetups.json")
if not os.path.exists(CFG) and os.path.exists(os.path.join(PROJECT, "Scripts", "grade_setups.json")):
    shutil.copyfile(os.path.join(PROJECT, "Scripts", "grade_setups.json"), CFG)        # setups made before the plugin existed

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


def show_levels(names):
    """names = levels to show (None = all)."""
    for n, l in sublevels().items():
        unreal.EditorLevelUtils.set_level_visibility(l, names is None or n in names, False)


def safe(s):
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s)).strip("_") or "x"


def set_id_view(cam, on):
    """Weight of M_StencilID on the camera (added once, other blendables are left alone). 1 = show stencil IDs."""
    comp = cam.get_cine_camera_component()
    pp = comp.get_editor_property("post_process_settings")
    idmat = eal.load_asset(PKG + "/M_StencilID")
    wbs = pp.get_editor_property("weighted_blendables")
    arr = list(wbs.get_editor_property("array"))
    mine = [b for b in arr if b.get_editor_property("object") == idmat]
    if not mine:
        wb = unreal.WeightedBlendable()
        wb.set_editor_property("object", idmat)
        arr.append(wb)
        mine = [wb]
    for b in mine:
        b.set_editor_property("weight", 1.0 if on else 0.0)
    wbs.set_editor_property("array", arr)
    pp.set_editor_property("weighted_blendables", wbs)
    comp.set_editor_property("post_process_settings", pp)


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
        self.rest_variants, self.changed = {}, set()
        self.cfg = {"cameras": [], "setups": []}
        if os.path.exists(CFG):
            try:
                with open(CFG) as f:
                    self.cfg = json.load(f)
            except Exception:
                unreal.log_warning("GRADE_PANEL could not read " + CFG)
        self.scan()

    def scan(self):
        """Cameras, grade volumes and stencil parts currently in the world."""
        self.cams, self.ppvs, parts, lv = {}, {}, {}, {}
        for a in eas.get_all_level_actors():
            if isinstance(a, unreal.CineCameraActor):
                self.cams[a.get_actor_label()] = a
            elif isinstance(a, unreal.PostProcessVolume) and a.get_actor_label().startswith("PPV_Grade_"):
                self.ppvs[a.get_actor_label()[len("PPV_Grade_"):]] = a
            elif isinstance(a, unreal.StaticMeshActor):
                comp = a.static_mesh_component
                if comp.get_editor_property("render_custom_depth"):
                    sid = int(comp.get_editor_property("custom_depth_stencil_value"))
                    if 0 < sid <= MAX_ID:
                        mat = comp.get_material(0)
                        parts.setdefault(sid, set()).add(mat.get_name() if mat else a.get_actor_label())
                        lv.setdefault(sid, set()).add(lname(a.get_outer()))
        self.parts = [{"id": k, "name": ", ".join(sorted(v)), "level": ", ".join(sorted(lv[k]))}
                      for k, v in sorted(parts.items())]

    def setup(self, sid):
        for s in self.cfg["setups"]:
            if s["id"] == sid:
                return s
        return None

    def save_cfg(self):
        with open(CFG, "w") as f:
            json.dump(self.cfg, f, indent=1)

    # ------------------------------------------------------------ grade instances
    def mi(self, sid):
        path = PKG + "/MI_Grade_" + sid
        return eal.load_asset(path) if eal.does_asset_exist(path) else None

    def read_layers(self, sid):
        mi, layers = self.mi(sid), []
        for k, prefix in enumerate(PREFIXES):
            params, mask = {}, []
            if mi:
                for p in PARAM_NAMES:
                    c = mel.get_material_instance_vector_parameter_value(mi, prefix + p)
                    params[p] = [c.r, c.g, c.b]
                if k:
                    bits = int(round(mel.get_material_instance_scalar_parameter_value(mi, prefix + "Mask")))
                    mask = [i for i in range(1, MAX_ID + 1) if bits >> (i - 1) & 1]
                    if mel.get_material_instance_scalar_parameter_value(mi, prefix + "BG") > 0.5:
                        mask.insert(0, 0)                  # 0 = background (everything without a stencil ID)
            layers.append({"params": params, "mask": mask})
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
                bits = sum(1 << (i - 1) for i in ids if 0 < i <= MAX_ID)
                bg = 0 in ids
                mel.set_material_instance_scalar_parameter_value(mi, prefix + "Mask", float(bits))
                mel.set_material_instance_scalar_parameter_value(mi, prefix + "BG", 1.0 if bg else 0.0)
                mel.set_material_instance_scalar_parameter_value(mi, prefix + "UseParts", 1.0 if (bits or bg) else 0.0)
                mel.set_material_instance_scalar_parameter_value(
                    mi, prefix + "UseShape", 1.0 if layers[k].get("shape") else 0.0)
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
            eal.save_asset(path, False)
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
            eal.save_asset(PKG + "/MI_Grade_" + sid, False)
        vol = self.ppvs.get(sid)
        if vol and lname(vol.get_outer()) != s["target"]:
            eas.destroy_actor(vol)                               # target level changed: rebuild it there
            vol = None
        if not vol:
            level = sublevels().get(s["target"])
            if not level:
                raise RuntimeError("Level %s is not loaded" % s["target"])
            unreal.EditorLevelUtils.set_level_visibility(level, True, False)
            les.set_current_level_by_name(s["target"])
            vol = eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0))
            les.set_current_level_by_name(lname(unreal.EditorLevelUtils.get_levels(get_world())[0]))
            vol.set_actor_label("PPV_Grade_" + sid)
            vol.set_editor_property("unbound", True)
            self.ppvs[sid] = vol
        vol.set_editor_property("priority", 10.0 + float(s.get("order", 0)))
        wb = unreal.WeightedBlendable()
        wb.set_editor_property("weight", 1.0)
        wb.set_editor_property("object", mi)
        wbs = unreal.WeightedBlendables()
        wbs.set_editor_property("array", [wb])
        settings = vol.get_editor_property("settings")
        settings.set_editor_property("weighted_blendables", wbs)
        vol.set_editor_property("settings", settings)

    def remove_setup(self, sid):
        vol = self.ppvs.pop(sid, None)
        if vol:
            eas.destroy_actor(vol)
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
                errors.append("%s: %s" % (s["name"], e))
        for n in cams:
            set_id_view(self.cams[n], False)
        self.rest_state()
        self.save_cfg()
        msg = "%d cameras, %d grade setups" % (len(cams), len(new))
        if skipped:
            msg += ". Skipped (protected level): " + ", ".join(skipped)
        if errors:
            msg += ". Errors: " + "; ".join(errors)
        self.write_state(msg)

    # ------------------------------------------------------------ state for the window
    def write_state(self, status=None):
        if status is not None:
            self.status = status
        setups = []
        for s in self.cfg["setups"]:
            d = dict(s)
            d["layers"] = self.read_layers(s["id"])
            setups.append(d)
        state = {"capture_id": self.capture_id, "status": self.status, "busy": bool(self.cap_queue),
                 "preview_id": self.preview_id, "progress": getattr(self, "progress", [0, 0]), "preview_key": self.preview_key, "variant_sets": self.variant_tables(),
                 "parts": self.parts, "levels": level_info(), "max_cams": MAX_CAMS,
                 "all_cameras": [{"name": n, "level": lname(a.get_outer())} for n, a in sorted(self.cams.items())],
                 "cameras": self.cfg["cameras"], "setups": setups}
        tmp = os.path.join(CAP_DIR, "state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, os.path.join(CAP_DIR, "state.json"))

    # ------------------------------------------------------------ commands
    def handle_cmd(self, c):
        op = c.get("op")
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
            for sid, layers in c.get("grades", {}).items():
                if self.apply(sid, layers):
                    eal.save_asset(PKG + "/MI_Grade_" + sid, False)
            for path in sorted(self.dirty_masks):
                eal.save_asset(path, False)
            self.dirty_masks = set()
            self.rest_state()
            levels = sublevels()
            pkgs = [levels[s["target"]].get_outermost() for s in self.cfg["setups"] if s["target"] in levels]
            if pkgs:
                unreal.EditorLoadingAndSavingUtils.save_packages(pkgs, True)
            with open(os.path.join(CAP_DIR, "grades.json"), "w") as f:
                json.dump(c.get("grades", {}), f, indent=1)
            self.save_cfg()
            self.write_state("Saved %d grade setups and their levels" % len(self.cfg["setups"]))
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
                self.rest_state(s["id"])
                self.switch_variant(s)
                les.pilot_level_actor(self.cams[s["cam"]])
        elif op == "eject":
            les.eject_pilot_level_actor()
            self.rest_state()
            self.switch_variant(None)
            show_levels(None)
        elif op == "capture":
            self.scan()                                          # pick up newly assigned part IDs
            self.start_capture(c.get("ids"))
        elif op == "py":                                         # run another script from the Scripts folder
            path = os.path.abspath(os.path.join(HERE, os.path.basename(c.get("file", ""))))
            with open(path) as f:
                exec(compile(f.read(), path, "exec"), {"__name__": "__main__", "__file__": path})
            self.scan()
            self.write_state("Ran " + os.path.basename(path))

    # ------------------------------------------------------------ capture: one still per setup, grades off
    def enable_grades(self, on):
        for vol in self.ppvs.values():
            vol.set_editor_property("enabled", on)

    def rest_state(self, active=None):
        """Always-on setups enabled. A variant setup (for example a paint code) is enabled only while it is
        the one being viewed; at rest it is off and the production script enables it by its name."""
        for s in self.cfg["setups"]:
            vol = self.ppvs.get(s["id"])
            if vol:
                vol.set_editor_property("enabled", (not s.get("variant")) or s["id"] == active)

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
            unreal.log_warning("GRADE_PANEL variant switch failed: " + traceback.format_exc())

    def start_capture(self, ids=None):
        """Capture the given setups (None = all of them)."""
        todo = [s for s in self.cfg["setups"] if ids is None or s["id"] in ids]
        if self.cap_queue or not todo:
            return
        self.enable_grades(False)
        self.progress = [0, len(todo)]                           # setups captured so far, setups to capture
        q = []
        for s in todo:
            sid = s["id"]
            q += [("state", sid), ("wait", SETTLE_SEC), ("shot", sid, ""), ("wait", SHOT_SEC),
                  ("id", sid, True), ("wait", ID_SEC), ("shot", sid, "_id"), ("wait", SHOT_SEC), ("id", sid, False)]
        self.cap_queue, self.cap_until = q + [("done",)], 0.0
        self.write_state("Capturing")

    def capture_step(self):
        if not self.cap_queue or self.t < self.cap_until:
            return
        step = self.cap_queue.pop(0)
        kind = step[0]
        if kind == "wait":
            self.cap_until = self.t + step[1]
            return
        if kind == "done":
            self.rest_state()
            self.switch_variant(None)
            show_levels(None)
            les.eject_pilot_level_actor()
            self.capture_id += 1
            return self.write_state("Capture finished")
        s = self.setup(step[1])
        cam = self.cams.get(s["cam"]) if s else None
        if not cam:
            return
        if kind == "state":
            show_levels(s["levels"])
            self.switch_variant(s)
            les.pilot_level_actor(cam)                           # look through the camera while the lighting settles
            self.write_state("Capturing %s / %s" % (s["cam"], s["name"]))
        elif kind == "id":
            set_id_view(cam, step[2])
            if not step[2]:
                self.progress[0] += 1                            # this setup is finished
        elif kind == "shot":
            unreal.AutomationLibrary.take_high_res_screenshot(
                CAP_RES[0], CAP_RES[1], os.path.join(CAP_DIR, step[1] + step[2] + ".png"))

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
        try:
            if not self.alive or (self.proc is not None and self.proc.poll() is not None):
                unreal.unregister_slate_post_tick_callback(self.handle)   # window closed, or replaced
                self.alive = False
                return
            self.t += dt
            self.capture_step()
            self.preview_step()
            if self.t >= self.next_poll:
                self.next_poll = self.t + 0.15
                for path in sorted(glob.glob(os.path.join(CMD_DIR, "*.json"))):
                    try:
                        with open(path) as f:
                            cmd = json.load(f)
                        os.remove(path)
                        self.handle_cmd(cmd)
                    except Exception:
                        unreal.log_warning("GRADE_PANEL cmd failed: " + traceback.format_exc())
                        self.write_state("Error: see the Unreal output log")
                        try:
                            os.remove(path)
                        except Exception:
                            pass
        except Exception:
            unreal.log_warning("GRADE_PANEL " + traceback.format_exc())
            self.cap_queue = []

    def start(self):
        unreal.SystemLibrary.execute_console_command(None, "r.CustomDepth 3")   # stencil on
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import importlib, grade_assets
        importlib.reload(grade_assets)
        built = grade_assets.ensure(PKG, CAP_DIR)                # builds the materials on first use
        if built:
            unreal.log("GRADE_PANEL built: " + ", ".join(built))
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


try:
    old = getattr(__main__, "_grade_bridge", None)
    if old is not None:
        old.alive = False                      # old one unregisters itself on its next tick
        if old.proc is not None and old.proc.poll() is None:
            old.proc.terminate()
    __main__._grade_bridge = Bridge()
    __main__._grade_bridge.start()
    unreal.log("GRADE_PANEL started: %d cameras in the level, %d grade setups" % (
        len(__main__._grade_bridge.cams), len(__main__._grade_bridge.cfg["setups"])))
except Exception:
    unreal.log_error("GRADE_PANEL failed:\n" + traceback.format_exc())
