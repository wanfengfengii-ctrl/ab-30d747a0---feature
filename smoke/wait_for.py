"""轮询健康端点直到 200，超时则非零退出。

用法：python3 wait_for.py URL [--timeout 秒] [--interval 秒]
"""

import sys
import time
import urllib.error
import urllib.request


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("用法：wait_for.py URL [--timeout N] [--interval N)", file=sys.stderr)
        sys.exit(2)
    url = args[0]
    timeout = 90
    interval = 2
    for i in range(1, len(args)):
        if args[i] == "--timeout" and i + 1 < len(args):
            timeout = int(args[i + 1])
        elif args[i] == "--interval" and i + 1 < len(args):
            interval = float(args[i + 1])

    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    print(f"[wait] {url} 已就绪")
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_err = str(exc)
        print(f"[wait] 等待 {url} …（{last_err}）")
        time.sleep(interval)
    print(f"[wait] 超时：{url} 在 {timeout}s 内未就绪（{last_err}）",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
