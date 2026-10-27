def load(path):
    try:
        f = open(path)
        data = f.read()
    except OSError as e:
        print("failed", e)
    finally:
        print("done")
    return data


def risky(x):
    try:
        raise ValueError("bad")
        print("unreachable")
    except ValueError:
        return 0
    return 1
