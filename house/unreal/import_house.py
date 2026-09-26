"""
import_house.py - run INSIDE the Unreal Editor to build the walkable house scene.

1. Create a project from the "Third Person" template (so you have a character
   that can walk), open a new Basic level (it already has sky + sun).
2. Enable the "Python Editor Script Plugin" (Edit > Plugins) and restart.
3. Tools > Execute Python Script... and pick this file (keep it inside the
   output/unreal folder, next to placements.json and the meshes folder).
4. Press Play - you spawn on the driveway and can walk all the way around.

What it does
  * imports every meshes/SM_*.fbx into /Game/MyHouse/Meshes (with textures)
  * places them exactly as in Blender, in the Outliner folders
    MyHouse/House, MyHouse/Yard and MyHouse/Plants
  * gives the house/yard per-polygon collision so you can walk up to the walls,
    through the entry and onto the porch
  * adds a Player Start on the driveway

Swapping plants
  * One plant: click it, and in Details change "Static Mesh" to any plant.
  * All plants of a type: put the new mesh's path in plant_swaps.json
    ("by_type") or for one object ("by_object"), then run this script again.
    Re-running replaces what it placed before.
"""

import json
import os

import unreal

DATA_DIR = ""                       # leave empty = folder this script is in
DEST = "/Game/MyHouse/Meshes"
TAG = "MyHouse"


def folder():
    if DATA_DIR:
        return DATA_DIR
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        raise RuntimeError("Set DATA_DIR at the top of import_house.py to the output/unreal folder")


def log(msg):
    unreal.log("[MyHouse] " + msg)


def actor_subsystem():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def import_meshes(mesh_dir, names):
    tasks = []
    for name in names:
        opts = unreal.FbxImportUI()
        opts.set_editor_property("import_mesh", True)
        opts.set_editor_property("import_as_skeletal", False)
        opts.set_editor_property("import_materials", True)
        opts.set_editor_property("import_textures", True)
        opts.set_editor_property("import_animations", False)
        opts.set_editor_property("mesh_type_to_import", unreal.FBXImportType.FBXIT_STATIC_MESH)
        sm = opts.get_editor_property("static_mesh_import_data")
        sm.set_editor_property("combine_meshes", True)
        sm.set_editor_property("auto_generate_collision", True)
        sm.set_editor_property("generate_lightmap_u_vs", True)

        task = unreal.AssetImportTask()
        task.set_editor_property("filename", os.path.join(mesh_dir, name + ".fbx"))
        task.set_editor_property("destination_path", DEST)
        task.set_editor_property("destination_name", name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        task.set_editor_property("options", opts)
        tasks.append(task)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)


def use_complex_collision(mesh):
    """Walls/ground: collide against the real triangles, not a box around the house."""
    try:
        body = mesh.get_editor_property("body_setup")
        body.set_editor_property("collision_trace_flag",
                                 unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
        unreal.EditorAssetLibrary.save_loaded_asset(mesh)
    except Exception as e:  # older engine versions
        unreal.log_warning("[MyHouse] could not set complex collision on %s: %s" % (mesh.get_name(), e))


def remove_previous():
    subsystem = actor_subsystem()
    for actor in subsystem.get_all_level_actors():
        if unreal.Name(TAG) in list(actor.tags):
            subsystem.destroy_actor(actor)


def main():
    base = folder()
    with open(os.path.join(base, "placements.json")) as f:
        objects = json.load(f)["objects"]
    swaps = {"by_type": {}, "by_object": {}}
    swap_path = os.path.join(base, "plant_swaps.json")
    if os.path.exists(swap_path):
        with open(swap_path) as f:
            swaps.update(json.load(f))

    names = sorted({o["mesh"] for o in objects if "mesh" in o})
    log("importing %d meshes..." % len(names))
    import_meshes(os.path.join(base, "meshes"), names)

    meshes = {}
    for name in names:
        mesh = unreal.EditorAssetLibrary.load_asset("%s/%s.%s" % (DEST, name, name))
        if mesh is None:
            unreal.log_error("[MyHouse] import failed for " + name)
            continue
        meshes[name] = mesh
    for o in objects:
        if o.get("collision") == "complex" and o["mesh"] in meshes:
            use_complex_collision(meshes[o["mesh"]])

    remove_previous()
    subsystem = actor_subsystem()
    for o in objects:
        loc = unreal.Vector(*o["location"])
        rot = unreal.Rotator(roll=0.0, pitch=0.0, yaw=o.get("yaw", 0.0))
        if o["category"] == "Spawn":
            actor = subsystem.spawn_actor_from_class(unreal.PlayerStart, loc, rot)
        else:
            mesh = meshes.get(o["mesh"])
            if o["category"] == "Plant":
                repl = swaps["by_object"].get(o["name"]) or swaps["by_type"].get(o["mesh"])
                if repl:
                    swapped = unreal.EditorAssetLibrary.load_asset(repl)
                    if swapped:
                        mesh = swapped
                    else:
                        unreal.log_warning("[MyHouse] swap mesh not found: " + repl)
            if mesh is None:
                continue
            actor = subsystem.spawn_actor_from_object(mesh, loc, rot)
            s = o.get("scale", 1.0)
            actor.set_actor_scale3d(unreal.Vector(s, s, s))
        actor.set_actor_label(o["name"])
        actor.set_folder_path("MyHouse/%ss" % o["category"] if o["category"] == "Plant"
                              else "MyHouse/" + o["category"])
        actor.tags = [unreal.Name(TAG), unreal.Name(o["category"])]
    log("placed %d objects. Press Play to walk around." % len(objects))


main()
