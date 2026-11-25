# Grade Sheet

A multi-camera grading contact sheet for Unreal Engine. See every camera of a scene side by side, each with its own
levels active, and grade them together so they match. Built for automotive real-time work where each camera angle
has its own level and lighting.

Requires **Unreal Engine 5.4 or newer** on **Windows**. Developed and tested on 5.8.

## Install

Pick one:

1. **Installer** - download this repository (Code > Download ZIP), unzip it, and drag your project's `.uproject`
   file onto `install.bat`.
2. **By hand** - copy this folder into your project as `<YourProject>/Plugins/GradeSheet`.
3. **Git** - from your project folder: `git clone <this repository's URL> Plugins/GradeSheet`

Then start (or restart) Unreal. **Grade Sheet** appears in the top menu bar and on the toolbar.

Nothing else to set up: the plugin is Python and HTML only, has no C++ to compile, and builds its materials inside
your project the first time you open it (in `Content/GradeSheet`). It enables the engine's *Python Editor Script
Plugin* and *Editor Scripting Utilities* for you.

## First use

1. **Grade Sheet > Assign part IDs to meshes** (only if your meshes have no stencil IDs yet). Existing stencil IDs
   are never changed: a mesh that already has a stencil value or renders custom depth is left as it is, and its ID
   shows up in the panel as a part. Meshes with no ID get new ones per material, taken only from numbers nothing in
   the open levels uses. It tells you the counts and asks before changing anything; a level with `_base` in its
   name, or a read-only level, asks again.
2. **Grade Sheet > Open Grade Sheet**. A window opens with the list of grade setups.
3. **Create new**: name the setup, optionally tie it to variants from your Variant Manager tables, tick the cameras
   (up to 8), then for each camera tick the levels that are active with it and choose the one level the grade lives
   in. A live preview from Unreal shows exactly what you ticked.
4. The contact sheet opens with one tile per camera. Grade, then **Save grades**.

## What you can do

- **Grade** with Nuke-style controls: blackpoint, whitepoint, lift, gain, multiply, offset, gamma, plus shadows,
  midtones and highlights, each with per-channel RGB values, and **saturation** (applied last in its layer;
  1 = unchanged, 0 = grey).
- **Exact values**: every control has a box next to its slider. Type a number and press Enter; it may go beyond
  the slider's range. Arrow up / down steps by 0.001 (0.01 with Shift), Esc cancels. Under **RGB** each channel has
  its own box.
- **Four layers per camera**: the whole frame, and layers A, B and C that only act inside a mask.
- **Mask by part** (click parts in the image, or tick whole levels or the background) and **by drawn bezier shapes**
  with per-shape feather. A layer with both grades the chosen parts only inside the shapes.
- **Match cameras**: set one as the match target, place a sample patch, and match another camera to it in one click.
- **Compare**: a before/after divider, and an uploaded **reference image** side by side with zoom and pan.
- **Full view** on any camera (or press `F`), **undo / redo** (`Ctrl+Z`, `Ctrl+Y`), `Ctrl`+click a slider to reset it.
- **Variants**: a setup tied to a variant switches the car to it for its capture and stacks on top of the always-on
  grade of the same camera.

## What it creates in your project

For a setup named `Overall` on a camera `Cam_01`:

| Thing | Name | Where |
| --- | --- | --- |
| Post process volume (unbound) | `PPV_Overall` (see naming below) | the level you chose for that camera |
| Grade material instance | `MI_Grade_Cam_01__Overall` | `Content/GradeSheet` |
| Drawn-shape mask texture | `T_Mask_Cam_01__Overall` | `Content/GradeSheet` |

Post process volumes are named the way artists name them before tagging (everything starts with `PPV_Nuke_`).
The studio Tool Suite (PPV and CCR > Tag PPV Nuke actors) then renames and tags them:

| Setup | Name the tool gives | After Tool Suite tagging |
| --- | --- | --- |
| Always on | `PPV_Nuke_<Name>` (`PPV_Nuke_Badge`) | `PPV_Badge` |
| Tied to variants, named like the variant | `PPV_Nuke_<code_code>` (`PPV_Nuke_8WF_9WF`) | `PPV_Variant_8WF-9WF` |
| Tied to variants, with its own name | `PPV_Nuke_<Name>_<code_code>` (`PPV_Nuke_Badge_8WF_9WF`) | `PPV_Badge_Variant_8WF-9WF` |

If a volume with either name already exists in the setup's level, the tool uses it instead of making a new one: it
adds its grade material to that volume's blendables and keeps everything else (for example PPM_NukeGrade materials),
its name and its priority. Such a volume is never deleted by the tool. A setup whose volume does not exist gets one
when the Grade Sheet opens, before a capture and on Save grades. Each camera's volume sits in that camera's level.

