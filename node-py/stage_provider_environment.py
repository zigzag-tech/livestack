#!/usr/bin/env python3
"""Stage exact public baseline packages without models or shared editables."""
import argparse
import email
import json
import os
import re
import subprocess
from pathlib import Path


def baseline(venv, omit_typing=False, python_version=None):
    site = list(Path(venv).glob('lib/python*/site-packages'))
    if len(site)!=1:
        raise ValueError('baseline_site_package_root_ambiguous')
    packages=[];excluded=[];omitted=False
    for info in sorted(site[0].glob('*.dist-info')):
        metadata=email.message_from_string((info/'METADATA').read_text())
        name,version=metadata['Name'],metadata['Version']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name or '') or not re.fullmatch(r'[A-Za-z0-9_.+!-]+',version or ''):
            raise ValueError('baseline_package_identity_invalid')
        normalized=name.lower().replace('_','-')
        direct=info/'direct_url.json'
        editable=direct.exists() and json.loads(direct.read_text()).get('dir_info',{}).get('editable')
        if omit_typing and normalized=='typing':
            if version!='3.10.0.0' or python_version is None or tuple(python_version)<(3,12):
                raise ValueError('obsolete_typing_policy_identity_refused')
            excluded.append({'name':name,'version':version,'reason':'explicit obsolete typing backport policy for Python >=3.12'})
            omitted=True
            continue
        if normalized in ('livestack-node','shared-py'):
            excluded.append({'name':name,'version':version,'reason':'rebuild exact frozen owner source'})
            continue
        if editable:
            raise ValueError('unqualified_non_owner_editable_distribution')
        packages.append({'name':name,'version':version})
    if omit_typing and not omitted:
        raise ValueError('obsolete_typing_policy_distribution_absent')
    if not packages or len(packages)>512:
        raise ValueError('baseline_distribution_bound')
    return packages,excluded


def run(argv,env=None):
    subprocess.run(argv,check=True,env=env)


def typing_proof(python):
    script="import sys,typing,json,hashlib;from pathlib import Path;p=Path(typing.__file__).resolve();print(json.dumps({'version':list(sys.version_info[:2]),'basePrefix':sys.base_prefix,'typingFile':str(p),'typingSha256':hashlib.sha256(p.read_bytes()).hexdigest()}))"
    proof=json.loads(subprocess.check_output([str(python),'-I','-B','-c',script],text=True))
    if tuple(proof['version'])<(3,12) or not Path(proof['typingFile']).is_relative_to(Path(proof['basePrefix']).resolve()) or Path(proof['typingFile']).name!='typing.py':
        raise ValueError('obsolete_typing_standard_library_proof_refused')
    return proof


def stage(args):
    output=Path(args.out)
    if not output.is_absolute() or output.resolve()!=output:
        raise ValueError('absolute_new_output_required')
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    policy=bool(args.omit_obsolete_typing_backport)
    original_typing=typing_proof(Path(args.baseline_venv)/'bin/python') if policy else None
    packages,excluded=baseline(args.baseline_venv,policy,original_typing['version'] if policy else None)
    compatibility=None
    if args.resemblyzer_compatibility_original_wheel:
        if not policy or {'name':'Resemblyzer','version':'0.1.4'} not in packages:
            raise ValueError('resemblyzer_compatibility_baseline_policy_refused')
        from compatibility_resemblyzer_wheel import derive
        site=list(Path(args.baseline_venv).glob('lib/python*/site-packages'))[0]
        compatibility=derive(args.resemblyzer_compatibility_original_wheel,output/'compatibility-artifact',original_typing['version'],site)
    (output/'baseline-distributions.json').write_text(json.dumps({'packages':packages,'excluded':excluded},indent=2)+'\n')
    requirements=output/'baseline-requirements.txt'
    cuda_requirements=output/'cuda-requirements.txt'
    cuda=[record for record in packages if '+cu' in record['version']]
    public=[record for record in packages if record not in cuda]
    requirements.write_text(''.join((compatibility['artifact']+'\n') if compatibility and record['name'].lower()=='resemblyzer' else record['name']+'=='+record['version']+'\n' for record in public))
    cuda_requirements.write_text(''.join(record['name']+'=='+record['version']+'\n' for record in cuda))
    original=(Path(args.baseline_venv)/'bin/python').resolve()
    base=original.parents[1]
    # Python standalone runtime contains no model/voice assets. New independent
    # files ensure later global cache/checkouts cannot alter the staged release.
    run(['cp','-a','--reflink=auto',str(base),str(output/'python-runtime')])
    python=output/'python-runtime/bin'/original.name
    run([args.uv,'venv','--python',str(python),str(output/'venv')])
    target=output/'venv/bin/python'
    staged_typing=typing_proof(target) if policy else None
    if policy:
        if staged_typing['version']!=original_typing['version'] or staged_typing['typingSha256']!=original_typing['typingSha256']:
            raise ValueError('obsolete_typing_standard_library_changed')
        (output/'typing-compatibility-proof.json').write_text(json.dumps({'policy':'omit typing==3.10.0.0 for Python>=3.12','original':original_typing,'staged':staged_typing},indent=2)+'\n')
    # Exact observed versions preserve the retained baseline. --no-deps does
    # not imply dependency consistency: the separate metadata gate records it.
    install=[args.uv,'pip','install','--no-deps','--link-mode','copy',
         '--python',str(target),'--default-index','https://pypi.org/simple',
         '-r',str(requirements)]
    if not args.allow_public_downloads:
        install.insert(3,'--offline')
    try:
        run(install)
        if cuda:
            cuda_install=list(install)
            cuda_install[cuda_install.index('https://pypi.org/simple')]='https://download.pytorch.org/whl/cu129'
            cuda_install[-1]=str(cuda_requirements)
            run(cuda_install)
    except subprocess.CalledProcessError as failure:
        (output/'stage-failure.json').write_text(json.dumps({'kind':'public-baseline-stage-refusal',
            'phase':'package-install','exitCode':failure.returncode,'environmentSealed':False,
            'providerQualified':False,'serviceActivated':False})+'\n')
        raise
    metadata_check=subprocess.run([args.uv,'pip','check','--python',str(target)],capture_output=True,text=True)
    (output/'dependency-consistency.txt').write_text(metadata_check.stdout+metadata_check.stderr)
    (output/'stage-receipt.json').write_text(json.dumps({'kind':'public-baseline-dependency-stage','version':1,
        'publicDownloadsAllowed':args.allow_public_downloads,'omitObsoleteTypingBackport':policy,'compatibilityArtifact':compatibility,
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
    parser.add_argument('--resemblyzer-compatibility-original-wheel',help='Verified exact public0.1.4 wheel for explicit source-preserving local compatibility artifact')
    parser.add_argument('--omit-obsolete-typing-backport',action='store_true',
                        help='Explicitly omit only typing==3.10.0.0 on Python>=3.12, preserving stdlib bytes')
    parser.add_argument('--allow-public-downloads',action='store_true',
                        help='Allow exact version-pinned public wheels; never model or voice downloads')
    args=parser.parse_args()
    stage(args)


if __name__=='__main__':main()
