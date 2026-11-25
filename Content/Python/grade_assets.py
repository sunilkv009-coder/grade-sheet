"""Grade Sheet - the materials the tool needs. They are built from code, so the plugin works in any project
and engine version without shipping .uasset files.

    M_NukeGrade   post process grade: whole frame + layers A, B, C
                  each layer = Nuke Grade (blackpoint, whitepoint, lift, gain, multiply, offset, gamma)
                               + tonal gains (shadows, midtones, highlights)
                               + saturation (applied last, around the Rec.709 luminance)
                  each layer's mask = (stencil parts / background, if chosen) x (drawn shapes, if drawn)
                  the stencil part of a mask can grow / shrink (dilate / erode) and soften (blur) at its edge
    M_StencilID   shows custom stencil IDs as colour (used for the part picker capture)
    T_Mask_None   black default for the drawn-shape mask
"""
import os, struct, zlib
import unreal

VERSION = "7"                      # bump to rebuild the materials in existing projects
PARAMS = [("Blackpoint", 0.0), ("Whitepoint", 1.0), ("Lift", 0.0), ("Gain", 1.0),
          ("Multiply", 1.0), ("Offset", 0.0), ("Gamma", 1.0),
          ("Shadows", 1.0), ("Midtones", 1.0), ("Highlights", 1.0), ("Saturation", 1.0)]
LAYERS = [("", "0 Whole frame"), ("L1_", "1 Layer A"), ("L2_", "2 Layer B"), ("L3_", "3 Layer C")]
mel = unreal.MaterialEditingLibrary
eal = unreal.EditorAssetLibrary
tools = unreal.AssetToolsHelpers.get_asset_tools()


def _after_tonemap(mat):
    mat.set_editor_property("material_domain", unreal.MaterialDomain.MD_POST_PROCESS)
    for n in ("BL_SCENE_COLOR_AFTER_TONEMAPPING", "BL_AFTER_TONEMAPPING"):
        loc = getattr(unreal.BlendableLocation, n, None)
        if loc is not None:
            mat.set_editor_property("blendable_location", loc)
            return


def _default_mask(pkg, tmp_dir):
    path = pkg + "/T_Mask_None"
    if eal.does_asset_exist(path):
        return eal.load_asset(path)
    png = os.path.join(tmp_dir, "mask_none.png")
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * 4 for _ in range(4))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    with open(png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", png)
    task.set_editor_property("destination_path", pkg)
    task.set_editor_property("destination_name", "T_Mask_None")
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", False)
    task.set_editor_property("factory", unreal.TextureFactory())
    tools.import_asset_tasks([task])
    tex = eal.load_asset(path)
    tex.set_editor_property("srgb", False)
    tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_VECTOR_DISPLACEMENTMAP)
    tex.set_editor_property("mip_gen_settings", unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS)
    eal.save_asset(path, False)
    return tex


