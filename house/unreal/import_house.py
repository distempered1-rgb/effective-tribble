"""
import_house.py - run INSIDE the Unreal Editor to build the walkable house scene.

EASIEST (nothing to download): in Unreal open  Window > Output Log, switch the
box at the bottom-left from "Cmd" to "Python", paste this one line and press Enter:

import urllib.request as r; exec(r.urlopen("https://raw.githubusercontent.com/distempered1-rgb/effective-tribble/refs/heads/claude/3d-landscape-unreal-engine-yw7zzp/house/unreal/import_house.py").read().decode())

The script downloads the model straight from GitHub into <YourProject>/Saved/MyHouse,
imports it and places it in the open level. (You can also run this file with
Tools > Execute Python Script; if it sits next to placements.json it uses those
local files instead of downloading.)

Before running: make a project from the "Third Person" template, File > New Level >
Basic, and enable the "Python Editor Script Plugin" (Edit > Plugins, restart).
Then press Play - you spawn on the driveway and can walk all the way around.

Swapping plants
  * One plant: click it, and in Details change "Static Mesh" to any plant.
  * All plants of a type: put the new mesh path in Saved/MyHouse/plant_swaps.json
    ("by_type") or for one object ("by_object") and run the line again.
    Re-running replaces what it placed before and never overwrites your swaps file.
"""

import json
import os
import traceback
import urllib.request

import unreal

REPO_RAW = ("https://raw.githubusercontent.com/distempered1-rgb/effective-tribble/"
            "refs/heads/claude/3d-landscape-unreal-engine-yw7zzp/house/output/unreal/")
DATA_DIR = ""                       # optional: force a local folder with placements.json
FORCE_DOWNLOAD = False              # True = always fetch the latest model from GitHub
DEST = "/Game/MyHouse/Meshes"
TAG = "MyHouse"


def log(msg):
    unreal.log("[MyHouse] " + msg)


def fail(msg):
    unreal.log_error("[MyHouse] " + msg)
    raise RuntimeError(msg)


# --------------------------------------------------------------------------- #
# Getting the files
# --------------------------------------------------------------------------- #

def local_folder():
    """Folder next to this script, if it holds the exported model."""
    if DATA_DIR:
        return DATA_DIR
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:              # pasted into the Python console
        return None
    return here if os.path.exists(os.path.join(here, "placements.json")) else None


def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "UnrealEditor-MyHouse"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as f:
        f.write(resp.read())


def fetch_from_github():
    saved = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
    base = os.path.join(saved, "MyHouse")
    os.makedirs(os.path.join(base, "meshes"), exist_ok=True)
    log("downloading model from GitHub into " + base)
    try:
        download(REPO_RAW + "placements.json", os.path.join(base, "placements.json"))
    except Exception as e:
        fail("could not reach GitHub (%s). Check your internet connection, or download "
             "the repo and run house/output/unreal/import_house.py with "
             "Tools > Execute Python Script instead." % e)
    swaps = os.path.join(base, "plant_swaps.json")
    if not os.path.exists(swaps):   # keep the user's plant choices
        download(REPO_RAW + "plant_swaps.json", swaps)
    with open(os.path.join(base, "placements.json")) as f:
        names = sorted({o["mesh"] for o in json.load(f)["objects"] if "mesh" in o})
    with unreal.ScopedSlowTask(len(names), "Downloading house model...") as task:
        task.make_dialog(True)
        for name in names:
            task.enter_progress_frame(1, "Downloading " + name)
            path = os.path.join(base, "meshes", name + ".fbx")
            if FORCE_DOWNLOAD or not os.path.exists(path):
                download(REPO_RAW + "meshes/" + name + ".fbx", path)
    log("download complete (%d meshes)" % len(names))
    return base


# --------------------------------------------------------------------------- #
# Importing and placing
# --------------------------------------------------------------------------- #

