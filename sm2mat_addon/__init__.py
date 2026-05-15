"""
SM2Mat - Space Marine 2 Material Importer for Blender 5.1
Imports FBX models and auto-constructs PBR shader trees from .td sidecar files.
"""

bl_info = {
    "name": "SM2Mat - Space Marine 2 Material Importer",
    "author": "Ryan",
    "version": (1, 0, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > SM2Mat",
    "description": "Import SM2 FBX models with automatic PBR material setup from .td files",
    "category": "Import-Export",
}

import bpy
import os
from bpy.props import StringProperty, BoolProperty, FloatProperty, PointerProperty
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ImportHelper


ADDON_ID = __package__ if __package__ else __name__
IMAGE_EXTS = ('.dds', '.tga', '.png', '.hdr', '.bmp', '.tif', '.tiff', '.jpg', '.jpeg')
_SM2MAT_SYNCING_DETAIL_PATH = False


# ---------------------------------------------------------------------------
# .td Parser
# ---------------------------------------------------------------------------

def parse_td(text):
    """Parse Saber Engine .td key-value format into nested dicts.

    Handles:
      - key = value (string, number, bool)
      - key = { ... } (nested block)
      - key = [ ... ] (array)
      - Flexible whitespace (spaces and tabs)
      - Case-insensitive booleans (true/TRUE/True/false/FALSE/False)
    """
    pos = [0]

    def skip_ws():
        while pos[0] < len(text) and text[pos[0]] in ' \t\r\n':
            pos[0] += 1

    def peek():
        skip_ws()
        return text[pos[0]] if pos[0] < len(text) else ''

    def read_string():
        """Read a quoted string value."""
        assert text[pos[0]] == '"'
        pos[0] += 1
        start = pos[0]
        while pos[0] < len(text) and text[pos[0]] != '"':
            if text[pos[0]] == '\\':
                pos[0] += 1  # skip escaped char
            pos[0] += 1
        result = text[start:pos[0]]
        pos[0] += 1  # skip closing quote
        return result

    def read_word():
        """Read an unquoted token (key name or bare value)."""
        start = pos[0]
        while pos[0] < len(text) and text[pos[0]] not in ' \t\r\n={}[]",':
            pos[0] += 1
        return text[start:pos[0]]

    def read_value():
        """Read a value: string, block, array, or bare token."""
        c = peek()
        if c == '"':
            return read_string()
        if c == '{':
            return read_block()
        if c == '[':
            return read_array()
        word = read_word()
        # Booleans (case-insensitive)
        if word.lower() == 'true':
            return True
        if word.lower() == 'false':
            return False
        # Numbers
        try:
            if '.' in word:
                return float(word)
            return int(word)
        except ValueError:
            return word

    def read_array():
        """Read a [...] array."""
        assert text[pos[0]] == '['
        pos[0] += 1
        items = []
        while True:
            c = peek()
            if c == ']' or c == '':
                pos[0] += 1
                return items
            items.append(read_value())
            # Skip optional commas
            skip_ws()
            if pos[0] < len(text) and text[pos[0]] == ',':
                pos[0] += 1

    def read_key():
        """Read a key: either a bare word or a quoted string."""
        skip_ws()
        if pos[0] < len(text) and text[pos[0]] == '"':
            return read_string()
        return read_word()

    def read_block():
        """Read a { ... } block as a dict."""
        assert text[pos[0]] == '{'
        pos[0] += 1
        d = {}
        while True:
            c = peek()
            if c == '}' or c == '':
                pos[0] += 1
                return d
            key = read_key()
            if not key:
                pos[0] += 1
                continue
            skip_ws()
            # Expect '='
            if pos[0] < len(text) and text[pos[0]] == '=':
                pos[0] += 1
            d[key] = read_value()
        return d

    # Top level is an implicit block (no braces)
    result = {}
    while pos[0] < len(text):
        c = peek()
        if c == '' or c == '}':
            break
        key = read_key()
        if not key:
            pos[0] += 1
            continue
        skip_ws()
        if pos[0] < len(text) and text[pos[0]] == '=':
            pos[0] += 1
        result[key] = read_value()
    return result


def read_td_file(filepath):
    """Read and parse a .td file, returning None on failure."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return parse_td(f.read())
    except Exception as e:
        print(f"SM2Mat: Failed to parse {filepath}: {e}")
        return None


def load_image(image_path, colorspace=None):
    """Load or reuse a Blender image by basename."""
    img_name = os.path.basename(image_path)
    if img_name in bpy.data.images:
        image = bpy.data.images[img_name]
    else:
        image = bpy.data.images.load(image_path, check_existing=True)
    if colorspace:
        image.colorspace_settings.name = colorspace
    return image


# ---------------------------------------------------------------------------
# Texture Discovery & Material Resolution
# ---------------------------------------------------------------------------

def discover_textures(folder):
    """Scan a folder and return {basename_without_ext: (image_path, td_path, td_data)}
    for every texture that has a .td sidecar with a 'usage' field."""
    textures = {}

    td_files = {}
    image_files = {}

    for fname in os.listdir(folder):
        fpath = os.path.join(folder, fname)
        if not os.path.isfile(fpath):
            continue
        base, ext = os.path.splitext(fname)
        ext_lower = ext.lower()
        if ext_lower == '.td':
            td_files[base] = fpath
        elif ext_lower in IMAGE_EXTS:
            # Prefer first found if multiple formats exist for same base
            if base not in image_files:
                image_files[base] = fpath

    for base, td_path in td_files.items():
        if base not in image_files:
            continue
        td_data = read_td_file(td_path)
        if td_data is None:
            continue
        usage = td_data.get('usage', '')
        if not usage:
            # Fallback to convert_settings.format_name
            cs = td_data.get('convert_settings', {})
            usage = cs.get('format_name', '')
        if usage:
            textures[base] = {
                'image_path': image_files[base],
                'td_path': td_path,
                'td_data': td_data,
                'usage': usage,
            }
    return textures


def find_texture_base_for_material(mat_name, textures):
    """Given a material name like 'default_ch_acheran_arms_01', find the
    matching texture base name and submaterial prefix.

    Convention: material name = {submaterial}_{texture_base}
    The texture_base must match a key in the textures dict.

    Returns (submaterial_prefix, texture_base) or (None, None).
    """
    # Try progressively shorter prefixes
    parts = mat_name.split('_')
    for i in range(1, len(parts)):
        prefix = '_'.join(parts[:i])
        base = '_'.join(parts[i:])
        if base in textures:
            return prefix, base

    # Fallback: maybe the whole name is the base (no prefix)
    if mat_name in textures:
        return '', mat_name

    return None, None


def get_texture_group(texture_base, textures):
    """Find all textures belonging to the same group as texture_base.

    A texture group shares the same base stem. For example:
      ch_acheran_arms_01     (MD - diffuse)
      ch_acheran_arms_01_nm  (MNM - normal)
      ch_acheran_arms_01_spec (MSCRGHAO - spec)
      ch_acheran_arms_01_em  (MEM or MAO)
      ch_acheran_arms_01_h   (MH - height)

    When multiple textures share the same usage code (e.g. base diffuse vs
    damaged diffuse variant), the shortest-named texture wins — it's always
    the primary. Variant textures are collected separately.

    Returns (base_group, variant_textures) where:
      base_group = {usage_code: texture_info_dict}  (primary textures)
      variant_textures = {texture_name: texture_info_dict}  (extras with colliding usage)
    """
    group = {}
    group_names = {}  # Track which name holds each usage slot
    variants = {}

    for name, info in textures.items():
        if name == texture_base or name.startswith(texture_base + '_'):
            usage = info['usage']
            if usage not in group or len(name) < len(group_names[usage]):
                # This is the primary (shortest name wins)
                if usage in group:
                    # Demote the previous holder to variants
                    variants[group_names[usage]] = group[usage]
                group[usage] = info
                group_names[usage] = name
            else:
                # Longer name = variant
                variants[name] = info

    return group, variants


def find_detail_texture(tex_name, detail_maps_folder):
    """Find a detail texture image file by its base name in the detail_maps folder.

    The .td files reference detail textures by base name (e.g., 'detail_04_det').
    We look for matching image files in the detail_maps folder.

    Returns the full image path or None.
    """
    if not tex_name or not detail_maps_folder or not os.path.isdir(detail_maps_folder):
        return None

    # Try exact name first, then case-insensitive
    for ext in IMAGE_EXTS:
        path = os.path.join(detail_maps_folder, tex_name + ext)
        if os.path.isfile(path):
            return path
    # Case-insensitive fallback
    try:
        lower_name = tex_name.lower()
        for fname in os.listdir(detail_maps_folder):
            base, ext = os.path.splitext(fname)
            if base.lower() == lower_name and ext.lower() in IMAGE_EXTS:
                return os.path.join(detail_maps_folder, fname)
    except OSError:
        pass
    return None


def get_shader_params(td_data, submaterial_key):
    """Extract shader parameters from a .td materials block.

    Returns dict with keys for material scalar parameters, detail maps, and
    wrinkle info.
    """
    defaults = {
        'roughness_scale': 1.0,
        'roughness_bias': 0.0,
        'metalness_scale': 1.0,
        'metalness_bias': 0.0,
        'emissive_intensity': 0.0,
        'no_backface_culling': False,
        'nm_scale': 1.0,
        'reflectance': 0.5,
        'detail_density': 0.0,
        'detail_scale': 1.0,
        'detail_normal_tex': '',
        'detail_diffuse_tex': '',
        'detail_diffuse_scale': 0.0,
        'covering_mask': '',
        'height_det_mask': '',
        # Wrinkle system
        'wrinkle_enable': False,
        'wrinkle_nm': '',
        'wrinkle_mask': '',
    }

    materials = td_data.get('materials', {})
    if not materials:
        return defaults

    # Find the submaterial block
    sub = materials.get(submaterial_key, {})
    if not sub:
        # Try 'default' as fallback
        sub = materials.get('default', {})
    if not sub:
        # Use the first available submaterial
        for k, v in materials.items():
            if isinstance(v, dict):
                sub = v
                break
    if not sub:
        return defaults

    shaders = sub.get('shaders', {})
    glt = shaders.get('glt', {})
    if not glt:
        return defaults

    # Check shader type - skip non-PBR shaders
    shader_type = glt.get('__type_id', '')
    if shader_type and shader_type not in ('glt_sh_templ', 'mtl_fill_sh_templ', ''):
        return defaults

    # Extract roughness
    rough = glt.get('roughness', {})
    if isinstance(rough, dict):
        defaults['roughness_scale'] = rough.get('scale', defaults['roughness_scale'])
        defaults['roughness_bias'] = rough.get('bias', defaults['roughness_bias'])

    # Extract metalness
    metal = glt.get('metalness', {})
    if isinstance(metal, dict):
        defaults['metalness_scale'] = metal.get('scale', defaults['metalness_scale'])
        defaults['metalness_bias'] = metal.get('bias', defaults['metalness_bias'])

    # Extract emissive
    emissive = glt.get('emissive', {})
    if isinstance(emissive, dict):
        defaults['emissive_intensity'] = emissive.get('intensity', defaults['emissive_intensity'])

    # Extract other params
    defaults['no_backface_culling'] = glt.get('no_backface_culling', False)
    defaults['nm_scale'] = glt.get('nmScale', 1.0)
    defaults['reflectance'] = glt.get('reflectance', 0.5)

    # Extract detail normal params
    detail = glt.get('detail', {})
    if isinstance(detail, dict):
        defaults['detail_density'] = detail.get('density', 0.0)
        defaults['detail_scale'] = detail.get('scale', 1.0)

    # Extract tex block (detail texture name and heightDetMask)
    tex_block = glt.get('tex', {})
    if isinstance(tex_block, dict):
        defaults['detail_normal_tex'] = tex_block.get('det', '')
        defaults['height_det_mask'] = tex_block.get('heightDetMask', '')

    # Extract detail diffuse params
    detail_diff = glt.get('detailDiffuse', {})
    if isinstance(detail_diff, dict):
        defaults['detail_diffuse_scale'] = detail_diff.get('scale', 0.0)
        defaults['detail_diffuse_tex'] = detail_diff.get('tex', '')
        # Also check if it's stored as a string directly (texture name)
        if not defaults['detail_diffuse_tex']:
            defaults['detail_diffuse_tex'] = detail_diff.get('diffuse_tex', '')
    elif isinstance(detail_diff, str) and detail_diff:
        defaults['detail_diffuse_tex'] = detail_diff

    # Extract covering mask name
    covering = glt.get('covering', {})
    if isinstance(covering, dict):
        defaults['covering_mask'] = covering.get('coveringMask', '')

    # Extract wrinkle system
    skin = glt.get('skin', {})
    if isinstance(skin, dict):
        wrinkles = skin.get('wrinkles', {})
        if isinstance(wrinkles, dict) and wrinkles.get('enable', False):
            defaults['wrinkle_enable'] = True
            defaults['wrinkle_nm'] = wrinkles.get('texArrayNormals', '')
            defaults['wrinkle_mask'] = wrinkles.get('texArrayMasks', '')

    return defaults


def get_variant_submaterials(td_data):
    """Scan all submaterials in a .td for those with texture overrides.

    Returns {submaterial_key: {override_type: texture_name}} for any
    submaterial whose tex block contains diffOverride, NM, or spec.
    """
    variants = {}
    materials = td_data.get('materials', {})
    if not materials:
        return variants

    for sub_key, sub_val in materials.items():
        if not isinstance(sub_val, dict):
            continue
        shaders = sub_val.get('shaders', {})
        glt = shaders.get('glt', {})
        if not isinstance(glt, dict):
            continue
        tex_block = glt.get('tex', {})
        if not isinstance(tex_block, dict):
            continue

        overrides = {}
        if tex_block.get('diffOverride'):
            overrides['diffuse'] = tex_block['diffOverride']
        if tex_block.get('NM'):
            overrides['normal'] = tex_block['NM']
        if tex_block.get('spec'):
            overrides['spec'] = tex_block['spec']
        if overrides:
            variants[sub_key] = overrides

    return variants


# ---------------------------------------------------------------------------
# Node Tree Builder
# ---------------------------------------------------------------------------

NODE_SPACING_X = 300
NODE_SPACING_Y = 300


class NodeTreeBuilder:
    """Builds a Principled BSDF shader tree for a Blender material."""

    def __init__(self, material, shader_params, emissive_divisor=100.0):
        self.mat = material
        self.params = shader_params
        self.emissive_divisor = emissive_divisor
        self.tree = material.node_tree
        self.nodes = self.tree.nodes
        self.links = self.tree.links
        self.principled = None
        self.output = None
        self._col = 0  # Column counter for positioning

        self._setup_base_nodes()

    def _setup_base_nodes(self):
        """Clear existing nodes and set up Principled BSDF + Output."""
        self.nodes.clear()

        self.output = self.nodes.new('ShaderNodeOutputMaterial')
        self.output.location = (400, 0)

        self.principled = self.nodes.new('ShaderNodeBsdfPrincipled')
        self.principled.location = (0, 0)
        self.links.new(self.principled.outputs['BSDF'], self.output.inputs['Surface'])

        # Set reflectance (maps to Specular IOR Level)
        self.principled.inputs['Specular IOR Level'].default_value = self.params['reflectance']

    def _add_tex_node(self, image_path, label, colorspace='sRGB', col_offset=0):
        """Create an Image Texture node and load the image."""
        tex = self.nodes.new('ShaderNodeTexImage')
        tex.label = label
        self._col -= 1
        tex.location = (-NODE_SPACING_X * (3 + col_offset), NODE_SPACING_Y * self._col)

        try:
            tex.image = load_image(
                image_path,
                colorspace='Non-Color' if colorspace == 'Non-Color' else None,
            )
        except Exception as e:
            print(f"SM2Mat: Could not load image {image_path}: {e}")

        return tex

    @staticmethod
    def _enum_text(item):
        parts = (
            getattr(item, 'identifier', ''),
            getattr(item, 'name', ''),
            getattr(item, 'description', ''),
        )
        return ' '.join(str(part) for part in parts).upper()

    @staticmethod
    def _try_set_enum(node, prop_name, value):
        try:
            setattr(node, prop_name, value)
            return True
        except Exception:
            return False

    def _set_normal_map_directx(self, node):
        """Set Blender 5.1 normal-map convention enums to DirectX when available."""
        bl_rna = getattr(node, 'bl_rna', None)
        properties = getattr(bl_rna, 'properties', ()) if bl_rna else ()

        for prop in properties:
            prop_name = getattr(prop, 'identifier', '')
            if not prop_name or prop_name in {'rna_type', 'type', 'space'}:
                continue
            if getattr(prop, 'is_readonly', False):
                continue

            enum_items = getattr(prop, 'enum_items_static', None) or getattr(prop, 'enum_items', ())
            directx_id = None
            has_opengl = False
            for item in enum_items:
                item_text = self._enum_text(item)
                compact_text = ''.join(ch for ch in item_text if ch.isalnum())
                if 'DIRECTX' in compact_text or compact_text.endswith('DX'):
                    directx_id = getattr(item, 'identifier', None)
                if 'OPENGL' in compact_text or compact_text.endswith('GL'):
                    has_opengl = True

            if directx_id and has_opengl and self._try_set_enum(node, prop_name, directx_id):
                return True

        return False

    def _add_normal_map_node(self, label=None):
        """Create a Normal Map node configured for DirectX normal maps."""
        node = self.nodes.new('ShaderNodeNormalMap')
        if label:
            node.label = label
        try:
            node.space = 'TANGENT'
        except Exception:
            pass
        return node, self._set_normal_map_directx(node)

    @staticmethod
    def _set_color_node_rgb(node):
        try:
            node.mode = 'RGB'
        except Exception:
            pass

    def _directx_normal_color(self, color_socket, source_location, native_directx, label):
        """Return a color socket suitable for the normal node.

        Blender 5.1 exposes DirectX/OpenGL as a normal-map node convention. If the
        running build does not, keep the material visually correct by converting
        DirectX input data to Blender's older OpenGL-only expectation.
        """
        if native_directx:
            return color_socket, source_location[0] + NODE_SPACING_X

        sep = self.nodes.new('ShaderNodeSeparateColor')
        self._set_color_node_rgb(sep)
        sep.location = (source_location[0] + NODE_SPACING_X, source_location[1])
        sep.label = f'{label} DX to GL Split'
        self.links.new(color_socket, sep.inputs['Color'])

        invert = self.nodes.new('ShaderNodeInvert')
        invert.location = (sep.location[0] + 160, sep.location[1] - 40)
        invert.label = f'{label} Flip G'
        self.links.new(sep.outputs['Green'], invert.inputs['Color'])

        combine = self.nodes.new('ShaderNodeCombineColor')
        self._set_color_node_rgb(combine)
        combine.location = (invert.location[0] + 160, sep.location[1])
        combine.label = f'{label} DX to GL Fallback'
        self.links.new(sep.outputs['Red'], combine.inputs['Red'])
        self.links.new(invert.outputs['Color'], combine.inputs['Green'])
        self.links.new(sep.outputs['Blue'], combine.inputs['Blue'])

        return combine.outputs['Color'], combine.location[0] + 160

    def _add_math_node(self, operation, value1=0.0, value2=0.0, label='', location=None):
        """Create a Math node."""
        math = self.nodes.new('ShaderNodeMath')
        math.operation = operation
        math.inputs[0].default_value = value1
        math.inputs[1].default_value = value2
        if label:
            math.label = label
        if location:
            math.location = location
        return math

    def apply_scale_bias(self, source_socket, scale, bias, label_prefix, location_y):
        """Apply final = source × scale + bias transform.
        If scale == 0, returns None (caller should use bias as flat value).
        """
        if scale == 0.0:
            return None  # Flat value = bias

        if scale == 1.0 and bias == 0.0:
            return source_socket  # Identity, just pass through

        # Multiply by scale
        current = source_socket
        if scale != 1.0:
            mul = self._add_math_node('MULTIPLY', value2=scale, label=f'{label_prefix} × Scale')
            mul.location = (-NODE_SPACING_X, location_y)
            self.links.new(current, mul.inputs[0])
            current = mul.outputs['Value']

        # Add bias
        if bias != 0.0:
            add = self._add_math_node('ADD', value2=bias, label=f'{label_prefix} + Bias')
            add.location = (-NODE_SPACING_X + 180, location_y)
            self.links.new(current, add.inputs[0])
            current = add.outputs['Value']

        return current

    # --- Usage handlers ---

    def apply_diffuse(self, tex_info):
        """Handle MD, MD+MT, MD+MAK, MD+MRGH, MD+MSP usage codes."""
        usage = tex_info['usage']
        tex = self._add_tex_node(tex_info['image_path'], f'Diffuse ({usage})')
        self.links.new(tex.outputs['Color'], self.principled.inputs['Base Color'])

        if usage == 'MD+MT':
            # Alpha transparency
            self.links.new(tex.outputs['Alpha'], self.principled.inputs['Alpha'])
            self.mat.blend_method = 'BLEND'
            self.mat.show_transparent_back = True

        elif usage == 'MD+MAK':
            # Alpha kill (clip)
            self.links.new(tex.outputs['Alpha'], self.principled.inputs['Alpha'])
            self.mat.blend_method = 'CLIP'
            # Use akill_ref from td if available
            td_data = tex_info.get('td_data', {})
            cs = td_data.get('convert_settings', {})
            akill_ref = cs.get('m_akill_ref', 127)
            self.mat.alpha_threshold = akill_ref / 255.0

        elif usage == 'MD+MRGH':
            # Alpha channel = roughness
            rough_scale = self.params['roughness_scale']
            rough_bias = self.params['roughness_bias']
            result = self.apply_scale_bias(
                tex.outputs['Alpha'], rough_scale, rough_bias, 'Roughness', -200
            )
            if result is not None:
                self.links.new(result, self.principled.inputs['Roughness'])
            else:
                self.principled.inputs['Roughness'].default_value = rough_bias

        elif usage == 'MD+MSP':
            # Legacy: diff + gloss (invert gloss → roughness)
            invert = self.nodes.new('ShaderNodeInvert')
            invert.location = (-NODE_SPACING_X, -200)
            invert.label = 'Gloss → Roughness'
            self.links.new(tex.outputs['Alpha'], invert.inputs['Color'])
            self.links.new(invert.outputs['Color'], self.principled.inputs['Roughness'])

        return tex

    def apply_normal(self, tex_info):
        """Handle MNM usage."""
        tex = self._add_tex_node(tex_info['image_path'], 'Normal (MNM)', colorspace='Non-Color')

        nm, native_directx = self._add_normal_map_node()
        normal_color, normal_x = self._directx_normal_color(
            tex.outputs['Color'], tex.location, native_directx, 'Normal'
        )
        nm.location = (normal_x, tex.location[1])
        nm.inputs['Strength'].default_value = self.params['nm_scale']
        self.links.new(normal_color, nm.inputs['Color'])

        self.links.new(nm.outputs['Normal'], self.principled.inputs['Normal'])

    def apply_spec_mscrghao(self, tex_info):
        """Handle MSCRGHAO: R=metallic, G=roughness, B=AO."""
        tex = self._add_tex_node(tex_info['image_path'], 'Spec (MSCRGHAO)', colorspace='Non-Color')

        sep = self.nodes.new('ShaderNodeSeparateColor')
        sep.location = (tex.location[0] + NODE_SPACING_X, tex.location[1])
        sep.label = 'M/R/AO Split'
        self.links.new(tex.outputs['Color'], sep.inputs['Color'])

        # Metallic (Red) with scale/bias
        metal_scale = self.params['metalness_scale']
        metal_bias = self.params['metalness_bias']
        metal_result = self.apply_scale_bias(
            sep.outputs['Red'], metal_scale, metal_bias, 'Metallic',
            sep.location[1] + 40
        )
        if metal_result is not None:
            self.links.new(metal_result, self.principled.inputs['Metallic'])
        else:
            self.principled.inputs['Metallic'].default_value = metal_bias

        # Roughness (Green) with scale/bias
        rough_scale = self.params['roughness_scale']
        rough_bias = self.params['roughness_bias']
        rough_result = self.apply_scale_bias(
            sep.outputs['Green'], rough_scale, rough_bias, 'Roughness',
            sep.location[1] - 40
        )
        if rough_result is not None:
            self.links.new(rough_result, self.principled.inputs['Roughness'])
        else:
            self.principled.inputs['Roughness'].default_value = rough_bias

        # AO (Blue) — multiply into base color if base color is connected
        # Store AO output for later mixing
        self._ao_socket = sep.outputs['Blue']
        self._ao_sep_location = sep.location

    def apply_spec_mscg_mrgh(self, tex_info):
        """Handle MSCG+MRGH: R=metallic gray, A=roughness."""
        tex = self._add_tex_node(tex_info['image_path'], 'Spec (MSCG+MRGH)', colorspace='Non-Color')

        # Metallic from Red channel
        sep = self.nodes.new('ShaderNodeSeparateColor')
        sep.location = (tex.location[0] + NODE_SPACING_X, tex.location[1])
        sep.label = 'Metal/Rough Split'
        self.links.new(tex.outputs['Color'], sep.inputs['Color'])

        metal_scale = self.params['metalness_scale']
        metal_bias = self.params['metalness_bias']
        metal_result = self.apply_scale_bias(
            sep.outputs['Red'], metal_scale, metal_bias, 'Metallic',
            sep.location[1] + 40
        )
        if metal_result is not None:
            self.links.new(metal_result, self.principled.inputs['Metallic'])
        else:
            self.principled.inputs['Metallic'].default_value = metal_bias

        # Roughness from Alpha
        rough_scale = self.params['roughness_scale']
        rough_bias = self.params['roughness_bias']
        rough_result = self.apply_scale_bias(
            tex.outputs['Alpha'], rough_scale, rough_bias, 'Roughness',
            sep.location[1] - 40
        )
        if rough_result is not None:
            self.links.new(rough_result, self.principled.inputs['Roughness'])
        else:
            self.principled.inputs['Roughness'].default_value = rough_bias

    def apply_ao(self, tex_info, usage='MAO'):
        """Handle MAO: alpha channel = AO. Multiplies into base color."""
        tex = self._add_tex_node(tex_info['image_path'], f'AO ({usage})', colorspace='Non-Color')
        self._ao_socket = tex.outputs['Alpha']
        self._ao_sep_location = tex.location

    def apply_emissive(self, tex_info, has_ao_alpha=False):
        """Handle MEM and MEM+MAO."""
        usage = tex_info['usage']
        tex = self._add_tex_node(tex_info['image_path'], f'Emission ({usage})')

        self.links.new(tex.outputs['Color'], self.principled.inputs['Emission Color'])

        # Emissive intensity from shader params
        intensity = self.params['emissive_intensity']
        if intensity > 0 and self.emissive_divisor > 0:
            self.principled.inputs['Emission Strength'].default_value = intensity / self.emissive_divisor
        elif intensity > 0:
            self.principled.inputs['Emission Strength'].default_value = intensity
        else:
            self.principled.inputs['Emission Strength'].default_value = 1.0

        if has_ao_alpha or usage == 'MEM+MAO':
            self._ao_socket = tex.outputs['Alpha']
            self._ao_sep_location = tex.location

    def apply_height(self, tex_info):
        """Handle MH: height/displacement map."""
        tex = self._add_tex_node(tex_info['image_path'], 'Height (MH)', colorspace='Non-Color')

        # Use Bump node (works in both EEVEE and Cycles)
        bump = self.nodes.new('ShaderNodeBump')
        bump.location = (tex.location[0] + NODE_SPACING_X, tex.location[1])

        # Use Red channel for height data
        sep = self.nodes.new('ShaderNodeSeparateColor')
        sep.location = (tex.location[0] + NODE_SPACING_X * 0.5, tex.location[1] - 60)
        self.links.new(tex.outputs['Color'], sep.inputs['Color'])
        self.links.new(sep.outputs['Red'], bump.inputs['Height'])

        # Connect to Normal input (chain with existing normal map if present)
        if self.principled.inputs['Normal'].is_linked:
            # Chain: existing normal → bump.Normal input
            existing_link = self.principled.inputs['Normal'].links[0]
            existing_normal = existing_link.from_socket
            self.links.remove(existing_link)
            self.links.new(existing_normal, bump.inputs['Normal'])

        self.links.new(bump.outputs['Normal'], self.principled.inputs['Normal'])

    def apply_height_detm(self, tex_info, detail_normal_path=None):
        """Handle MH+MDTM: R=height, G=detail mask.

        Height goes to Bump as before. If a detail normal texture is available,
        the G channel is extracted as the detail mask for blending.
        """
        # Apply height from Red channel (reuse existing height logic)
        self.apply_height(tex_info)

        # Store the detail mask source (Green channel of same texture) for later
        # Find the tex node we just created for height - it's the last TEX_IMAGE
        height_tex = None
        for n in reversed(list(self.nodes)):
            if n.type == 'TEX_IMAGE' and n.label.startswith('Height'):
                height_tex = n
                break

        if height_tex and detail_normal_path:
            # Extract Green channel as detail mask
            sep_mask = self.nodes.new('ShaderNodeSeparateColor')
            sep_mask.location = (height_tex.location[0] + 180, height_tex.location[1] - 180)
            sep_mask.label = 'Detail Mask (G)'
            self.links.new(height_tex.outputs['Color'], sep_mask.inputs['Color'])
            self._detail_mask_socket = sep_mask.outputs['Green']
            self._detail_mask_location = sep_mask.location

    def apply_detail_mask(self, tex_info, detail_normal_path=None):
        """Handle standalone MDTM: G=detail mask.

        If a detail normal texture is available, extract the G channel for blending.
        Otherwise, create the node unconnected.
        """
        tex = self._add_tex_node(tex_info['image_path'], 'Detail Mask (MDTM)', colorspace='Non-Color')

        if detail_normal_path:
            sep_mask = self.nodes.new('ShaderNodeSeparateColor')
            sep_mask.location = (tex.location[0] + NODE_SPACING_X, tex.location[1])
            sep_mask.label = 'Detail Mask (G)'
            self.links.new(tex.outputs['Color'], sep_mask.inputs['Color'])
            self._detail_mask_socket = sep_mask.outputs['Green']
            self._detail_mask_location = sep_mask.location
        else:
            tex.label = 'Detail Mask (MDTM) - No det texture found'

    def apply_detail_normal(self, detail_normal_path):
        """Blend a tiling detail normal map into the base normal using the detail mask.

        Requires:
          - self._detail_mask_socket to be set (from apply_detail_mask or apply_height_detm)
          - detail.density > 0 in shader params (tiling rate)
          - detail.scale > 0 in shader params (blend strength)
          - A base normal already connected to Principled BSDF Normal input

        Node chain:
          [Detail Normal Tex] → [Mapping: UV×density] → [Normal Map] ──┐
                                                                        ├→ [Mix Vector] → Normal
          [Base Normal (existing)] ────────────────────────────────────┘       ↑
                                                                               │
          [Detail Mask G] → [Math: × scale] ──────────────────────────────────┘
        """
        mask_socket = getattr(self, '_detail_mask_socket', None)
        if mask_socket is None:
            print("SM2Mat: Detail normal requested but no detail mask available")
            return

        density = self.params.get('detail_density', 0.0)
        scale = self.params.get('detail_scale', 0.0)

        if density <= 0:
            print("SM2Mat: Detail density is 0, skipping detail normal")
            return
        if scale == 0.0:
            print("SM2Mat: Detail scale is 0, skipping detail normal")
            return

        mask_loc = getattr(self, '_detail_mask_location', (-900, -900))

        # --- Detail normal texture with tiling ---
        det_tex = self.nodes.new('ShaderNodeTexImage')
        det_tex.label = f'Detail Normal (×{density})'
        det_tex.location = (mask_loc[0] - NODE_SPACING_X * 2, mask_loc[1] - NODE_SPACING_Y)

        try:
            det_tex.image = load_image(detail_normal_path, colorspace='Non-Color')
        except Exception as e:
            print(f"SM2Mat: Could not load detail normal {detail_normal_path}: {e}")
            return

        # UV Mapping node for tiling
        tex_coord = self.nodes.new('ShaderNodeTexCoord')
        tex_coord.location = (det_tex.location[0] - NODE_SPACING_X * 1.5, det_tex.location[1])

        mapping = self.nodes.new('ShaderNodeMapping')
        mapping.location = (det_tex.location[0] - NODE_SPACING_X, det_tex.location[1])
        mapping.label = f'Tile ×{density}'
        mapping.inputs['Scale'].default_value = (density, density, 1.0)

        self.links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])
        self.links.new(mapping.outputs['Vector'], det_tex.inputs['Vector'])

        # Detail Normal Map node
        det_nm, native_directx = self._add_normal_map_node('Detail NM')
        detail_color, detail_nm_x = self._directx_normal_color(
            det_tex.outputs['Color'], det_tex.location, native_directx, 'Detail Normal'
        )
        det_nm.location = (detail_nm_x, det_tex.location[1])
        det_nm.label = 'Detail NM'
        det_nm.inputs['Strength'].default_value = 1.0
        self.links.new(detail_color, det_nm.inputs['Color'])

        # --- Mask × scale factor ---
        mask_mul = self.nodes.new('ShaderNodeMath')
        mask_mul.operation = 'MULTIPLY'
        mask_mul.location = (mask_loc[0] + 180, mask_loc[1])
        mask_mul.label = f'Mask × {scale}'
        mask_mul.inputs[1].default_value = abs(scale)
        self.links.new(mask_socket, mask_mul.inputs[0])

        # --- Mix the two normals ---
        # Check if base normal is connected
        normal_input = self.principled.inputs['Normal']
        if normal_input.is_linked:
            # Intercept the existing normal connection
            existing_link = normal_input.links[0]
            base_normal_socket = existing_link.from_socket
            self.links.remove(existing_link)

            mix = self.nodes.new('ShaderNodeMix')
            mix.data_type = 'VECTOR'
            mix.label = 'Base + Detail Normal'
            mix.location = (det_nm.location[0] + NODE_SPACING_X, det_nm.location[1] + 100)
            mix.clamp_factor = True

            # Factor = mask × scale
            self.links.new(mask_mul.outputs['Value'], mix.inputs['Factor'])
            # A = base normal (inputs[4] for VECTOR A)
            self.links.new(base_normal_socket, mix.inputs[4])
            # B = detail normal (inputs[5] for VECTOR B)
            self.links.new(det_nm.outputs['Normal'], mix.inputs[5])
            # Result → Principled Normal (outputs[1] for VECTOR result)
            self.links.new(mix.outputs[1], normal_input)
        else:
            # No base normal - just use the detail normal scaled by mask
            det_nm.inputs['Strength'].default_value = abs(scale)
            self.links.new(det_nm.outputs['Normal'], normal_input)

        print(f"SM2Mat:   Detail normal wired: density={density}, scale={scale}, "
              f"tex={os.path.basename(detail_normal_path)}")

    def apply_detail_diffuse(self, detail_diffuse_path):
        """Blend a tiling detail diffuse texture into the base color using the detail mask.

        Similar to detail normal but targets Base Color instead of Normal.
        """
        mask_socket = getattr(self, '_detail_mask_socket', None)
        if mask_socket is None:
            return

        density = self.params.get('detail_density', 0.0)
        diff_scale = self.params.get('detail_diffuse_scale', 0.0)

        if density <= 0 or diff_scale == 0.0:
            return

        mask_loc = getattr(self, '_detail_mask_location', (-900, -900))

        # Detail diffuse texture with same tiling as detail normal
        det_tex = self.nodes.new('ShaderNodeTexImage')
        det_tex.label = f'Detail Diffuse (×{density})'
        det_tex.location = (mask_loc[0] - NODE_SPACING_X * 2, mask_loc[1] - NODE_SPACING_Y * 2)

        try:
            det_tex.image = load_image(detail_diffuse_path)
        except Exception as e:
            print(f"SM2Mat: Could not load detail diffuse {detail_diffuse_path}: {e}")
            return

        # Reuse the UV tiling from detail normal if it exists, otherwise create new
        # Find existing mapping node
        mapping = None
        for n in self.nodes:
            if n.type == 'MAPPING' and n.label.startswith('Tile'):
                mapping = n
                break

        if mapping is None:
            tex_coord = self.nodes.new('ShaderNodeTexCoord')
            tex_coord.location = (det_tex.location[0] - NODE_SPACING_X * 1.5, det_tex.location[1])
            mapping = self.nodes.new('ShaderNodeMapping')
            mapping.location = (det_tex.location[0] - NODE_SPACING_X, det_tex.location[1])
            mapping.label = f'Tile ×{density}'
            mapping.inputs['Scale'].default_value = (density, density, 1.0)
            self.links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

        self.links.new(mapping.outputs['Vector'], det_tex.inputs['Vector'])

        # Blend into base color
        base_input = self.principled.inputs['Base Color']
        if not base_input.is_linked:
            return

        existing_link = base_input.links[0]
        base_source = existing_link.from_socket
        self.links.remove(existing_link)

        # Mask × diffuse scale
        mask_mul = self.nodes.new('ShaderNodeMath')
        mask_mul.operation = 'MULTIPLY'
        mask_mul.location = (det_tex.location[0] + NODE_SPACING_X, det_tex.location[1] - 40)
        mask_mul.label = f'Mask × {diff_scale}'
        mask_mul.inputs[1].default_value = abs(diff_scale)
        self.links.new(mask_socket, mask_mul.inputs[0])

        mix = self.nodes.new('ShaderNodeMix')
        mix.data_type = 'RGBA'
        mix.blend_type = 'OVERLAY'
        mix.label = 'Base + Detail Diffuse'
        mix.location = (det_tex.location[0] + NODE_SPACING_X, det_tex.location[1] + 60)

        self.links.new(mask_mul.outputs['Value'], mix.inputs['Factor'])
        self.links.new(base_source, mix.inputs[6])   # A (RGBA)
        self.links.new(det_tex.outputs['Color'], mix.inputs[7])  # B (RGBA)
        self.links.new(mix.outputs[2], base_input)    # Result (RGBA)

        print(f"SM2Mat:   Detail diffuse wired: density={density}, scale={diff_scale}")

    def finalize_ao(self):
        """If AO was collected, multiply it into the base color."""
        ao_socket = getattr(self, '_ao_socket', None)
        if ao_socket is None:
            return

        base_input = self.principled.inputs['Base Color']
        if not base_input.is_linked:
            return

        # Get the existing base color connection
        base_link = base_input.links[0]
        base_source = base_link.from_socket
        self.links.remove(base_link)

        # Create Mix node: Multiply mode
        mix = self.nodes.new('ShaderNodeMix')
        mix.data_type = 'RGBA'
        mix.blend_type = 'MULTIPLY'
        mix.label = 'AO × Base Color'

        ao_loc = getattr(self, '_ao_sep_location', (0, 0))
        mix.location = (-NODE_SPACING_X * 0.5, ao_loc[1] if ao_loc else -400)

        # Factor = 1.0 (full AO effect)
        mix.inputs['Factor'].default_value = 1.0

        # A = base color, B = AO (as grayscale → needs to be white * ao)
        self.links.new(base_source, mix.inputs[6])  # A (RGBA)

        # For AO: need to route single value into a color
        # Create a MixRGB that blends white with AO factor
        # Actually, for Multiply blend mode: A * B
        # We want: base_color * ao_value
        # So B should be (ao, ao, ao, 1)
        # Use a Combine Color from the AO value
        combine = self.nodes.new('ShaderNodeCombineColor')
        combine.location = (mix.location[0] - 180, mix.location[1] - 100)
        combine.label = 'AO → Color'
        self.links.new(ao_socket, combine.inputs['Red'])
        self.links.new(ao_socket, combine.inputs['Green'])
        self.links.new(ao_socket, combine.inputs['Blue'])
        self.links.new(combine.outputs['Color'], mix.inputs[7])  # B (RGBA)

        self.links.new(mix.outputs[2], base_input)  # Result (RGBA)

    def apply_wrinkle_nodes(self, wrinkle_nm_path, wrinkle_mask_path):
        """Load wrinkle normal map and wrinkle mask as unconnected, labeled nodes.

        These are placed off to the side for manual hookup. The wrinkle system
        requires shape key drivers to work dynamically, which is beyond the
        scope of automatic material import.

        Wrinkle mask channels typically map to facial regions:
          R = region 1 (e.g. forehead)
          G = region 2 (e.g. brow)
          B = region 3 (e.g. cheeks/nose)
          A = region 4 (e.g. chin/mouth)
        """
        wrinkle_x = -NODE_SPACING_X * 6
        wrinkle_y = NODE_SPACING_Y * 2

        if wrinkle_nm_path:
            tex_nm = self.nodes.new('ShaderNodeTexImage')
            tex_nm.label = 'Wrinkle Normal (manual hookup)'
            tex_nm.location = (wrinkle_x, wrinkle_y)
            try:
                tex_nm.image = load_image(wrinkle_nm_path, colorspace='Non-Color')
            except Exception as e:
                print(f"SM2Mat: Could not load wrinkle normal {wrinkle_nm_path}: {e}")

        if wrinkle_mask_path:
            tex_mask = self.nodes.new('ShaderNodeTexImage')
            tex_mask.label = 'Wrinkle Mask (R/G/B/A = face regions)'
            tex_mask.location = (wrinkle_x, wrinkle_y - NODE_SPACING_Y)
            try:
                tex_mask.image = load_image(wrinkle_mask_path, colorspace='Non-Color')
            except Exception as e:
                print(f"SM2Mat: Could not load wrinkle mask {wrinkle_mask_path}: {e}")

        if wrinkle_nm_path or wrinkle_mask_path:
            print(f"SM2Mat:   Wrinkle nodes loaded (unconnected, manual hookup)")

    def apply_backface_culling(self):
        """Set backface culling based on shader params."""
        if not self.params.get('no_backface_culling', False):
            self.mat.use_backface_culling = True
        else:
            self.mat.use_backface_culling = False


# ---------------------------------------------------------------------------
# Material Processor
# ---------------------------------------------------------------------------

def _find_texture_image(tex_name, folder, all_textures):
    """Find an image file for a texture name, checking all_textures first,
    then scanning the folder directly."""
    # Check if it's a known texture
    if tex_name in all_textures:
        return all_textures[tex_name].get('image_path')

    # Direct file search in folder
    for ext in IMAGE_EXTS:
        path = os.path.join(folder, tex_name + ext)
        if os.path.isfile(path):
            return path
    return None


def _build_material_nodes(material, tex_group, shader_params, emissive_divisor,
                          detail_maps_folder, all_textures,
                          wrinkle_nm_path=None, wrinkle_mask_path=None):
    """Core shader tree builder — shared by primary and variant materials."""

    material.use_nodes = True
    builder = NodeTreeBuilder(material, shader_params, emissive_divisor)

    # 1. Diffuse (base color) — compound codes first, plain MD last
    for usage in ('MD+MAK', 'MD+MT', 'MD+MRGH', 'MD+MSP', 'MD'):
        if usage in tex_group:
            builder.apply_diffuse(tex_group[usage])
            break

    # 2. Spec/packed maps (metallic, roughness, AO)
    if 'MSCRGHAO' in tex_group:
        builder.apply_spec_mscrghao(tex_group['MSCRGHAO'])
    elif 'MSCG+MRGH' in tex_group:
        builder.apply_spec_mscg_mrgh(tex_group['MSCG+MRGH'])

    # 3. Standalone AO
    if 'MAO' in tex_group and 'MSCRGHAO' not in tex_group:
        builder.apply_ao(tex_group['MAO'])

    # 4. Normal map
    if 'MNM' in tex_group:
        builder.apply_normal(tex_group['MNM'])

    # 5. Emissive
    if 'MEM' in tex_group:
        builder.apply_emissive(tex_group['MEM'])
    elif 'MEM+MAO' in tex_group:
        builder.apply_emissive(tex_group['MEM+MAO'], has_ao_alpha=True)

    # 6. Resolve detail normal texture path
    detail_normal_path = None
    detail_diffuse_path = None
    det_tex_name = shader_params.get('detail_normal_tex', '')
    if det_tex_name and detail_maps_folder:
        detail_normal_path = find_detail_texture(det_tex_name, detail_maps_folder)
        if detail_normal_path:
            print(f"SM2Mat:   Found detail normal: {det_tex_name}")
        else:
            print(f"SM2Mat:   Detail normal '{det_tex_name}' not found in {detail_maps_folder}")

    det_diff_name = shader_params.get('detail_diffuse_tex', '')
    if det_diff_name and detail_maps_folder:
        detail_diffuse_path = find_detail_texture(det_diff_name, detail_maps_folder)

    # 7. Height (standalone or combined with detail mask)
    if 'MH' in tex_group:
        builder.apply_height(tex_group['MH'])
    elif 'MH+MDTM' in tex_group:
        builder.apply_height_detm(tex_group['MH+MDTM'], detail_normal_path)

    # 8. Detail mask — sources in priority order:
    #    a) MH+MDTM already handled in step 7 (sets _detail_mask_socket)
    #    b) Standalone MDTM in texture group
    #    c) tex.heightDetMask reference (the proper detail system field)
    #    d) covering.coveringMask fallback (separate system, but same mask format)
    if 'MDTM' in tex_group and 'MH+MDTM' not in tex_group:
        builder.apply_detail_mask(tex_group['MDTM'], detail_normal_path)
    elif 'MH+MDTM' not in tex_group:
        # Try heightDetMask first (proper detail system field)
        hdm_name = shader_params.get('height_det_mask', '')
        covering_name = shader_params.get('covering_mask', '')
        mask_info = None

        if hdm_name and detail_normal_path:
            mask_info = all_textures.get(hdm_name)
            if mask_info:
                print(f"SM2Mat:   Using heightDetMask: {hdm_name}")
        if not mask_info and covering_name and detail_normal_path:
            mask_info = all_textures.get(covering_name)
            if mask_info:
                print(f"SM2Mat:   Using covering mask: {covering_name}")

        if mask_info:
            builder.apply_detail_mask(mask_info, detail_normal_path)

    # 9. Detail normal blending
    if detail_normal_path:
        builder.apply_detail_normal(detail_normal_path)

    # 10. Detail diffuse blending
    if detail_diffuse_path:
        builder.apply_detail_diffuse(detail_diffuse_path)

    # 11. Finalize AO mixing
    builder.finalize_ao()

    # 12. Wrinkle nodes (unconnected, for manual hookup)
    if wrinkle_nm_path or wrinkle_mask_path:
        builder.apply_wrinkle_nodes(wrinkle_nm_path, wrinkle_mask_path)

    # 13. Backface culling
    builder.apply_backface_culling()


def process_material(material, folder, all_textures, emissive_divisor,
                     detail_maps_folder=''):
    """Process a single Blender material: find its textures, build the shader tree,
    and create variant materials for submaterials with texture overrides."""
    mat_name = material.name

    # Find which texture base this material corresponds to
    submaterial, tex_base = find_texture_base_for_material(mat_name, all_textures)
    if tex_base is None:
        print(f"SM2Mat: No texture match for material '{mat_name}', skipping")
        return False

    # Get all textures in this group (primary + variants)
    tex_group, variant_textures = get_texture_group(tex_base, all_textures)
    if not tex_group:
        print(f"SM2Mat: No texture group for '{tex_base}', skipping")
        return False

    # Find the .td with the materials block (shader params source)
    diffuse_td = None
    for usage in ('MD+MAK', 'MD+MT', 'MD+MRGH', 'MD+MSP', 'MD'):
        if usage in tex_group:
            diffuse_td = tex_group[usage].get('td_data', {})
            break
    if diffuse_td is None:
        for info in tex_group.values():
            td = info.get('td_data', {})
            if 'materials' in td:
                diffuse_td = td
                break

    shader_params = get_shader_params(diffuse_td or {}, submaterial or 'default')

    # Resolve wrinkle texture paths
    wrinkle_nm_path = None
    wrinkle_mask_path = None
    if shader_params.get('wrinkle_enable', False):
        wn = shader_params.get('wrinkle_nm', '')
        wm = shader_params.get('wrinkle_mask', '')
        if wn:
            wrinkle_nm_path = _find_texture_image(wn, folder, all_textures)
        if wm:
            wrinkle_mask_path = _find_texture_image(wm, folder, all_textures)
        # If shader params didn't have explicit names, try finding by convention
        if not wrinkle_nm_path:
            wrinkle_nm_path = _find_texture_image(tex_base + '_wrinkle_nm', folder, all_textures)
        if not wrinkle_mask_path:
            wrinkle_mask_path = _find_texture_image(tex_base + '_wrinkle_mask', folder, all_textures)

    # --- Build the primary material ---
    _build_material_nodes(material, tex_group, shader_params, emissive_divisor,
                          detail_maps_folder, all_textures,
                          wrinkle_nm_path, wrinkle_mask_path)

    print(f"SM2Mat: Built shader for '{mat_name}' "
          f"(base={tex_base}, sub={submaterial}, textures={list(tex_group.keys())})")

    # --- Create variant materials for submaterials with texture overrides ---
    if diffuse_td:
        variant_subs = get_variant_submaterials(diffuse_td)
        for var_sub_key, overrides in variant_subs.items():
            # Skip the current submaterial (it's the primary)
            if var_sub_key == (submaterial or 'default'):
                continue

            variant_mat_name = f"{var_sub_key}_{tex_base}"

            # Don't recreate if it already exists
            if variant_mat_name in bpy.data.materials:
                print(f"SM2Mat:   Variant '{variant_mat_name}' already exists, skipping")
                continue

            # Build a modified tex_group with overridden textures
            var_tex_group = dict(tex_group)  # Shallow copy of primary group

            for override_type, override_tex_name in overrides.items():
                override_info = all_textures.get(override_tex_name)
                if not override_info:
                    # Also check variant_textures
                    override_info = variant_textures.get(override_tex_name)
                if not override_info:
                    print(f"SM2Mat:   Variant override '{override_tex_name}' not found, skipping")
                    continue

                # Swap the appropriate usage slot
                if override_type == 'diffuse':
                    # Replace whatever diffuse usage is in the group
                    for u in ('MD+MAK', 'MD+MT', 'MD+MRGH', 'MD+MSP', 'MD'):
                        if u in var_tex_group:
                            var_tex_group[u] = override_info
                            break
                    else:
                        # Use the override's own usage code
                        var_tex_group[override_info['usage']] = override_info
                elif override_type == 'normal':
                    var_tex_group['MNM'] = override_info
                elif override_type == 'spec':
                    # Replace whichever spec format exists
                    for u in ('MSCRGHAO', 'MSCG+MRGH'):
                        if u in var_tex_group:
                            var_tex_group[u] = override_info
                            break
                    else:
                        var_tex_group[override_info['usage']] = override_info

            # Get shader params for this variant's submaterial
            var_shader_params = get_shader_params(diffuse_td, var_sub_key)

            # Create the variant material (standalone, not assigned to any slot)
            var_mat = bpy.data.materials.new(name=variant_mat_name)
            _build_material_nodes(var_mat, var_tex_group, var_shader_params,
                                  emissive_divisor, detail_maps_folder,
                                  all_textures, wrinkle_nm_path, wrinkle_mask_path)

            print(f"SM2Mat:   Created variant material '{variant_mat_name}' "
                  f"(overrides: {list(overrides.keys())})")

    return True


# ---------------------------------------------------------------------------
# Stored Settings
# ---------------------------------------------------------------------------

def _get_addon_preferences(context=None):
    preferences = getattr(context, 'preferences', None) if context else bpy.context.preferences
    addon = preferences.addons.get(ADDON_ID) if preferences else None
    return addon.preferences if addon else None


def _save_user_preferences():
    try:
        bpy.ops.wm.save_userpref()
    except Exception as e:
        print(f"SM2Mat: Could not save sticky detail maps path: {e}")


def _remember_detail_maps_path(self, context):
    global _SM2MAT_SYNCING_DETAIL_PATH
    if _SM2MAT_SYNCING_DETAIL_PATH:
        return

    prefs = _get_addon_preferences(context)
    if not prefs:
        return

    path = self.detail_maps_path
    if prefs.detail_maps_path == path:
        return

    _SM2MAT_SYNCING_DETAIL_PATH = True
    try:
        prefs.detail_maps_path = path
    finally:
        _SM2MAT_SYNCING_DETAIL_PATH = False
    _save_user_preferences()


def _apply_sticky_detail_maps_path(scene=None):
    global _SM2MAT_SYNCING_DETAIL_PATH
    prefs = _get_addon_preferences()
    path = getattr(prefs, 'detail_maps_path', '') if prefs else ''
    if not path:
        return

    scene = scene or bpy.context.scene
    props = getattr(scene, 'sm2mat', None)
    if not props or props.detail_maps_path == path:
        return

    _SM2MAT_SYNCING_DETAIL_PATH = True
    try:
        props.detail_maps_path = path
    finally:
        _SM2MAT_SYNCING_DETAIL_PATH = False


def _preferences_detail_maps_path_update(self, context):
    if _SM2MAT_SYNCING_DETAIL_PATH:
        return
    _apply_sticky_detail_maps_path(context.scene if context else None)
    _save_user_preferences()


@persistent
def _sm2mat_load_post(_dummy):
    _apply_sticky_detail_maps_path()


class SM2MAT_Preferences(bpy.types.AddonPreferences):
    """Addon-level settings that survive Blender restarts."""
    bl_idname = ADDON_ID

    detail_maps_path: StringProperty(
        name="Detail Maps Folder",
        description="Last detail maps folder used in the SM2Mat N-panel",
        default="",
        subtype='DIR_PATH',
        update=_preferences_detail_maps_path_update,
    )

    def draw(self, context):
        self.layout.prop(self, "detail_maps_path")


class SM2MAT_Properties(bpy.types.PropertyGroup):
    """Settings shown in the N-panel."""

    fbx_path: StringProperty(
        name="FBX File",
        description="Path to the SM2 .fbx model file to import",
        default="",
    )

    detail_maps_path: StringProperty(
        name="Detail Maps Folder",
        description="Folder containing shared detail normal/diffuse textures "
                    "(e.g., detail_04_det.dds, leather_02_det.dds). "
                    "Leave empty to skip detail normal blending",
        default="",
        subtype='DIR_PATH',
        update=_remember_detail_maps_path,
    )

    emissive_divisor: FloatProperty(
        name="Emissive Divisor",
        description="Divide engine emissive intensity by this value for Blender "
                    "(engine range 0-4500, Blender expects ~0-50)",
        default=100.0,
        min=1.0,
        max=1000.0,
    )

    remove_empty_branches: BoolProperty(
        name="Remove Empty Branches",
        description="After import, remove any Empty objects whose hierarchy "
                    "contains no Mesh or Armature descendants (cleans up "
                    "leftover Empties from the FBX scene graph)",
        default=True,
    )


# ---------------------------------------------------------------------------
# Import Helpers
# ---------------------------------------------------------------------------

def _has_mesh_or_armature_descendant(obj):
    """Return True if obj itself is a MESH/ARMATURE, or any descendant is."""
    if obj.type in {'MESH', 'ARMATURE'}:
        return True
    for child in obj.children:
        if _has_mesh_or_armature_descendant(child):
            return True
    return False


def _remove_orphan_empty_branches(objects):
    """Delete every Empty in `objects` whose hierarchy has no MESH/ARMATURE.

    Returns the number of empties removed.
    """
    to_remove = [
        obj for obj in objects
        if obj.type == 'EMPTY' and not _has_mesh_or_armature_descendant(obj)
    ]
    for obj in to_remove:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except (RuntimeError, ReferenceError):
            pass
    return len(to_remove)


def _scale_and_apply_transforms(objects, factor):
    """Scale the root-level objects (within `objects`) by `factor`, then
    apply scale + rotation to every object in `objects`."""
    if not objects:
        return

    obj_set = set(objects)

    # Multiply scale on roots only — children inherit through the hierarchy
    for obj in objects:
        if obj.parent not in obj_set:
            obj.scale = (obj.scale[0] * factor,
                         obj.scale[1] * factor,
                         obj.scale[2] * factor)

    # Apply scale + rotation. The FBX importer leaves the imported objects
    # selected, so transform_apply operates on the right set.
    try:
        bpy.ops.object.transform_apply(
            location=False, rotation=True, scale=True
        )
    except RuntimeError as e:
        print(f"SM2Mat: transform_apply warning: {e}")


def _resolve_detail_maps_folder(detail_maps_path, texture_folder, report=None):
    """Resolve the optional detail maps folder relative to the FBX texture folder."""
    detail_folder = bpy.path.abspath(detail_maps_path).strip()
    if detail_folder and not os.path.isabs(detail_folder):
        detail_folder = os.path.join(os.path.dirname(texture_folder), detail_folder)

    if detail_folder and os.path.isdir(detail_folder):
        if report:
            report({'INFO'}, f"SM2Mat: Using detail maps from {detail_folder}")
        return detail_folder

    if detail_folder and report:
        report({'WARNING'}, f"SM2Mat: Detail maps folder not found: {detail_folder}")
    return ''


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class SM2MAT_OT_select_fbx(bpy.types.Operator, ImportHelper):
    """Choose an FBX file for SM2Mat"""
    bl_idname = "sm2mat.select_fbx"
    bl_label = "Select FBX"
    bl_options = {'REGISTER'}

    filename_ext = ".fbx"

    filter_glob: StringProperty(
        default="*.fbx",
        options={'HIDDEN'},
    )

    def invoke(self, context, event):
        fbx_path = context.scene.sm2mat.fbx_path.strip()
        if fbx_path:
            self.filepath = bpy.path.abspath(fbx_path)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.filepath.lower().endswith('.fbx'):
            self.report({'ERROR'}, "SM2Mat: Selected file is not an .fbx")
            return {'CANCELLED'}

        context.scene.sm2mat.fbx_path = self.filepath
        return {'FINISHED'}


class SM2MAT_OT_import_fbx(bpy.types.Operator):
    """Import the selected FBX with locked SM2 settings and build PBR materials"""
    bl_idname = "sm2mat.import_fbx"
    bl_label = "Import & Build Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.sm2mat

        fbx_path = bpy.path.abspath(props.fbx_path).strip()
        if not fbx_path or not os.path.isfile(fbx_path):
            self.report({'ERROR'}, "SM2Mat: Please set a valid FBX file path")
            return {'CANCELLED'}

        if not fbx_path.lower().endswith('.fbx'):
            self.report({'ERROR'}, "SM2Mat: Selected file is not an .fbx")
            return {'CANCELLED'}

        folder = os.path.dirname(fbx_path)

        # Step 1: Import FBX with locked settings
        self.report({'INFO'}, "SM2Mat: Importing FBX...")
        result = bpy.ops.import_scene.fbx(
            filepath=fbx_path,
            use_custom_normals=True,
            use_image_search=False,
            use_anim=True,
            automatic_bone_orientation=False,
            global_scale=1.0,
            axis_forward='-Z',
            axis_up='Y',
            use_custom_props=True,
            primary_bone_axis='Y',
            secondary_bone_axis='X',
        )

        if result != {'FINISHED'}:
            self.report({'ERROR'}, "SM2Mat: FBX import failed")
            return {'CANCELLED'}

        # Step 1b: Scale imported objects by 100× and apply scale + rotation.
        # SM2 FBX exports come in at a tiny scale; this lifts them to Blender's
        # typical working size and bakes the transforms so downstream math is
        # in clean local space.
        imported_after_import = list(context.selected_objects)
        _scale_and_apply_transforms(imported_after_import, 100.0)

        # Step 1c: Optionally strip Empty objects whose hierarchy has no
        # mesh or armature in it (cleans up the noisy FBX scene graph).
        if props.remove_empty_branches:
            removed = _remove_orphan_empty_branches(
                list(context.selected_objects)
            )
            if removed:
                self.report(
                    {'INFO'},
                    f"SM2Mat: Removed {removed} empty branch object(s)"
                )

        # Step 2: Discover all textures in the folder
        all_textures = discover_textures(folder)
        if not all_textures:
            self.report({'WARNING'}, f"SM2Mat: No .td files found in {folder}")
            return {'FINISHED'}

        self.report({'INFO'}, f"SM2Mat: Found {len(all_textures)} textures with .td files")

        detail_folder = _resolve_detail_maps_folder(
            props.detail_maps_path, folder, self.report
        )

        # Step 3: Process each material on imported objects
        imported_objects = context.selected_objects
        processed = 0
        skipped = 0

        materials_seen = set()
        for obj in imported_objects:
            if obj.type != 'MESH':
                continue
            for slot in obj.material_slots:
                if slot.material and slot.material.name not in materials_seen:
                    materials_seen.add(slot.material.name)
                    success = process_material(
                        slot.material, folder, all_textures,
                        props.emissive_divisor, detail_folder,
                    )
                    if success:
                        processed += 1
                    else:
                        skipped += 1

        self.report({'INFO'},
                    f"SM2Mat: Processed {processed} materials, skipped {skipped}")
        return {'FINISHED'}


class SM2MAT_OT_rebuild_materials(bpy.types.Operator):
    """Reload PBR materials to their default state on the selected mesh(es) from .td sidecar files"""
    bl_idname = "sm2mat.rebuild_materials"
    bl_label = "Reload Materials to Default (Selected Mesh)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        props = context.scene.sm2mat

        # We need to know which folder the textures are in.
        # Use the FBX path's folder, or fall back to trying to find textures
        # in the same folder as the first mesh's texture.
        fbx_path = bpy.path.abspath(props.fbx_path).strip()
        if not fbx_path or not os.path.isfile(fbx_path):
            self.report({'ERROR'},
                        "SM2Mat: Set the FBX path so we know where to find textures")
            return {'CANCELLED'}

        folder = os.path.dirname(fbx_path)
        all_textures = discover_textures(folder)
        if not all_textures:
            self.report({'WARNING'}, f"SM2Mat: No .td files found in {folder}")
            return {'FINISHED'}

        detail_folder = _resolve_detail_maps_folder(props.detail_maps_path, folder)

        processed = 0
        skipped = 0
        materials_seen = set()

        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            for slot in obj.material_slots:
                if slot.material and slot.material.name not in materials_seen:
                    materials_seen.add(slot.material.name)
                    success = process_material(
                        slot.material, folder, all_textures,
                        props.emissive_divisor, detail_folder,
                    )
                    if success:
                        processed += 1
                    else:
                        skipped += 1

        self.report({'INFO'},
                    f"SM2Mat: Rebuilt {processed} materials, skipped {skipped}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# N-Panel (Sidebar)
# ---------------------------------------------------------------------------

class SM2MAT_PT_main(bpy.types.Panel):
    """SM2Mat panel in the 3D Viewport sidebar"""
    bl_label = "SM2Mat"
    bl_idname = "SM2MAT_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SM2Mat"

    def draw(self, context):
        layout = self.layout
        props = context.scene.sm2mat

        # --- File Paths ---
        box = layout.box()
        box.label(text="Paths", icon='FILE_FOLDER')
        row = box.row(align=True)
        row.prop(props, "fbx_path", text="FBX")
        row.operator("sm2mat.select_fbx", text="", icon='FILEBROWSER')
        box.prop(props, "detail_maps_path", text="Detail Maps")

        # --- Settings ---
        box = layout.box()
        box.label(text="Settings", icon='PREFERENCES')
        box.prop(props, "emissive_divisor")

        # --- Import (checkbox + import operator live in their own group) ---
        box = layout.box()
        box.label(text="Import", icon='IMPORT')
        box.prop(props, "remove_empty_branches")
        col = box.column(align=True)
        col.scale_y = 1.5
        col.operator("sm2mat.import_fbx", icon='IMPORT')

        # --- Reload Materials (standalone action) ---
        layout.separator()
        col = layout.column(align=True)
        col.operator("sm2mat.rebuild_materials", icon='MATERIAL')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    SM2MAT_Preferences,
    SM2MAT_Properties,
    SM2MAT_OT_select_fbx,
    SM2MAT_OT_import_fbx,
    SM2MAT_OT_rebuild_materials,
    SM2MAT_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sm2mat = PointerProperty(type=SM2MAT_Properties)
    if _sm2mat_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_sm2mat_load_post)
    _apply_sticky_detail_maps_path()


def unregister():
    if _sm2mat_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_sm2mat_load_post)
    del bpy.types.Scene.sm2mat
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
