import argparse
from .models import TargetPlatform
from .parser import parse_qemu_command
from .translator import convert_config
def main():
    p=argparse.ArgumentParser(); p.add_argument('--command',required=True); p.add_argument('--target',required=True,choices=['macos','windows','linux']); a=p.parse_args(); r=convert_config(parse_qemu_command(a.command),TargetPlatform(a.target)); print(r.command); [print(f'[{i.level}] {i.message}') for i in r.issues]
if __name__=='__main__': main()
