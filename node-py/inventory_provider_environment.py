#!/usr/bin/env python3
"""Inventory independently staged Python bytes; never import provider/model code."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def inventory(root, python=None):
    root=Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('absolute_regular_inventory_root_required')
    files=[];links=[];total=0
    for directory,names,entries in os.walk(root,followlinks=False):
        for name in sorted(names+entries):
            path=Path(directory)/name
            relative=str(path.relative_to(root))
            if path.is_symlink():
                if python and relative.startswith('bin/python') and path.resolve()==python:
                    continue
                if not path.resolve().is_relative_to(root) or not path.resolve().exists():
                    raise ValueError('external_or_broken_inventory_alias')
                links.append({'path':relative,'target':os.readlink(path)})
                if len(links)>4096:raise ValueError('inventory_alias_bound')
            elif path.is_file():
                size=path.stat().st_size;total+=size
                if len(files)>=65536 or total>32*1024**3:raise ValueError('inventory_file_bound')
                digest=hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
                files.append({'path':relative,'bytes':size,'sha256':digest.hexdigest()})
            elif not path.is_dir():raise ValueError('inventory_special_file')
    return {'root':str(root),'files':sorted(files,key=lambda x:x['path']),
            'links':sorted(links,key=lambda x:x['path'])}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    out=Path(args.out)
    if not out.is_absolute() or out.is_relative_to(Path(sys.prefix)) or out.is_relative_to(Path(sys.base_prefix)):
        raise ValueError('manifest_outside_inventoried_roots_required')
    python=Path(sys.executable).resolve()
    value=inventory(Path(sys.prefix),python)
    if len(value['links'])>16:raise ValueError('venv_alias_bound')
    value.update({'pythonExecutable':str(python),'pythonSha256':hashlib.sha256(python.read_bytes()).hexdigest(),
                  'runtimeTree':inventory(Path(sys.base_prefix))})
    with out.open('x') as stream:json.dump(value,stream,sort_keys=True,separators=(',',':'));stream.write('\n')
    print(json.dumps({'manifest':str(out),'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),
                     'venvFiles':len(value['files']),'runtimeFiles':len(value['runtimeTree']['files']),
                     'runtimeAliases':len(value['runtimeTree']['links']),
                     'environmentSealed':False,'providerQualified':False,'serviceActivated':False}))


if __name__=='__main__':main()
