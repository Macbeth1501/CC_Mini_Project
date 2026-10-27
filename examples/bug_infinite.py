def spin(n):
    while True:
        n += 1
    print("after")


def const_if(x):
    if True:
        y = 1
    else:
        y = 2
    if 1 > 2:
        print("never")
    return y + x
