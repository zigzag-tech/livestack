#!/usr/bin/env python3
"""Exact-source packaging policy; preserves all public Python/model bytes."""
import argparse
import base64
import csv
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

ORIGINAL_SHA256='8f12eb2f1a9982d32e8db7856de754709b59c93a77bcf0ff536584b619a9dd1f'
ORIGINAL_INFO='Resemblyzer-0.1.4.dist-info'
VERSION='0.1.4+zzops.typing1'
NEW_INFO='Resemblyzer-'+VERSION+'.dist-info'


def sha(data):return hashlib.sha256(data).hexdigest()


def derive(original, output, python_version, baseline_site):
    if tuple(python_version)<(3,12):raise ValueError('compatibility_python_runtime_refused')
    content=Path(original).read_bytes()
    if sha(content)!=ORIGINAL_SHA256:raise ValueError('compatibility_original_source_digest_refused')
    output=Path(output)
    if not output.is_absolute() or output.resolve()!=output or output.exists():raise ValueError('new_canonical_output_required')
    with zipfile.ZipFile(io.BytesIO(content)) as wheel:
        names=wheel.namelist()
        if len(names)>256 or len(set(names))!=len(names) or any(name.startswith('/') or '..' in name.split('/') or name.endswith('/') for name in names):
            raise ValueError('compatibility_wheel_inventory_refused')
        files={name:wheel.read(name) for name in names}
    metadata=files[ORIGINAL_INFO+'/METADATA']
    if metadata.count(b'Version: 0.1.4\r\n')!=1 or metadata.count(b'Requires-Dist: typing\r\n')!=1:
        raise ValueError('compatibility_metadata_contract_refused')
    records=list(csv.reader(io.StringIO(files[ORIGINAL_INFO+'/RECORD'].decode())))
    if {record[0] for record in records}!=set(files):raise ValueError('compatibility_original_record_inventory_refused')
    for name,digest,size in records:
        if name==ORIGINAL_INFO+'/RECORD':
            if digest or size:raise ValueError('compatibility_record_self_hash_refused')
        elif digest!='sha256='+base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).decode().rstrip('=') or size!=str(len(files[name])):
            raise ValueError('compatibility_original_record_bytes_refused')
    unchanged=[]
    for name,data in files.items():
        if name.startswith(ORIGINAL_INFO+'/'):continue
        path=Path(baseline_site)/name
        if path.is_symlink() or not path.is_file() or path.read_bytes()!=data:
            raise ValueError('compatibility_retained_baseline_bytes_refused')
        unchanged.append({'path':name,'bytes':len(data),'sha256':sha(data)})
    if not unchanged:raise ValueError('compatibility_code_inventory_empty')
    replacement=metadata.replace(b'Version: 0.1.4\r\n',('Version: '+VERSION+'\r\n').encode()).replace(b'Requires-Dist: typing\r\n',b'Requires-Dist: typing; python_version < "3.5"\r\n')
    derived={name.replace(ORIGINAL_INFO+'/',NEW_INFO+'/',1):data for name,data in files.items() if name!=ORIGINAL_INFO+'/RECORD'}
    derived[NEW_INFO+'/METADATA']=replacement
    table=io.StringIO();writer=csv.writer(table,lineterminator='\n')
    for name,data in sorted(derived.items()):writer.writerow([name,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='),len(data)])
    writer.writerow([NEW_INFO+'/RECORD','','']);derived[NEW_INFO+'/RECORD']=table.getvalue().encode()
    output.mkdir(mode=0o700)
    target=output/('Resemblyzer-'+VERSION+'-py3-none-any.whl')
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as wheel:
        for name,data in sorted(derived.items()):
            entry=zipfile.ZipInfo(name,(1980,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED;entry.external_attr=0o100644<<16
            wheel.writestr(entry,data)
    receipt={'kind':'resemblyzer-packaging-compatibility','version':1,'originalVersion':'0.1.4','compatibilityVersion':VERSION,
             'originalArtifactSha256':ORIGINAL_SHA256,'artifact':str(target),'artifactSha256':sha(target.read_bytes()),
             'pythonVersion':list(python_version),'baselineSite':str(Path(baseline_site).resolve()),'unchangedPythonAndModelFiles':unchanged,
             'metadataChanges':['local version label','typing Requires-Dist marker','derived RECORD paths/hashes'],
             'modelRuntimeQualified':False,'registered':False,'activated':False}
    (output/'compatibility-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--original-wheel',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--python',required=True);parser.add_argument('--baseline-site-packages',required=True)
    args=parser.parse_args()
    version=json.loads(subprocess.check_output([args.python,'-I','-B','-c','import sys,json;print(json.dumps(list(sys.version_info[:2])))'],text=True))
    print(json.dumps(derive(args.original_wheel,args.out,version,args.baseline_site_packages)))


if __name__=='__main__':main()
