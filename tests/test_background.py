import threading

from stage_director.background import Periodic


def test_periodic_calls_the_function_repeatedly_until_stopped():
    calls, three = [], threading.Event()

    def fn():
        calls.append(1)
        if len(calls) == 3:
            three.set()

    task = Periodic(fn, 0.01, "test")
    task.start()
    assert three.wait(2)
    task.stop()
    seen = len(calls)
    threading.Event().wait(0.1)
    assert len(calls) <= seen + 1  # 멈춘 뒤에는 (진행 중이던 한 번을 빼면) 더 돌지 않는다


def test_periodic_keeps_running_after_the_function_raises():
    calls, two = [], threading.Event()

    def fn():
        calls.append(1)
        if len(calls) == 2:
            two.set()
        raise RuntimeError("boom")

    task = Periodic(fn, 0.01, "test")
    task.start()
    assert two.wait(2)
    task.stop()
