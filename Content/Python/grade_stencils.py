"""Grade Sheet - give meshes a custom stencil ID so grades can be masked by part.
Menu: Grade Sheet > Assign part IDs to meshes.

Stencil IDs that already exist are never changed. A mesh that already renders custom depth, or already has a
stencil value, is left exactly as it is, and the panel shows its ID as a part you can pick (IDs 1-255).

Only static meshes in sublevels that have no ID at all are offered new ones, grouped by first material
(same material = same ID). New IDs are taken only from numbers nothing in the open levels uses yet, so they
cannot collide with an existing ID. The persistent level is left alone. A base or read-only level asks first.
"""
import os
import unreal

TAG = "GradeSheetPartID"           # marks actors whose ID this tool assigned (so a second run can reuse it)
ask = unreal.EditorDialog.show_message


def run():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    levels = unreal.EditorLevelUtils.get_levels(world)
    content = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
    info = {}
    for level in levels[1:]:
        name = level.get_path_name().split("/")[-1].split(".")[0]
        pkg = level.get_outermost().get_name()
        f = os.path.join(content, pkg[len("/Game/"):] + ".umap") if pkg.startswith("/Game/") else ""
        readonly = bool(f) and os.path.exists(f) and not os.access(f, os.W_OK)
        info[level] = (name, "_base" in name.lower(), readonly)

    used, kept, own, todo = set(), 0, {}, {}       # own: material -> ID this tool gave earlier
    for a in eas.get_all_level_actors():
        for c in a.get_components_by_class(unreal.PrimitiveComponent):      # every ID in use, in every level
            v = int(c.get_editor_property("custom_depth_stencil_value"))
            if v:
                used.add(v)
        if not isinstance(a, unreal.StaticMeshActor) or a.get_outer() not in info:
            continue
        comp = a.static_mesh_component
        mat = comp.get_material(0)
        key = mat.get_name() if mat else "NoMaterial"
        sid = int(comp.get_editor_property("custom_depth_stencil_value"))
        if sid or comp.get_editor_property("render_custom_depth"):
            kept += 1
            if sid and TAG in [str(t) for t in a.tags]:
                own.setdefault(key, sid)
        else:
            todo.setdefault(a.get_outer(), []).append((a, comp, key))

    n_todo = sum(len(v) for v in todo.values())
    if not n_todo:
        ask("Grade Sheet", ("All %d meshes already have a stencil ID. Nothing was changed." % kept) if kept
            else "No static meshes found in any sublevel.", unreal.AppMsgType.OK)
        return
    if kept:
        answer = ask("Grade Sheet",
                     "%d meshes already have a stencil ID or custom depth setting. They will NOT be changed.\n\n"
                     "%d meshes have none. Give those new IDs, using only numbers nothing uses yet?" % (kept, n_todo),
                     unreal.AppMsgType.YES_NO)
        if answer != unreal.AppReturnType.YES:
            return

    groups, skipped = {}, []
    for level, items in todo.items():
        name, base, readonly = info[level]
        if base or readonly:
            why = "is a base level" if base else "is read-only (not checked out)"
            answer = ask("Grade Sheet", "%s %s.\n%d of its meshes have no ID. Assign new IDs to them anyway?"
                         % (name, why, len(items)), unreal.AppMsgType.YES_NO)
            if answer != unreal.AppReturnType.YES:
                skipped.append(name)
                continue
        for a, comp, key in items:
            groups.setdefault(key, []).append((a, comp))

    free = [i for i in range(1, 256) if i not in used]
    done, no_room = 0, []
    unreal.SystemLibrary.execute_console_command(None, "r.CustomDepth 3")
    with unreal.ScopedEditorTransaction("Grade Sheet: assign part IDs"):
        for key in sorted(groups):
            sid = own.get(key) or (free.pop(0) if free else 0)
            if not sid:
                no_room.append(key)
                continue
            for a, comp in groups[key]:
                if comp.get_editor_property("render_custom_depth") or \
                        int(comp.get_editor_property("custom_depth_stencil_value")):
                    continue                       # safety net: never write over an existing setting
                a.modify()
                comp.modify()
                comp.set_editor_property("render_custom_depth", True)
                comp.set_editor_property("custom_depth_stencil_value", sid)
                a.tags = list(a.tags) + [TAG]
                done += 1
    msg = "%d meshes got a new part ID. %d existing ones were left untouched." % (done, kept)
    if no_room:
        msg += "\n\nAll 255 stencil numbers are in use, so these materials got no ID: " + ", ".join(no_room[:12])
    if skipped:
        msg += "\n\nSkipped levels: " + ", ".join(skipped)
    msg += "\n\nSave the changed levels, then press Recapture all in the Grade Sheet."
    ask("Grade Sheet", msg, unreal.AppMsgType.OK)


run()