def import_meshes(mesh_dir, names):
    tasks = []
    for name in names:
        path = os.path.join(mesh_dir, name + ".fbx")
        if not os.path.exists(path):
            fail("missing file " + path)
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

        task = unreal.AssetImportTask()
        task.set_editor_property("filename", path)
        task.set_editor_property("destination_path", DEST)
        task.set_editor_property("destination_name", name)
        task.set_editor_property("automated", True)       # no import dialogs
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        task.set_editor_property("options", opts)
        try:
            task.set_editor_property("async_", False)       # UE 5.x Interchange importer
        except Exception:
            pass
        tasks.append(task)
    log("importing %d meshes into %s ..." % (len(tasks), DEST))
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)

    # Find what was imported. Newer engines (Interchange importer) may name or
    # nest assets differently, so search the folder rather than assume a path.
    found = {}
    for path in unreal.EditorAssetLibrary.list_assets("/Game/MyHouse", recursive=True):
        asset = unreal.EditorAssetLibrary.load_asset(path.split(".")[0])
        if isinstance(asset, unreal.StaticMesh):
            found[asset.get_name()] = asset
    log("static meshes found after import: %d" % len(found))
    meshes = {}
    for name in names:
        match = found.get(name) or next(
            (m for n, m in found.items() if n.endswith(name) or n.startswith(name)), None)
        if match is None:
            unreal.log_error("[MyHouse] import failed for %s - see the messages above" % name)
            continue
        meshes[name] = match
    if not meshes:
        fail("nothing was imported - scroll up in the Output Log for the FBX importer's error")
    return meshes


def use_complex_collision(mesh):
    """Walls/ground: collide against the real triangles, not a box around the house."""
    try:
        body = mesh.get_editor_property("body_setup")
        body.set_editor_property("collision_trace_flag",
                                 unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
        unreal.EditorAssetLibrary.save_loaded_asset(mesh)
    except Exception as e:  # older engine versions
        unreal.log_warning("[MyHouse] could not set complex collision on %s: %s" % (mesh.get_name(), e))


def remove_previous(subsystem):
    for actor in subsystem.get_all_level_actors():
        if unreal.Name(TAG) in list(actor.get_editor_property("tags")):
            subsystem.destroy_actor(actor)


def place(objects, meshes, swaps):
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    remove_previous(subsystem)
    count = 0
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
        if actor is None:
            unreal.log_warning("[MyHouse] could not spawn " + o["name"])
            continue
        actor.set_actor_label(o["name"])
        cat = o["category"]
        actor.set_folder_path("MyHouse/" + ("Plants" if cat == "Plant" else cat))
        actor.set_editor_property("tags", [unreal.Name(TAG), unreal.Name(cat)])
        count += 1
    return count


def main():
    try:
        log("Unreal Engine " + unreal.SystemLibrary.get_engine_version())
    except Exception:
        pass
    base = None if FORCE_DOWNLOAD else local_folder()
    if base:
        log("using local files in " + base)
    else:
        base = fetch_from_github()

    with open(os.path.join(base, "placements.json")) as f:
        objects = json.load(f)["objects"]
    swaps = {"by_type": {}, "by_object": {}}
    swap_path = os.path.join(base, "plant_swaps.json")
    if os.path.exists(swap_path):
        with open(swap_path) as f:
            swaps.update(json.load(f))

    names = sorted({o["mesh"] for o in objects if "mesh" in o})
    meshes = import_meshes(os.path.join(base, "meshes"), names)
    for o in objects:
        if o.get("collision") == "complex" and o.get("mesh") in meshes:
            use_complex_collision(meshes[o["mesh"]])

    count = place(objects, meshes, swaps)
    log("placed %d objects (outliner folder 'MyHouse'). Press Play to walk around." % count)
    log("plant swaps file: " + swap_path)


try:
    main()
except Exception:
    unreal.log_error("[MyHouse] FAILED:\n" + traceback.format_exc())
