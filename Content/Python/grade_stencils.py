"""Grade Sheet - give meshes a custom stencil ID so grades can be masked by part.
Menu: Grade Sheet > Assign part IDs to meshes.

Every static mesh in a sublevel gets an ID, grouped by its first material (same material = same ID).
The persistent level is left alone (it counts as background). A base or read-only level asks first.
A grade layer can address 24 IDs; with more materials than that, the extra ones share the last ID.
"""
import os
import unreal

MAX_ID = 24


def run():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    levels = unreal.EditorLevelUtils.get_levels(world)
    content = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
    allowed = {}
    for level in levels[1:]:
        name = level.get_path_name().split("/")[-1].split(".")[0]
        pkg = level.get_outermost().get_name()
        f = os.path.join(content, pkg[len("/Game/"):] + ".umap") if pkg.startswith("/Game/") else ""
        readonly = bool(f) and os.path.exists(f) and not os.access(f, os.W_OK)
        allowed[level] = (name, "_base" in name.lower(), readonly)

    by_level = {}
    for a in eas.get_all_level_actors():
        if isinstance(a, unreal.StaticMeshActor) and a.get_outer() in allowed:
            by_level.setdefault(a.get_outer(), []).append(a)
    if not by_level:
        unreal.EditorDialog.show_message("Grade Sheet", "No static meshes found in any sublevel.", unreal.AppMsgType.OK)
        return

    groups, skipped = {}, []
    for level, actors in by_level.items():
        name, base, readonly = allowed[level]
        if base or readonly:
            why = "is a base level" if base else "is read-only (not checked out)"
            answer = unreal.EditorDialog.show_message(
                "Grade Sheet", "%s %s.\nIt has %d meshes. Assign part IDs to them anyway?" % (name, why, len(actors)),
                unreal.AppMsgType.YES_NO)
            if answer != unreal.AppReturnType.YES:
                skipped.append(name)
                continue
        for a in actors:
            comp = a.static_mesh_component
            mat = comp.get_material(0)
            groups.setdefault(mat.get_name() if mat else "NoMaterial", []).append(comp)

    unreal.SystemLibrary.execute_console_command(None, "r.CustomDepth 3")
    names = sorted(groups)
    with unreal.ScopedEditorTransaction("Grade Sheet: assign part IDs"):
        for i, name in enumerate(names):
            sid = min(i + 1, MAX_ID)
            for comp in groups[name]:
                comp.modify()
                comp.set_editor_property("render_custom_depth", True)
                comp.set_editor_property("custom_depth_stencil_value", sid)
    msg = "%d meshes in %d material groups now have a part ID." % (sum(len(v) for v in groups.values()), len(names))
    if len(names) > MAX_ID:
        msg += "\n\nThere are more than %d materials, so the last %d share one ID." % (MAX_ID, len(names) - MAX_ID + 1)
    if skipped:
        msg += "\n\nSkipped levels: " + ", ".join(skipped)
    msg += "\n\nSave the changed levels, then press Recapture all in the Grade Sheet."
    unreal.EditorDialog.show_message("Grade Sheet", msg, unreal.AppMsgType.OK)


run()
