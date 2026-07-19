import argparse
from .models import TargetPlatform
from .parser import parse_input
from .translator import convert_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True, help="QEMU command or .utm package/config.plist path")
    parser.add_argument("--target", required=True, choices=["macos", "windows", "linux"])
    args = parser.parse_args()
    result = convert_config(parse_input(args.command), TargetPlatform(args.target))
    print(result.command)
    for issue in result.issues:
        print(f"[{issue.level}] {issue.code}: {issue.message}")


if __name__ == "__main__":
    main()
