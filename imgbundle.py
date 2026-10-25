"""imgbundle: move rules, flows and macros to another config, or another computer,
as one file.

A bundle (.imgbundle) is a zip file holding:

    bundle.json     the manifest: what's inside, and every other file's size and SHA-256
    config.json     the rules, flows and macros, in the config's own format
    templates/      the images they look for
    macros/         the recordings

Exporting a flow brings along the macros and flows it runs; a macro brings its
safeguards' and checkpoints' images. Importing checks the whole bundle before it
writes anything: only the files the manifest lists, with names that can't
point outside the config's folder, the right sizes and digests, images that
open and recordings that parse, 200 MB at most. Nothing is overwritten: an
imported file whose name is taken gets a new one (or the existing file is used
if it's the same), and a flow or macro whose name is taken gets " (2)".

    python imgbundle.py export config.json out.imgbundle [--rules] [--flow NAME]... [--macro NAME]...
    python imgbundle.py import config.json in.imgbundle
"""

import argparse
import copy
import hashlib
import json
import re
import sys
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np

import imgmacro as im

FORMAT, VERSION = "imgmacro-bundle", 1
MAX_FILES = 5000
MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 200 * 1024 * 1024
# The only names a bundle may hold: one folder deep, plain characters.
SAFE_NAME = re.compile(r"(config\.json|templates/[A-Za-z0-9][A-Za-z0-9._ -]{0,120}\.png"
                       r"|macros/[A-Za-z0-9][A-Za-z0-9._ -]{0,120}\.json)")