# One layer's stencil-parts mask, with its edge moved (Grow, pixels at 1080 lines; negative = erode) and
# softened (Soft, pixels at 1080 lines). The signed distance to the nearest pixel on the other side of the edge
# is found on rings of 16 samples, then mask = saturate((distance + grow) / soft + 0.5). The same maths runs
# in the window (grade_ui.html, partMask) so the preview matches.
# Cen = the stencil at this pixel. IDs: W0..W10 hold 24 stencil IDs each (bit (id-1)%24 of word (id-1)/24); BG = 1 grades the background (ID 0).
EDGE_HLSL = r"""
float W[11] = { W0, W1, W2, W3, W4, W5, W6, W7, W8, W9, W10 };
float2 uv0 = GetDefaultSceneTextureUV(Parameters, STENCIL_ID);
float sc = max(VS.y, 1.0) / 1080.0;
float g = Grow * sc;
float b = max(Soft, 0.0) * sc;
float idc = round(Cen.r);
float c = BG;
if (idc > 0.5) { float i1 = min(idc, 255.0) - 1.0; float wi = floor(i1 / 24.0);
  c = fmod(floor(W[(int)wi] / exp2(i1 - wi * 24.0)), 2.0) >= 0.5 ? 1.0 : 0.0; }
if (abs(g) < 0.01 && b < 0.01) return c;
float R = min(abs(g) + b * 0.5 + 1.0, 96.0);
float st = max(1.0, R / 16.0);
float m = R + 1.0;
[loop] for (int ri = 1; ri <= 17; ri++) {
  float r = ri * st;
  if (r > R + 0.001 || r >= m) break;
  [loop] for (int a = 0; a < 16; a++) {
    float ang = (a + 0.5 * (ri % 2)) * 0.3926991;
    float2 uv = uv0 + float2(cos(ang), sin(ang)) * r * Inv;
    float idn = round(SceneTextureLookup(uv, STENCIL_ID, false).r);
    float cn = BG;
    if (idn > 0.5) { float j1 = min(idn, 255.0) - 1.0; float wj = floor(j1 / 24.0);
      cn = fmod(floor(W[(int)wj] / exp2(j1 - wj * 24.0)), 2.0) >= 0.5 ? 1.0 : 0.0; }
    if (cn != c) m = min(m, r);
  }
}
float s = (c > 0.5 ? 1.0 : -1.0) * (m - 0.5) + g;
return b > 0.01 ? saturate(s / b + 0.5) : (s > 0.0 ? 1.0 : 0.0);
"""


