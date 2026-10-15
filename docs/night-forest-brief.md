# Brief: night forest clearing (game background)

Hand this to the agent along with the reference image.

## Goal

Recreate the reference as a layered Krita document: a dark plum night sky framed by trees, with a clearing that leads into a far pine forest. The look is soft oil pastel / wax crayon on dark toned paper: visible grain, scribbly hatching, pale outlines on flowers and leaves, no hard digital edges. The centre of the image stays mostly empty because it is space for UI.

Canvas: 1920×1080, background `#332437`. Save `night-forest.kra` often (`overwrite=true` after the first save).

## Layer stack (bottom → top)

Each group becomes one parallax layer in the export.

| Group | Layers | Notes |
|-------|--------|-------|
| `Background` | paper | Fill `#332437`, add faint `noise` for paper grain |
| `Sky` | stars, sparkles | 4-point sparkles and small 5-point stars in the upper arc only |
| `Far forest` | far pines, haze | Pine silhouettes along the horizon, low contrast, `#35263a` to `#514b67` |
| `Midground` | mid trees, bushes | Rounded canopies left and right, slate blue `#4a536f` to `#5f5a7a` |
| `Ground` | path, grass | Clearing floor `#49496e` / `#614a6d` with horizontal hatching, lighter toward the centre |
| `Frame left` | trunk, canopy, rocks, flowers | Big trunk with pink-lilac highlights, leaf clusters, boulder lower left |
| `Frame right` | trunk, stump, canopy, rocks, flowers | Mirrors the left side without copying it |

Inside each group, block in the shapes as filled polygons first. Then add a shading layer above with `inherit_alpha=true` (clipped), or alpha-lock the base layer, and texture it with textured native strokes.

## Palette

- Sky and paper: `#332437`, `#2c2233`, `#35273b`
- Shadows: `#342a3f`, `#373651`, `#413b56`
- Mid tones: `#4a536f`, `#685a76`, `#7d768b`, `#614a6d`
- Highlights: `#948b97`, `#aea3a2`, `#b7848d` (pink bark), `#a483a6` (lilac)
- Leaf accents: `#758f9b` (teal), `#7c988c` (sage), `#697ead` (periwinkle)
- Flowers and stars: `#d3af89` (butter yellow), `#caa18e` (peach), `#cf989c` (pink), `#9283a4` (violet), `#decbaf` (cream)

## Technique

- Find textured presets with `krita_list_brushes` (filters: `pastel`, `crayon`, `chalk`, `pencil`, `charcoal`). Try a few strokes on a scratch layer and look at them with `krita_get_canvas` before committing.
- Hatch with many short strokes of 3–6 points, using `pressures` that taper both ends (e.g. `[0.3, 1, 0.8, 0.2]`) and slight colour variation between strokes.
- Flowers: a filled pale shape, then a thin darker outline and a dot centre. Stars: small `#decbaf` / `#d3af89` shapes; mint and lilac sparkles.
- Check progress with `krita_get_canvas` after each group, and per layer with `layer=`.

## Deliverables

1. `night-forest.kra`: layered, editable source.
2. `krita_export_layers("night-forest-layers")`: one full-size PNG per group, plus `manifest.json`.
3. `night-forest.png`: flattened preview.
