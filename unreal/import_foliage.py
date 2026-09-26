"""
import_foliage.py - run INSIDE the Unreal Editor to place the plants.

Prerequisites
  1. Enable the "Python Editor Script Plugin" (Edit > Plugins), restart.
  2. Import heightmap.png as a Landscape (see README - use the scale from manifest.json).
  3. Keep this script next to foliage_points.csv / foliage_config.json
     (image_to_landscape.py copies it into every output folder).

Run it with  Tools > Execute Python Script...  (or File > Execute Python Script)
and pick this file. Run it again any time: it clears what it placed before.

How plants are placed
  For each category (Trees, Bushes, Grass, Rocks) and each mesh listed in
  foliage_config.json, a Foliage Type asset is created under
  /Game/ImageLandscape/Foliage/  (FT_Trees_0, FT_Trees_1, ...) and its instances
  are added to the level's foliage. They show up in Foliage mode, so you can
  paint/erase them by hand afterwards.

Swapping plants (two ways)
  a) Quick: open an FT_* asset, change its Mesh -> every instance updates.
  b) Edit foliage_config.json (several meshes per category = random mix) and
     run this script again.
"""

import csv
import json
import os

import unreal

# Folder holding foliage_points.csv; defaults to wherever this script lives.
DATA_DIR = ""
ASSET_DIR = "/Game/ImageLandscape/Foliage"
BATCH = 20000


def data_dir():
    if DATA_DIR:
        return DATA_DIR
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        raise RuntimeError("Set DATA_DIR at the top of import_foliage.py to your output folder")


def log(msg):
    unreal.log("[ImageLandscape] " + msg)


def editor_world():
    try:
        return unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    except Exception:
        return unreal.EditorLevelLibrary.get_editor_world()


def all_actors():
    try:
        return unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
    except Exception:
        return unreal.EditorLevelLibrary.get_all_level_actors()


def find_landscape():
    for actor in all_actors():
        if isinstance(actor, unreal.LandscapeProxy):
            return actor
    return None


def ensure_foliage_type(name, mesh_path, cfg):
    path = ASSET_DIR + "/" + name
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if mesh is None:
        unreal.log_warning("[ImageLandscape] mesh not found: %s (skipping)" % mesh_path)
        return None

    if unreal.EditorAssetLibrary.does_asset_exist(path):
        ft = unreal.EditorAssetLibrary.load_asset(path)
    else:
        factory_cls = getattr(unreal, "FoliageType_InstancedStaticMeshFactory", None)
        factory = factory_cls() if factory_cls else None
        ft = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            name, ASSET_DIR, unreal.FoliageType_InstancedStaticMesh, factory)
        if ft is None:
            raise RuntimeError("could not create Foliage Type " + path)

    # The config is the source of truth for the mesh when the script runs.
    ft.set_editor_property("mesh", mesh)
    for prop, value in (("align_to_normal", cfg.get("align_to_normal", False)),
                        ("random_yaw", cfg.get("random_yaw", True)),
                        ("cast_shadow", cfg.get("cast_shadow", True))):
        try:
            ft.set_editor_property(prop, value)
        except Exception:
            pass
    cull = int(cfg.get("cull_distance_cm", 0))
    try:
        ft.set_editor_property("cull_distance", unreal.Int32Interval(min=0, max=cull))
    except Exception:
        pass
    unreal.EditorAssetLibrary.save_loaded_asset(ft)
    return ft


def read_points(path):
    by_cat = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            by_cat.setdefault(row["category"], []).append(row)
    return by_cat


def make_transforms(rows, origin, scale3d, cfg):
    mult = float(cfg.get("scale_multiplier", 1.0))
    pivot = float(cfg.get("mesh_pivot_offset_cm", 0.0))
    out = []
    for r in rows:
        loc = unreal.Vector(
            origin.x + float(r["col"]) * scale3d.x,
            origin.y + float(r["row"]) * scale3d.y,
            origin.z + (float(r["height16"]) - 32768.0) / 128.0 * scale3d.z)
        s = float(r["scale"]) * mult
        loc.z += pivot * s  # lift meshes whose pivot is in their middle
        out.append(unreal.Transform(
            location=loc,
            rotation=unreal.Rotator(roll=0.0, pitch=0.0, yaw=float(r["yaw"])),
            scale=unreal.Vector(s, s, s)))
    return out