def build_grade(mat, none_tex):
    """(Re)build the node graph of a grade material in place. Instances keep their values (same parameter names)."""
    mel.delete_all_material_expressions(mat)
    _after_tonemap(mat)

    def new(cls, x, y):
        return mel.create_material_expression(mat, cls, x, y)

    def op(cls, a, b, x, y, an="A", bn="B"):
        e = new(cls, x, y)
        mel.connect_material_expressions(a, "", e, an)
        mel.connect_material_expressions(b, "", e, bn)
        return e

    def const(v, x, y):
        e = new(unreal.MaterialExpressionConstant, x, y)
        e.set_editor_property("r", v)
        return e

    def chan(src, out_name, ch, x, y):
        m = new(unreal.MaterialExpressionComponentMask, x, y)
        for c in "rgba":
            m.set_editor_property(c, c in ch)
        mel.connect_material_expressions(src, out_name, m, "")
        return m

    def scalar(name, group, x, y):
        e = new(unreal.MaterialExpressionScalarParameter, x, y)
        e.set_editor_property("parameter_name", name)
        e.set_editor_property("default_value", 0.0)
        e.set_editor_property("group", group)
        return e

    def lerp(a, b, alpha, x, y):
        e = new(unreal.MaterialExpressionLinearInterpolate, x, y)
        mel.connect_material_expressions(a, "", e, "A")
        mel.connect_material_expressions(b, "", e, "B")
        mel.connect_material_expressions(alpha, "", e, "Alpha")
        return e

    S, D, M, A = (unreal.MaterialExpressionSubtract, unreal.MaterialExpressionDivide,
                  unreal.MaterialExpressionMultiply, unreal.MaterialExpressionAdd)
    one, zero, two = const(1.0, -2900, -600), const(0.0, -2900, -520), const(2.0, -2900, -440)
    luma = new(unreal.MaterialExpressionConstant3Vector, -2900, -360)
    luma.set_editor_property("constant", unreal.LinearColor(0.2126, 0.7152, 0.0722, 0.0))

    st = new(unreal.MaterialExpressionSceneTexture, -2900, -250)
    st.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_POST_PROCESS_INPUT0)
    x = chan(st, "Color", "rgb", -2650, -250)

    sten = new(unreal.MaterialExpressionSceneTexture, -2900, 50)
    sten.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_CUSTOM_STENCIL)
    vsize = new(unreal.MaterialExpressionViewSize, -2900, 200)

    scr = new(unreal.MaterialExpressionScreenPosition, -2900, 350)
    shape = new(unreal.MaterialExpressionTextureSampleParameter2D, -2650, 350)
    shape.set_editor_property("parameter_name", "ShapeMask")
    shape.set_editor_property("texture", none_tex)
    shape.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
    mel.connect_material_expressions(scr, "ViewportUV", shape, "UVs")

    def grade(src, prefix, group, y0):
        p = {}
        for i, (name, d) in enumerate(PARAMS):
            e = new(unreal.MaterialExpressionVectorParameter, -1700, y0 + i * 200)
            e.set_editor_property("parameter_name", prefix + name)
            e.set_editor_property("default_value", unreal.LinearColor(d, d, d, 1.0))
            e.set_editor_property("group", group)
            p[name] = e
        # Nuke Grade: A = multiply*(gain-lift)/(white-black); B = offset+lift-A*black; out = (A*x+B)^(1/gamma)
        slope = op(M, p["Multiply"], op(D, op(S, p["Gain"], p["Lift"], -1400, y0 + 500),
                                         op(S, p["Whitepoint"], p["Blackpoint"], -1400, y0 + 100),
                                         -1250, y0 + 300), -1100, y0 + 500)
        bias = op(S, op(A, p["Offset"], p["Lift"], -1400, y0 + 900),
                  op(M, slope, p["Blackpoint"], -950, y0 + 200), -800, y0 + 700)
        y = op(unreal.MaterialExpressionMax, op(A, op(M, slope, src, -950, y0 - 100), bias, -650, y0), zero, -500, y0)
        g = op(unreal.MaterialExpressionPower, y, op(D, one, p["Gamma"], -1400, y0 + 1300), -350, y0, "Base", "Exp")
        # tonal ranges (like Nuke ColorCorrect): weights from the graded luminance
        sat = new(unreal.MaterialExpressionSaturate, -250, y0 + 300)
        mel.connect_material_expressions(op(unreal.MaterialExpressionDotProduct, g, luma, -300, y0 + 200), "", sat, "")

        def sstep(lo, hi, yy):
            e = new(unreal.MaterialExpressionSmoothStep, -150, yy)
            mel.connect_material_expressions(const(lo, -300, yy), "", e, "Min")
            mel.connect_material_expressions(const(hi, -300, yy + 60), "", e, "Max")
            mel.connect_material_expressions(sat, "", e, "Value")
            return e
        ws = op(S, one, sstep(0.0, 0.35, y0 + 400), 0, y0 + 400)
        wh = sstep(0.55, 1.0, y0 + 550)
        wm = op(S, op(S, one, ws, 100, y0 + 700), wh, 200, y0 + 700)
        gain = op(A, op(A, op(M, p["Shadows"], ws, 300, y0 + 400), op(M, p["Midtones"], wm, 300, y0 + 700), 450, y0 + 500),
                  op(M, p["Highlights"], wh, 300, y0 + 550), 600, y0 + 500)
        toned = op(M, g, gain, 750, y0)
        # saturation: out = max(luma + (colour - luma) * saturation, 0)    (1 = unchanged, 0 = grey)
        lum = op(unreal.MaterialExpressionDotProduct, toned, luma, 900, y0 + 200)
        return op(unreal.MaterialExpressionMax,
                  op(A, op(M, op(S, toned, lum, 1050, y0), p["Saturation"], 1200, y0), lum, 1350, y0), zero, 1500, y0)

    out = grade(x, LAYERS[0][0], LAYERS[0][1], 0)
    for k, (prefix, group) in enumerate(LAYERS[1:]):
        y0 = 2600 * (k + 1)
        part = new(unreal.MaterialExpressionCustom, -1900, y0 - 700)
        try:
            sid = int(unreal.SceneTextureId.PPI_CUSTOM_STENCIL.value)
        except Exception:
            sid = 25
        part.set_editor_property("code", EDGE_HLSL.replace("STENCIL_ID", str(sid)))
        part.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT1)
        part.set_editor_property("description", "Stencil parts (grow / soften)")
        names = ["W%d" % j for j in range(11)] + ["BG", "Grow", "Soft", "Inv", "VS", "Cen"]
        ins = []
        for n in names:
            ci = unreal.CustomInput()
            ci.set_editor_property("input_name", n)
            ins.append(ci)
        part.set_editor_property("inputs", ins)
        for j in range(11):
            mel.connect_material_expressions(scalar(prefix + ("Mask%d" % j if j else "Mask"), group, -2500, y0 - 1300 + j * 60),
                                             "", part, "W%d" % j)
        mel.connect_material_expressions(scalar(prefix + "BG", group, -2500, y0 - 600), "", part, "BG")
        mel.connect_material_expressions(scalar(prefix + "Grow", group, -2500, y0 - 540), "", part, "Grow")
        mel.connect_material_expressions(scalar(prefix + "Soft", group, -2500, y0 - 480), "", part, "Soft")
        mel.connect_material_expressions(sten, "InvSize", part, "Inv")
        mel.connect_material_expressions(vsize, "", part, "VS")
        mel.connect_material_expressions(sten, "Color", part, "Cen")     # also keeps the stencil texture bound
        use_parts = scalar(prefix + "UseParts", group, -2200, y0 - 400)
        use_shape = scalar(prefix + "UseShape", group, -2200, y0 - 300)
        pm = lerp(one, part, use_parts, -1550, y0 - 500)
        sm = lerp(one, chan(shape, "RGB", "rgb"[k], -1750, y0 - 300), use_shape, -1550, y0 - 300)
        mask = op(M, op(M, pm, sm, -1350, y0 - 400), op(unreal.MaterialExpressionMax, use_parts, use_shape, -1550, y0 - 150),
                  -1150, y0 - 400)
        out = lerp(out, grade(out, prefix, group, y0), mask, 1700, y0)
    mel.connect_material_property(out, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)