def _dicts(node):
    """Every dict inside a JSON value, depth first."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _dicts(value)


def _uses(node):
    """The flows and macros a rule, flow or macro (as JSON) runs -> (flow names, macro names)."""
    flows, macros = set(), set()
    for d in _dicts(node):
        if d.get("type") == "flow" and d.get("flow"):
            flows.add(d["flow"])
        if (d.get("type") == "macro" or d.get("action") == "macro") and d.get("macro"):
            macros.add(d["macro"])
    return flows, macros


FILE_KEYS = {"image": "templates", "release_image": "templates", "file": "macros"}


def _file_refs(node):
    """(dict, key) for every file a rule, flow or macro (as JSON) refers to."""
    for d in _dicts(node):
        for key in FILE_KEYS:
            if isinstance(d.get(key), str):
                yield d, key


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _safe_stem(path):
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", Path(path).stem)
    stem = re.sub(r"^[^A-Za-z0-9]+", "", stem).rstrip(" .-")[:100]  # SAFE_NAME starts with a letter or digit
    return stem or "file"


# --- export -----------------------------------------------------------------------

def export_bundle(cfg, config_path, out_path, rules=(), flows=(), macros=()):
    """Write a bundle of `rules` (Rules), `flows` (flow names) and `macros` (macro
    names) from `cfg`, plus the flows and macros those run. -> a summary line."""
    base = Path(config_path).parent
    all_flows = {d["name"]: d for d in cfg.flows}
    all_macros = {d["name"]: d for d in cfg.macros}
    rule_dicts = [im.rule_to_dict(r, base) for r in rules]
    want_flows, want_macros = list(flows), list(macros)
    for d in rule_dicts:
        want_macros += sorted(_uses(d)[1])
    chosen_flows, chosen_macros = {}, {}
    while want_flows or want_macros:  # follow what each one runs
        if want_flows:
            name = want_flows.pop()
            if name in chosen_flows:
                continue
            if name not in all_flows:
                raise ValueError(f"there's no flow called “{name}”")
            chosen_flows[name] = all_flows[name]
            f, m = _uses(all_flows[name])
            want_flows += sorted(f)
            want_macros += sorted(m)
        else:
            name = want_macros.pop()
            if name in chosen_macros:
                continue
            if name not in all_macros:
                raise ValueError(f"there's no macro called “{name}”")
            chosen_macros[name] = all_macros[name]
    content = copy.deepcopy({"rules": rule_dicts, "flows": list(chosen_flows.values()),
                             "macros": list(chosen_macros.values())})
    files, by_hash = {}, {}  # bundle name -> bytes; digest -> bundle name
    for d, key in _file_refs(content):
        source = Path(d[key])
        if not source.is_absolute():
            source = base / source
        data = source.read_bytes()
        digest = _sha(data)
        if digest not in by_hash:  # the same image used twice is stored once
            folder, ext = FILE_KEYS[key], ".json" if key == "file" else ".png"
            name, n = f"{folder}/{_safe_stem(source)}{ext}", 2
            while name in files:
                name, n = f"{folder}/{_safe_stem(source)}-{n}{ext}", n + 1
            files[name], by_hash[digest] = data, name
        d[key] = by_hash[digest]
    files["config.json"] = json.dumps(content, indent=2).encode("utf-8")
    manifest = {
        "format": FORMAT, "version": VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "contents": {"rules": len(rule_dicts), "flows": list(chosen_flows), "macros": list(chosen_macros)},
        "files": {name: {"size": len(data), "sha256": _sha(data)} for name, data in sorted(files.items())},
    }
    out_path = Path(out_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("bundle.json", json.dumps(manifest, indent=2))
        for name, data in sorted(files.items()):
            z.writestr(name, data)
    tmp.replace(out_path)
    return describe(manifest)


def describe(manifest):
    c = manifest.get("contents", {})
    parts = [f"{c.get('rules', 0)} rule(s)", f"{len(c.get('flows', []))} flow(s)",
             f"{len(c.get('macros', []))} macro(s)", f"{len(manifest.get('files', {})) - 1} file(s)"]
    return ", ".join(parts)


# --- import -----------------------------------------------------------------------

def read_bundle(path):
    """Open and check a bundle, without writing anything. -> (manifest, content, files)."""
    import imgplay
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError(f"{Path(path).name} isn't a bundle (not a zip file)") from None
    with z:
        infos = {i.filename: i for i in z.infolist() if not i.is_dir()}
        if "bundle.json" not in infos or infos["bundle.json"].file_size > 1024 * 1024:
            raise ValueError("not an imgmacro bundle: bundle.json is missing")
        manifest = json.loads(z.read("bundle.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError("not an imgmacro bundle")
        if manifest.get("version") != VERSION:
            raise ValueError(f"this bundle is version {manifest.get('version')}; "
                             f"this imgmacro reads version {VERSION}")
        declared = manifest.get("files")
        if not isinstance(declared, dict) or "config.json" not in declared:
            raise ValueError("the bundle's manifest doesn't list its files")
        if len(declared) > MAX_FILES:
            raise ValueError("the bundle holds too many files")
        extra = set(infos) - set(declared) - {"bundle.json"}
        if extra:
            raise ValueError(f"the bundle holds files its manifest doesn't list: {', '.join(sorted(extra))}")
        files, total = {}, 0
        for name, meta in declared.items():
            if not SAFE_NAME.fullmatch(name):
                raise ValueError(f"the bundle holds a file with an unsafe name: {name!r}")
            if name not in infos:
                raise ValueError(f"the bundle is missing {name}")
            size = int(meta.get("size", -1))
            total += size
            if not 0 <= size <= MAX_FILE or total > MAX_TOTAL:
                raise ValueError("the bundle is too big to import (200 MB at most)")
            with z.open(infos[name]) as f:
                data = f.read(size + 1)  # never more than declared, whatever the zip claims
            if len(data) != size or _sha(data) != meta.get("sha256"):
                raise ValueError(f"{name} in the bundle is damaged (its size or digest is wrong)")
            files[name] = data
    for name, data in files.items():
        if name.startswith("templates/"):
            if cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) is None:
                raise ValueError(f"{name} in the bundle isn't an image")
        elif name.startswith("macros/"):
            raw = json.loads(data.decode("utf-8"))
            if raw.get("format") != "imgmacro-macro":
                raise ValueError(f"{name} in the bundle isn't a recording")
            for e in raw.get("events", []):
                imgplay.check_event(e)
    content = json.loads(files["config.json"].decode("utf-8"))
    if not isinstance(content, dict):
        raise ValueError("the bundle's config.json is damaged")
    for d, key in _file_refs(content):
        if d[key] not in files:
            raise ValueError(f"the bundle refers to {d[key]}, which it doesn't hold")
    return manifest, content, files


def _unused(name, taken):
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


def import_bundle(path, config_path, flow_names=(), macro_names=()):
    """Check a bundle, copy its files next to the config, and -> (rules, flows,
    macros, summary): JSON ready for the config, renamed where `flow_names` or
    `macro_names` (the names already in use) are taken."""
    manifest, content, files = read_bundle(path)
    base = Path(config_path).parent
    # Rename flows and macros whose names are taken, and what runs them.
    renames = {"flow": {}, "macro": {}}
    for kind, taken in (("flow", set(flow_names)), ("macro", set(macro_names))):
        for d in content.get(f"{kind}s", []):
            new = _unused(d.get("name", kind.capitalize()), taken)
            taken.add(new)
            if new != d.get("name"):
                renames[kind][d.get("name")] = new
                d["name"] = new
    for d in _dicts(content):
        if d.get("type") == "flow" and d.get("flow") in renames["flow"]:
            d["flow"] = renames["flow"][d["flow"]]
        if (d.get("type") == "macro" or d.get("action") == "macro") and d.get("macro") in renames["macro"]:
            d["macro"] = renames["macro"][d["macro"]]
    # Copy the files in, never over an existing one.
    placed = {}
    for name, data in files.items():
        if name == "config.json":
            continue
        target = base / name
        stem, ext, n = target.stem, target.suffix, 2
        while target.exists() and target.read_bytes() != data:
            target, n = target.with_name(f"{stem}-{n}{ext}"), n + 1
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        placed[name] = im._relative(target, base)
    for d, key in _file_refs(content):
        d[key] = placed[d[key]]
    summary = describe(manifest)
    renamed = [f"“{a}” as “{b}”" for kind in renames.values() for a, b in kind.items()]
    if renamed:
        summary += "; imported " + ", ".join(renamed) + " (the names were taken)"
    return content.get("rules", []), content.get("flows", []), content.get("macros", []), summary


# --- command line -----------------------------------------------------------------

def main(argv=None):
    sys.stdout.reconfigure(errors="replace")  # window titles and names may not fit the console's code page
    parser = argparse.ArgumentParser(prog="imgbundle", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("export", help="write rules, flows and macros to a bundle")
    p.add_argument("config")
    p.add_argument("out")
    p.add_argument("--rules", action="store_true", help="include all the rules")
    p.add_argument("--flow", action="append", default=[], help="a flow to include (repeatable)")
    p.add_argument("--macro", action="append", default=[], help="a macro to include (repeatable)")
    p = sub.add_parser("import", help="add a bundle's rules, flows and macros to a config")
    p.add_argument("config")
    p.add_argument("bundle")
    args = parser.parse_args(argv)
    try:
        cfg = im.load_config(args.config)
        if args.command == "export":
            if not (args.rules or args.flow or args.macro):
                raise ValueError("say what to export: --rules, --flow NAME or --macro NAME")
            print(f"Exported {export_bundle(cfg, args.config, args.out, cfg.rules if args.rules else (), args.flow, args.macro)}.")
            return 0
        rules, flows, macros, summary = import_bundle(
            args.bundle, args.config, [d["name"] for d in cfg.flows], [d["name"] for d in cfg.macros])
        base = Path(args.config).parent
        import imgflow
        import imgplay
        new_rules = [im.rule_from_dict(r, base, i) for i, r in enumerate(rules, len(cfg.rules) + 1)]
        for d in flows:
            imgflow.flow_from_dict(d, base)  # check they load before saving them
        for d in macros:
            imgplay.macro_from_dict(d, base)
        cfg.rules += new_rules
        cfg.flows += flows
        cfg.macros += macros
        im.save_config(cfg, args.config)
        print(f"Imported {summary}.")
    except (ValueError, OSError, KeyError) as exc:
        print(f"imgbundle: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
