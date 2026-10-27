def helper(v):
    return v + 1


def outer(a):
    base = a * 2

    def inner(b):
        return b + base

    return inner(3)


class Counter:
    def bump(self, n):
        self.n = helper(n)
        return self.n


if __name__ == "__main__":
    result = Counter().bump(2)
    if result:
        print(result)
