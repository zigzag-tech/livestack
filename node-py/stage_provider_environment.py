#!/usr/bin/env python3
"""Stage exact public baseline packages without models or shared editables."""
import argparse
import email
import json
import os
import re
import subprocess
from pathlib import Path


def baseline(venv):
    site = list(Path(venv).glob('lib/python*/site-packages'))
    if len(site)!=1:
        raise ValueError('baseline_site_package_root_ambiguous')
    packages=[];excluded=[]
    for info in sorted(site[0].glob('*.dist-info')):
        metadata=email.message_from_string((info/'METADATA').read_text())
        name,version=metadata['Name'],metadata['Version']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name or '') or not re.fullmatch(r'[A-Za-z0-9_.+!-]+',version or ''):
            raise ValueError('baseline_package_identity_invalid')
        normalized=name.lower().replace('_','-')
        direct=info/'direct_url.json'
        editable=direct.exists() and json.loads(direct.read_text()).get('dir_info',{}).get('editable')
        if normalized in ('livestack-node','shared-py'):
            excluded.append({'name':name,'version':version,'reason':'rebuild exact frozen owner source'})
            continue
        if editable:
            raise ValueError('unqualified_non_owner_editable_distribution')
        packages.append({'name':name,'version':version})
    if not packages or len(packages)>512:
        raise ValueError('baseline_distribution_bound')
    return packages,excluded


def run(argv,env=None):
    subprocess.run(argv,check=True,env=env)


def stage(args):
    output=Path(args.out)
    if not output.is_absolute():
        raise ValueError('absolute_new_output_required')
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    packages,excluded=baseline(args.baseline_venv)
    (output/'baseline-distributions.json').write_text(json.dumps({'packages':packages,'excluded':excluded},indent=2)+'\n')
    requirements=output/'baseline-requirements.txt'
    requirements.write_text(''.join(record['name']+'=='+record['version']+'\n' for record in packages))
    original=(Path(args.baseline_venv)/'bin/python').resolve()
    base=original.parents[1]
    # Python standalone runtime contains no model/voice assets. New independent
    # files ensure later global cache/checkouts cannot alter the staged release.
    run(['cp','-a','--reflink=auto',str(base),str(output/'python-runtime')])
    python=output/'python-runtime/bin'/original.name
    run([args.uv,'venv','--python',str(python),str(output/'venv')])
    target=output/'venv/bin/python'
    # Exact observed versions preserve the retained baseline. --no-deps does
    # not imply dependency consistency: the separate metadata gate records it.
    install=[args.uv,'pip','install','--no-deps','--link-mode','copy',
         '--python',str(target),'--extra-index-url','https://download.pytorch.org/whl/cu129',
         '-r',str(requirements)]
    if not args.allow_public_downloads:
        install.insert(3,'--offline')
    try:
        run(install)
    except subprocess.CalledProcessError as failure:
        (output/'stage-failure.json').write_text(json.dumps({'kind':'public-baseline-stage-refusal',
            'phase':'package-install','exitCode':failure.returncode,'environmentSealed':False,
            'providerQualified':False,'serviceActivated':False})+'\n')
        raise
    metadata_check=subprocess.run([args.uv,'pip','check','--python',str(target)],capture_output=True,text=True)
    (output/'dependency-consistency.txt').write_text(metadata_check.stdout+metadata_check.stderr)
    (output/'stage-receipt.json').write_text(json.dumps({'kind':'public-baseline-dependency-stage','version':1,
        'publicDownloadsAllowed':args.allow_public_downloads,
        'sourceBaseline':str(Path(args.baseline_venv).resolve()),'packages':packages,'excluded':excluded,
        'dependencyConsistencyPassed':metadata_check.returncode==0,'frozenOwnerPackagesInstalled':False,
        'environmentSealed':False,'providerQualified':False,'serviceActivated':False},indent=2)+'\n')
    print(json.dumps({'out':str(output),'distributionCount':len(packages),'dependencyConsistencyPassed':metadata_check.returncode==0,
                      'frozenOwnerPackagesInstalled':False,'providerQualified':False}))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline-venv',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--uv',required=True)
    parser.add_argument('--allow-public-downloads',action='store_true',
                        help='Allow exact version-pinned public wheels; never model or voice downloads')
    args=parser.parse_args()
    stage(args)


if __name__=='__main__':main()
