"""Grade Sheet plugin - runs when the editor starts and adds the menu and the toolbar button.

    Main menu:  Grade Sheet > Open Grade Sheet
                Grade Sheet > Assign part IDs to meshes
    Toolbar:    Grade Sheet button
"""
import os, sys
import unreal


def _here():
    if "__file__" in globals():
        return os.path.dirname(os.path.abspath(__file__))
    for p in sys.path:                                   # fallback: find our folder on the Python path
        if os.path.exists(os.path.join(p, "grade_panel.py")):
            return os.path.abspath(p)
    return ""


HERE = _here().replace("\\", "/")


def _run(script):
    return "import runpy; runpy.run_path(r'%s/%s', run_name='__main__')" % (HERE, script)


def _entry(name, label, tip, script, kind=unreal.MultiBlockType.MENU_ENTRY):
    e = unreal.ToolMenuEntry(name=name, type=kind)
    e.set_label(label)
    e.set_tool_tip(tip)
    e.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "", _run(script))
    return e


def register():
    if not HERE:
        unreal.log_warning("Grade Sheet: could not find the plugin's Python folder")
        return
    try:                                                 # written for Unreal Engine 5.4 and newer
        major, minor = [int(x) for x in unreal.SystemLibrary.get_engine_version().split("-")[0].split(".")[:2]]
        if (major, minor) < (5, 4):
            unreal.log_warning("Grade Sheet is written for Unreal Engine 5.4 or newer; this is %d.%d" % (major, minor))
    except Exception:
        pass
    menus = unreal.ToolMenus.get()
    main = menus.find_menu("LevelEditor.MainMenu")
    if main:
        sub = main.add_sub_menu("GradeSheet", "", "GradeSheet", "Grade Sheet", "Multi-camera grading contact sheet")
        sub.add_menu_entry("Main", _entry("GradeSheetOpen", "Open Grade Sheet",
                                          "Choose cameras and levels, then grade every camera side by side", "grade_panel.py"))
        sub.add_menu_entry("Main", _entry("GradeSheetStencils", "Assign part IDs to meshes",
                                          "Give meshes a stencil ID per material, so grades can be masked by part",
                                          "grade_stencils.py"))
    bar = menus.find_menu("LevelEditor.LevelEditorToolBar.User")
    if bar:
        bar.add_menu_entry("GradeSheet", _entry("GradeSheetButton", "Grade Sheet", "Open the Grade Sheet", "grade_panel.py",
                                                unreal.MultiBlockType.TOOL_BAR_BUTTON))
    menus.refresh_all_widgets()
    unreal.log("Grade Sheet: menu registered (%s)" % HERE)


register()
