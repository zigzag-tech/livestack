"""Linux private owner control client with kernel peer incarnation evidence."""
import argparse
import json
import os
import socket
import stat
import struct
from pathlib import Path


def process_start_ticks(pid):
    content=(Path('/proc')/str(pid)/'stat').read_text()
    return int(content[content.rfind(')')+2:].split()[19])


def control(path, request):
    path=Path(path)
    parent=path.parent.stat();info=path.lstat()
    if not path.is_absolute() or not stat.S_ISSOCK(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid() or parent.st_mode&0o077 or parent.st_uid!=os.getuid():
        raise PermissionError('private_owner_socket_required')
    body=json.dumps(request,separators=(',',':')).encode()+b'\n'
    if len(body)>8192:raise ValueError('owner_request_bound')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(20)
        connection.connect(str(path))
        pid,uid,gid=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i')))
        if uid!=os.getuid() or pid<=0:raise PermissionError('owner_peer_identity_refused')
        start=process_start_ticks(pid)
        connection.sendall(body);content=b''
        while b'\n' not in content:
            part=connection.recv(8193-len(content))
            if not part:raise ConnectionError('owner_control_lost_ack_unknown_effect')
            content+=part
            if len(content)>8192:raise ValueError('owner_receipt_bound')
        value=json.loads(content.split(b'\n',1)[0])
        if value.get('ok') is not True or type(value.get('result',{}).get('serverProcessId')) is not int or value['result']['serverProcessId']!=pid:
            raise PermissionError('owner_response_peer_process_refused')
        try:
            if process_start_ticks(pid)!=start:raise PermissionError('owner_peer_incarnation_changed')
            present=True
        except FileNotFoundError:present=False
        return {'receipt':value,'peer':{'pid':pid,'uid':uid,'gid':gid,'startTicks':start,'presentAfterReply':present}}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--socket',required=True);parser.add_argument('--request',required=True)
    args=parser.parse_args()
    if len(args.request.encode())>8192:raise ValueError('owner_request_bound')
    print(json.dumps(control(args.socket,json.loads(args.request)),separators=(',',':')))


if __name__=='__main__':main()
