"""Grade Sheet - open the tool's log file (menu: Grade Sheet > Open log)."""
import os, sys
import unreal

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import grade_log as glog

glog.setup(os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()), "GradeCaptures"), "unreal")
if not glog.open_in_editor():
    unreal.EditorDialog.show_message("Grade Sheet", "There is no log yet. It is written at\n%s\nonce Grade Sheet has been opened."
                                     % glog.PATH, unreal.AppMsgType.OK)