# --- fallback for engine versions without InstancedFoliageActor.add_instances ---

def spawn_hism_actor(label, mesh_path, transforms):
    subsystem = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.Actor, unreal.Vector(0, 0, 0))
    actor.set_actor_label(label)
    actor.tags = [unreal.Name("ImageLandscape")]
    root = subsystem.k2_gather_subobject_data_for_instance(actor)[0]
    handle, _ = subsystem.add_new_subobject(unreal.AddNewSubobjectParams(
        parent_handle=root, new_class=unreal.HierarchicalInstancedStaticMeshComponent))
    lib = unreal.SubobjectDataBlueprintFunctionLibrary
    comp = lib.get_object(lib.get_data(handle))
    comp.set_editor_property("static_mesh", unreal.EditorAssetLibrary.load_asset(mesh_path))
    comp.add_instances(transforms, False, True)


def clear_previous_fallback_actors():
    for actor in all_actors():
        if unreal.Name("ImageLandscape") in list(actor.tags):
            actor.destroy_actor()


def main():
    folder = data_dir()
    with open(os.path.join(folder, "foliage_config.json")) as f:
        config = json.load(f)["categories"]
    points = read_points(os.path.join(folder, "foliage_points.csv"))

    landscape = find_landscape()
    if landscape:
        origin = landscape.get_actor_location()
        scale3d = landscape.get_actor_scale3d()
        log("aligning to landscape '%s' at %s scale %s"
            % (landscape.get_actor_label(), origin, scale3d))
    else:
        with open(os.path.join(folder, "manifest.json")) as f:
            s = json.load(f)["unreal"]["landscape_scale"]
        origin = unreal.Vector(0, 0, 0)
        scale3d = unreal.Vector(s["x"], s["y"], s["z"])
        unreal.log_warning("[ImageLandscape] no Landscape in level; assuming it sits at 0,0,0")

    world = editor_world()
    ifa = unreal.InstancedFoliageActor
    use_foliage = hasattr(ifa, "add_instances")
    if not use_foliage:
        unreal.log_warning("[ImageLandscape] this engine version can't add foliage from "
                           "Python; spawning HISM actors instead (swap mesh on the component)")
        clear_previous_fallback_actors()

    total = sum(len(v) for v in points.values())
    with unreal.ScopedSlowTask(total, "Placing plants from picture...") as task:
        task.make_dialog(True)
        for cat, rows in points.items():
            cfg = config.get(cat, {})
            meshes = cfg.get("meshes") or []
            if not cfg.get("enabled", True) or not meshes:
                log("skipping %s" % cat)
                task.enter_progress_frame(len(rows))
                continue

            # Split this category's points between its meshes using the stable
            # per-point 'variant' value, so re-runs keep the same layout.
            buckets = [[] for _ in meshes]
            for r in rows:
                buckets[min(int(float(r["variant"]) * len(meshes)), len(meshes) - 1)].append(r)

            for i, (mesh_path, bucket) in enumerate(zip(meshes, buckets)):
                name = "FT_%s_%d" % (cat, i)
                transforms = make_transforms(bucket, origin, scale3d, cfg)
                if use_foliage:
                    ft = ensure_foliage_type(name, mesh_path, cfg)
                    if ft is None:
                        task.enter_progress_frame(len(bucket))
                        continue
                    if hasattr(ifa, "remove_all_instances"):
                        ifa.remove_all_instances(world, ft)
                    for start in range(0, len(transforms), BATCH):
                        if task.should_cancel():
                            return
                        chunk = transforms[start:start + BATCH]
                        ifa.add_instances(world, ft, chunk)
                        task.enter_progress_frame(len(chunk), "%s: %d instances" % (name, len(chunk)))
                else:
                    spawn_hism_actor("Plants_" + name[3:], mesh_path, transforms)
                    task.enter_progress_frame(len(bucket))
                log("%s -> %d x %s" % (name, len(bucket), mesh_path))

    log("done: %d instances. Swap plants by editing the FT_* assets in %s" % (total, ASSET_DIR))


main()