def _build_id(mat):
    mel.delete_all_material_expressions(mat)
    _after_tonemap(mat)
    sten = mel.create_material_expression(mat, unreal.MaterialExpressionSceneTexture, -700, 0)
    sten.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_CUSTOM_STENCIL)
    m = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -450, 0)
    for c in "rgba":
        m.set_editor_property(c, c == "r")
    mel.connect_material_expressions(sten, "Color", m, "")
    k = mel.create_material_expression(mat, unreal.MaterialExpressionConstant, -450, 150)
    k.set_editor_property("r", 255.0)
    d = mel.create_material_expression(mat, unreal.MaterialExpressionDivide, -250, 0)
    mel.connect_material_expressions(m, "", d, "A")
    mel.connect_material_expressions(k, "", d, "B")
    mel.connect_material_property(d, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)


def ensure(pkg, tmp_dir):
    """Create or update everything under pkg. Returns a list of what was built (empty = already up to date)."""
    built = []
    none_tex = _default_mask(pkg, tmp_dir)
    names = ["M_NukeGrade"] + sorted(a.split(".")[-1] for a in eal.list_assets(pkg, False, False)
                                     if a.split(".")[-1].startswith("M_NukeGrade_S"))
    for name in names + ["M_StencilID"]:
        path = pkg + "/" + name
        mat = eal.load_asset(path) if eal.does_asset_exist(path) else None
        if mat is None:
            mat = tools.create_asset(name, pkg, unreal.Material, unreal.MaterialFactoryNew())
        elif eal.get_metadata_tag(mat, "GradeSheetVersion") == VERSION:
            continue
        if name == "M_StencilID":
            _build_id(mat)
        else:
            build_grade(mat, none_tex)
        if name == "M_StencilID":
            mat.set_editor_property("blendable_priority", 10000)   # runs after every other post process material
        eal.set_metadata_tag(mat, "GradeSheetVersion", VERSION)
        eal.save_asset(path, False)
        built.append(name)
    idmat = eal.load_asset(pkg + "/M_StencilID")             # the ID view must run after every other post process
    if idmat and idmat.get_editor_property("blendable_priority") != 10000:
        idmat.set_editor_property("blendable_priority", 10000)
        eal.save_asset(pkg + "/M_StencilID", False)
    return built
