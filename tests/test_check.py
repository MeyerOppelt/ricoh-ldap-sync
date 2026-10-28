from ricoh_ldap_sync import check


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Sock:
    def close(self):
        pass


def connector(fail_times):
    calls = {"n": 0}

    def connect(addr, timeout):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise ConnectionRefusedError
        return Sock()

    return connect


def test_wait_for_port_immediate():
    clock = FakeClock()
    assert check.wait_for_port("ldap", 636, 60, connect=connector(0), sleep=clock.sleep, clock=clock) == 0


def test_wait_for_port_after_retries():
    clock = FakeClock()
    assert check.wait_for_port("ldap", 636, 60, connect=connector(3), sleep=clock.sleep, clock=clock) == 6


def test_wait_for_port_gives_up():
    clock = FakeClock()
    assert check.wait_for_port("ldap", 636, 10, connect=connector(100), sleep=clock.sleep, clock=clock) is None
