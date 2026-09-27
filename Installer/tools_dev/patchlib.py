import sys
def patch(path, pairs):
    s = open(path, encoding="utf-8").read()
    for i, (old, new) in enumerate(pairs):
        n = s.count(old)
        if n != 1:
            print(f"[patch {i}] anchor found {n} times:\n{old[:200]}")
            sys.exit(1)
        s = s.replace(old, new)
    open(path, "w", encoding="utf-8").write(s)
    print(f"applied {len(pairs)} patches")