The grade switches on and off with its level, so no extra Blueprint logic is needed at runtime. A setup tied to a
variant is left **disabled** at rest; enable the volume whose name ends with the active variant from your own
runtime logic.

The list of setups is stored in `<YourProject>/Config/GradeSheetSetups.json`. Captures and reference images are in
`<YourProject>/Saved/GradeCaptures`.

## Good to know

- **Log.** Everything the tool does, and every error, is written to `Saved/GradeCaptures/GradeSheet.log` in your
  project (Unreal, the window and the page each tag their lines). Open it with **Grade Sheet > Open log** or the
  **Log** button in the window, and send it along when reporting a problem.

- **What it changes and saves in your project.** Nothing is saved until you press **Save grades**, and then only
  the tool's own folder (`Content/GradeSheet`). Levels are never saved or checked out by the tool: grade volumes it
  adds just make their level show as unsaved, and you save the levels yourself when you are ready. Showing and hiding levels
  for previews and captures does not mark any level as changed, and the current level is put back the way you had
  it. After a capture (or *Stop viewing*) the levels of the camera shown last stay visible and the others stay
  hidden, so you do not have to switch levels off by hand. The stencil ID view used for part picking sits on the tool's own grade volume, so
  cameras are not touched. Switching Variant Manager variants for a capture can mark the car's level as changed;
  the variant is switched back afterwards, so you can leave that level unsaved.
- **Perforce / read-only levels.** Levels that are not checked out count as read-only. The setup wizard shows a
  warning and an *Allow base / read-only levels* tick box instead of placing a grade there silently. Check those
  levels out yourself before you save them.

- **Captures while the window is in front.** For the length of a capture the tool keeps Unreal drawing in the
  background (it switches off *Use Less CPU when in Background* and the interface's sleep, and turns the viewport's
  Realtime on) and puts your settings back afterwards. If you close the window during a capture, Unreal finishes
  the capture on its own; if you open Grade Sheet again meanwhile, it carries on with the cameras that are left.
- **Finished pictures.** Each camera gets 6 seconds to settle, and its picture is rendered 64 frames before it is
  saved, so lighting, reflections and anti-aliasing are finished. If pictures still look soft or noisy in a heavy
  scene, raise `SHOT_FRAMES` (and `SETTLE_SEC`) at the top of `Content/Python/grade_panel.py`.
- **Stencil IDs box** in a layer's mask: type the stencil IDs to grade and press Enter, for example `20` for the
  car paint, or `20, 31-33`. `0` is the background. It stays in step with the part chips and Pick parts.
- **Grow / shrink and Soften edge** under the Stencil IDs box, like Erode / Dilate and Blur on a matte in Nuke.
  Grow / shrink moves the edge of the layer's stencil parts out (plus) or in (minus); Soften edge blurs it. Both are
  in pixels at 1080 lines, so a value looks the same at any capture size. They work on the stencil parts only;
  drawn shapes keep their own feather.
- **Drawing inside parts**: on a layer that has stencil parts and no shape yet, Draw a new shape draws into that
  layer, so only the parts inside the shape are graded (a roto inside a matte).
- **Create PPVs for colours** (Setups > Create PPVs for colours): tick the levels, then the colours from Variant
  Manager (tables with COLOR in the name; otherwise every table). Unreal makes one `PPV_Nuke_<code_code>` per colour in
  each level: unbound, enabled, attached to the level's `PPV_CarPaint` actor (or put in a `PPV_CarPaint` folder).
  Volumes that are already there are left alone; locked (read-only) levels are reported. Afterwards tag the volumes
  with Tool Suite > PPV and CCR > Tag PPV Nuke actors, and save the levels.
- **When a capture gets stuck**, press **Diagnostics** in the window. It saves one zip (Grade Sheet's log, the end
  of Unreal's own log, what the GPU and other programs were doing, which window was in front) and opens its
  folder, ready to send. During a capture the log also gets a line every 5 s from Unreal and every 10 s from the
  window, so a stall shows exactly where it happened.
- The tiles are captured stills. After changing lighting, the model or a camera, press **Recapture all**.
- Grades run after tonemapping, so what you see in the sheet is what the viewport shows.
- Part masks use custom stencil. For a packaged build set **Project Settings > Rendering > Custom Depth-Stencil
  Pass** to *Enabled with Stencil*.
- Part masks read whatever stencil IDs the meshes have (1 to 255). The tool never rewrites an existing ID.
- Part-mask edges are not anti-aliased. Drawn shapes belong to one camera's framing.
- The window is served from your own machine only (`127.0.0.1`) and opens in Edge or Chrome as an app window.
